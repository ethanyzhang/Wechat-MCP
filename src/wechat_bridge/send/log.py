"""Send log, throttle and the cross-process send lock. The log holds keyed digests, never message text."""
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
TAIL_BYTES = 65536
LOG_NAME = 'send-log.jsonl'
DEFAULT_LOCK_DIR = Path.home() / 'Library/Application Support/WechatBridgeSend'


def digest(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode('utf-8'), hashlib.sha256).hexdigest()


@contextmanager
def send_lock(directory: Path = DEFAULT_LOCK_DIR):
    """Exclusive per-user lock, outside any state directory so every bridge process on the machine shares it."""
    ensure_private_dir(directory)
    fd = os.open(directory / 'send.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class SendLog:
    def __init__(self, state_dir: Path, key: bytes, clock=time.time):
        self.path, self.key, self.clock = state_dir / LOG_NAME, key, clock

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
        rows = []
        for line in data.decode('utf-8', 'replace').splitlines()[1 if size > TAIL_BYTES else 0:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def check_allowed(self, conversation_id: str, text: str) -> None:
        """Raise throttled or duplicate_send. Attempts count, resolved or not, so an unknown outcome cannot be retried around."""
        now = self.clock()
        attempts = [r for r in self._tail() if r.get('event') == 'attempt']
        if attempts and now - attempts[-1]['time'] < MIN_INTERVAL_SECONDS:
            raise BridgeError('throttled')
        if sum(1 for r in attempts if now - r['time'] < 3600) >= MAX_PER_HOUR:
            raise BridgeError('throttled')
        marker = digest(self.key, text)
        if any(r['conversation_id'] == conversation_id and r['text_hmac'] == marker and now - r['time'] < DUPLICATE_SECONDS for r in attempts):
            raise BridgeError('duplicate_send')

    def _append(self, record: dict) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_private_fd(fd)
            os.write(fd, (json.dumps(record, sort_keys=True) + '\n').encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)

    def attempt(self, conversation_id: str, text: str) -> None:
        """Durable record written before the send control is used; raises if it cannot be written (the send then does not happen)."""
        try:
            self._append({'event': 'attempt', 'time': self.clock(), 'conversation_id': conversation_id,
                          'text_hmac': digest(self.key, text), 'text_length': len(text)})
        except (OSError, BridgeError):
            raise BridgeError('send_log_unavailable') from None

    def result(self, conversation_id: str, text: str, state: str) -> None:
        try:
            self._append({'event': 'result', 'time': self.clock(), 'conversation_id': conversation_id,
                          'text_hmac': digest(self.key, text), 'state': state})
        except (OSError, BridgeError):
            pass  # The attempt record already exists; the reply still reports the state.
