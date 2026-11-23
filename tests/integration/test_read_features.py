"""Unread and recency on conversations, cross-conversation time listing and display names in message results."""
import hashlib

import pytest

from tests.support.encrypted import Engine
from tests.support.live import import_case, factory, repository
from tests.support.schemas import TABLE
from wechat_bridge.core.errors import BridgeError

ALICE = 'fictional-alice'
ALICE_TABLE = 'Msg_' + hashlib.md5(ALICE.encode()).hexdigest()
LONG_NAME = 'é' * 300
NO_ACTIVITY = -(1 << 63)

SESSION = """
ALTER TABLE SessionTable ADD COLUMN unread_count INTEGER;
ALTER TABLE SessionTable ADD COLUMN last_timestamp INTEGER;
UPDATE SessionTable SET unread_count=3,last_timestamp=1700000500 WHERE username='fictional-room@chatroom';
UPDATE SessionTable SET unread_count=0,last_timestamp=1700000900 WHERE username='fictional-alice';
"""
CONTACT = f"""
INSERT INTO chat_room VALUES(11,'only-room@chatroom','fictional-alice',x'');
UPDATE contact SET nick_name='{LONG_NAME}' WHERE username='fictional-bob';
"""
ALICE_MESSAGE = f"""
CREATE TABLE {ALICE_TABLE}(local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, sort_seq INTEGER,
real_sender_id INTEGER, create_time INTEGER, message_content TEXT, compress_content TEXT,
packed_info_data BLOB, WCDB_CT_message_content INTEGER);
INSERT INTO {ALICE_TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content)
VALUES(1,200,1,1,1700000300,'direct hello',0);
"""


def activity_case(tmp_path):
    cfg = import_case(tmp_path, extra={'session/session.db': SESSION, 'contact/contact.db': CONTACT,
                                       'message/message_0.db': ALICE_MESSAGE})
    return cfg, repository(cfg)


def full_conversations(cfg):
    """Unredacted conversation records as the repository holds them (with source usernames and sort keys)."""
    repo = repository(cfg)
    return repo._people(factory(cfg))['conversations']


def by_name(repo, **options):
    return {c['source_username'] if 'source_username' in c else c['display_name']: c
            for c in repo.list_conversations(**options)['items']}


def names(repo, **options):
    return [c['display_name'] for c in repo.list_conversations(**options)['items']]


def test_conversations_carry_unread_count_and_last_activity(tmp_path):
    cfg, _ = activity_case(tmp_path)
    rows = {r['display_name'] or r['kind'] + r['conversation_id'][-4:]: r for r in full_conversations(cfg)}
    group = rows['Fictional group']
    assert (group['unread_count'], group['last_timestamp']) == (3, '2023-11-14T22:21:40+00:00')
    assert group['last_sort'] == 1700000500
    assert group['diagnostic_codes'] == []
    alice = rows['Same name']
    assert (alice['unread_count'], alice['last_sort']) == (0, 1700000900)
    only = next(r for r in rows.values() if r['source_username'] == 'only-room@chatroom')
    assert (only['unread_count'], only['last_timestamp'], only['last_sort']) == (None, None, NO_ACTIVITY)


def test_old_session_schema_reports_unavailable_instead_of_guessing(tmp_path):
    cfg = import_case(tmp_path)
    rows = full_conversations(cfg)
    session_rows = [r for r in rows if r['sources'][0]['table'] == 'SessionTable']
    assert session_rows
    for row in session_rows:
        assert row['diagnostic_codes'] == ['session_unread_unavailable']
        assert row['unread_count'] is None and row['last_timestamp'] is None and row['last_sort'] == NO_ACTIVITY


@pytest.mark.parametrize('row, columns, expected', [
    ({'unread_count': 2, 'last_timestamp': 1700000000}, {'unread_count', 'last_timestamp'}, (2, '2023-11-14T22:13:20+00:00', 1700000000, ())),
    ({'unread_count': True, 'last_timestamp': 5}, {'unread_count', 'last_timestamp'}, (None, '1970-01-01T00:00:05+00:00', 5, ('session_activity_invalid',))),
    ({'unread_count': -1, 'last_timestamp': 1700000000000}, {'unread_count', 'last_timestamp'}, (None, None, NO_ACTIVITY, ('session_activity_invalid',))),
    ({'unread_count': None, 'last_timestamp': 0}, {'unread_count', 'last_timestamp'}, (None, None, NO_ACTIVITY, ())),
    ({'unread_count': 'x', 'last_timestamp': 1.5}, {'unread_count', 'last_timestamp'}, (None, None, NO_ACTIVITY, ('session_activity_invalid',))),
    ({'last_timestamp': 7}, {'last_timestamp'}, (None, '1970-01-01T00:00:07+00:00', 7, ('session_unread_unavailable',))),
    ({}, set(), (None, None, NO_ACTIVITY, ('session_unread_unavailable',))),
])
def test_session_activity_values_are_validated(row, columns, expected):
    from wechat_bridge.adapters.wechat_4_1 import session_activity
    assert session_activity(row, columns) == expected


