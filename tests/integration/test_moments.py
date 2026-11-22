"""Moments: optional sns database import, field whitelist, failure isolation, archive migration and the three read tools."""
import json
import sqlite3

import pytest

from tests.integration.test_importer import import_case, factory, active_rows
from wechat_bridge.core.errors import BridgeError

SECRETS = ('SECRETKEY', 'TOKENXYZ', 'ENCKEY99', '1 Poi Street', '37.7749', '-122.4194', 'MD5HASH', 'https://cdn.example/')
ALICE, BOB = 'fictional-alice', 'fictional-bob'


def post(tid, author, text, *, comments='', likes='', created=1700000000, media=2, poi=True):
    items = ''.join(f'<media><id>{i}</id><type>2</type><sub_type>0</sub_type><enc key="ENCKEY99">1</enc>'
                    f'<url type="1" key="SECRETKEY" token="TOKENXYZ" md5="MD5HASH">https://cdn.example/u{i}</url>'
                    f'<thumb type="1" key="SECRETKEY" token="TOKENXYZ">https://cdn.example/t{i}</thumb>'
                    f'<size width="100" height="200" totalSize="3000"/></media>' for i in range(media))
    place = ('<location latitude="37.7749" longitude="-122.4194" poiName="Test Cafe" poiAddress="1 Poi Street" city="Testville" country="TC"/>'
             if poi else '<location latitude="0" longitude="0"/>')
    return (f'<SnsDataItem><TimelineObject><id>{tid}</id><username>{author}</username><createTime>{created}</createTime>'
            f'<contentDesc>{text}</contentDesc><private>0</private>{place}'
            f'<ContentObject><type>1</type><title>A link title</title><contentUrl>https://cdn.example/page</contentUrl>'
            f'<mediaList>{items}</mediaList></ContentObject></TimelineObject>'
            f'<LocalExtraInfo><like_flag>0</like_flag><comment_user_list>{comments}</comment_user_list>'
            f'<like_user_list>{likes}</like_user_list></LocalExtraInfo></SnsDataItem>')


def comment(user, nick, text, when, deleted=0, reply=None):
    ref = f'<ref_username>{reply}</ref_username>' if reply else ''
    return (f'<user_comment><username>{user}</username><nickname>{nick}</nickname><content>{text}</content>'
            f'<create_time>{when}</create_time><b_deleted>{deleted}</b_deleted>{ref}</user_comment>')


def like(user, nick, when):
    return f'<user_comment><username>{user}</username><nickname>{nick}</nickname><create_time>{when}</create_time><b_deleted>0</b_deleted></user_comment>'


def sql_text(value):
    return value.replace("'", "''")


MAIN = post(1001, ALICE, 'Hello 朋友圈 world', created=1700000500,
            comments=comment(BOB, 'Bobby', 'Nice one', 1700000600) + comment('stranger', 'Strange Nick', 'Removed text', 1700000700, deleted=1)
            + comment(ALICE, 'Alice', 'Thanks', 1700000800, reply=BOB),
            likes=like(BOB, 'Bobby', 1700000550) + like('stranger', 'Strange Nick', 1700000560))
OVERSIZE = post(1002, BOB, 'big ' + 'x' * 270000)
BROKEN = '<SnsDataItem><TimelineObject>'
DOCTYPE = '<!DOCTYPE x [<!ENTITY a "b">]>' + post(1004, BOB, 'entity')

