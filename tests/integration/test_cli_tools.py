"""The CLI subcommands that mirror the MCP tools: same answers as the service the tools call, JSON on stdout, fixed error codes."""
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from tests.integration.test_mcp_service import service_case
from tests.support.live import sync_text_index
from tests.support.proof import installed_build
from tests.unit.test_send import Fake
from wechat_bridge.cli import main
from wechat_bridge.core.errors import BridgeError


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """(service, run): the MCP tools' ToolService over the shared fixture, and run(*argv) -> (exit code, parsed stdout)."""
    case, service = service_case(tmp_path)
    installed_build(monkeypatch, tmp_path)
    sync_text_index(case.config)

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr('sys.stdin', io.StringIO(stdin))
        code = main(['--state-dir', str(case.config.state_dir), *argv])
        return code, json.loads(capsys.readouterr().out)
    return service, run


def expected(callable_):
    try:
        return 0, callable_()
    except BridgeError as error:
        return 2, error.public_dict()


def timeless(value):
    """The answer without its read times, which differ between two reads of the same data."""
    if isinstance(value, dict):
        return {k: timeless(v) for k, v in value.items() if k != 'observed_at'}
    if isinstance(value, (list, tuple)):
        return [timeless(v) for v in value]
    return value


def group_recipient(service):
    group = next(c for c in service.queries.list_conversations()['items'] if c['kind'] == 'group')
    return group['conversation_id'], service.queries.resolve_recipient(group['conversation_id'])['display_name']


def test_every_read_command_answers_like_the_service_behind_the_mcp_tool(cli):
    service, run = cli
    q = service.queries
    group = next(c for c in q.list_conversations()['items'] if c['kind'] == 'group')
    message = q.search_messages('Known message')['items'][0]
    moment_like = 'moment_' + '0' * 64
    cases = [
        (['list-conversations'], lambda: q.list_conversations()),
        (['list-conversations', '--unread-only', '--sort', 'recent', '--limit', '1'],
         lambda: q.list_conversations(unread_only=True, sort='recent', limit=1)),
        (['list-contacts', '--query', 'Same name'], lambda: q.list_contacts('Same name')),
        (['get-group', group['conversation_id']], lambda: q.get_group(group['conversation_id'])),
        (['search-messages', 'Known message'], lambda: q.search_messages('Known message')),
        (['search', 'Known message', '--limit', '1'], lambda: q.search_messages('Known message', limit=1)),
        (['get-messages', group['conversation_id'], '--start', '2000-01-01T00:00:00Z'],
         lambda: q.get_messages(group['conversation_id'], start='2000-01-01T00:00:00Z')),
        (['recent-messages', '--start', '2000-01-01T00:00:00Z', '--order', 'asc'],
         lambda: q.recent_messages('2000-01-01T00:00:00Z', order='asc')),
        (['get-context', message['message_id'], '--before', '1', '--after', '1'],
         lambda: q.get_context(message['message_id'], before=1, after=1)),
        (['list-attachments'], lambda: q.list_attachments()),
        (['list-moments'], lambda: q.list_moments()),
        (['get-moment', moment_like], lambda: q.get_moment(moment_like)),
        (['list-moment-notifications'], lambda: q.list_moment_notifications()),
    ]
    for argv, call in cases:
        assert timeless(run(*argv)) == timeless(expected(call)), argv
    code, status = run('status')
    assert code == 0 and status['ready_for_local_queries'] and status['read_only'] is True


def test_bad_values_and_unknown_flags_print_fixed_codes_only(cli):
    _, run = cli
    assert run('list-contacts', '--limit', '201') == (2, {'error': 'invalid_limit'})
    assert run('get-group', 'conversation_nothex') == (2, {'error': 'invalid_id'})
    assert run('recent-messages', '--start', '2000-01-01') == (2, {'error': 'invalid_time_range'})
    with pytest.raises(SystemExit) as info:
        run('list-contacts', '--limit', 'secret-value')
    assert info.value.code == 2


def test_argparse_errors_never_echo_the_value(tmp_path, capsys):
    for argv in (['list-contacts', '--limit', 'secret-value'], ['list-contacts', '--secret-flag'], ['no-such-command']):
        with pytest.raises(SystemExit) as info:
            main(['--state-dir', str(tmp_path), *argv])
        assert info.value.code == 2
        output = capsys.readouterr()
        assert json.loads(output.out) == {'error': 'invalid_arguments'} and 'secret' not in output.out + output.err


