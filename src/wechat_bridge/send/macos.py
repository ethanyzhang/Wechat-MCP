"""The real UiDriver: a small compiled Swift helper (wechat_ui.swift) that drives WeChat through Accessibility.

The helper runs as a child of the bridge, so the Accessibility grant belongs to the app that launched the bridge
(for example Claude.app). It is compiled once into the state directory, keyed by the hash of its source.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .driver import UiError, WindowState

SOURCE = Path(__file__).with_name('wechat_ui.swift')
OPEN_TIMEOUT = 120
STEP_TIMEOUT = 20


class MacDriver:
    def __init__(self, config, *, runner=subprocess.run):
        self.config, self.runner, self._binary = config, runner, None

    def _helper(self) -> Path:
        if self._binary is None:
            source = SOURCE.read_bytes()
            binary = self.config.state_dir / ('wechat-ui-' + hashlib.sha256(source).hexdigest()[:16])
            if not binary.is_file():
                temp = binary.with_suffix('.tmp')
                result = self.runner(['swiftc', '-O', '-o', str(temp), str(SOURCE)], capture_output=True, timeout=300)
                if result.returncode != 0:
                    temp.unlink(missing_ok=True)
                    raise UiError('ui_unavailable')
                os.replace(temp, binary)
            self._binary = binary
        return self._binary

    def _call(self, *args, stdin: str | None = None, timeout=STEP_TIMEOUT) -> dict:
        try:
            result = self.runner([str(self._helper()), *args], input=stdin, capture_output=True, text=True, timeout=timeout)
            reply = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise UiError('ui_unavailable') from None
        if not isinstance(reply, dict):
            raise UiError('ui_unavailable')
        if 'error' in reply:
            code = reply['error']
            raise UiError('no_accessibility' if code == 'no_accessibility' else 'ui_unavailable')
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

    def click_send(self) -> None:
        self._call('click_send')