SNS_SCHEMA = f"""
CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT, pack_info_buf TEXT);
CREATE TABLE SnsTopItem_1(tid INTEGER, username TEXT, summary TEXT, create_time INTEGER, last_read_time INTEGER, is_read INTEGER);
CREATE TABLE SnsMessage_tmp3(local_id INTEGER, create_time INTEGER, type INTEGER, feed_id INTEGER, is_unread INTEGER, from_username TEXT,
  from_nickname TEXT, to_username TEXT, to_nickname TEXT, content TEXT, serialized_comment_buf BLOB, serialized_ref_buf BLOB,
  comment_id INTEGER, client_id TEXT, comment64_id INTEGER, comment_flag INTEGER, del_status INTEGER, is_relative_me INTEGER);
INSERT INTO SnsTimeLine VALUES(1001,'{ALICE}','{sql_text(MAIN)}','secret-pack');
INSERT INTO SnsTimeLine VALUES(1002,'{BOB}','{sql_text(OVERSIZE)}',NULL);
INSERT INTO SnsTimeLine VALUES(1003,'{BOB}','{sql_text(BROKEN)}',NULL);
INSERT INTO SnsTimeLine VALUES(1004,'{BOB}','{sql_text(DOCTYPE)}',NULL);
INSERT INTO SnsTopItem_1 VALUES(1001,'{ALICE}','sum',1700000500,1,1),(1001,'{ALICE}','sum',1700000500,1,1);
INSERT INTO SnsTopItem_1 VALUES(1002,'{BOB}','sum',1700000100,1,1),(1002,'{BOB}','sum',1700000100,0,0);
INSERT INTO SnsTopItem_1 VALUES(1005,'{BOB}','Only a summary',1700000900,1,1);
INSERT INTO SnsMessage_tmp3(local_id,create_time,type,feed_id,is_unread,from_username,from_nickname,to_username,content,del_status,is_relative_me,client_id,comment_id)
 VALUES(1,1700000550,1,1001,0,'{BOB}','Bobby','{ALICE}','',0,1,'CLIENT-SECRET-1',11),
       (2,1700000600,2,1001,1,'{BOB}','Bobby','{ALICE}','Nice one',0,1,'CLIENT-SECRET-2',12),
       (3,1700000700,2,1001,0,'stranger','Strange Nick','{ALICE}','Removed text',1,0,'CLIENT-SECRET-3',13),
       (4,1700000800,7,1001,0,'{BOB}','Bobby','{ALICE}','',0,0,'CLIENT-SECRET-4',14);
"""
CASE = {'sns/sns.db': SNS_SCHEMA}


def imported(tmp_path, schema=SNS_SCHEMA):
    from wechat_bridge.archive.importer import refresh_archive
    cfg = import_case(tmp_path, extra_schemas={'sns/sns.db': schema})
    status = refresh_archive(cfg, adapter_factory=factory)
    return cfg, status


def repository(cfg):
    from wechat_bridge.mcp.service import ToolService
    return ToolService(cfg).queries


def test_import_records_posts_with_a_strict_whitelist_and_no_secret_anywhere(tmp_path):
    cfg, status = imported(tmp_path)
    assert status['status'] == 'complete' and status['coverage']['moments']['status'] == 'ok'
    assert status['coverage']['moments']['moments'] == {'ok': 1, 'oversize': 1, 'malformed': 2, 'summary_only': 1}
    assert status['counts']['moments'] == 5 and status['counts']['moment_notifications'] == 4
    stored = json.dumps(active_rows(cfg, 'moments') + active_rows(cfg, 'moment_notifications'), ensure_ascii=False)
    for secret in SECRETS + ('CLIENT-SECRET', 'secret-pack'):
        assert secret not in stored, secret
    raw = (cfg.state_dir / 'archive.sqlite3').read_bytes()
    assert not any(secret.encode() in raw for secret in SECRETS + ('CLIENT-SECRET',))
    main = next(m for m in active_rows(cfg, 'moments') if m['parse_status'] == 'ok')
    assert main['text'] == 'Hello 朋友圈 world' and main['utc_timestamp'] == '2023-11-14T22:21:40+00:00'
    assert main['media'] == [{'type': 2, 'width': 100, 'height': 200, 'size_bytes': 3000}] * 2 and main['media_count'] == 2
    assert main['location'] == {'poi_name': 'Test Cafe', 'city': 'Testville'} and main['link_title'] == 'A link title'
    assert main['like_count'] == 2 and [l['nickname'] for l in main['likes']] == ['Bobby', 'Strange Nick']
    assert [(c['text'], c['deleted']) for c in main['comments']] == [('Nice one', False), (None, True), ('Thanks', False)]
    assert main['comments'][2]['reply_to_user_id'] == main['comments'][0]['user_id'] and main['source_read_flag'] == 1


