"""MacDriver against a stubbed subprocess runner: compile, error mapping, coercion and what is sent to the helper."""
from types import SimpleNamespace
import json
import subprocess

import pytest

from wechat_bridge.send.driver import PressUncertain, UiError
from wechat_bridge.send.macos import MacDriver


class Runner:
    def __init__(self, replies, compile_ok=True):
        self.replies, self.compile_ok, self.calls = list(replies), compile_ok, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == 'swiftc':
            if isinstance(self.compile_ok, Exception):
                raise self.compile_ok
            if self.compile_ok:
                open(args[args.index('-o') + 1], 'w').close()
            return SimpleNamespace(returncode=0 if self.compile_ok else 1, stdout='', stderr='')
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(returncode=0, stdout=reply if isinstance(reply, str) else json.dumps(reply), stderr='')


def driver(tmp_path, replies, **kw):
    config = SimpleNamespace(state_dir=tmp_path)
    runner = Runner(replies, **kw)
    return MacDriver(config, runner=runner), runner


def test_compiles_once_and_passes_text_only_on_stdin_as_utf8(tmp_path):
    d, runner = driver(tmp_path, [{'ok': True}, {'ok': True}])
    d.set_composer('你好 secret')
    d.click_send('小逸', 'direct', '你好 secret')
    assert [c[0][0] for c in runner.calls].count('swiftc') == 1
    first, second = runner.calls[1], runner.calls[2]
    assert first[1]['input'] == '你好 secret' and first[1]['encoding'] == 'utf-8' and '你好 secret' not in first[0]
    assert second[0][1:] == ['click_send', '小逸', 'direct'] and second[1]['input'] == '你好 secret'


def test_compile_failure_is_ui_unavailable_and_prepare_swallows_it(tmp_path):
    d, _ = driver(tmp_path, [], compile_ok=False)
    with pytest.raises(UiError) as info:
        d.trusted()
    assert info.value.code == 'ui_unavailable'
    d.prepare()   # does not raise


@pytest.mark.parametrize('reply, code', [({'error': 'no_accessibility'}, 'no_accessibility'), ({'error': 'target_obscured'}, 'target_obscured'),
                                         ({'error': 'something odd'}, 'ui_unavailable'), ('not json', 'ui_unavailable'), ('[1]', 'ui_unavailable'),
                                         (subprocess.TimeoutExpired('x', 1), 'ui_unavailable'), (OSError('boom'), 'ui_unavailable')])
def test_helper_errors_map_to_fixed_codes(tmp_path, reply, code):
    d, _ = driver(tmp_path, [reply])
    with pytest.raises(UiError) as info:
        d.activate()
    assert info.value.code == code


def test_a_click_that_dies_or_returns_garbage_is_press_uncertain_but_a_reported_error_is_not(tmp_path):
    for reply in (subprocess.TimeoutExpired('x', 1), 'garbage', OSError('x')):
        d, _ = driver(tmp_path, [reply])
        with pytest.raises(PressUncertain):
            d.click_send('a', 'direct', 't')
    d, _ = driver(tmp_path, [{'error': 'state_changed'}])
    with pytest.raises(UiError) as info:
        d.click_send('a', 'direct', 't')
    assert info.value.code == 'state_changed'


def test_state_is_coerced_and_junk_fields_become_none(tmp_path):
    d, _ = driver(tmp_path, [{'state': {'title': 'A', 'count_label': None, 'composer_text': 5, 'send_enabled': 'yes', 'bubbles': ['x', 3]}}, {'state': None}])
    state = d.state()
    assert (state.title, state.count_label, state.composer_text, state.send_enabled, state.bubbles) == ('A', None, None, None, ('x',))
    with pytest.raises(UiError):
        d.state()


@pytest.mark.parametrize('failure', [False, FileNotFoundError('swiftc'), subprocess.TimeoutExpired('swiftc', 300), PermissionError('x')])
def test_build_failures_are_normalized_and_prepare_never_raises(tmp_path, failure):
    d, _ = driver(tmp_path, [], compile_ok=failure)
    d.prepare()
    with pytest.raises(UiError) as info:
        d.activate()
    assert info.value.code == 'ui_unavailable'
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('wechat-ui-build-')]   # no leftover temporary files


def test_overlapping_builds_compile_once(tmp_path):
    import threading
    d, runner = driver(tmp_path, [{'ok': True}, {'ok': True}])
    threads = [threading.Thread(target=d.prepare) for _ in range(4)] + [threading.Thread(target=d.activate)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert [c[0][0] for c in runner.calls].count('swiftc') == 1


def test_clear_composer_carries_the_expected_chat_and_text(tmp_path):
    d, runner = driver(tmp_path, [{'ok': True}])
    d.clear_composer('A', 'group', 'mine')
    args, kwargs = runner.calls[-1]
    assert args[1:] == ['clear_composer', 'A', 'group'] and kwargs['input'] == 'mine'
