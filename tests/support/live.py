"""Live-query fixtures: the synthetic account, an adapter factory pinned to build 270102, and a text-index emulation."""
import hashlib
import heapq
import re

from tests.support.encrypted import Engine
from tests.support.samples import QUOTE
from tests.support.schemas import account_case, TABLE
from wechat_bridge.adapters.wechat_4_1 import open_adapter

TEXT_INDEX = 'message/message_fts.db'


def import_case(tmp_path, extra=None, extra_schemas=None):
    """The shared synthetic account: one text message, one undated unknown-type message and one quote reply."""
    changes = {
        'message/message_0.db': f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(1,123,1,1,1700000000,'发票 invoice',0),(2,124,99,4,NULL,'unknown',0)",
        'message/message_1.db': f"INSERT INTO {TABLE}(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content) VALUES(1,125,{(57<<32)|49},1,1700000001,x'{QUOTE.hex()}',0)",
    }
    for relative, sql in (extra or {}).items():
        changes[relative] = changes.get(relative, '') + ';' + sql
    return account_case(tmp_path, changes=changes, extra_schemas=extra_schemas)


def _quote(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def sync_text_index(cfg):
    """Rebuild the fixture text index from the message tables, as WeChat keeps its own index current.

    Text messages index their content; app messages index their title followed by the rest of the payload (WeChat indexes
    a reply's title plus its quote); other types are not indexed. Session and sender columns refer to the index's name2id.
    """
    root = cfg.account_root / 'db_storage'
    shards = sorted(p for p in (root / 'message').glob('message_[0-9]*.db'))
    names, rows = {}, []
    for shard in shards:
        with Engine(shard, readonly=True) as reader:
            senders = {int(r['rowid']): r['user_name'] for r in reader.run('SELECT rowid,user_name FROM Name2Id')}
            tables = [r['name'] for r in reader.run("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for table in tables:
                owner = next((u for u in senders.values() if 'Msg_' + hashlib.md5(u.encode()).hexdigest() == table), None)
                if owner is None:
                    continue
                for row in reader.run(f'SELECT local_id,sort_seq,local_type,real_sender_id,create_time,hex(CAST(message_content AS BLOB)) AS hex,WCDB_CT_message_content AS ct FROM "{table}"'):
                    raw = int(row['local_type']) if row['local_type'] is not None else None
                    text = bytes.fromhex(row['hex'] or '').decode('utf-8', 'replace')
                    if row['ct'] not in (None, '0') or raw is None:
                        continue
                    if raw == 1:
                        indexed = text.split(':\n', 1)[1] if owner.endswith('@chatroom') and ':\n' in text else text
                    elif raw & 0xffffffff == 49:
                        title = re.search(r'<title>(.*?)</title>', text, re.S)
                        indexed = (title.group(1) if title else '') + ' ' + re.sub(r'<[^>]+>', ' ', text)
                    else:
                        continue
                    sender = senders.get(int(row['real_sender_id'])) if row['real_sender_id'] is not None else None
                    rows.append((indexed, row['local_id'], row['sort_seq'], raw, owner, sender, row['create_time']))
                    names.setdefault(owner, len(names) + 1)
                    if sender:
                        names.setdefault(sender, len(names) + 1)
    with Engine(root / TEXT_INDEX) as writer:
        statements = ['DELETE FROM name2id', 'DELETE FROM message_fts_v4_0_content', 'DELETE FROM message_fts_v4_1_content']
        if names:
            statements.append('INSERT INTO name2id(rowid,username) VALUES' + ','.join(f'({rowid},{_quote(u)})' for u, rowid in names.items()))
        for shard in (0, 1):
            values = [f'({_quote(text)},{local_id},{sort_seq or "NULL"},{raw},{names[owner]},{names[sender] if sender else "NULL"},{created or "NULL"})'
                      for i, (text, local_id, sort_seq, raw, owner, sender, created) in enumerate(rows) if i % 2 == shard]
            if values:
                statements.append(f'INSERT INTO message_fts_v4_{shard}_content(c0,c1,c2,c3,c4,c5,c6) VALUES' + ','.join(values))
        writer.run(';'.join(statements))


def factory(cfg):
    """Adapter for build 270102 after bringing the emulated text index up to date."""
    if (cfg.account_root / 'db_storage' / TEXT_INDEX).exists():
        sync_text_index(cfg)
    return open_adapter(cfg, build='270102')


def repository(cfg):
    from wechat_bridge.search.repository import QueryRepository
    return QueryRepository(cfg, adapter_factory=factory)


def repo_case(tmp_path, **kwargs):
    cfg = import_case(tmp_path, **kwargs)
    return cfg, repository(cfg)


def conversation_messages(adapter, conversation_id, *, batch_size=200):
    """One conversation's messages across shards in total order, from the adapter's key stream and row decoder."""
    username = adapter.conversation(conversation_id).source_username
    tables = list(adapter.message_tables(username))
    schemas = {s.database_id: s for s, _ in tables}
    keys = heapq.merge(*(adapter.message_keys(s, t, batch_size=batch_size) for s, t in tables))
    return [adapter.messages_at(schemas[k[1]], k[2], username, [k[3]])[k[3]][1] for k in keys]