def test_oversize_malformed_doctype_and_disagreeing_read_flags_become_status_records(tmp_path):
    cfg, _ = imported(tmp_path)
    rows = {m['parse_status'] + str(i): m for i, m in enumerate(active_rows(cfg, 'moments'))}
    statuses = sorted(m['parse_status'] for m in rows.values())
    assert statuses == ['malformed', 'malformed', 'ok', 'oversize', 'summary_only']
    oversize = next(m for m in rows.values() if m['parse_status'] == 'oversize')
    assert oversize['text'] is None and oversize['source_read_flag'] is None   # the two feed rows disagree
    assert {c for m in rows.values() for c in m['diagnostic_codes']} >= {'moment_content_oversize', 'moment_xml_declaration_denied', 'moment_malformed_xml'}
    summary = next(m for m in rows.values() if m['parse_status'] == 'summary_only')
    assert summary['text'] == 'Only a summary' and summary['utc_timestamp'] is not None


def test_notifications_keep_codes_inference_flags_and_drop_deleted_text(tmp_path):
    cfg, _ = imported(tmp_path)
    repo = repository(cfg)
    moment = next(m for m in active_rows(cfg, 'moments') if m['parse_status'] == 'ok')['moment_id']
    result = repo.list_moment_notifications()
    items = result['items']
    assert [i['type_code'] for i in items] == [7, 2, 2, 1] and [i['kind_inferred'] for i in items] == ['unknown', 'comment', 'comment', 'like']
    assert all(i['moment_id'] == moment for i in items)
    by_text = {i['text']: i for i in items}
    assert by_text['Nice one']['involves_me'] is True and by_text['Nice one']['source_unread_flag'] == 1
    assert [i['deleted'] for i in items] == [False, True, False, False] and None in [i['text'] for i in items if i['deleted']]
    assert 'Removed text' not in json.dumps(items, ensure_ascii=False)
    assert all(i['from_display_name'] in ('Same name', None) for i in items)
    assert len(repo.list_moment_notifications(moment_id=moment)['items']) == 4
    assert repo.list_moment_notifications(start='2023-11-14T22:23:00+00:00')['items'][-1]['type_code'] == 2


def test_list_moments_filters_pages_and_names(tmp_path):
    cfg, _ = imported(tmp_path)
    repo = repository(cfg)
    everything = repo.list_moments()
    times = [m['utc_timestamp'] for m in everything['items'] if m['utc_timestamp']]
    assert times == sorted(times, reverse=True) and everything['items'][-1]['utc_timestamp'] is None and everything['coverage']['moments']['status'] == 'ok'
    alice = next(c for c in repo.list_contacts()['items'] if 'Same name' == c['display_name'])
    first = repo.list_moments(limit=2)
    assert len(first['items']) == 2 and first['next_cursor']
    second = repo.list_moments(limit=2, cursor=first['next_cursor'])
    assert {m['moment_id'] for m in first['items']}.isdisjoint(m['moment_id'] for m in second['items'])
    with pytest.raises(BridgeError) as error:
        repo.list_moments(limit=3, cursor=first['next_cursor'])   # cursor is bound to its filters and limit
    assert error.value.code == 'invalid_cursor'
    hello = repo.list_moments(query='HELLO')['items']
    assert len(hello) == 1 and hello[0]['author_display_name'] == 'Same name'
    assert hello[0]['likes'][0]['display_name'] == 'Same name' and hello[0]['likes'][1]['display_name'] == 'Strange Nick'
    assert repo.list_moments(author_id=hello[0]['author_id'])['items'][0]['moment_id'] == hello[0]['moment_id']
    assert repo.list_moments(start='2023-11-14T22:23:21+00:00', end='2030-01-01T00:00:00Z')['items'][0]['text'] == 'Only a summary'
    assert repo.list_moments(query='100%')['items'] == []   # no LIKE wildcards
    for bad in ({'author_id': 'x'}, {'start': '2023-01-01'}, {'limit': 0}, {'query': 'x' * 300}):
        with pytest.raises(BridgeError):
            repo.list_moments(**bad)
    one = repo.get_moment(hello[0]['moment_id'])
    assert one['moment']['comment_count'] == 3 and 'items' not in one
    with pytest.raises(BridgeError) as error:
        repo.get_moment('moment_' + '0' * 64)
    assert error.value.code == 'moment_not_found'
    assert alice['display_name'] == 'Same name'


