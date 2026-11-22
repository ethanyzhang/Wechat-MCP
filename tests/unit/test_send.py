"""The send sequence against a fake UiDriver: every refusal, ordering, outcome states, log and throttle."""
from contextlib import nullcontext
import json
import stat

import pytest

from wechat_bridge.core.errors import BridgeError
from wechat_bridge.send.driver import UiError, WindowState
from wechat_bridge.send.log import MAX_PER_HOUR, SendLog, digest
from wechat_bridge.send.sender import Sender

CONV = 'conversation_' + 'a' * 64
KEY = b'k' * 32


class Fake:
    """A scriptable WeChat window. `echo` makes Send append the composer text as a bubble."""
    def __init__(self, title='Alice', count=None, composer='', echo=True, trusted=True, open_title=None):
        self.title, self.count, self.composer, self.echo, self._trusted = title, count, composer, echo, trusted
        self.bubbles = ['older']; self.calls = []; self.send_enabled = True
        self.fail_after_click = False; self.open_title = open_title; self.set_ignored = False

    def trusted(self): return self._trusted
    def activate(self): self.calls.append('activate')
    def open_chat(self, name):
        self.calls.append('open'); self.title = self.open_title or name
    def state(self):
        return WindowState(self.title, self.count, self.composer, self.send_enabled if self.composer else False, tuple(self.bubbles))
    def set_composer(self, text):
        self.calls.append('set:' + text)
        if not self.set_ignored:
            self.composer = text
    def click_send(self):
        self.calls.append('click')
        if self.echo:
            self.bubbles.append(self.composer); self.composer = ''
        if self.fail_after_click:
            raise UiError('ui_unavailable')


def build(tmp_path, driver=None, *, name='Alice', kind='direct', count=1, clock=None):
    driver = driver or Fake(count=None if kind == 'direct' else '(3)')
    now = [1000.0]
    log = SendLog(tmp_path, KEY, clock=clock or (lambda: now[0]))
    resolve = lambda conversation_id: {'display_name': name, 'kind': kind, 'name_count': count}
    ticks = iter(range(10 ** 6))
    sender = Sender(driver, log, resolve, lock=nullcontext, sleep=lambda s: None, clock=lambda: next(ticks) * 0.5)
    return sender, driver, log, now


def code(callable_):
    with pytest.raises(BridgeError) as info:
        callable_()
    return info.value.code


def test_sent_direct_and_group_chat_in_order(tmp_path):
    sender, driver, log, _ = build(tmp_path)
    assert sender.send(CONV, 'Alice', 'hello\nworld') == {'state': 'sent', 'evidence': 'window_shows_message'}
    assert driver.calls == ['activate', 'open', 'set:hello\nworld', 'click']
    sender, driver, *_ = build(tmp_path / 'g' if (tmp_path / 'g').mkdir() is None else tmp_path, kind='group', name='Team')
    assert sender.send(CONV, 'Team', 'hi')['state'] == 'sent'


@pytest.mark.parametrize('text', ['', '   ', 'x' * 2001, 'a\x00b', 'a\rb', 'tab\there', 5])
def test_invalid_text_is_refused_before_any_ui_step(tmp_path, text):
    sender, driver, *_ = build(tmp_path)
    assert code(lambda: sender.send(CONV, 'Alice', text)) == 'invalid_text'
    assert driver.calls == []


@pytest.mark.parametrize('kwargs, given, expected', [
    ({'name': None}, 'Alice', 'refused_recipient_unnamed'),
    ({'count': 2}, 'Alice', 'refused_recipient_ambiguous'),
    ({'count': 0}, 'Alice', 'refused_recipient_ambiguous'),
    ({}, 'alice', 'refused_recipient_mismatch'),
    ({}, 'Alice ', 'refused_recipient_mismatch'),
])
def test_recipient_binding_refusals_touch_nothing(tmp_path, kwargs, given, expected):
    sender, driver, *_ = build(tmp_path, **kwargs)
    assert code(lambda: sender.send(CONV, given, 'hi')) == expected
    assert driver.calls == []


def test_no_accessibility_refuses_before_activating(tmp_path):
    sender, driver, *_ = build(tmp_path, Fake(trusted=False))
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_no_accessibility'
    assert driver.calls == []


