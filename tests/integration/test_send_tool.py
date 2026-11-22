"""send_message registration: absent by default, flagged on, strict, annotated, driven through a fake UiDriver."""
import asyncio
import subprocess
import sys

import pytest

from mcp import Client

from tests.integration.test_mcp_service import service_case
from tests.unit.test_send import Fake
from wechat_bridge.core.errors import BridgeError


@pytest.fixture(autouse=True)
def send_dir(tmp_path, monkeypatch):
    monkeypatch.setattr('wechat_bridge.send.tool.log_directory', tmp_path / 'send-dir')
    return tmp_path / 'send-dir'


def conversations(case):
    from wechat_bridge.mcp.service import ToolService
    return ToolService(case.config).queries.list_conversations()['items']


def test_default_server_has_no_send_tool_and_never_imports_the_send_package(tmp_path):
    case, _ = service_case(tmp_path)
    program = ("import asyncio, sys\nfrom mcp import Client\nfrom wechat_bridge.core.config import load_config\n"
               "from wechat_bridge.mcp.server import create_server\n"
               "async def go():\n"
               f"    async with Client(create_server(load_config(__import__('pathlib').Path({str(case.config.state_dir)!r})), watch=False)) as c:\n"
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
        async with Client(create_server(case.config, watch=False, enable_send=True, send_driver=Fake())) as client:
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
        async with Client(create_server(case.config, watch=False, enable_send=True, send_driver=fake)) as client:
            sent = await client.call_tool('send_message', {'conversation_id': group['conversation_id'], 'recipient_name': name, 'text': 'test'})
            assert not sent.is_error and sent.structured_content['state'] == 'sent'
            wrong = await client.call_tool('send_message', {'conversation_id': group['conversation_id'], 'recipient_name': name + 'x', 'text': 'test2'})
            assert wrong.is_error and wrong.structured_content == {'error': 'refused_recipient_mismatch'}
            missing = await client.call_tool('send_message', {'conversation_id': 'conversation_' + '0' * 64, 'recipient_name': name, 'text': 'x'})
            assert missing.is_error and missing.structured_content == {'error': 'conversation_not_found'}
    asyncio.run(scenario())
    assert (send_dir / 'send-log.jsonl').exists()


def cid(n): return 'conversation_' + format(n, '064x')
def kid(n): return 'contact_' + format(n, '064x')


def publish(case, contacts, conversations):
    from wechat_bridge.archive.store import Archive
    archive = Archive(case.config); generation = archive.begin_generation(archive.status()['account_id'])
    archive.write_records(generation, 'contacts', contacts)
    archive.write_records(generation, 'conversations', conversations)
    archive.publish(generation, {})


def test_name_count_is_exact_for_own_contact_same_named_contacts_conversations_and_collisions(tmp_path):
    from wechat_bridge.mcp.service import ToolService
    case, _ = service_case(tmp_path)
    publish(case,
            [{'contact_id': kid(1), 'display_name': 'Solo', 'source_username': 'solo'},          # own contact row of a direct chat
             {'contact_id': kid(2), 'display_name': 'Twin', 'source_username': 'twin-a'},         # a different same-named contact...
             {'contact_id': kid(3), 'display_name': 'Pair', 'source_username': 'pair-a'},
             {'contact_id': kid(4), 'display_name': 'Pair', 'source_username': 'pair-b'},
             {'contact_id': kid(5), 'display_name': 'Crew', 'source_username': 'crew@chatroom'},  # a group's own chatroom row
             {'contact_id': kid(6), 'display_name': 'Mixed', 'source_username': 'mixed-contact'}],
            [{'conversation_id': cid(1), 'kind': 'direct', 'display_name': 'Solo', 'source_username': 'solo'},
             {'conversation_id': cid(2), 'kind': 'direct', 'display_name': 'Twin', 'source_username': 'twin-b'},
             {'conversation_id': cid(3), 'kind': 'direct', 'display_name': 'Pair', 'source_username': 'pair-a'},
             {'conversation_id': cid(4), 'kind': 'direct', 'display_name': 'Pair', 'source_username': 'pair-b'},
             {'conversation_id': cid(5), 'kind': 'group', 'display_name': 'Crew', 'source_username': 'crew@chatroom'},
             {'conversation_id': cid(6), 'kind': 'group', 'display_name': 'Mixed', 'source_username': 'mixed@chatroom'},
             {'conversation_id': cid(7), 'kind': 'direct', 'display_name': None, 'source_username': 'nameless'}])
    repository = ToolService(case.config).queries
    counts = {n: repository.resolve_recipient(cid(n))['name_count'] for n in range(1, 8)}
    assert counts == {1: 1, 2: 2, 3: 2, 4: 2, 5: 1, 6: 2, 7: 0}
    assert repository.resolve_recipient(cid(7))['display_name'] is None


def test_enabled_server_starts_and_reports_status_without_a_search_ready_archive(tmp_path):
    from wechat_bridge.mcp.server import create_server
    from tests.integration.test_importer import import_case
    config = import_case(tmp_path / 'empty')
    async def scenario():
        async with Client(create_server(config, watch=False, enable_send=True, send_driver=Fake())) as client:
            assert 'send_message' in {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool('send_message', {'conversation_id': 'conversation_' + '0' * 64, 'recipient_name': 'x', 'text': 'hi'})
            assert result.is_error and 'error' in result.structured_content
    asyncio.run(scenario())
