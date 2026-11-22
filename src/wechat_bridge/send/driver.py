"""The UI boundary of the send path: what the sender needs from WeChat's window, behind a swappable interface."""
from dataclasses import dataclass
from typing import Protocol


class UiError(Exception):
    """A UI step failed; `code` is a fixed refusal code (never window content)."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PressUncertain(Exception):
    """The Send press may or may not have happened (crash, timeout or unreadable output after dispatch)."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WindowState:
    """What is visible in the WeChat window right now. Text fields are None when the element is not reachable."""
    title: str | None            # current chat title
    count_label: str | None      # member-count label, present only in group chats
    composer_text: str | None    # draft in the input box
    send_enabled: bool | None    # whether the Send control is active
    bubbles: tuple[str, ...]     # visible message bubble texts, oldest first


class UiDriver(Protocol):
    """Every method either returns or raises UiError. Callers verify results by reading `state()`, never by return codes."""
    def trusted(self) -> bool: ...
    def activate(self) -> None: ...
    def open_chat(self, name: str) -> None: ...
    def state(self) -> WindowState: ...
    def set_composer(self, text: str) -> None: ...
    def click_send(self, name: str, kind: str, text: str) -> None:
        """Re-verify title, kind and composer text in the same step, then press Send once. UiError means nothing was pressed;
        any other exception means the press may have happened."""
        ...