def test_comments_and_likes_are_capped_to_the_newest_with_counts():
    from wechat_bridge.adapters.moments import MAX_COMMENTS, MAX_LIKES, parse_moment
    from wechat_bridge.adapters.identity import IdentityLookup
    identities = IdentityLookup('account', b'k' * 32)
    comments = ''.join(comment('u' + str(i), 'n', f'c{i}', 1700000000 + i) for i in range(MAX_COMMENTS + 20))
    likes = ''.join(like('u' + str(i), 'n', 1700000000 + i) for i in range(MAX_LIKES + 5))
    moment = parse_moment(1, ALICE, post(1, ALICE, 'many', comments=comments, likes=likes), identities)
    assert moment.comment_count == MAX_COMMENTS + 20 and len(moment.comments) == MAX_COMMENTS and moment.comments_truncated
    assert moment.comments[-1]['text'] == f'c{MAX_COMMENTS + 19}' and moment.comments[0]['text'] == 'c20'
    assert moment.like_count == MAX_LIKES + 5 and len(moment.likes) == MAX_LIKES and moment.likes_truncated


def test_unsupported_sns_schema_never_blocks_messages(tmp_path):
    cfg, status = imported(tmp_path, SNS_SCHEMA.replace('content TEXT, pack_info_buf', 'body TEXT, pack_info_buf'))
    assert status['status'] == 'complete' and status['counts']['messages'] == 3
    assert status['coverage']['moments']['status'] == 'unsupported_schema' and status['counts']['moments'] == 0


def test_a_failure_after_some_moments_discards_the_sns_part_and_still_publishes(tmp_path, monkeypatch):
    from wechat_bridge.archive.importer import refresh_archive
    cfg = import_case(tmp_path, extra_schemas={'sns/sns.db': SNS_SCHEMA})
    def broken(config):
        adapter = factory(config)
        def notifications(checkpoint=None):
            raise BridgeError('query_timeout')
            yield
        adapter.moment_notifications = notifications
        return adapter
    status = refresh_archive(cfg, adapter_factory=broken)
    assert status['status'] == 'complete' and status['counts']['messages'] == 3
    assert status['coverage']['moments']['status'] == 'failed:query_timeout'
    assert status['counts']['moments'] == 0 and status['counts']['moment_notifications'] == 0 and active_rows(cfg, 'moments') == []


def test_no_sns_database_reports_not_present_and_empty_lists(tmp_path):
    from wechat_bridge.archive.importer import refresh_archive
    cfg = import_case(tmp_path)
    status = refresh_archive(cfg, adapter_factory=factory)
    assert status['coverage']['moments']['status'] == 'not_present'
    repo = repository(cfg)
    assert repo.list_moments()['items'] == [] and repo.list_moment_notifications()['items'] == []
    assert repo.list_moments()['coverage']['moments']['status'] == 'not_present'


