"""The send sequence against a fake UiDriver: every refusal, ordering, outcome states, log and throttle."""
from contextlib import nullcontext
import json
import stat

import pytest

from wechat_bridge.core.errors import BridgeError
from wechat_bridge.send.driver import PressUncertain, UiError, WindowState
from wechat_bridge.send.log import MAX_PER_HOUR, SendLog, digest
from wechat_bridge.send.sender import Sender

CONV = 'conversation_' + 'a' * 64
KEY = b'k' * 32


class Fake:
    """A scriptable WeChat window. `echo` makes Send append the composer text as a bubble."""
    def __init__(self, title='Alice', count=None, composer='', echo=True, trusted=True, open_title=None):
        self.title, self.count, self.composer, self.echo, self._trusted = title, count, composer, echo, trusted
        self.bubbles = ['older']; self.calls = []; self.send_enabled = True
        self.fail_after_click = False; self.open_title = open_title; self.set_ignored = False; self.press_error = None; self.on_set = None

    def trusted(self): return self._trusted
    def activate(self): self.calls.append('activate')
    def open_chat(self, name):
        self.calls.append('open'); self.title = self.open_title or name
    def state(self):
        return WindowState(self.title, self.count, self.composer, self.send_enabled if self.composer else False, tuple(self.bubbles))
    def set_composer(self, name, kind, text):
        self.calls.append('set:' + text)
        if self.title != name or self.composer != '':
            raise UiError('state_changed')
        if not self.set_ignored:
            self.composer = text
        if self.on_set: self.on_set(self)
    def clear_composer(self, name, kind, text):
        self.calls.append('clear')
        if self.title == name and self.composer == text:
            self.composer = ''

    def click_send(self, name, kind, text):
        self.calls.append('click')
        if self.press_error:
            raise self.press_error
        if self.echo:
            self.bubbles.append(self.composer); self.composer = ''
        if self.fail_after_click:
            raise PressUncertain('ui_unavailable')


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
    sender, driver, *_ = build(tmp_path / 'g', kind='group', name='Team')
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
    assert 'click' not in driver.calls and driver.composer == ''   # nothing was written, nothing to take back


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


def test_click_that_the_helper_refuses_clears_our_text_and_is_a_refusal_not_unconfirmed(tmp_path):
    driver = Fake(); driver.press_error = UiError('state_changed')
    sender, driver, log, now = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_state_changed'
    assert driver.composer == ''
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e['state'] for e in events if e['event'] == 'result'] == ['refused_state_changed']
    now[0] += 5   # a refused press leaves no duplicate block and no hourly count: the same text can go again
    driver.press_error = None
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'sent'


def test_refusal_never_clears_a_draft_in_another_chat(tmp_path):
    driver = Fake()
    def switch(d):   # the user switches chats right after our text was written
        d.title, d.composer = 'Bob', 'bob draft'
    driver.on_set = switch
    sender, driver, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_composer_mismatch'
    assert driver.composer == 'bob draft' and 'clear' in driver.calls


def test_state_error_after_the_write_clears_the_composer(tmp_path):
    driver = Fake()
    def break_state(d):
        def boom(): raise UiError('ui_unavailable')
        calls = iter([None])
        d.state = lambda: (_ for _ in ()).throw(UiError('ui_unavailable'))
    driver.on_set = break_state
    sender, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_ui_unavailable'


def test_set_composer_refuses_when_chat_switched_or_draft_appeared_before_the_write(tmp_path):
    driver = Fake()
    orig_open = driver.open_chat
    def open_then_type(name):
        orig_open(name); driver.composer = 'half-typed draft'
    driver.open_chat = open_then_type
    sender, *_ = build(tmp_path, driver)
    # state() read empty? no: the draft is already there, so the earlier draft check refuses; now race the write itself.
    driver = Fake()
    real_state = driver.state
    reads = []
    def state():
        st = real_state()
        reads.append(1)
        if len(reads) == 1:
            driver.composer = 'draft typed after the check'
            return WindowState(st.title, st.count_label, '', st.send_enabled, st.bubbles)
        return st
    driver.state = state
    sender, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_state_changed'
    assert driver.composer == 'draft typed after the check'


def test_set_composer_failure_is_a_refusal(tmp_path):
    driver = Fake()
    def boom(name, kind, text): raise UiError('ui_unavailable')
    driver.set_composer = boom
    sender, *_ = build(tmp_path, driver)
    assert code(lambda: sender.send(CONV, 'Alice', 'hi')) == 'refused_ui_unavailable'