def test_unread_only_filters_and_combines_with_kind(tmp_path):
    _, repo = activity_case(tmp_path)
    assert names(repo, unread_only=True) == ['Fictional group']
    assert names(repo, unread_only=True, kind='direct') == []
    assert len(repo.list_conversations()['items']) == 3
    items = repo.list_conversations(unread_only=True)['items']
    assert items[0]['unread_count'] == 3 and 'last_sort' not in items[0]


def test_recent_sort_orders_by_activity_with_undated_last_and_pages_across_the_boundary(tmp_path):
    _, repo = activity_case(tmp_path)
    full = [c['conversation_id'] for c in repo.list_conversations(sort='recent')['items']]
    assert names(repo, sort='recent')[:2] == ['Same name', 'Fictional group']
    assert len(full) == 3
    seen, cursor = [], None
    for _ in range(3):
        page = repo.list_conversations(sort='recent', limit=1, cursor=cursor)
        seen += [c['conversation_id'] for c in page['items']]
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert seen == full and cursor is None
    boundary = repo.list_conversations(sort='recent', limit=2)  # the page ends on the last dated row
    rest = repo.list_conversations(sort='recent', limit=2, cursor=boundary['next_cursor'])
    assert [c['conversation_id'] for c in rest['items']] == full[2:]


def test_equal_activity_pages_by_record_id_without_skipping(tmp_path):
    cfg = import_case(tmp_path, extra={'session/session.db': SESSION.replace('1700000900', '1700000500')})
    repo = repository(cfg)
    full = [c['conversation_id'] for c in repo.list_conversations(sort='recent')['items']]
    paged, cursor = [], None
    while True:
        page = repo.list_conversations(sort='recent', limit=1, cursor=cursor)
        paged += [c['conversation_id'] for c in page['items']]
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert paged == full and len(set(full)) == len(full)


def test_default_order_keeps_its_cursor_binding_and_the_sort_is_bound(tmp_path):
    from wechat_bridge.search.cursor import binding, decode_cursor
    _, repo = activity_case(tmp_path)
    page = repo.list_conversations(limit=1)
    # A cursor minted before this feature bound exactly these filters; it must still verify.
    decode_cursor(page['next_cursor'], repo._key, binding('conversations', {'kind': None, 'query': ''}, 1))
    for options in ({'sort': 'recent'}, {'unread_only': True}, {'kind': 'group'}):
        with pytest.raises(BridgeError, match='invalid_cursor'):
            repo.list_conversations(limit=1, cursor=page['next_cursor'], **options)
    recent = repo.list_conversations(sort='recent', limit=1)
    with pytest.raises(BridgeError, match='invalid_cursor'):
        repo.list_conversations(limit=1, cursor=recent['next_cursor'])


@pytest.mark.parametrize('options', [{'sort': 'newest'}, {'unread_only': 'yes'}, {'unread_only': 1}, {'kind': 'other'}])
def test_invalid_conversation_options_are_rejected(tmp_path, options):
    _, repo = activity_case(tmp_path)
    with pytest.raises(BridgeError, match='invalid_filter'):
        repo.list_conversations(**options)


def test_recent_messages_lists_across_conversations_newest_first(tmp_path):
    _, repo = activity_case(tmp_path)
    result = repo.recent_messages('2023-11-14T22:13:20Z')
    assert [m['text'] for m in result['items']] == ['direct hello', 'A reply', '发票 invoice']  # the unknown-time message is excluded
    assert [m['conversation_display_name'] for m in result['items']] == ['Same name', 'Fictional group', 'Fictional group']
    ascending = repo.recent_messages('2023-11-14T22:13:20Z', order='asc')['items']
    assert [m['message_id'] for m in ascending] == [m['message_id'] for m in reversed(result['items'])]
    assert [m['text'] for m in repo.recent_messages('2023-11-14T22:13:22Z', end='2023-11-14T22:18:20Z')['items']] == ['direct hello']
    assert 'utc_timestamp' in result['items'][0]