def test_an_existing_archive_without_moments_tables_gains_them_when_opened(tmp_path):
    from wechat_bridge.archive.importer import refresh_archive
    from wechat_bridge.archive.store import Archive
    cfg = import_case(tmp_path)
    assert refresh_archive(cfg, adapter_factory=factory)['status'] == 'complete'
    path = cfg.state_dir / 'archive.sqlite3'
    db = sqlite3.connect(path)
    db.executescript('DROP TABLE moments; DROP TABLE moment_notifications;')
    db.commit(); db.close()
    version = sqlite3.connect(path).execute('PRAGMA user_version').fetchone()[0]
    assert version in (1, 2)
    Archive(cfg)   # opening migrates in place
    db = sqlite3.connect(path)
    assert {'moments', 'moment_notifications'} <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert db.execute('PRAGMA user_version').fetchone()[0] == version
    assert repository(cfg).list_moments()['items'] == []   # pre-feature generation: queryable, empty, no Moments coverage entry
    assert refresh_archive(cfg, adapter_factory=factory)['status'] == 'complete'


def test_multibyte_content_is_bounded_by_bytes_not_characters(tmp_path):
    from wechat_bridge.adapters.moments import MAX_CONTENT_BYTES
    heavy = post(2001, ALICE, '汉' * 100000)   # about 300 KB of UTF-8 in ~100k characters: under the character bound, over the byte bound
    assert len(heavy) < MAX_CONTENT_BYTES < len(heavy.encode())
    schema = SNS_SCHEMA + f"INSERT INTO SnsTimeLine VALUES(2001,'{ALICE}','{sql_text(heavy)}',NULL);" + ''.join(
        f"INSERT INTO SnsTimeLine VALUES({2100 + i},'{BOB}','{sql_text(post(2100 + i, BOB, '汉' * 80000))}',NULL);" for i in range(30))
    cfg, status = imported(tmp_path, schema)
    assert status['coverage']['moments']['status'] == 'ok'   # many heavy rows never exceed the reader budget
    assert status['coverage']['moments']['moments']['oversize'] == 2   # the original oversize row and the multibyte one


def test_structure_time_truncation_nickname_and_unknown_delete_flags(tmp_path):
    from wechat_bridge.adapters.identity import IdentityLookup
    from wechat_bridge.adapters.moments import MAX_COMMENT_TEXT, MAX_TEXT, notification, parse_moment, summary_moment
    identities = IdentityLookup('account', b'k' * 32)
    assert parse_moment(1, ALICE, '<unrelated/>', identities).parse_status == 'malformed'
    assert parse_moment(1, ALICE, '<SnsDataItem><LocalExtraInfo/></SnsDataItem>', identities).diagnostic_codes == ('moment_unexpected_structure',)
    undated = post(2, ALICE, 'no time').replace('<createTime>1700000000</createTime>', '')
    assert parse_moment(2, ALICE, undated, identities).parse_status == 'partial'
    assert parse_moment(2, ALICE, undated, identities, fallback_time=1700000000).diagnostic_codes == ('moment_time_from_feed',)
    long = parse_moment(3, ALICE, post(3, ALICE, 'y' * (MAX_TEXT + 5), comments=comment(BOB, 'b', 'z' * (MAX_COMMENT_TEXT + 5), 1700000001)), identities)
    assert long.text_truncated and len(long.text) == MAX_TEXT and long.comments[0]['text_truncated'] and not parse_moment(4, ALICE, post(4, ALICE, 'short'), identities).text_truncated
    assert summary_moment(5, BOB, 's' * 2500, 1700000000, identities).text_truncated
    odd = parse_moment(6, ALICE, post(6, ALICE, 'x', comments=comment(BOB, 'b', 'secret words', 1700000001, deleted=2)), identities)
    assert odd.comments[0]['deleted'] is None and odd.comments[0]['text'] is None
    nick = parse_moment(7, 'someone-not-a-contact', post(7, 'someone-not-a-contact', 'x').replace('<like_flag>0</like_flag>', '<nickname>Stranger Nick</nickname>'), identities)
    assert nick.author_nickname == 'Stranger Nick'
    row = {'local_id': 1, 'create_time': 1700000000, 'type': 2, 'feed_id': 7, 'content': 'hi', 'del_status': 2, 'is_relative_me': 1, 'is_unread': 0, 'from_username': BOB}
    flagged = notification(row, identities, 'db1', 'SnsMessage_tmp3')
    assert flagged.deleted is None and flagged.text is None and 'notification_unknown_delete_flag' in flagged.diagnostic_codes
    assert flagged.notification_id != notification(row, identities, 'db2', 'SnsMessage_tmp3').notification_id != notification(row, identities, 'db1', 'SnsMessage_tmp9').notification_id


