import pytest

from tests.support.live import import_case, factory, repo_case, repository
from tests.support.schemas import TABLE
from tests.support.encrypted import Engine
from wechat_bridge.core.errors import BridgeError


def test_short_chinese_and_structured_filters_search_wechats_text_index(tmp_path):
    cfg, repo = repo_case(tmp_path)
    result = repo.search_messages('发票')
    assert len(result['items']) == 1
    hit = result['items'][0]
    assert hit['excerpt'] == '发票 invoice'
    assert hit['sender_id'] is not None
    assert repo.search_messages('invoice', sender_id=hit['sender_id'], type='text',
                                start='2023-11-14T22:13:20Z', end='2023-11-14T22:13:20Z')['items'][0]['message_id'] == hit['message_id']
    assert repo.search_messages('invoice', start='2023-11-14T22:13:20.5Z')['items'] == []
    assert repo.search_messages('absent')['items'] == []


def test_unicode_and_sql_looking_queries_are_literal(tmp_path):
    cfg, repo = repo_case(tmp_path)
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"UPDATE {TABLE} SET message_content='ＡＢＣ 发票 INVＯICE café 🐈！' WHERE local_id=1")
    for query in ('abc 发票 invoice', 'CAFÉ', '🐈!'):
        assert len(repo.search_messages(query)['items']) == 1
    assert repo.search_messages("%' OR 1=1--")['items'] == []


def test_duplicate_names_group_members_and_source_coverage_stay_explicit(tmp_path):
    cfg, repo = repo_case(tmp_path)
    contacts = repo.list_contacts('Same name')['items']
    assert len(contacts) == 2
    assert contacts[0]['contact_id'] != contacts[1]['contact_id']
    assert all('source_username' not in c for c in contacts)
    group = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    result = repo.get_group(group['conversation_id'])
    assert result['group']['membership_status'] == 'recorded'
    assert len(result['items']) == 2
    direct = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'direct')
    timeline = repo.get_messages(direct['conversation_id'])
    assert timeline['items'] == []
    assert timeline['local_history_status'] == 'no_local_message_table'
    assert timeline['source'] == 'live' and 'generation' not in timeline


def test_context_includes_invalid_time_messages_with_bounded_neighbors(tmp_path):
    _, repo = repo_case(tmp_path)
    chat = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    messages = repo.get_messages(chat['conversation_id'])['items']
    assert len(messages) == 3
    invalid = next(m for m in messages if m['utc_timestamp'] is None)
    result = repo.get_context(invalid['message_id'], before=50, after=50)
    assert result['message']['message_id'] == invalid['message_id']
    assert len(result['items']) == 3
    with pytest.raises(BridgeError, match='invalid_context_limit'):
        repo.get_context(invalid['message_id'], before=51, after=50)


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 201}, {'limit': True}, {'start': '2026-01-01'},
                                    {'start': '2026-02-01Z', 'end': '2026-01-01Z'}, {'sender_id': '../escape'}])
def test_invalid_query_bounds_and_filters_are_rejected(tmp_path, options):
    _, repo = repo_case(tmp_path)
    with pytest.raises(BridgeError):
        repo.search_messages('发票', **options)


@pytest.mark.parametrize('query', ['', '   ', 'a' * 257])
def test_empty_or_oversized_search_is_rejected(tmp_path, query):
    _, repo = repo_case(tmp_path)
    with pytest.raises(BridgeError, match='invalid_query'):
        repo.search_messages(query)


def test_unknown_valid_ids_and_missing_text_index_are_distinct(tmp_path):
    from wechat_bridge.adapters.wechat_4_1 import open_adapter
    from wechat_bridge.search.repository import QueryRepository
    cfg, repo = repo_case(tmp_path)
    with pytest.raises(BridgeError, match='message_not_found'):
        repo.get_context('message_' + '0' * 64)
    def without_index(cfg):
        adapter = open_adapter(cfg, build='270102')
        adapter.text_index_status = 'unsupported_schema'
        return adapter
    plain = QueryRepository(cfg, adapter_factory=without_index)
    with pytest.raises(BridgeError, match='text_index_unavailable'):
        plain.search_messages('发票')
    group = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    assert len(plain.get_messages(group['conversation_id'])['items']) == 3


def test_a_failed_source_read_is_an_error_never_an_empty_result(tmp_path):
    from wechat_bridge.search.repository import QueryRepository
    cfg, _ = repo_case(tmp_path)
    def fail(_):
        raise BridgeError('source_unreadable')
    with pytest.raises(BridgeError, match='source_unreadable'):
        QueryRepository(cfg, adapter_factory=fail).search_messages('发票')


def test_live_reads_reflect_edits_deletions_and_delayed_reply_targets(tmp_path):
    cfg, repo = repo_case(tmp_path)
    chat = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    first = repo.get_messages(chat['conversation_id'])['items']
    assert len(first) == 3
    assert any(m['sender_id'] is None and m['parse_status'] == 'unsupported' for m in first)
    assert next(m for m in first if m['reply'])['reply']['resolution_status'] == 'unresolved'
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"UPDATE {TABLE} SET message_content='Edited' WHERE local_id=1; DELETE FROM {TABLE} WHERE local_id=2; "
                   f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) "
                   "VALUES(3,9007199254740993,1,1,1700000000,'Later target',0)")
    rows = repo.get_messages(chat['conversation_id'])['items']
    assert len(rows) == 3
    edited = next(m for m in rows if m['text'] == 'Edited')
    assert edited['message_id'] == next(m for m in first if m['text'] == '发票 invoice')['message_id']
    target = next(m for m in rows if m['text'] == 'Later target')
    assert next(m for m in rows if m['reply'])['reply']['target_message_id'] == target['message_id']
    assert repo.search_messages('Edited')['items'][0]['message_id'] == edited['message_id']
    assert repo.search_messages('发票')['items'] == []


