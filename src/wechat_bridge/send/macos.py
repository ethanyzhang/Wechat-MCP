"""The real UiDriver: a small compiled Swift helper (wechat_ui.swift) that drives WeChat through Accessibility.

The helper runs as a child of the bridge, so the Accessibility grant belongs to the app that launched the bridge
(for example Claude.app). It is compiled once into the state directory, keyed by the hash of its source.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from .driver import PressUncertain, UiError, WindowState

SOURCE = Path(__file__).with_name('wechat_ui.swift')
OPEN_TIMEOUT = 120
STEP_TIMEOUT = 20
KNOWN_CODES = {'no_accessibility', 'wechat_not_running', 'wechat_not_frontmost', 'target_obscured', 'no_window', 'chat_pane_unavailable',
               'chat_not_in_list', 'row_not_visible', 'state_changed', 'send_control_not_found', 'ui_unavailable'}


class MacDriver:
    def __init__(self, config, *, runner=subprocess.run):
        self.config, self.runner, self._binary, self._lock = config, runner, None, threading.Lock()

    def prepare(self) -> None:
        """Compile the helper now (called at server start) so the first approved send does not wait for swiftc."""
        try:
            self._helper()
        except UiError:
            pass  # A failure is reported again, as a refusal, when a send needs the helper.

    def _helper(self) -> Path:
        with self._lock:   # one build at a time in this process; the unique temporary name covers other processes
            if self._binary is None:
                try:
                    source = SOURCE.read_bytes()
                    binary = self.config.state_dir / ('wechat-ui-' + hashlib.sha256(source).hexdigest()[:16])
                    if not binary.is_file():
                        descriptor, temp = tempfile.mkstemp(dir=self.config.state_dir, prefix='wechat-ui-build-')
                        os.close(descriptor)
                        try:
                            result = self.runner(['swiftc', '-O', '-o', temp, str(SOURCE)], capture_output=True, timeout=300)
                            if result.returncode != 0:
                                raise UiError('ui_unavailable')
                            os.replace(temp, binary)
                        finally:
                            Path(temp).unlink(missing_ok=True)
                except (OSError, subprocess.SubprocessError):
                    raise UiError('ui_unavailable') from None
                self._binary = binary
            return self._binary

    def _call(self, *args, stdin: str | None = None, timeout=STEP_TIMEOUT, pressing=False) -> dict:
        """One helper run. A reply carrying an error code is a UiError (the helper reports errors only before it acts).
        With pressing=True a crash, timeout or unreadable output raises PressUncertain instead, since the press may have happened."""
        fail = PressUncertain if pressing else UiError
        try:
            result = self.runner([str(self._helper()), *args], input=stdin, capture_output=True, encoding='utf-8', timeout=timeout)
            reply = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise fail('ui_unavailable') from None
        if not isinstance(reply, dict):
            raise fail('ui_unavailable')
        if 'error' in reply:
            code = reply['error']
            if isinstance(reply.get('diag'), str):   # counts and flags only, never names; stderr is the server log
                print('wechat_ui ' + str(code)[:40] + ': ' + reply['diag'][:200], file=sys.stderr, flush=True)
            raise UiError(code if code in KNOWN_CODES else 'ui_unavailable')
        return reply

    def trusted(self) -> bool:
        return self._call('trusted').get('trusted') is True

    def activate(self) -> None:
        self._call('activate')

    def open_chat(self, name: str) -> None:
        self._call('open', name, timeout=OPEN_TIMEOUT)

    def state(self) -> WindowState:
        state = self._call('state').get('state')
        if not isinstance(state, dict):
            raise UiError('ui_unavailable')
        def text(key):
            value = state.get(key)
            return value if isinstance(value, str) else None
        send = state.get('send_enabled')
        return WindowState(title=text('title'), count_label=text('count_label'), composer_text=text('composer_text'),
                           send_enabled=send if isinstance(send, bool) else None,
                           bubbles=tuple(b for b in state.get('bubbles', []) if isinstance(b, str)))

    def set_composer(self, text: str) -> None:
        self._call('set_composer', stdin=text)

    def clear_composer(self, name: str, kind: str, text: str) -> None:
        self._call('clear_composer', name, kind, stdin=text)

    def click_send(self, name: str, kind: str, text: str) -> None:
        self._call('click_send', name, kind, stdin=text, pressing=True)
