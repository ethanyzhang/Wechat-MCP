import pytest

from tests.support.live import repo_case
from tests.support.schemas import TABLE
from tests.support.encrypted import Engine
from wechat_bridge.core.errors import BridgeError


def test_cursor_binds_filters_limit_and_authenticates_payload(tmp_path):
    _, repo = repo_case(tmp_path)
    page = repo.list_contacts(limit=1)
    token = page['next_cursor']; assert token
    next_page = repo.list_contacts(limit=1, cursor=token)
    assert next_page['items'][0]['contact_id'] != page['items'][0]['contact_id']
    for options in ({'limit': 2}, {'limit': 1, 'query': 'Same'}, {'limit': 1, 'cursor': token[:-2] + 'XX'}):
        with pytest.raises(BridgeError):
            repo.list_contacts(cursor=token, **options) if 'cursor' not in options else repo.list_contacts(**options)


def test_rank_cursor_resumes_after_its_position_when_rows_are_added_live(tmp_path):
    cfg, repo = repo_case(tmp_path)
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"UPDATE {TABLE} SET local_type=1,message_content='发票 invoice',create_time=1700000000 WHERE local_id=2; INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(3,126,1,1,1700000000,'发票 invoice',0)")
    expected = [m['message_id'] for m in repo.search_messages('发票')['items']]
    assert len(expected) == 3
    first = repo.search_messages('发票', limit=1)
    second = repo.search_messages('发票', limit=1, cursor=first['next_cursor'])
    with Engine(cfg.account_root / 'db_storage/message/message_1.db') as writer:   # a later, longer match ranks after every page so far
        writer.run(f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(9,999,1,1,1700000005,'发票 invoice and much more text',0)")
    third = repo.search_messages('发票', limit=1, cursor=second['next_cursor'])
    assert [first['items'][0]['message_id'], second['items'][0]['message_id'], third['items'][0]['message_id']] == expected
    fourth = repo.search_messages('发票', limit=1, cursor=third['next_cursor'])
    assert fourth['items'][0]['message_id'] not in expected and fourth['next_cursor'] is None


def test_cursor_from_older_code_is_invalid_not_misread(tmp_path):
    import base64, hashlib, hmac, json
    _, repo = repo_case(tmp_path)
    raw = json.dumps({'v': 1, 'g': 1, 'b': 'x', 'last': ['a']}, separators=(',', ':')).encode()
    old = base64.urlsafe_b64encode(raw + hmac.digest(repo.identity_key, b'wechat-cursor-v1\0' + raw, 'sha256')).decode().rstrip('=')
    with pytest.raises(BridgeError, match='invalid_cursor'):
        repo.list_contacts(limit=1, cursor=old)


def test_search_cursor_handles_invalid_source_time_without_integer_overflow(tmp_path):
    cfg, repo = repo_case(tmp_path)
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"UPDATE {TABLE} SET local_type=1,message_content='发票 invoice',create_time=NULL WHERE local_id IN (1,2)")
    first = repo.search_messages('发票', limit=1)
    second = repo.search_messages('发票', limit=1, cursor=first['next_cursor'])
    assert first['items'][0]['message_id'] != second['items'][0]['message_id']
    assert second['next_cursor'] is None


def test_timeline_pages_merge_shards_with_equal_times_and_local_ids_past_the_read_cap(tmp_path):
    cfg, repo = repo_case(tmp_path)
    rows = ','.join(f"({i},{1000 + i},1,1,1700000100,'m{i}',0)" for i in range(10, 260))
    for shard in ('message_0', 'message_1'):   # the same local_ids and times in both shards
        with Engine(cfg.account_root / f'db_storage/message/{shard}.db') as writer:
            writer.run(f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES{rows}")
    chat = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    for order in ('asc', 'desc'):
        seen, cursor = [], None
        while True:
            page = repo.get_messages(chat['conversation_id'], limit=200, order=order, cursor=cursor)
            seen += [m['message_id'] for m in page['items']]
            cursor = page['next_cursor']
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 3 + 500
    whole = repo.get_messages(chat['conversation_id'], limit=200)['items']
    assert [m['utc_timestamp'] for m in whole] == sorted((m['utc_timestamp'] for m in whole), key=lambda t: (t is not None, t or ''))


def test_type_filter_is_the_decoded_type_and_scans_past_rejected_rows(tmp_path):
    cfg, repo = repo_case(tmp_path)
    broken = ','.join(f"({i},{2000 + i},1,1,{1700000200 + i},x'ff',0)" for i in range(20, 260))   # invalid UTF-8: decoded as unsupported
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES{broken},(300,3000,1,1,1700009999,'last text',0)")
    chat = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    texts = repo.get_messages(chat['conversation_id'], type='text', limit=2)
    assert [m['text'] for m in texts['items']] == ['发票 invoice', 'last text'] and texts['next_cursor'] is None
    unsupported = repo.get_messages(chat['conversation_id'], type='unsupported', limit=200)
    assert len(unsupported['items']) == 200 and unsupported['next_cursor']
    assert all(m['type'] == 'unsupported' for m in unsupported['items'])


def test_recent_sorted_conversations_page_by_activity_then_id(tmp_path):
    _, repo = repo_case(tmp_path)
    everything = [c['conversation_id'] for c in repo.list_conversations(sort='recent')['items']]
    seen, cursor = [], None
    while True:
        page = repo.list_conversations(sort='recent', limit=1, cursor=cursor)
        seen += [c['conversation_id'] for c in page['items']]
        cursor = page['next_cursor']
        if not cursor:
            break
    assert seen == everything and len(everything) == 2


def test_search_keeps_messages_in_different_shards_that_share_a_local_id(tmp_path):
    from tests.support.live import sync_text_index
    cfg, repo = repo_case(tmp_path)
    for shard, when in (('message_0', 1700000000), ('message_1', 1700000500)):
        with Engine(cfg.account_root / f'db_storage/message/{shard}.db') as writer:
            writer.run(f"DELETE FROM {TABLE} WHERE local_id=7; INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(7,{9000 + when % 7},1,1,{when},'发票 shared-id',0)")
    sync_text_index(cfg)
    items = [m for m in repo.search_messages('shared-id', limit=10)['items']]
    assert sorted(m['utc_timestamp'] for m in items) == ['2023-11-14T22:13:20+00:00', '2023-11-14T22:21:40+00:00']
