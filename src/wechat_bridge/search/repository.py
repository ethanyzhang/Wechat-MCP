"""Live bounded queries over WeChat's databases; a query writes no records to disk."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import heapq
import json
import os
import re
import threading

from ..adapters.identity import load_identity_key, stable_id
from ..adapters.models import SourceRef
from ..adapters.wechat_4_1 import ATTACHMENT_TYPE_SQL, MAX_TIME, MIN_TIME, NO_TIME, RAW_TYPE_SQL, open_adapter
from ..attachments.models import AttachmentRef
from ..core.clock import now_utc
from ..core.config import Config
from ..core.errors import BridgeError
from ..core.limits import DEFAULT_RECORDS, MAX_RECORDS, MAX_CONTEXT_NEIGHBORS, MAX_QUERY_CHARACTERS
from ..core.validate import require_int_range
from .cursor import binding, encode_cursor, decode_cursor
from .tokenize import normalize, source_offset

MAX_QUERY_BYTES = 8 * 1024 * 1024
MAX_NAME_CHARACTERS = 256
LIST_COMMENTS, LIST_LIKES = 5, 10   # entries shown per post in list_moments; get_moment returns them all (up to the parsed caps)
NO_ACTIVITY = -(1 << 63)
MESSAGE_TYPES = ('text', 'image', 'file', 'reply', 'unsupported', 'voice', 'video')
PRIVATE_FIELDS = ('source_username', 'relative_path', 'file_identity', 'expected_digest', 'record_id', 'last_sort', 'time_sort', 'search_text')


def valid_id(value, kind):
    """Return value if it is an opaque `<kind>_<64 hex>` ID; raises invalid_id."""
    if not isinstance(value, str) or not re.fullmatch(kind + r'_[0-9a-f]{64}', value):
        raise BridgeError('invalid_id')
    return value


def query_text(value, *, required=False):
    """Normalize a query of at most 256 characters without NUL; raises invalid_query."""
    if not isinstance(value, str) or len(value) > MAX_QUERY_CHARACTERS or '\0' in value:
        raise BridgeError('invalid_query')
    value = normalize(value)
    if required and not value:
        raise BridgeError('invalid_query')
    return value


def valid_limit(limit):
    """Require an int page size in [1, MAX_RECORDS]; raises invalid_limit."""
    require_int_range(limit, 1, MAX_RECORDS, 'invalid_limit')


def timestamp(value):
    """Parse an ISO-8601 time with an explicit offset to a UTC epoch; raises invalid_time_range."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).timestamp()
    except (ValueError, OverflowError):
        raise BridgeError('invalid_time_range') from None


def time_window(start, end):
    """(lower, upper) UTC epochs; raises invalid_time_range when start is after end."""
    lower, upper = timestamp(start), timestamp(end)
    if lower is not None and upper is not None and lower > upper:
        raise BridgeError('invalid_time_range')
    return lower, upper


def public_record(record):
    """Strip source usernames, paths, file identity, digests and table names before a record leaves the process."""
    result = {k: v for k, v in record.items() if k not in PRIVATE_FIELDS}
    for field in ('source', 'sources'):
        if field in result:
            values = result[field] if field == 'sources' else (result[field],)
            projected = [{k: v for k, v in source.items() if k != 'table'} for source in values]
            result[field] = projected if field == 'sources' else projected[0]
    return result


def attachment_availability(record):
    """Availability derived from the mapping alone; never reports a verified state (bytes are checked only at resolve time)."""
    if record['mapping_provenance'] == 'conflicting':
        return 'identity_mismatch'
    if record['mapping_provenance'] not in ('resource_hardlink', 'resource_layout', 'explicit_resource'):
        return 'unsupported_format'
    if not record.get('relative_path') or record.get('declared_local') is False or record.get('file_identity') is None:
        return 'not_local'
    return 'local_unverified'


def file_signature(adapter, relatives):
    """Stat signature (database ID, inode, size, mtime of the .db and its -wal) of the named databases; reads no content."""
    root = adapter.config.account_root / 'db_storage'
    ids = {d.relative_path: d.database_id for d in adapter.inventory.databases}
    parts = []
    for relative in sorted(relatives):
        entry = [relative, ids.get(relative)]
        for suffix in ('', '-wal'):
            try:
                info = os.stat(root / (relative + suffix), follow_symlinks=False)
                entry.append((info.st_ino, info.st_size, info.st_mtime_ns))
            except FileNotFoundError:
                entry.append(None)
            except OSError:
                raise BridgeError('source_unreadable') from None
        parts.append(tuple(entry))
    return tuple(parts)