@pytest.mark.parametrize('order', ['desc', 'asc'])
def test_recent_messages_paginate_without_gaps(tmp_path, order):
    _, repo = activity_case(tmp_path)
    full = [m['message_id'] for m in repo.recent_messages('2023-11-14T00:00:00Z', order=order)['items']]
    paged, cursor = [], None
    while True:
        page = repo.recent_messages('2023-11-14T00:00:00Z', order=order, limit=1, cursor=cursor)
        paged += [m['message_id'] for m in page['items']]
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert paged == full and len(full) == 3
    with pytest.raises(BridgeError, match='invalid_cursor'):
        repo.recent_messages('2023-11-14T00:00:00Z', order='asc' if order == 'desc' else 'desc', limit=1,
                             cursor=repo.recent_messages('2023-11-14T00:00:00Z', order=order, limit=1)['next_cursor'])


def test_recent_messages_sender_filter_and_option_validation(tmp_path):
    _, repo = activity_case(tmp_path)
    everyone = repo.recent_messages('2023-11-14T00:00:00Z')['items']
    sender = next(m['sender_id'] for m in everyone if m['text'] == 'direct hello')
    mine = repo.recent_messages('2023-11-14T00:00:00Z', sender_id=sender)['items']
    assert 'direct hello' in [m['text'] for m in mine] and {m['sender_id'] for m in mine} == {sender}
    assert len(mine) < len(everyone) or {m['sender_id'] for m in everyone} == {sender}
    for options in ({'start': '2023-11-14'}, {'start': None}, {'start': '2023-11-14T00:00:00Z', 'end': '2023-11-13T00:00:00Z'},
                    {'start': '2023-11-14T00:00:00Z', 'limit': 201}, {'start': '2023-11-14T00:00:00Z', 'order': 'sideways'},
                    {'start': '2023-11-14T00:00:00Z', 'type': 'nonsense'}):
        with pytest.raises(BridgeError, match='invalid_time_range|invalid_limit|invalid_filter'):
            repo.recent_messages(**options)


def test_message_results_carry_display_names_everywhere(tmp_path):
    _, repo = activity_case(tmp_path)
    group = next(c for c in repo.list_conversations()['items'] if c['display_name'] == 'Fictional group')
    timeline = repo.get_messages(group['conversation_id'])['items']
    assert {m['conversation_display_name'] for m in timeline} == {'Fictional group'}
    assert any(m['sender_display_name'] == 'Same name' for m in timeline)
    assert any(m['sender_id'] is None and m['sender_display_name'] is None for m in timeline)
    found = repo.search_messages('invoice')['items'][0]
    assert (found['sender_display_name'], found['conversation_display_name']) == ('Same name', 'Fictional group')
    context = repo.get_context(found['message_id'])
    assert (context['message']['sender_display_name'], context['message']['conversation_display_name']) == ('Same name', 'Fictional group')
    assert all('conversation_display_name' in m for m in context['items'])
    same = next(m for m in context['items'] if m['message_id'] == found['message_id'])
    assert (same['sender_display_name'], same['conversation_display_name']) == (context['message']['sender_display_name'], context['message']['conversation_display_name'])


def test_long_display_names_are_cut_and_counted(tmp_path, monkeypatch):
    from wechat_bridge.search import repository as module
    cfg, repo = activity_case(tmp_path)
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:   # bob, whose name is long, sends one message
        writer.run(f"UPDATE {ALICE_TABLE} SET real_sender_id=2 WHERE local_id=1")
    item = next(m for m in repo.recent_messages('2023-11-14T00:00:00Z')['items'] if m['text'] == 'direct hello')
    assert len(item['sender_display_name']) == module.MAX_NAME_CHARACTERS
    monkeypatch.setattr(module, 'MAX_QUERY_BYTES', 2000)
    with pytest.raises(BridgeError, match='content_too_large'):
        repo.recent_messages('2023-11-14T00:00:00Z')


def test_huge_source_names_are_cut_in_message_results(tmp_path):
    from wechat_bridge.search import repository as module
    cfg, repo = activity_case(tmp_path)
    with Engine(cfg.account_root / 'db_storage/contact/contact.db') as writer:
        writer.run("UPDATE contact SET nick_name=printf('%.*c', 60000, 'x')")
    items = repo.recent_messages('2023-11-14T00:00:00Z')['items']
    assert items and all(len(m['sender_display_name'] or '') <= module.MAX_NAME_CHARACTERS
                         and len(m['conversation_display_name']) == module.MAX_NAME_CHARACTERS for m in items)