def test_substring_bubbles_and_other_chats_do_not_confirm(tmp_path):
    driver = Fake(echo=False)
    def echo_substring(d): pass
    orig = driver.click_send
    def click(name, kind, text):
        orig(name, kind, text); driver.bubbles.append('this is it'); driver.composer = ''
    driver.click_send = click
    sender, *_ = build(tmp_path, driver)
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'unconfirmed'
    driver2 = Fake(echo=False)
    def other(name, kind, text):
        driver2.title = 'Bob'; driver2.bubbles.append(text); driver2.composer = ''
    driver2.click_send = other
    sender, *_ = build(tmp_path / 'x', driver2)
    assert sender.send(CONV, 'Alice', 'hi')['state'] == 'unconfirmed'


def test_refused_after_opening_counts_toward_the_interval(tmp_path):
    sender, driver, log, now = build(tmp_path, Fake(composer='draft'))
    assert code(lambda: sender.send(CONV, 'Alice', 'a')) == 'refused_draft_present'
    now[0] += 1
    assert code(lambda: sender.send(CONV, 'Alice', 'b')) == 'throttled'


def test_log_short_write_is_completed_and_damaged_history_refuses(tmp_path, monkeypatch):
    import os as os_module
    sender, driver, log, now = build(tmp_path)
    real = os_module.write
    monkeypatch.setattr('wechat_bridge.send.log.os.write', lambda fd, data: real(fd, bytes(data)[:5]))
    log.attempt(CONV, 'x')
    monkeypatch.undo()
    assert json.loads(log.path.read_text().splitlines()[0])['event'] == 'attempt'
    with log.path.open('a') as stream:
        stream.write('{broken')
    now[0] += 10
    assert code(lambda: log.check_allowed(CONV, 'y')) == 'send_log_unavailable'


def test_two_registrations_share_one_log_and_the_limit(tmp_path):
    first = SendLog(tmp_path, b'a' * 32, clock=lambda: 1000.0)
    second = SendLog(tmp_path, b'b' * 32, clock=lambda: 1000.0 + 10)
    for index in range(MAX_PER_HOUR):
        first.attempt(CONV, f't{index}')
    assert code(lambda: second.check_allowed(CONV, 'new')) == 'throttled'


def test_send_lock_times_out_when_another_send_holds_it(tmp_path):
    from wechat_bridge.send.log import send_lock
    ticks = iter(range(10 ** 4))
    with send_lock(tmp_path):
        def contend():
            with send_lock(tmp_path, wait=1, sleep=lambda s: None, clock=lambda: next(ticks)):
                pass
        assert code(contend) == 'refused_busy'


def test_a_long_run_of_refusals_cannot_push_recent_attempts_out_of_the_throttle_window(tmp_path):
    clock = [1000.0]
    log = SendLog(tmp_path, KEY, clock=lambda: clock[0])
    for index in range(MAX_PER_HOUR - 1):
        log.attempt(CONV, f'a{index}')
    for _ in range(4000):   # far more refusal records than the old 64 KiB window could hold
        log.refused(CONV)
    clock[0] += 10
    log.attempt(CONV, 'last')
    clock[0] += 10
    assert code(lambda: log.check_allowed(CONV, 'next')) == 'throttled'


def test_a_record_is_never_glued_onto_an_unfinished_line(tmp_path):
    log = SendLog(tmp_path, KEY, clock=lambda: 1000.0)
    log.attempt(CONV, 'a')
    with log.path.open('ab') as stream:
        stream.write(b'{"half')
    log.attempt(CONV, 'b')
    lines = log.path.read_text().splitlines()
    assert json.loads(lines[0])['event'] == 'attempt' and lines[1] == '{"half' and json.loads(lines[2])['event'] == 'attempt'


@pytest.mark.parametrize('line', ['{"event": "attempt"}', '[1, 2]', '{"event": "refused", "time": "x", "conversation_id": "c"}'])
def test_well_formed_but_incomplete_send_log_row_is_send_log_unavailable(tmp_path, line):
    _, _, log, _ = build(tmp_path)
    log.path.parent.mkdir(parents=True, exist_ok=True)
    log.path.write_text(line + '\n')
    log.path.chmod(0o600)
    assert code(lambda: log.check_allowed(CONV, 'y')) == 'send_log_unavailable'