def test_get_attachment_prints_metadata_and_writes_bytes_only_to_a_new_private_file(cli, tmp_path):
    service, run = cli
    for item in service.queries.list_attachments()['items']:
        code, metadata = run('get-attachment', item['attachment_id'])
        assert code == 0 and metadata['output_written'] is False and 'resource_uri' not in metadata
        target = tmp_path / ('out-' + item['kind'])
        code, metadata = run('get-attachment', item['attachment_id'], '--output', str(target))
        assert code == 0 and metadata['output_written'] is True
        assert target.read_bytes() == service.attachment(item['attachment_id'])[1]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert run('get-attachment', item['attachment_id'], '--output', str(target)) == (2, {'error': 'output_exists'})
    first = service.queries.list_attachments()['items'][0]['attachment_id']
    assert run('get-attachment', first, '--max-bytes', '0') == (2, {'error': 'invalid_content_limit'})


def test_send_message_reads_text_from_stdin_and_uses_the_shared_guards(cli, tmp_path, monkeypatch):
    service, run = cli
    conversation_id, name = group_recipient(service)
    driver = Fake(title=name, count='(2)')
    monkeypatch.setattr('wechat_bridge.send.macos.MacDriver', lambda config: driver)
    monkeypatch.setattr('wechat_bridge.send.log.DEFAULT_DIR', tmp_path / 'send-dir')
    args = ['send-message', '--conversation-id', conversation_id, '--recipient-name', name]
    assert run(*args, stdin='hello\nthere\n') == (0, {'state': 'sent', 'evidence': 'window_shows_message'})
    assert driver.calls[-2:] == ['set:hello\nthere', 'click']
    assert run(*args, stdin='hello\nthere\n') == (2, {'error': 'throttled'})   # the shared 3 s interval
    assert (tmp_path / 'send-dir' / 'send-log.jsonl').is_file()
    wrong = ['send-message', '--conversation-id', conversation_id, '--recipient-name', name + 'x']
    assert run(*wrong, stdin='hi') == (2, {'error': 'refused_recipient_mismatch'})


def test_send_message_unconfirmed_exits_3(cli, tmp_path, monkeypatch):
    service, run = cli
    conversation_id, name = group_recipient(service)
    monkeypatch.setattr('wechat_bridge.send.macos.MacDriver', lambda config: Fake(title=name, count='(2)', echo=False))
    monkeypatch.setattr('wechat_bridge.send.log.DEFAULT_DIR', tmp_path / 'send-dir')
    monkeypatch.setattr('wechat_bridge.send.sender.CONFIRM_SECONDS', 0.0)
    code, result = run('send-message', '--conversation-id', conversation_id, '--recipient-name', name, stdin='hi')
    assert code == 3 and result['state'] == 'unconfirmed'


def test_read_commands_never_import_the_send_package(cli, tmp_path):
    service, _ = cli
    state = str(service.config.state_dir)
    program = ("import sys\nfrom wechat_bridge.cli import main\n"
               f"main(['--state-dir', {state!r}, 'list-contacts'])\nmain(['--state-dir', {state!r}, 'status'])\n"
               "assert not [m for m in sys.modules if m.startswith('wechat_bridge.send')], 'send package imported'\n")
    done = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_concurrent_cli_processes_share_one_state_directory_safely(tmp_path):
    """Each command refreshes on demand under the state flock: parallel runs all succeed and agree on the one identity
    secret they race to create, and the inventory they all rewrite stays valid JSON."""
    from tests.support.live import import_case
    cfg = import_case(tmp_path)
    plist = tmp_path / 'Info.plist'
    import plistlib
    plist.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '4.1.15', 'CFBundleVersion': '270102'}))
    (cfg.state_dir / 'identity.json').unlink(missing_ok=True)   # created once, under the flock, by whichever run is first
    program = ("import sys, pathlib\nfrom wechat_bridge.adapters import wechat_4_1\n"
               f"wechat_4_1.INFO_PLIST = pathlib.Path({str(plist)!r})\n"
               "from wechat_bridge.cli import main\n"
               f"raise SystemExit(main(['--state-dir', {str(cfg.state_dir)!r}, 'list-contacts']))\n")
    env = os.environ | {'PYTHONPATH': os.pathsep.join(sys.path)}
    runs = [subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for _ in range(8)]
    outputs = [p.communicate(timeout=120) for p in runs]
    assert all(p.returncode == 0 for p in runs), outputs
    ids = {tuple(c['contact_id'] for c in json.loads(out)['items']) for out, _ in outputs}
    assert len(ids) == 1   # one identity secret: every process derived the same opaque IDs
    json.loads((cfg.state_dir / 'inventory.json').read_text())
