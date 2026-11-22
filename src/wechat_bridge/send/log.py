"""Send log, throttle and the cross-process send lock. The log holds keyed digests, never message text.

Both live in one fixed per-user directory, so every bridge process on the machine shares one lock and one history:
the rate limit and the duplicate window cannot be multiplied by registering the bridge with several state directories.
Digests are keyed by the identity key of the state directory (a random per-directory secret), so the duplicate window only
recognizes repeats made through the same state directory; the interval and the hourly cap are shared by all of them.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import time

from ..core.config import check_private_fd, ensure_private_dir
from ..core.errors import BridgeError

MIN_INTERVAL_SECONDS = 3
MAX_PER_HOUR = 30
DUPLICATE_SECONDS = 600
LOCK_WAIT_SECONDS = 30
# One event per 3 s at most, so an hour holds at most ~1200 events of <300 bytes (~360 KB): this window always covers the
# whole hour the hourly cap looks at, however many refusals are logged.
TAIL_BYTES = 4 * 1024 * 1024
LOG_NAME = 'send-log.jsonl'
DEFAULT_DIR = Path.home() / 'Library/Application Support/WechatBridgeSend'


def digest(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode('utf-8'), hashlib.sha256).hexdigest()


@contextmanager
def send_lock(directory: Path = DEFAULT_DIR, *, wait=LOCK_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """Exclusive per-user lock; raises refused_busy when another send holds it past `wait` seconds."""
    ensure_private_dir(directory)
    fd = os.open(directory / 'send.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        end = clock() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= end:
                    raise BridgeError('refused_busy') from None
                sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _last_byte(path: Path) -> bytes:
    with path.open('rb') as stream:
        stream.seek(-1, os.SEEK_END)
        return stream.read(1)


class SendLog:
    def __init__(self, directory: Path, key: bytes, clock=time.time):
        self.directory, self.path, self.key, self.clock = directory, directory / LOG_NAME, key, clock

    def _tail(self) -> list[dict]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        except OSError:
            raise BridgeError('send_log_unavailable') from None
        try:
            check_private_fd(fd)
            size = os.fstat(fd).st_size
            os.lseek(fd, max(0, size - TAIL_BYTES), os.SEEK_SET)
            data = os.read(fd, TAIL_BYTES)
        except (OSError, BridgeError):
            raise BridgeError('send_log_unavailable') from None   # an unreadable log is not "no history": refuse
        finally:
            os.close(fd)
        lines = data.decode('utf-8', 'replace').splitlines()
        if size > TAIL_BYTES:
            lines = lines[1:]   # the first line of a tail window may be cut
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                # Damaged history cannot be trusted to hide a recent attempt: refuse rather than skip it.
                raise BridgeError('send_log_unavailable') from None
        return rows

    def check_allowed(self, conversation_id: str, text: str) -> None:
        """Raise throttled or duplicate_send. Attempts count, resolved or not, so an unknown outcome cannot be retried around;
        an attempt whose result says nothing was pressed (refused_*) no longer counts toward the hourly cap or the duplicate window."""
        now = self.clock()
        rows = self._tail()
        marker = digest(self.key, text)
        events = [r for r in rows if r.get('event') in ('attempt', 'refused')]
        if events and now - events[-1]['time'] < MIN_INTERVAL_SECONDS:
            raise BridgeError('throttled')
        # A result closes the latest attempt with the same key; a refused one means nothing was pressed.
        latest, attempts = {}, []
        for r in rows:
            key = (r.get('conversation_id'), r.get('text_hmac'))
            if r.get('event') == 'attempt':
                latest[key] = r; attempts.append(r)
            elif r.get('event') == 'result' and str(r.get('state', '')).startswith('refused') and key in latest:
                attempts.remove(latest.pop(key))
        if sum(1 for r in attempts if now - r['time'] < 3600) >= MAX_PER_HOUR:
            raise BridgeError('throttled')
        if any(r['conversation_id'] == conversation_id and r['text_hmac'] == marker and now - r['time'] < DUPLICATE_SECONDS for r in attempts):
            raise BridgeError('duplicate_send')

    def _append(self, record: dict) -> None:
        ensure_private_dir(self.directory)
        created = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_private_fd(fd)
            data = (json.dumps(record, sort_keys=True) + '\n').encode('utf-8')
            if os.fstat(fd).st_size and _last_byte(self.path) != b'\n':
                data = b'\n' + data   # never glue a record onto an unfinished line from an earlier crash
            view = memoryview(data)
            while view:   # os.write may be short; the record is complete or the send does not happen
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def attempt(self, conversation_id: str, text: str) -> None:
        """Durable record written before the send control is used; raises if it cannot be written (the send then does not happen)."""
        try:
            self._append({'event': 'attempt', 'time': self.clock(), 'conversation_id': conversation_id,
                          'text_hmac': digest(self.key, text), 'text_length': len(text)})
        except (OSError, BridgeError):
            raise BridgeError('send_log_unavailable') from None

    def refused(self, conversation_id: str) -> None:
        """A refusal after the chat was opened still touches WeChat (it marks the chat read), so it counts toward the interval."""
        try:
            self._append({'event': 'refused', 'time': self.clock(), 'conversation_id': conversation_id})
        except (OSError, BridgeError):
            raise BridgeError('send_log_unavailable') from None

    def result(self, conversation_id: str, text: str, state: str) -> None:
        try:
            self._append({'event': 'result', 'time': self.clock(), 'conversation_id': conversation_id,
                          'text_hmac': digest(self.key, text), 'state': state})
        except (OSError, BridgeError):
            pass  # The attempt record already exists; the reply still reports the state.