def test_author_nickname_is_the_fallback_name_and_names_do_not_use_a_contact_when_missing(tmp_path):
    stranger = post(3001, 'unknown-person', 'from a stranger').replace('<like_flag>0</like_flag>', '<nickname>Stranger Nick</nickname>')
    cfg, _ = imported(tmp_path, SNS_SCHEMA + f"INSERT INTO SnsTimeLine VALUES(3001,'unknown-person','{sql_text(stranger)}',NULL);")
    item = repository(cfg).list_moments(query='from a stranger')['items'][0]
    assert item['author_display_name'] == 'Stranger Nick'


def test_search_matches_normalized_text_and_time_bounds_are_exact(tmp_path):
    full = post(4001, ALICE, 'Straße  ＡＢＣ\nx', created=1700001000)
    cfg, _ = imported(tmp_path, SNS_SCHEMA + f"INSERT INTO SnsTimeLine VALUES(4001,'{ALICE}','{sql_text(full)}',NULL);")
    repo = repository(cfg)
    assert [m['moment_id'] for m in repo.list_moments(query='Straße abc x')['items']]   # same normalization as message search
    assert repo.list_moments(start='2023-11-14T22:30:00.5+00:00')['items'] == []   # the record at :40.000 is before :40.5
    assert [m['utc_timestamp'][:19] for m in repo.list_moments(start='2023-11-14T22:30:00+00:00')['items']] == ['2023-11-14T22:30:00']
    assert repo.list_moments(end='2023-11-14T22:29:59.9+00:00', query='abc')['items'] == []


def test_list_moments_trims_embedded_lists_but_get_moment_returns_them_all(tmp_path):
    many = post(5001, ALICE, 'busy', comments=''.join(comment(BOB, 'b', f'c{i}', 1700000000 + i) for i in range(12)),
                likes=''.join(like(BOB, 'b', 1700000000 + i) for i in range(20)), created=1700002000)
    cfg, _ = imported(tmp_path, SNS_SCHEMA + f"INSERT INTO SnsTimeLine VALUES(5001,'{ALICE}','{sql_text(many)}',NULL);")
    repo = repository(cfg)
    listed = repo.list_moments(query='busy')['items'][0]
    assert len(listed['comments']) == 5 and listed['comments'][-1]['text'] == 'c11' and listed['comments_truncated'] and listed['comment_count'] == 12
    assert len(listed['likes']) == 10 and listed['likes_truncated'] and listed['like_count'] == 20
    full = repo.get_moment(listed['moment_id'])['moment']
    assert len(full['comments']) == 12 and len(full['likes']) == 20 and not full['comments_truncated']


def test_cancellation_is_checked_while_the_feed_is_scanned_and_a_failed_discard_is_not_published(tmp_path, monkeypatch):
    import threading
    from wechat_bridge.archive.importer import refresh_archive
    from wechat_bridge.archive.store import Archive
    cfg = import_case(tmp_path, extra_schemas={'sns/sns.db': SNS_SCHEMA})
    cancel = threading.Event(); cancel.set()
    status = refresh_archive(cfg, adapter_factory=factory, cancel_event=cancel)
    assert status['status'] == 'failed' and status['error'] == 'operation_cancelled'
    def broken(config):
        adapter = factory(config)
        def notifications(checkpoint=None):
            raise BridgeError('query_timeout')
            yield
        adapter.moment_notifications = notifications
        return adapter
    def failing(self, generation, kinds):
        raise BridgeError('archive_write_failed')
    monkeypatch.setattr(Archive, 'discard', failing)
    status = refresh_archive(cfg, adapter_factory=broken)
    assert status['status'] == 'failed' and status['error'] == 'moments_discard_failed'