@pytest.mark.parametrize('driver, expected', [
    (Fake(open_title='Bob'), 'refused_wrong_chat'),            # opened a different chat
    (Fake(title='Alice', count='(3)'), 'refused_wrong_chat'),  # a group titled like a direct chat
    (Fake(composer='half-typed'), 'refused_draft_present'),
])
def test_wrong_chat_and_draft_are_refused_without_writing(tmp_path, driver, expected):
    sender, driver, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == expected
    assert not any(c.startswith('set:') or c == 'click' for c in driver.calls)


def test_direct_chat_with_a_count_label_and_group_without_one_are_wrong(tmp_path):
    sender, driver, *_ = build(tmp_path, Fake(title='Team', count=None), name='Team', kind='group')
    assert code(lambda: sender.send(CONV, 'Team', 'hi')) == 'refused_wrong_chat'


def test_composer_that_does_not_take_the_text_is_cleared_and_refused(tmp_path):
    driver = Fake(); driver.set_ignored = True
    sender, driver, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_composer_mismatch'
    assert 'click' not in driver.calls and driver.calls[-1] == 'set:'


def test_ui_error_while_opening_is_a_refusal(tmp_path):
    driver = Fake()
    def boom(name): raise UiError('ui_unavailable')
    driver.open_chat = boom
    sender, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_ui_unavailable'


def test_no_new_bubble_is_unconfirmed_and_writes_attempt_then_result(tmp_path):
    driver = Fake(echo=False)
    sender, driver, log, _ = build(tmp_path, driver)
    reply = sender.send(CONV, 'Alice', 'hi')
    assert reply['state'] == 'unconfirmed' and 'do not resend' in reply['note']
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e['event'] for e in events] == ['attempt', 'result'] and events[1]['state'] == 'unconfirmed'


def test_an_older_identical_bubble_does_not_confirm_a_new_send(tmp_path):
    driver = Fake(echo=False); driver.bubbles = ['hi']
    sender, *_ = build(tmp_path, driver)
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'unconfirmed'


def test_an_identical_text_sent_again_is_confirmed_by_the_extra_bubble(tmp_path):
    driver = Fake(); driver.bubbles = ['hi']
    sender, *_ = build(tmp_path, driver)
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'sent'


def test_exception_after_the_press_is_unconfirmed_not_an_error(tmp_path):
    driver = Fake(); driver.fail_after_click = True
    sender, driver, log, _ = build(tmp_path, driver)
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'unconfirmed'
    assert driver.calls.count('click') == 1   # never retried


def test_log_has_keyed_digest_not_text_and_is_private_append_only(tmp_path):
    sender, driver, log, _ = build(tmp_path)
    sender.send(CONV, 'Alice', 'secret words')
    raw = log.path.read_text()
    assert 'secret' not in raw and 'Alice' not in raw
    assert digest(KEY, 'secret words') in raw and digest(b'other', 'secret words') not in raw
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_unwritable_log_fails_closed_before_the_press(tmp_path):
    sender, driver, log, _ = build(tmp_path)
    log.path.mkdir()   # a directory where the log file should be
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'send_log_unavailable'
    assert 'click' not in driver.calls


def test_throttle_interval_hourly_cap_and_duplicate_window(tmp_path):
    sender, driver, log, now = build(tmp_path)
    sender.send(CONV, 'Alice', 'one')
    now[0] += 1
    assert code(lambda: sender.send(CONV, 'Alice', 'two')) == 'throttled'
    now[0] += 5
    assert code(lambda: sender.send(CONV, 'Alice', 'one')) == 'duplicate_send'
    now[0] += 700
    assert sender.send(CONV, 'Alice', 'one')['state'] == 'sent'   # outside the duplicate window
    for index in range(MAX_PER_HOUR):
        log.attempt(CONV, f't{index}')
    now[0] += 5
    assert code(lambda: sender.send(CONV, 'Alice', 'late')) == 'throttled'


def test_unresolved_attempt_still_counts_for_the_throttle(tmp_path):
    sender, driver, log, now = build(tmp_path)
    log.attempt(CONV, 'x')   # attempt without a result, as after a crash
    now[0] += 1
    assert code(lambda: sender.send(CONV, 'Alice', 'y')) == 'throttled'
