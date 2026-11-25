"""The send sequence. Fails closed: every step is verified by reading the window, never by a return code."""
import time

from ..core.errors import BridgeError
from .driver import UiDriver, UiError, WindowState
from .log import SendLog, send_lock

MAX_TEXT_CHARACTERS = 2000
CONFIRM_SECONDS = 3.0


def check_text(text) -> str:
    """Non-empty text of at most 2000 characters with no control character except newline; raises invalid_text."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise BridgeError('invalid_text')
    if any(ord(c) < 32 and c != '\n' or ord(c) == 127 for c in text):
        raise BridgeError('invalid_text')
    return text


def title_matches(state: WindowState, name: str, kind: str) -> bool:
    # Exact title; a group also shows a member-count label, a direct chat never does.
    return state.title == name and (state.count_label is not None) == (kind == 'group')


class Sender:
    def __init__(self, driver: UiDriver, log: SendLog, resolve, *, lock=send_lock, sleep=time.sleep, clock=time.monotonic):
        """`resolve(conversation_id)` returns display_name, kind and name_count (QueryRepository.resolve_recipient)."""
        self.driver, self.log, self.resolve, self.lock, self.sleep, self.clock = driver, log, resolve, lock, sleep, clock

    def send(self, conversation_id: str, recipient_name: str, text: str) -> dict:
        """Send one text message. Returns {'state': 'sent'|'unconfirmed', ...}; refusals raise BridgeError('refused_*').

        `sent` means the window shows the message, not that WeChat delivered it. After `unconfirmed` do not resend.
        """
        check_text(text)
        recipient = self.resolve(conversation_id)
        name, kind = recipient['display_name'], recipient['kind']
        if not name or kind not in ('direct', 'group'):
            raise BridgeError('refused_recipient_unnamed')
        if recipient['name_count'] != 1:
            raise BridgeError('refused_recipient_ambiguous')
        if recipient_name != name:
            raise BridgeError('refused_recipient_mismatch')
        with self.lock():
            self.log.check_allowed(conversation_id, text)
            return self._send_locked(conversation_id, name, kind, text)

    def _state(self) -> WindowState:
        try:
            return self.driver.state()
        except UiError as error:
            raise BridgeError('refused_' + error.code) from None

    def _send_locked(self, conversation_id, name, kind, text):
        driver = self.driver
        try:
            if not driver.trusted():
                raise BridgeError('refused_no_accessibility')
            driver.activate()
            driver.open_chat(name)
        except UiError as error:
            raise BridgeError('refused_' + error.code) from None
        try:
            return self._after_open(conversation_id, name, kind, text)
        except BridgeError as error:
            if error.code.startswith('refused_'):
                self.log.refused(conversation_id)   # opening the chat already touched WeChat (it marks the chat read)
            raise

    def _after_open(self, conversation_id, name, kind, text):
        driver = self.driver
        state = self._state()
        if not title_matches(state, name, kind):
            raise BridgeError('refused_wrong_chat')
        if state.composer_text is None or state.send_enabled is None:
            raise BridgeError('refused_ui_unavailable')
        if state.composer_text != '':
            raise BridgeError('refused_draft_present')
        baseline = state.bubbles
        try:
            driver.set_composer(name, kind, text)
            state = self._state()
            if not title_matches(state, name, kind) or state.composer_text != text or state.send_enabled is not True:
                raise BridgeError('refused_composer_mismatch')
            self.log.attempt(conversation_id, text)   # Durable before the press; raises send_log_unavailable (nothing sent).
        except UiError as error:
            self._clear_if_ours(name, kind, text)
            raise BridgeError('refused_' + error.code) from None
        except BridgeError:
            self._clear_if_ours(name, kind, text)
            raise
        try:
            driver.click_send(name, kind, text)
        except UiError as error:
            # The helper refused before pressing: nothing was sent. Take our text back out and say so.
            self._clear_if_ours(name, kind, text)
            self.log.result(conversation_id, text, 'refused_' + error.code)
            raise BridgeError('refused_' + error.code) from None
        except BaseException:
            # The press may have happened: never claim otherwise, never retry. BaseException: a Ctrl-C in the command line
            # during the press or the confirmation still ends as a logged `unconfirmed`, never as "cancelled".
            outcome = 'unconfirmed'
        else:
            try:
                outcome = self._confirm(name, kind, text, baseline)
            except BaseException:
                outcome = 'unconfirmed'
        self.log.result(conversation_id, text, outcome)
        if outcome == 'sent':
            return {'state': 'sent', 'evidence': 'window_shows_message'}
        return {'state': 'unconfirmed', 'note': 'The message may or may not have been sent; do not resend. Check the chat.'}

    def _clear_if_ours(self, name, kind, text):
        """Take our text back out. The driver checks chat and contents in the same step it clears, so a chat switch between
        a check and the write cannot erase someone else's draft."""
        try:
            self.driver.clear_composer(name, kind, text)
        except Exception:
            pass

    def _confirm(self, name, kind, text, baseline) -> str:
        """sent only when, in the same chat, the composer emptied and the count of bubbles exactly equal to the text grew."""
        end = self.clock() + CONFIRM_SECONDS
        before = sum(b == text for b in baseline)
        while True:
            state = self.driver.state()
            if title_matches(state, name, kind) and state.composer_text == '' and sum(b == text for b in state.bubbles) > before:
                return 'sent'
            if self.clock() >= end:
                return 'unconfirmed'
            self.sleep(0.25)


def build_sender(repository, driver, directory) -> Sender:
    """The Sender every entry point uses (CLI and MCP): the shared per-user send log and lock in `directory`."""
    return Sender(driver, SendLog(directory, repository.identity_key), repository.resolve_recipient,
                  lock=lambda: send_lock(directory))