def test_attachment_metadata_filters_and_private_refs_are_separate(tmp_path):
    from tests.support.proof import proof_case
    case = proof_case(tmp_path); cfg = case.config
    repo = repository(cfg)
    files = repo.list_attachments(kind='file', availability='local_unverified')
    assert len(files['items']) == 1
    item = files['items'][0]
    assert item['original_filename'] == 'test.bin'
    assert item['verification_status'] == 'not_checked'
    assert 'relative_path' not in item and 'file_identity' not in item
    ref = repo.attachment_ref(item['attachment_id'])
    assert ref.relative_path == 'msg/file/2023-11/test.bin'
    assert isinstance(ref.file_identity, tuple)
    assert repo.list_attachments(kind='image')['items'][0]['kind'] == 'image'
    assert repo.list_attachments(availability='not_local')['items'] == []


@pytest.mark.parametrize('prefix, original, query', [
    ('heading' + '\n' * 300, '发票', '发票'),
    ('ﬃ' * 300, 'café', 'CAFÉ'),
    ('ß' * 300, 'ＡＢＣ', 'abc'),
    ('á' * 300, 'Invoice', 'invoice'),
    ('각' * 300, 'ｶﾞ', 'ガ'),
    ('ͅ ' * 300, 'Straße', 'STRASSE'),
], ids=['collapsed-whitespace', 'compatibility-expansion', 'casefold-expansion',
        'combining-composition', 'hangul-and-katakana', 'leading-combining'])
def test_normalized_search_excerpt_contains_original_match(tmp_path, prefix, original, query):
    cfg, repo = repo_case(tmp_path)
    text = prefix + ' ' + original + ' tail'
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"UPDATE {TABLE} SET message_content='{text}' WHERE local_id=1")
    hit = repo.search_messages(query)['items'][0]
    assert original in hit['excerpt']
    assert hit['excerpt'] in text
    assert len(hit['excerpt']) <= 240
    assert hit['excerpt_is_partial']


def test_queries_write_no_records_and_a_fresh_process_resolves_saved_ids(tmp_path):
    cfg, repo = repo_case(tmp_path)
    group = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'group')
    message = repo.get_messages(group['conversation_id'])['items'][0]['message_id']
    before = {p.name: p.stat().st_mtime_ns for p in cfg.state_dir.iterdir()}
    fresh = repository(cfg)   # a new process: empty caches
    assert fresh.get_context(message)['message']['message_id'] == message
    fresh.search_messages('发票'); fresh.recent_messages('2000-01-01T00:00:00Z'); fresh.list_contacts()
    after = {p.name: p.stat().st_mtime_ns for p in cfg.state_dir.iterdir()}
    assert after == before   # inventory.json is rewritten only for a new incarnation; nothing else is written


def test_a_new_shard_table_and_conversation_are_seen_without_restarting(tmp_path):
    import hashlib
    cfg, repo = repo_case(tmp_path)
    assert repo.get_messages(next(c for c in repo.list_conversations()['items'] if c['kind'] == 'direct')['conversation_id'])['local_history_status'] == 'no_local_message_table'
    table = 'Msg_' + hashlib.md5(b'fictional-alice').hexdigest()
    with Engine(cfg.account_root / 'db_storage/message/message_1.db') as writer:
        writer.run(f"CREATE TABLE {table}(local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, sort_seq INTEGER, real_sender_id INTEGER, "
                   f"create_time INTEGER, message_content TEXT, compress_content TEXT, packed_info_data BLOB, WCDB_CT_message_content INTEGER); "
                   f"INSERT INTO {table}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(1,500,1,2,1700000500,'new direct text',0)")
    direct = next(c for c in repo.list_conversations()['items'] if c['kind'] == 'direct')
    timeline = repo.get_messages(direct['conversation_id'])
    assert [m['text'] for m in timeline['items']] == ['new direct text'] and timeline['local_history_status'] == 'available_local_scope'
    assert repo.search_messages('new direct')['items'][0]['message_id'] == timeline['items'][0]['message_id']


def test_a_very_long_indexed_message_stays_inside_the_read_limit(tmp_path):
    from wechat_bridge.adapters.wechat_4_1 import TEXT_INDEX_PREFIX
    cfg, repo = repo_case(tmp_path)
    long_text = '发票 ' + '长' * 300000
    rows = ','.join(f"({10 + i},{900 + i},1,1,1700000100,'{long_text}',0)" for i in range(30))
    with Engine(cfg.account_root / 'db_storage/message/message_0.db') as writer:
        writer.run(f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES{rows}")
    assert len(repo.search_messages('发票', limit=50)['items']) == 31
    assert len(long_text) > TEXT_INDEX_PREFIX
