"""send_message registration: absent by default, flagged on, strict, annotated, driven through a fake UiDriver."""
import asyncio
import subprocess
import sys

import pytest

from mcp import Client

from tests.integration.test_mcp_service import service_case
from tests.support.live import factory, import_case
from tests.unit.test_send import Fake
from wechat_bridge.core.errors import BridgeError


@pytest.fixture(autouse=True)
def send_dir(tmp_path, monkeypatch):
    monkeypatch.setattr('wechat_bridge.send.tool.log_directory', tmp_path / 'send-dir')
    return tmp_path / 'send-dir'


def conversations(case):
    from wechat_bridge.mcp.service import ToolService
    return ToolService(case.config, adapter_factory=factory).queries.list_conversations()['items']


def test_default_server_has_no_send_tool_and_never_imports_the_send_package(tmp_path):
    case, _ = service_case(tmp_path)
    program = ("import asyncio, sys\nfrom mcp import Client\nfrom wechat_bridge.core.config import load_config\n"
               "from wechat_bridge.mcp.server import create_server\n"
               "async def go():\n"
               f"    async with Client(create_server(load_config(__import__('pathlib').Path({str(case.config.state_dir)!r})))) as c:\n"
               "        names = {t.name for t in (await c.list_tools()).tools}\n"
               "        status = (await c.call_tool('status', {})).structured_content\n"
               "    assert 'send_message' not in names and len(names) == 13 and status['read_only'] is True\n"
               "asyncio.run(go())\n"
               "assert not [m for m in sys.modules if m.startswith('wechat_bridge.send')], 'send package imported'\n")
    done = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_enabled_server_registers_one_destructive_open_world_tool_and_status_is_not_read_only(tmp_path):
    from wechat_bridge.mcp.server import create_server
    case, _ = service_case(tmp_path)
    async def scenario():
        async with Client(create_server(case.config, adapter_factory=factory, enable_send=True, send_driver=Fake())) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            send = tools.pop('send_message')
            assert all(t.annotations.read_only_hint for t in tools.values())
            assert (send.annotations.read_only_hint, send.annotations.destructive_hint, send.annotations.open_world_hint,
                    send.annotations.idempotent_hint) == (False, True, True, False)
            assert send.input_schema['additionalProperties'] is False
            assert (await client.call_tool('status', {})).structured_content['read_only'] is False
            unknown = await client.call_tool('send_message', {'conversation_id': 'conversation_' + '0' * 64, 'recipient_name': 'x',
                                                              'text': 'hi', 'files': []})
            assert unknown.structured_content == {'error': 'invalid_arguments'}
    asyncio.run(scenario())


def test_send_message_through_the_server_sends_refuses_and_reports_codes_only(tmp_path, send_dir):
    from wechat_bridge.mcp.server import create_server
    case, _ = service_case(tmp_path)
    group = next(c for c in conversations(case) if c['kind'] == 'group')
    name = group.get('display_name')
    assert name
    async def scenario():
        fake = Fake(title=name, count='(2)')
        async with Client(create_server(case.config, adapter_factory=factory, enable_send=True, send_driver=fake)) as client:
            sent = await client.call_tool('send_message', {'conversation_id': group['conversation_id'], 'recipient_name': name, 'text': 'test'})
            assert not sent.is_error and sent.structured_content['state'] == 'sent'
            wrong = await client.call_tool('send_message', {'conversation_id': group['conversation_id'], 'recipient_name': name + 'x', 'text': 'test2'})
            assert wrong.is_error and wrong.structured_content == {'error': 'refused_recipient_mismatch'}
            missing = await client.call_tool('send_message', {'conversation_id': 'conversation_' + '0' * 64, 'recipient_name': name, 'text': 'x'})
            assert missing.is_error and missing.structured_content == {'error': 'conversation_not_found'}
    asyncio.run(scenario())
    assert (send_dir / 'send-log.jsonl').exists()


def test_name_count_is_exact_for_own_contact_same_named_contacts_conversations_and_collisions(tmp_path):
    from tests.support.encrypted import Engine
    from wechat_bridge.mcp.service import ToolService
    case, _ = service_case(tmp_path)
    root = case.config.account_root / 'db_storage'
    contacts = [('solo', 'Solo'), ('twin-a', 'Twin'), ('twin-b', 'Twin'), ('pair-a', 'Pair'), ('pair-b', 'Pair'),
                ('crew@chatroom', 'Crew'), ('mixed@chatroom', 'Mixed'), ('mixed-contact', 'Mixed')]
    with Engine(root / 'contact/contact.db') as writer:
        writer.run('INSERT INTO contact(id,username,nick_name,remark) VALUES' + ','.join(f"({100 + i},'{u}','{n}','')" for i, (u, n) in enumerate(contacts)))
    sessions = ['solo', 'twin-b', 'pair-a', 'pair-b', 'crew@chatroom', 'mixed@chatroom', 'nameless']
    with Engine(root / 'session/session.db') as writer:
        writer.run('INSERT INTO SessionTable(username,type) VALUES' + ','.join(f"('{u}',0)" for u in sessions))
    identities = factory(case.config).identities
    repository = ToolService(case.config, adapter_factory=factory).queries
    counts = {u: repository.resolve_recipient(identities.conversation_id(u))['name_count'] for u in sessions}
    # A contact that is the conversation itself (its own row, a group's chatroom row) is the same entity, never counted twice.
    assert counts == {'solo': 1, 'twin-b': 2, 'pair-a': 2, 'pair-b': 2, 'crew@chatroom': 1, 'mixed@chatroom': 2, 'nameless': 0}
    assert repository.resolve_recipient(identities.conversation_id('nameless'))['display_name'] is None


def test_enabled_server_starts_and_reports_status_without_readable_keys(tmp_path):
    from wechat_bridge.mcp.server import create_server
    config = import_case(tmp_path / 'empty')
    (config.state_dir / 'keys.json').unlink()
    async def scenario():
        async with Client(create_server(config, adapter_factory=factory, enable_send=True, send_driver=Fake())) as client:
            assert 'send_message' in {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool('send_message', {'conversation_id': 'conversation_' + '0' * 64, 'recipient_name': 'x', 'text': 'hi'})
            assert result.is_error and 'error' in result.structured_content
    asyncio.run(scenario())