def plain(record):
    """The record as JSON would carry it (tuples become lists), so in-process results match the wire."""
    return json.loads(json.dumps(record, ensure_ascii=False))


def table_of(username):
    """WeChat's message table name for one conversation username."""
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


class QueryRepository:
    """Bounded read-only queries answered from WeChat's databases at call time.

    Each call opens a fresh adapter, so new shards, tables and replaced databases are seen at once. In-memory caches map
    opaque IDs back to source rows; each is keyed by the stat signature of exactly the databases it was built from, and an
    ID found through a cache is always re-derived from the decoded row before it is returned. Every record and page has a
    byte bound, and every source read goes through the read-only reader.
    """
    def __init__(self, config: Config, *, adapter_factory=None):
        self.config = config
        self._factory = adapter_factory or open_adapter
        self._key = load_identity_key(config)
        self._lock = threading.Lock()
        self._caches = {}

    @property
    def identity_key(self) -> bytes:
        """Keyed-digest secret shared with cursors; the send log uses it so text digests cannot be guessed offline."""
        return self._key

    # ---- caches -------------------------------------------------------------------------------------------------

    @contextmanager
    def _source(self):
        """A fresh adapter for one call; its source connections are closed when the call ends."""
        adapter = self._factory(self.config)
        try:
            yield adapter
        finally:
            close = getattr(adapter, 'close', None)
            if close is not None:
                close()

    def _cached(self, name, signature, build):
        with self._lock:
            hit = self._caches.get(name)
        if hit is not None and hit[0] == signature:
            return hit[1]
        value = build()
        with self._lock:
            self._caches[name] = (signature, value)
        return value

    def _people(self, adapter):
        """Contacts and conversations (full records) with ID and message-table maps."""
        def build():
            contacts = [plain(asdict(c)) for c in adapter.contacts()]
            conversations = [plain(asdict(c)) for c in adapter.conversations()]
            for record in contacts + conversations:
                record['search_text'] = normalize(' '.join(record.get(f) or '' for f in ('display_name', 'nickname', 'remark')))
            contacts.sort(key=lambda r: r['contact_id'])
            conversations.sort(key=lambda r: r['conversation_id'])
            return {'contacts': contacts, 'conversations': conversations,
                    'contact': {r['contact_id']: r for r in contacts},
                    'conversation': {r['conversation_id']: r for r in conversations},
                    'tables': {table_of(r['source_username']): r['source_username'] for r in conversations}}
        return self._cached('people', file_signature(adapter, ('contact/contact.db', 'session/session.db')), build)

    @staticmethod
    def _message_schemas(adapter):
        seen = {}
        for schema, _ in adapter.all_message_tables():
            seen.setdefault(schema.database_id, schema)
        return list(seen.values())

    def _shard(self, adapter, schema):
        """Per shard: message ID → (table, local_id) and contact ID → Name2Id rows."""
        def build():
            ids = {}
            for table in [t for s, t in adapter.all_message_tables() if s.database_id == schema.database_id]:
                for local_id in adapter.local_ids(schema, table):
                    source = SourceRef(adapter.inventory.account_id, schema.database_id, table, str(local_id))
                    ids[adapter.identities.message_id(source)] = (table, local_id)
            senders = {}
            for rowid, username in adapter.shard_names(schema).items():
                senders.setdefault(adapter.identities.contact_id(username), []).append(rowid)
            return {'ids': ids, 'senders': {k: tuple(v) for k, v in senders.items()}}
        return self._cached('shard:' + schema.relative_path, file_signature(adapter, (schema.relative_path,)), build)

    def _attachments(self, adapter, people):
        """attachment ID → (database_id, table, local_id) for every ID an attachment-type message can produce."""
        relatives = [s.relative_path for s in self._message_schemas(adapter)] + ['message/message_resource.db', 'contact/contact.db', 'session/session.db']
        def build():
            resources = {}
            for username, local_id, resource_id in adapter.resource_ids():
                # str(None) too: an info row without detail rows yields a diagnostic ref whose ID derives from 'None'.
                resources.setdefault((username, local_id), []).append(str(resource_id))
            index = {}
            for schema, table in adapter.all_message_tables():
                username = people['tables'].get(table)
                if username is None:
                    continue
                for local_id in adapter.local_ids(schema, table, where=ATTACHMENT_TYPE_SQL):
                    coords = (schema.database_id, table, local_id)
                    # The same IDs attachments/mapping.py derives: one per resource row, or the missing/conflict markers.
                    for resource in ('missing', 'conflict', *resources.get((username, local_id), ())):
                        aid = stable_id('attachment', (adapter.inventory.account_id, schema.database_id, table, str(local_id), resource), self._key)
                        index[aid] = coords
            return index
        return self._cached('attachments', file_signature(adapter, relatives), build)

    def _moments(self, adapter):
        if adapter.sns_status != 'available':
            raise BridgeError('moments_unavailable')
        def build():
            try:
                return {'moments': sorted((plain(asdict(m)) for m in adapter.moments()), key=lambda r: (r['time_sort'], r['moment_id']), reverse=True),
                        'moment_notifications': sorted((plain(asdict(n)) for n in adapter.moment_notifications()),
                                                       key=lambda r: (r['time_sort'], r['notification_id']), reverse=True)}
            except BridgeError as error:
                if error.code in ('source_busy', 'source_unreadable', 'source_changed'):
                    raise
                raise BridgeError('moments_unavailable') from None
        return self._cached('moments', file_signature(adapter, ('sns/sns.db',)), build)

    # ---- shared result helpers -----------------------------------------------------------------------------------

    @staticmethod
    def _result(items, *, next_cursor=None, **extra):
        return {'source': 'live', 'observed_at': now_utc(), 'items': items, 'next_cursor': next_cursor} | extra

    def _cursor(self, cursor, bound):
        return decode_cursor(cursor, self._key, bound) if cursor is not None else None

    def _token(self, selected, limit, bound, key_of):
        """Next-page cursor from the last row of this page when a further row was found."""
        return encode_cursor(self._key, bound, key_of(selected[limit - 1])) if len(selected) > limit else None

    @staticmethod
    def _budget(items):
        total = 0
        for item in items:
            total += len(json.dumps(item, ensure_ascii=False).encode())
            if total > MAX_QUERY_BYTES:
                raise BridgeError('content_too_large')

    @staticmethod
    def _name(record):
        name = record.get('display_name') if record else None
        return name[:MAX_NAME_CHARACTERS] if isinstance(name, str) and name else None

    def _message_items(self, people, messages, *, query=None):
        items = []
        for message in messages:
            item = plain(public_record(asdict(message)))
            if query is not None:
                text = item.pop('text') or ''
                offset = normalize(text).find(query)
                begin = max(0, source_offset(text, offset) - 80)
                item['excerpt'] = text[begin:begin + 240]
                item['excerpt_is_partial'] = len(text) > 240
            item['sender_display_name'] = self._name(people['contact'].get(item.get('sender_id')))
            item['conversation_display_name'] = self._name(people['conversation'].get(item.get('conversation_id')))
            items.append(item)
        self._budget(items)
        return items

    def _decode(self, adapter, people, keys):
        """Decode message keys; returns {key: Message} for rows that still exist in a table with a known conversation."""
        schemas = {s.database_id: s for s in self._message_schemas(adapter)}
        groups = {}
        for key in keys:
            groups.setdefault((key[1], key[2]), []).append(key[3])
        found = {}
        for (database_id, table), local_ids in groups.items():
            username = people['tables'].get(table)
            if username is None or database_id not in schemas:
                continue
            for key, message in adapter.messages_at(schemas[database_id], table, username, local_ids).values():
                found[key] = message
        return found

    def _take(self, adapter, people, keys, limit, accept):
        """Decode merged keys in batches, keeping accepted messages until limit + 1 survive; returns [(key, Message)]."""
        selected, batch = [], []
        def flush():
            decoded = self._decode(adapter, people, batch)
            for key in batch:
                message = decoded.get(key)
                if message is not None and accept(message) and len(selected) <= limit:
                    selected.append((key, message))
            batch.clear()
        for key in keys:
            batch.append(key)
            if len(batch) >= limit + 1 - len(selected):
                flush()
                if len(selected) > limit:
                    break
        if batch:
            flush()
        return selected

    # ---- contacts, conversations, groups ---------------------------------------------------------------------

    def list_contacts(self, query: str = '', *, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Contacts by name substring, paginated by contact ID; raises invalid_query, invalid_limit or invalid_cursor."""
        valid_limit(limit); query = query_text(query)
        bound = binding('contacts', {'query': query}, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            people = self._people(adapter)
            selected = [r for r in people['contacts'] if (not query or query in r['search_text']) and (not last or r['contact_id'] > last[0])][:limit + 1]
            items = [public_record(r) for r in selected[:limit]]
            self._budget(items)
            return self._result(items, next_cursor=self._token(selected, limit, bound, lambda r: [r['contact_id']]))

    def list_conversations(self, query: str = '', *, kind: str | None = None, unread_only: bool = False, sort: str = 'name',
                           limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Conversations by name substring, optionally direct or group only or with unread messages.

        sort is 'name' (conversation ID order, the default) or 'recent' (last activity, newest first; a conversation can move
        between pages when it gets new messages). Raises invalid_filter.
        """
        if kind not in (None, 'direct', 'group') or type(unread_only) is not bool or sort not in ('name', 'recent'):
            raise BridgeError('invalid_filter')
        valid_limit(limit); query = query_text(query)
        filters = {'kind': kind, 'query': query}
        if unread_only:
            filters['unread_only'] = True
        if sort != 'name':
            filters['sort'] = sort
        bound = binding('conversations', filters, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            people = self._people(adapter)
            records = [r for r in people['conversations'] if (not query or query in r['search_text']) and (kind is None or r['kind'] == kind)
                       and (not unread_only or (r.get('unread_count') or 0) > 0)]
            if sort == 'recent':
                activity = lambda r: r['last_sort'] if type(r.get('last_sort')) is int else NO_ACTIVITY
                records.sort(key=lambda r: (-activity(r), r['conversation_id']))
                if last:
                    records = [r for r in records if (-activity(r), r['conversation_id']) > (-last[0], last[1])]
                key_of = lambda r: [activity(r), r['conversation_id']]
            else:
                if last:
                    records = [r for r in records if r['conversation_id'] > last[0]]
                key_of = lambda r: [r['conversation_id']]
            selected = records[:limit + 1]
            items = [public_record(r) for r in selected[:limit]]
            self._budget(items)
            return self._result(items, next_cursor=self._token(selected, limit, bound, key_of))

    def resolve_recipient(self, conversation_id: str) -> dict:
        """Display name and kind of one conversation, plus how many distinct contacts and conversations share that name.

        Used by the send path to bind a send to one unambiguous recipient: the name is the full display name (never cut);
        name_count counts every distinct contact and conversation with that exact name. Raises invalid_id or conversation_not_found.
        """
        valid_id(conversation_id, 'conversation')
        with self._source() as adapter:
            people = self._people(adapter)
            record = people['conversation'].get(conversation_id)
            if record is None:
                raise BridgeError('conversation_not_found')
            name = record.get('display_name')
            count = 0
            if isinstance(name, str) and name:
                # Entities, not rows: a contact that already has a conversation is the same entity and is not counted again.
                usernames = {r['source_username'] for r in people['conversations']}
                count = sum(1 for r in people['conversations'] if r.get('display_name') == name)
                count += sum(1 for r in people['contacts'] if r.get('display_name') == name and r['source_username'] not in usernames)
            return {'display_name': name if isinstance(name, str) and name else None, 'kind': record.get('kind'), 'name_count': count}

    def get_group(self, conversation_id: str, *, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Group record plus locally observed membership page; raises group_not_found or conversation_not_found."""
        valid_id(conversation_id, 'conversation'); valid_limit(limit)
        bound = binding('group', {'conversation_id': conversation_id}, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            group = self._people(adapter)['conversation'].get(conversation_id)
            if group is None:
                raise BridgeError('conversation_not_found')
            if group['kind'] != 'group':
                raise BridgeError('group_not_found')
            members = [plain(asdict(m)) for m in adapter.memberships(conversation_id)] if group['membership_status'] == 'recorded' else []
            # Keyset on the member's contact ID, so a member who leaves between pages never shifts the rest. Members without a
            # contact come last, in source order (they have no stable key).
            keyed = sorted(([0, m['contact_id'], 0] if m['contact_id'] else [1, '', i], m) for i, m in enumerate(members))
            selected = [(k, m) for k, m in keyed if not last or k > last][:limit + 1]
            items = [public_record(m) for _, m in selected[:limit]]
            self._budget(items)
            return self._result(items, next_cursor=self._token(selected, limit, bound, lambda s: s[0]), group=public_record(group))

    # ---- messages --------------------------------------------------------------------------------------------

    def _messages(self, query: str | None, *, conversation_id: str | None = None, sender_id: str | None = None, start: str | None = None,
                  end: str | None = None, type: str | None = None, order: str = 'asc', limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        valid_limit(limit)
        if order not in ('asc', 'desc'):
            raise BridgeError('invalid_filter')
        normalized = query_text(query, required=True) if query is not None else None
        if conversation_id is not None:
            valid_id(conversation_id, 'conversation')
        if sender_id is not None:
            valid_id(sender_id, 'contact')
        if type is not None and type not in MESSAGE_TYPES:
            raise BridgeError('invalid_filter')
        lower, upper = time_window(start, end)
        filters = {'query': normalized, 'conversation_id': conversation_id, 'sender_id': sender_id, 'start': lower, 'end': upper, 'type': type}
        newest_first = order == 'desc'
        if newest_first:
            filters['order'] = order  # only a non-default order enters the cursor binding
        bound = binding('search' if normalized is not None else 'timeline', filters, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            people = self._people(adapter)
            conversation = None
            if conversation_id is not None:
                conversation = people['conversation'].get(conversation_id)
                if conversation is None:
                    raise BridgeError('conversation_not_found')
            def accept(message):
                if (type is not None and message.type != type) or (sender_id is not None and message.sender_id != sender_id):
                    return False
                if lower is None and upper is None:
                    return True
                # Checked on the decoded row too: a lagging text index can carry an older time than the message now has.
                when = int(message.source_timestamp) if message.utc_timestamp is not None else None
                return when is not None and (lower is None or when >= lower) and (upper is None or when <= upper)
            if conversation is not None:
                tables = list(adapter.message_tables(conversation['source_username']))
                if not tables:
                    return self._result([], local_history_status='no_local_message_table')
            if normalized is not None:
                return self._search(adapter, people, normalized, conversation, sender_id, lower, upper, type, limit, last, bound, accept)
            if conversation is None:
                tables = [(s, t) for s, t in adapter.all_message_tables() if t in people['tables']]
            streams = []
            for schema, table in tables:
                senders = self._shard(adapter, schema)['senders'].get(sender_id, ()) if sender_id is not None else None
                streams.append(adapter.message_keys(schema, table, after=tuple(last) if last else None, descending=newest_first,
                                                    lower=lower, upper=upper, senders=senders, type_sql=RAW_TYPE_SQL.get(type)))
            selected = self._take(adapter, people, heapq.merge(*streams, reverse=newest_first), limit, accept)
            items = self._message_items(people, [m for _, m in selected[:limit]])
            return self._result(items, next_cursor=self._token(selected, limit, bound, lambda s: list(s[0])), local_history_status='available_local_scope')

    def _search(self, adapter, people, normalized, conversation, sender_id, lower, upper, type, limit, last, bound, accept):
        """Candidates from WeChat's text index, ranked on the indexed text and confirmed on the decoded text.

        Rank and cursor key: (position, length, -time, table:local_id). Candidates are decoded in rank order until
        limit + 1 pass; a dropped candidate is dropped again on any later page, so none is ever returned twice.
        """
        if adapter.text_index_status != 'available':
            raise BridgeError('text_index_unavailable')
        names = adapter.text_index_names()
        rows_of = {}
        for rowid, username in names.items():
            rows_of.setdefault(username, []).append(rowid)
        sessions = tuple(rows_of.get(conversation['source_username'], ())) if conversation is not None else None
        senders = None
        if sender_id is not None:
            senders = tuple(r for username, rowids in rows_of.items() if adapter.identities.contact_id(username) == sender_id for r in rowids)
        candidates, seen = [], set()
        for row in adapter.text_index_rows(sessions=sessions, senders=senders, lower=lower, upper=upper, type_sql=RAW_TYPE_SQL.get(type)):
            username = names.get(row['session'])
            if username is None:
                continue
            text = normalize(row['text'])
            position = text.find(normalized)
            if position < 0:
                continue
            created = row['create_time'] if type_of_int(row['create_time']) and MIN_TIME <= row['create_time'] <= MAX_TIME else NO_TIME
            table = table_of(username)
            if (table, row['local_id']) in seen:   # one message held in two content tables is one candidate
                continue
            seen.add((table, row['local_id']))
            candidates.append((position, len(text), -created, table + ':' + format(row['local_id'], '020d'),
                               username, table, row['local_id'], row['local_type'], row['create_time']))
        candidates.sort()
        if last:
            cutoff = (last[0], last[1], -last[2], last[3])
            candidates = [c for c in candidates if c[:4] > cutoff]
        selected, index = [], 0
        while index < len(candidates) and len(selected) <= limit:
            chunk = candidates[index:index + limit + 1 - len(selected)]
            index += len(chunk)
            found = {}
            for username, table in dict.fromkeys((c[4], c[5]) for c in chunk):
                local_ids = [c[6] for c in chunk if c[5] == table]
                for schema, _ in adapter.message_tables(username):
                    for local_id, (_, message) in adapter.messages_at(schema, table, username, local_ids).items():
                        found.setdefault((table, local_id), []).append(message)
            for candidate in chunk:
                # The index names no shard: a local_id in several shards is narrowed by source type and time, and skipped
                # if still ambiguous.
                messages = found.get((candidate[5], candidate[6]), [])
                if len(messages) > 1:
                    messages = [m for m in messages if m.source_type == str(candidate[7]) and m.source_timestamp == str(candidate[8])]
                message = messages[0] if len(messages) == 1 else None
                if (message is not None and message.text and normalized in normalize(message.text) and accept(message)
                        and len(selected) <= limit):
                    selected.append((candidate, message))
        items = self._message_items(people, [m for _, m in selected[:limit]], query=normalized)
        token = self._token(selected, limit, bound, lambda s: [s[0][0], s[0][1], -s[0][2], s[0][3]])
        return self._result(items, next_cursor=token, local_history_status='available_local_scope')

    def search_messages(self, query: str, **filters) -> dict:
        """Literal substring search with optional conversation, sender, time and type filters; ranked, cursor-paginated.

        Candidates come from WeChat's own text index; messages it does not index (system messages) are not searchable.
        Raises text_index_unavailable when that index cannot be read.
        """
        return self._messages(query, **filters)

    def recent_messages(self, start: str, *, end: str | None = None, type: str | None = None, sender_id: str | None = None,
                        order: str = 'desc', limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Messages across all conversations in a time window, newest first by default; start is required and needs a timezone."""
        if start is None:
            raise BridgeError('invalid_time_range')
        return self._messages(None, start=start, end=end, type=type, sender_id=sender_id, order=order, limit=limit, cursor=cursor)

    def get_messages(self, conversation_id: str, **filters) -> dict:
        """Chronological timeline of one conversation with the same filters as search; unsupported messages stay in."""
        return self._messages(None, conversation_id=conversation_id, **filters)

    def _message(self, adapter, people, message_id):
        """((key, Message), username) for one message ID; raises message_not_found."""
        for schema in self._message_schemas(adapter):
            hit = self._shard(adapter, schema)['ids'].get(message_id)
            if hit is None:
                continue
            table, local_id = hit
            username = people['tables'].get(table)
            found = adapter.messages_at(schema, table, username, [local_id]).get(local_id) if username else None
            if found is not None and found[1].message_id == message_id:
                return found, username
        raise BridgeError('message_not_found')

    def get_context(self, message_id: str, *, before: int = 10, after: int = 10) -> dict:
        """One message plus at most MAX_CONTEXT_NEIGHBORS neighbors across its conversation's shards; raises invalid_context_limit."""
        valid_id(message_id, 'message')
        if any(type(n) is not int or n < 0 for n in (before, after)) or before + after > MAX_CONTEXT_NEIGHBORS:
            raise BridgeError('invalid_context_limit')
        with self._source() as adapter:
            people = self._people(adapter)
            (key, anchor), username = self._message(adapter, people, message_id)
            tables = list(adapter.message_tables(username))
            def neighbors(descending, count):
                if count == 0:
                    return []
                merged = heapq.merge(*(adapter.message_keys(s, t, after=key, descending=descending) for s, t in tables), reverse=descending)
                keys = [k for _, k in zip(range(count), merged)]
                decoded = self._decode(adapter, people, keys)
                return [decoded[k] for k in keys if k in decoded]
            messages = neighbors(True, before)[::-1] + [anchor] + neighbors(False, after)
            return self._result(self._message_items(people, messages), message=self._message_items(people, [anchor])[0])

    # ---- attachments -----------------------------------------------------------------------------------------

    def _refs(self, adapter, people, coords):
        """Attachment refs of the message at (database_id, table, local_id), or () when it no longer exists."""
        message = next(iter(self._decode(adapter, people, [(0, *coords)]).values()), None)
        return adapter.attachment_refs(message) if message is not None else ()

    @staticmethod
    def _attachment_item(ref):
        record = asdict(ref)
        item = plain(public_record(record))
        item['availability'] = attachment_availability(record)
        item['verification_status'] = 'not_checked'
        return item

    def list_attachments(self, message_id: str | None = None, *, kind: str | None = None, representation: str | None = None,
                         availability: str | None = None, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Attachment metadata with an unverified availability label, in attachment ID order; raises invalid_filter."""
        if message_id is not None:
            valid_id(message_id, 'message')
        if kind not in (None, 'image', 'file') or representation not in (None, 'original', 'thumbnail'):
            raise BridgeError('invalid_filter')
        if availability not in (None, 'local_unverified', 'not_local', 'unsupported_format', 'identity_mismatch'):
            raise BridgeError('invalid_filter')
        valid_limit(limit)
        bound = binding('attachments', {'message_id': message_id, 'kind': kind, 'representation': representation,
                                        'availability': availability, 'query': ''}, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            people = self._people(adapter)
            def wanted(ref):
                return ((kind is None or ref.kind == kind) and (representation is None or ref.representation == representation)
                        and (availability is None or attachment_availability(asdict(ref)) == availability) and (not last or ref.attachment_id > last[0]))
            if message_id is not None:
                (_, message), _ = self._message(adapter, people, message_id)
                refs = sorted((r for r in adapter.attachment_refs(message) if wanted(r)), key=lambda r: r.attachment_id)[:limit + 1]
            else:
                index = self._attachments(adapter, people)
                refs, seen = [], {}
                for aid in sorted(a for a in index if not last or a > last[0]):
                    coords = index[aid]
                    if coords not in seen:
                        seen[coords] = {r.attachment_id: r for r in self._refs(adapter, people, coords)}
                    ref = seen[coords].get(aid)
                    if ref is not None and wanted(ref):
                        refs.append(ref)
                        if len(refs) > limit:
                            break
            items = [self._attachment_item(r) for r in refs[:limit]]
            self._budget(items)
            return self._result(items, next_cursor=self._token(refs, limit, bound, lambda r: [r.attachment_id]))

    def attachment_ref(self, attachment_id: str) -> AttachmentRef:
        """The current AttachmentRef for one attachment ID, recomputed from its message; raises attachment_not_found."""
        valid_id(attachment_id, 'attachment')
        with self._source() as adapter:
            people = self._people(adapter)
            coords = self._attachments(adapter, people).get(attachment_id)
            ref = next((r for r in self._refs(adapter, people, coords) if r.attachment_id == attachment_id), None) if coords else None
            if ref is None:
                raise BridgeError('attachment_not_found')
            return ref

    # ---- Moments ---------------------------------------------------------------------------------------------

    def _add_moment_names(self, people, items):
        """Display names for authors, likers, commenters and notification parties; a person with no contact keeps the source nickname."""
        def name(contact_id):
            return self._name(people['contact'].get(contact_id))
        for item in items:
            for field, source in (('author_display_name', 'author_id'), ('from_display_name', 'from_id'), ('to_display_name', 'to_id')):
                if source in item:
                    item[field] = name(item[source]) or (item.get('author_nickname') if field == 'author_display_name' else None)
            for entry in (item.get('likes') or []) + (item.get('comments') or []):
                entry['display_name'] = name(entry.get('user_id')) or entry.get('nickname')
                if 'reply_to_user_id' in entry:
                    entry['reply_to_display_name'] = name(entry['reply_to_user_id'])

    @staticmethod
    def _copy(record):
        """A copy of a cached record, so trimming and names never change the cache."""
        return plain(public_record(record))

    def _moment_page(self, kind, id_field, method, filters, keep, *, limit, cursor, trim=False):
        bound = binding(method, filters, limit)
        last = self._cursor(cursor, bound)
        with self._source() as adapter:
            records = self._moments(adapter)[kind]
            people = self._people(adapter)
            selected = []
            for record in records:   # newest first: (time_sort, id) descending
                if last and (record['time_sort'], record[id_field]) >= (last[0], last[1]):
                    continue
                if keep(record):
                    selected.append(record)
                    if len(selected) > limit:
                        break
            items = [self._copy(r) for r in selected[:limit]]
            if kind == 'moments' and trim:
                for item in items:   # the full lists come from get_moment; a page of 50 posts must stay inside the wire bound
                    item['comments'], item['likes'] = item['comments'][-LIST_COMMENTS:], item['likes'][:LIST_LIKES]
                    item['comments_truncated'] = item['comments_truncated'] or item['comment_count'] > len(item['comments'])
                    item['likes_truncated'] = item['likes_truncated'] or item['like_count'] > len(item['likes'])
            self._add_moment_names(people, items)
            self._budget(items)
            return self._result(items, next_cursor=self._token(selected, limit, bound, lambda r: [r['time_sort'], r[id_field]]))

    def list_moments(self, *, author_id: str | None = None, query: str = '', start: str | None = None, end: str | None = None,
                     limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Moments posts newest first, optionally by author, text substring (normalized like message search) and time window.

        Text, likes and comments are untrusted data from other people. Raises moments_unavailable when sns.db cannot be read.
        """
        valid_limit(limit); query = query_text(query)
        if author_id is not None:
            valid_id(author_id, 'contact')
        lower, upper = time_window(start, end)
        def keep(r):
            return ((author_id is None or r['author_id'] == author_id) and (not query or query in normalize(r['text'] or ''))
                    and (lower is None or (r['utc_timestamp'] is not None and r['time_sort'] >= lower))
                    and (upper is None or (r['utc_timestamp'] is not None and r['time_sort'] <= upper)))
        return self._moment_page('moments', 'moment_id', 'moments', {'author_id': author_id, 'query': query, 'start': lower, 'end': upper},
                                 keep, limit=limit, cursor=cursor, trim=True)

    def get_moment(self, moment_id: str) -> dict:
        """One Moments post with its media summary, venue, likes and comments; raises moment_not_found or moments_unavailable."""
        valid_id(moment_id, 'moment')
        with self._source() as adapter:
            record = next((r for r in self._moments(adapter)['moments'] if r['moment_id'] == moment_id), None)
            if record is None:
                raise BridgeError('moment_not_found')
            item = self._copy(record)
            self._add_moment_names(self._people(adapter), [item])
            self._budget([item])
            return {'source': 'live', 'observed_at': now_utc(), 'moment': item}

    def list_moment_notifications(self, *, moment_id: str | None = None, start: str | None = None, end: str | None = None,
                                  limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Rows of the account's Moments notification table, newest first. `kind_inferred` (like, comment, unknown) is
        derived from the source type code and is not a source fact; involves_me is the source's is_relative_me flag."""
        valid_limit(limit)
        if moment_id is not None:
            valid_id(moment_id, 'moment')
        lower, upper = time_window(start, end)
        def keep(r):
            return ((moment_id is None or r['moment_id'] == moment_id)
                    and (lower is None or (r['utc_timestamp'] is not None and r['time_sort'] >= lower))
                    and (upper is None or (r['utc_timestamp'] is not None and r['time_sort'] <= upper)))
        return self._moment_page('moment_notifications', 'notification_id', 'moment_notifications',
                                 {'moment_id': moment_id, 'start': lower, 'end': upper}, keep, limit=limit, cursor=cursor)

    # ---- status ----------------------------------------------------------------------------------------------

    def source_status(self) -> dict:
        """Readiness and coverage of the live source: build, parse status, optional databases and unmapped message tables."""
        with self._source() as adapter:
            people = self._people(adapter)
            tables = adapter.all_message_tables()
            present = {t for _, t in tables}
            return {'ready_for_local_queries': True, 'account_id': adapter.inventory.account_id,
                    'adapter_version': adapter.version, 'adapter_status': adapter.parse_status,
                    'text_index_status': adapter.text_index_status, 'moments_status': adapter.sns_status,
                    'coverage': {'message_tables': len(tables), 'unmapped_message_tables': sum(1 for _, t in tables if t not in people['tables']),
                                 'conversations_without_local_messages': sum(1 for t in people['tables'] if t not in present),
                                 'database_count': len(adapter.inventory.databases), 'account_wide_atomic_snapshot': False,
                                 'auxiliary_storage_unhandled': 'auxiliary_storage_unhandled' in adapter.inventory.issues,
                                 'complete_phone_history': False}}


def type_of_int(value):
    """True for a real int (bool excluded)."""
    return type(value) is int
