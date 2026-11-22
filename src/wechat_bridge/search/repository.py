"""Small indexed queries; no source decryption, import or migration at query time."""
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import re
import sqlite3
import time

from ..adapters.identity import load_identity_key
from ..archive.importer import archive_status
from ..archive.store import Archive
from ..attachments.models import AttachmentRef
from ..core.config import Config
from ..core.errors import BridgeError
from ..core.limits import DEFAULT_RECORDS, MAX_RECORDS, MAX_CONTEXT_NEIGHBORS, MAX_QUERY_CHARACTERS
from ..core.validate import require_int_range
from .cursor import binding, encode_cursor, decode_cursor
from .tokenize import normalize, match_expression, source_offset

MAX_QUERY_BYTES = 8 * 1024 * 1024
MAX_NAME_CHARACTERS = 256
LIST_COMMENTS, LIST_LIKES = 5, 10   # entries shown per post in list_moments; get_moment returns them all (up to the stored caps)
NO_ACTIVITY = -(1 << 63)
AVAILABILITY_SQL = """CASE
WHEN json_extract(r.data,'$.mapping_provenance')='conflicting' THEN 'identity_mismatch'
WHEN json_extract(r.data,'$.mapping_provenance') NOT IN ('resource_hardlink','resource_layout','explicit_resource') THEN 'unsupported_format'
WHEN json_extract(r.data,'$.relative_path') IS NULL OR json_extract(r.data,'$.declared_local')=0 OR json_extract(r.data,'$.file_identity') IS NULL THEN 'not_local'
ELSE 'local_unverified' END"""


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


def public_record(record):
    """Strip source usernames, paths, file identity, digests and table names before a record leaves the process."""
    result = {k: v for k, v in record.items() if k not in ('source_username', 'relative_path', 'file_identity', 'expected_digest', 'record_id', 'last_sort', 'time_sort')}
    for field in ('source', 'sources'):
        if field in result:
            values = result[field] if field == 'sources' else (result[field],)
            projected = [{k: v for k, v in source.items() if k != 'table'} for source in values]
            result[field] = projected if field == 'sources' else projected[0]
    return result


def attachment_availability(record):
    """Availability derived from the archived mapping alone; mirrors AVAILABILITY_SQL.

    Never reports a verified state: bytes are checked only at resolve time.
    """
    if record['mapping_provenance'] == 'conflicting':
        return 'identity_mismatch'
    if record['mapping_provenance'] not in ('resource_hardlink', 'resource_layout', 'explicit_resource'):
        return 'unsupported_format'
    if not record.get('relative_path') or record.get('declared_local') is False or record.get('file_identity') is None:
        return 'not_local'
    return 'local_unverified'


class QueryRepository:
    """Bounded read-only queries over the active archive generation.

    Needs a search-ready archive (search_not_ready). Every page is pinned to one
    complete generation and every record, page and query has a byte or time bound.
    """
    def __init__(self, config: Config):
        self.config, self.archive = config, Archive(config)
        with self.archive.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 2:
                raise BridgeError('search_not_ready')
        self._key = load_identity_key(config)

    @contextmanager
    def _snapshot(self, *, token=None, bound=None, generation=None):
        # A cursor pins its generation; an evicted one raises cursor_expired instead of silently switching.
        last = None
        if token is not None:
            generation, last = decode_cursor(token, self._key, bound)
        if generation is not None and (type(generation) is not int or not 1 <= generation < 1 << 63):
            raise BridgeError('invalid_generation')
        try:
            with self.archive.connection() as db:
                if generation is None:
                    generation = db.execute('SELECT active_generation FROM archive_state').fetchone()[0]
                row = db.execute('SELECT * FROM generations WHERE generation=? AND state="complete"', (generation,)).fetchone()
                if not row:
                    raise BridgeError('cursor_expired' if token or generation is not None else 'archive_not_ready')
                # 2 s query budget enforced through the SQLite progress handler.
                deadline = time.monotonic() + 2
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                yield db, row, last
        except sqlite3.Error:
            raise BridgeError('archive_query_failed') from None

    @staticmethod
    def _record(db, generation, kind, record_id):
        row = db.execute(f'SELECT data FROM {kind} WHERE generation=? AND record_id=?', (generation, record_id)).fetchone()
        if row is None:
            raise BridgeError(kind.rstrip('s') + '_not_found')
        if len(row[0].encode()) > MAX_QUERY_BYTES:
            raise BridgeError('content_too_large')
        return json.loads(row[0])

    def _items(self, db, generation, kind, hits, *, query=None):
        items, total = [], 0
        for hit in hits:
            record = self._record(db, generation, kind, hit['record_id'])
            total += len(json.dumps(record, ensure_ascii=False).encode())
            if total > MAX_QUERY_BYTES:
                raise BridgeError('content_too_large')
            item = public_record(record)
            if query is not None:
                text = item.pop('text') or ''
                offset = normalize(text).find(query)
                begin = max(0, source_offset(text, offset) - 80)
                item['excerpt'] = text[begin:begin + 240]
                item['excerpt_is_partial'] = len(text) > 240
            if kind == 'attachments':
                item['availability'] = attachment_availability(record)
                item['verification_status'] = 'not_checked'
            items.append(item)
        if kind == 'messages':
            total = self._add_names(db, generation, items, total)
        elif kind in ('moments', 'moment_notifications'):
            self._add_moment_names(db, generation, items)
        return items

    @staticmethod
    def _add_names(db, generation, items, total):
        """Add sender_display_name and conversation_display_name from the same generation.

        One batched lookup per table and page; each name is cut to MAX_NAME_CHARACTERS in the query itself (so a huge stored name is never read) and counted
        in the page's byte total, so names cannot push a page past MAX_QUERY_BYTES.
        """
        for kind, id_field, name_field in (('contacts', 'sender_id', 'sender_display_name'),
                                           ('conversations', 'conversation_id', 'conversation_display_name')):
            ids = sorted({item[id_field] for item in items if item.get(id_field)})
            names = {}
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in db.execute(f"SELECT record_id,substr(json_extract(data,'$.display_name'),1,{MAX_NAME_CHARACTERS}) FROM {kind} WHERE generation=? AND record_id IN ("
                                      + ','.join('?' for _ in chunk) + ')', (generation, *chunk)):
                    if isinstance(row[1], str) and row[1]:
                        names[row[0]] = row[1]
            for item in items:
                name = names.get(item.get(id_field))
                item[name_field] = name
                total += len(json.dumps(name, ensure_ascii=False).encode())
                if total > MAX_QUERY_BYTES:
                    raise BridgeError('content_too_large')
        return total

    def _result(self, row, items, *, next_cursor=None):
        metadata = json.loads(row['metadata'])
        status = archive_status(self.config)
        return {'generation': row['generation'], 'snapshot_published_at': row['published_at'],
                'coverage': {k: v for k, v in metadata.get('coverage', {}).items() if k != 'missing_conversation_ids'},
                'freshness': {'stale': status['stale'] or row['generation'] != status['active_generation'],
                              'is_current_generation': row['generation'] == status['active_generation'],
                              'last_refresh_error': status['last_refresh_error'], 'source_error': status['source_error']},
                'items': items, 'next_cursor': next_cursor}

    def _paginate(self, hits, limit, generation, bound, key_of):
        # Over-fetch by one, slice to the page, and mint a next-page cursor from
        # the last kept row. key_of builds the per-path cursor key from that row;
        # it must pass the row's values through unchanged (no type coercion), or
        # SQLite affinity comparisons in the `after` clause would silently skip.
        selected = hits[:limit]
        token = encode_cursor(self._key, generation, bound, key_of(selected[-1])) if len(hits) > limit else None
        return selected, token

    def _list(self, kind: str, *, query: str = '', limit: int = DEFAULT_RECORDS, cursor: str | None = None, filters: dict | None = None) -> dict:
        valid_limit(limit); query = query_text(query)
        filters = dict(filters or {}) | {'query': query}
        bound = binding(kind, filters, limit)
        with self._snapshot(token=cursor, bound=bound) as (db, row, last):
            generation = row['generation']
            where, params = ['r.generation=?'], [generation]
            if query:
                normalized, expression = match_expression(query, generation, kind)
                where += ['r.record_id IN (SELECT record_id FROM record_search WHERE tokens MATCH ? AND instr(normalized,?)>0)']
                params += [expression, normalized]
            recent = kind == 'conversations' and filters.get('sort') == 'recent'
            # Recent-first pages on a non-null integer key, so the boundary to conversations without
            # activity needs no NULL comparison; the default order and its cursors are unchanged.
            key_sql = f"COALESCE(json_extract(r.data,'$.last_sort'),{NO_ACTIVITY})"
            if last and recent:
                where += [f'({key_sql}<? OR ({key_sql}=? AND r.record_id>?))']; params += [last[0], last[0], last[1]]
            elif last:
                where += ['r.record_id>?']; params += [last[0]]
            for key, value in filters.items():
                if value is None or key == 'query':
                    continue
                if key == 'unread_only' and kind == 'conversations':
                    where += ["COALESCE(json_extract(r.data,'$.unread_count'),0)>0"]
                elif key == 'sort':
                    pass
                elif key == 'kind' and kind == 'conversations':
                    where += ["json_extract(r.data,'$.kind')=?"]; params += [value]
                elif key == 'availability':
                    where += [AVAILABILITY_SQL + '=?']; params += [value]
                elif key in ('conversation_id', 'message_id', 'kind', 'representation'):
                    where += [f'r.{key}=?']; params += [value]
            if recent:
                hits = db.execute(f'SELECT r.record_id,{key_sql} AS sort_key FROM {kind} r WHERE ' + ' AND '.join(where) + ' ORDER BY sort_key DESC,r.record_id LIMIT ?', (*params, limit + 1)).fetchall()
                selected, next_token = self._paginate(hits, limit, generation, bound, lambda last: [last['sort_key'], last['record_id']])
            else:
                hits = db.execute(f'SELECT r.record_id FROM {kind} r WHERE ' + ' AND '.join(where) + ' ORDER BY r.record_id LIMIT ?', (*params, limit + 1)).fetchall()
                selected, next_token = self._paginate(hits, limit, generation, bound, lambda last: [last['record_id']])
            result = self._result(row, self._items(db, generation, kind, selected), next_cursor=next_token)
            return result

    def list_contacts(self, query: str = '', *, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Contacts by name substring, paginated by record ID; raises invalid_query, invalid_limit or invalid_cursor."""
        return self._list('contacts', query=query, limit=limit, cursor=cursor)

    def list_conversations(self, query: str = '', *, kind: str | None = None, unread_only: bool = False, sort: str = 'name',
                           limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Conversations by name substring, optionally filtered by direct or group or to those with unread messages.

        sort is 'name' (record order, the default) or 'recent' (last activity, newest first). Archives
        imported before activity was recorded have no unread or recency data until refreshed (see freshness.stale).
        Raises invalid_filter.
        """
        if kind not in (None, 'direct', 'group') or type(unread_only) is not bool or sort not in ('name', 'recent'):
            raise BridgeError('invalid_filter')
        filters = {'kind': kind}
        # Only non-default options enter the cursor binding, so default-order cursors stay valid.
        if unread_only:
            filters['unread_only'] = True
        if sort != 'name':
            filters['sort'] = sort
        return self._list('conversations', query=query, limit=limit, cursor=cursor, filters=filters)

    @staticmethod
    def _add_moment_names(db, generation, items):
        """Display names for authors, likers, commenters and notification parties from this generation's contacts.

        Each name is cut in the query; a person with no contact record keeps the source nickname (already bounded in the record).
        """
        ids = set()
        for item in items:
            ids.update(i for i in (item.get('author_id'), item.get('from_id'), item.get('to_id')) if i)
            for entry in (item.get('likes') or []) + (item.get('comments') or []):
                ids.update(i for i in (entry.get('user_id'), entry.get('reply_to_user_id')) if i)
        names, ordered = {}, sorted(ids)
        for start in range(0, len(ordered), 500):
            chunk = ordered[start:start + 500]
            for row in db.execute(f"SELECT record_id,substr(json_extract(data,'$.display_name'),1,{MAX_NAME_CHARACTERS}) FROM contacts WHERE generation=? AND record_id IN ("
                                  + ','.join('?' for _ in chunk) + ')', (generation, *chunk)):
                if isinstance(row[1], str) and row[1]:
                    names[row[0]] = row[1]
        for item in items:
            for field, source in (('author_display_name', 'author_id'), ('from_display_name', 'from_id'), ('to_display_name', 'to_id')):
                if source in item:
                    item[field] = names.get(item[source]) or (item.get('author_nickname') if field == 'author_display_name' else None)
            for entry in (item.get('likes') or []) + (item.get('comments') or []):
                entry['display_name'] = names.get(entry.get('user_id')) or entry.get('nickname')
                if 'reply_to_user_id' in entry:
                    entry['reply_to_display_name'] = names.get(entry['reply_to_user_id'])

    def _moment_page(self, kind, method, filters, where, params, *, limit, cursor, trim=False):
        """Newest-first page over a Moments table: (time_sort, record_id) keyset, cursor bound to the filters."""
        bound = binding(method, filters, limit)
        with self._snapshot(token=cursor, bound=bound) as (db, row, last):
            generation = row['generation']
            where, params = ['t.generation=?', *where], [generation, *params]
            sql = f'SELECT t.record_id,t.time_sort FROM {kind} t WHERE ' + ' AND '.join(where)
            if last:
                sql += ' AND (t.time_sort,t.record_id)<(?,?)'; params += list(last)
            hits = db.execute(sql + ' ORDER BY t.time_sort DESC,t.record_id DESC LIMIT ?', (*params, limit + 1)).fetchall()
            selected, token = self._paginate(hits, limit, generation, bound, lambda last: [last['time_sort'], last['record_id']])
            items = self._items(db, generation, kind, selected)
            if kind == 'moments' and trim:
                for item in items:   # the full lists come from get_moment; a page of 50 posts must stay inside the wire bound
                    item['comments'], item['likes'] = item['comments'][-LIST_COMMENTS:], item['likes'][:LIST_LIKES]
                    item['comments_truncated'] = item['comments_truncated'] or item['comment_count'] > len(item['comments'])
                    item['likes_truncated'] = item['likes_truncated'] or item['like_count'] > len(item['likes'])
            return self._result(row, items, next_cursor=token)

    def list_moments(self, *, author_id: str | None = None, query: str = '', start: str | None = None, end: str | None = None,
                     limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Moments posts newest first, optionally by author, text substring (NFKC, case-folded, whitespace-collapsed like message search) and time window.

        Text, likes and comments are untrusted data from other people. coverage.moments says whether Moments were imported
        (coverage.moments: status and record counts by parse_status; absent in archives imported before Moments existed); an empty list
        with a status other than ok is not "no posts".
        """
        valid_limit(limit); query = query_text(query)
        if author_id is not None:
            valid_id(author_id, 'contact')
        lower, upper = timestamp(start), timestamp(end)
        if lower is not None and upper is not None and lower > upper:
            raise BridgeError('invalid_time_range')
        where, params = [], []
        if author_id is not None:
            where += ['t.author_id=?']; params += [author_id]
        if query:
            where += ['instr(t.text_normalized,?)>0']; params += [query]   # query_text() already normalized it like the stored column
        for value, operator in ((lower, '>='), (upper, '<=')):
            if value is not None:
                where += [f't.utc_timestamp IS NOT NULL AND t.time_sort{operator}?']; params += [value]
        return self._moment_page('moments', 'moments', {'author_id': author_id, 'query': query, 'start': lower, 'end': upper}, where, params, limit=limit, cursor=cursor, trim=True)

    def get_moment(self, moment_id: str) -> dict:
        """One Moments post with its media summary, venue, likes and comments; raises moment_not_found."""
        valid_id(moment_id, 'moment')
        with self._snapshot() as (db, row, _):
            generation = row['generation']
            self._record(db, generation, 'moments', moment_id)
            result = self._result(row, self._items(db, generation, 'moments', [{'record_id': moment_id}]))
            item = result.pop('items')[0]
            result.pop('next_cursor')
            return result | {'moment': item}

    def list_moment_notifications(self, *, moment_id: str | None = None, start: str | None = None, end: str | None = None,
                                  limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Rows of the account's Moments notification table, newest first. `kind_inferred` (like, comment, unknown) is
        derived from the source type code and is not a source fact; involves_me is the source's is_relative_me flag."""
        valid_limit(limit)
        if moment_id is not None:
            valid_id(moment_id, 'moment')
        lower, upper = timestamp(start), timestamp(end)
        if lower is not None and upper is not None and lower > upper:
            raise BridgeError('invalid_time_range')
        where, params = [], []
        if moment_id is not None:
            where += ['t.moment_id=?']; params += [moment_id]
        for value, operator in ((lower, '>='), (upper, '<=')):
            if value is not None:
                where += [f't.utc_timestamp IS NOT NULL AND t.time_sort{operator}?']; params += [value]
        return self._moment_page('moment_notifications', 'moment_notifications', {'moment_id': moment_id, 'start': lower, 'end': upper}, where, params, limit=limit, cursor=cursor)

    def get_group(self, conversation_id: str, *, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Group record plus locally observed membership page; raises group_not_found or conversation_not_found."""
        valid_id(conversation_id, 'conversation'); valid_limit(limit)
        bound = binding('group', {'conversation_id': conversation_id}, limit)
        with self._snapshot(token=cursor, bound=bound) as (db, row, last):
            generation = row['generation']
            group = self._record(db, generation, 'conversations', conversation_id)
            if group['kind'] != 'group':
                raise BridgeError('group_not_found')
            hits = db.execute('SELECT record_id FROM membership WHERE generation=? AND conversation_id=? AND record_id>? ORDER BY record_id LIMIT ?',
                              (generation, conversation_id, last[0] if last else '', limit + 1)).fetchall()
            selected, token = self._paginate(hits, limit, generation, bound, lambda last: [last['record_id']])
            return self._result(row, self._items(db, generation, 'membership', selected), next_cursor=token) | {'group': public_record(group)}

    def _messages(self, query: str | None, *, conversation_id: str | None = None, sender_id: str | None = None, start: str | None = None, end: str | None = None, type: str | None = None, order: str = 'asc', limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        valid_limit(limit)
        if order not in ('asc', 'desc'):
            raise BridgeError('invalid_filter')
        normalized = query_text(query, required=True) if query is not None else None
        if conversation_id is not None:
            valid_id(conversation_id, 'conversation')
        if sender_id is not None:
            valid_id(sender_id, 'contact')
        if type is not None and type not in ('text', 'image', 'file', 'reply', 'unsupported', 'voice', 'video'):
            raise BridgeError('invalid_filter')
        lower, upper = timestamp(start), timestamp(end)
        if lower is not None and upper is not None and lower > upper:
            raise BridgeError('invalid_time_range')
        filters = {'query': normalized, 'conversation_id': conversation_id, 'sender_id': sender_id, 'start': lower, 'end': upper, 'type': type}
        newest_first = order == 'desc'
        if newest_first:
            filters['order'] = order  # only a non-default order enters the cursor binding
        bound = binding('search' if normalized is not None else 'timeline', filters, limit)
        with self._snapshot(token=cursor, bound=bound) as (db, row, last):
            generation = row['generation']
            if conversation_id is not None:
                self._record(db, generation, 'conversations', conversation_id)
            where, params = ['m.generation=?'], [generation]
            for field in ('conversation_id', 'sender_id', 'type'):
                if filters[field] is not None:
                    where += [f'm.{field}=?']; params += [filters[field]]
            for value, operator in ((lower, '>='), (upper, '<=')):
                if value is not None:
                    where += [f'm.timestamp IS NOT NULL AND m.time_sort{operator}?']; params += [value]
            if normalized is not None:
                _, expression = match_expression(normalized, generation, 'messages')
                query_sql = ('SELECT m.record_id AS record_id,m.time_sort AS time_sort,instr(f.normalized,?) AS position,length(f.normalized) AS length '
                             'FROM record_search f JOIN messages m ON m.generation=f.generation AND m.record_id=f.record_id WHERE tokens MATCH ? AND instr(f.normalized,?)>0 AND ' + ' AND '.join(where))
                args = [normalized, expression, normalized, *params]
                order = 'position ASC,length ASC,time_sort DESC,record_id ASC'
                after = '((position,length)>(?,?) OR ((position,length)=(?,?) AND (time_sort<? OR (time_sort=? AND record_id>?))))'
            else:
                query_sql = 'SELECT m.record_id,m.time_sort FROM messages m WHERE ' + ' AND '.join(where)
                args, order, after = ((params, 'time_sort DESC,record_id DESC', '(time_sort,record_id)<(?,?)') if newest_first
                                      else (params, 'time_sort ASC,record_id ASC', '(time_sort,record_id)>(?,?)'))
            if last:
                query_sql = 'SELECT * FROM (' + query_sql + ') WHERE ' + after
                args += [last[0], last[1], last[0], last[1], last[2], last[2], last[3]] if normalized is not None else last
            hits = db.execute(query_sql + ' ORDER BY ' + order + ' LIMIT ?', (*args, limit + 1)).fetchall()
            selected, token = self._paginate(hits, limit, generation, bound,
                lambda last: ([last['position'], last['length'], last['time_sort'], last['record_id']]
                              if normalized is not None else [last['time_sort'], last['record_id']]))
            result = self._result(row, self._items(db, generation, 'messages', selected, query=normalized), next_cursor=token)
            missing = json.loads(row['metadata']).get('coverage', {}).get('missing_conversation_ids', [])
            result['local_history_status'] = 'no_local_message_table' if conversation_id in missing else 'available_local_scope'
            return result

    def search_messages(self, query: str, **filters) -> dict:
        """Literal substring search with optional conversation, sender, time and type filters; ranked, cursor-paginated."""
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

    def get_context(self, message_id: str, *, before: int = 10, after: int = 10, generation: int | None = None) -> dict:
        """One message plus at most MAX_CONTEXT_NEIGHBORS neighbors in its conversation; raises invalid_context_limit."""
        valid_id(message_id, 'message')
        if any(type(n) is not int or n < 0 for n in (before, after)) or before + after > MAX_CONTEXT_NEIGHBORS:
            raise BridgeError('invalid_context_limit')
        with self._snapshot(generation=generation) as (db, row, _):
            generation = row['generation']
            anchor = self._record(db, generation, 'messages', message_id)
            sort = int(anchor['source_timestamp']) if anchor['utc_timestamp'] else -(1 << 63)
            base = 'SELECT record_id FROM messages WHERE generation=? AND conversation_id=? AND (time_sort,record_id)'
            previous = db.execute(base + '<(?,?) ORDER BY time_sort DESC,record_id DESC LIMIT ?', (generation, anchor['conversation_id'], sort, message_id, before)).fetchall()[::-1]
            following = db.execute(base + '>(?,?) ORDER BY time_sort,record_id LIMIT ?', (generation, anchor['conversation_id'], sort, message_id, after)).fetchall()
            hits = previous + [{'record_id': message_id}] + following
            result = self._result(row, self._items(db, generation, 'messages', hits))
            message = public_record(anchor)
            self._add_names(db, generation, [message], 0)
            return result | {'message': message}

    def list_attachments(self, message_id: str | None = None, *, kind: str | None = None, representation: str | None = None, availability: str | None = None, limit: int = DEFAULT_RECORDS, cursor: str | None = None) -> dict:
        """Attachment metadata with an unverified availability label; raises invalid_filter."""
        if message_id is not None:
            valid_id(message_id, 'message')
        if kind not in (None, 'image', 'file') or representation not in (None, 'original', 'thumbnail'):
            raise BridgeError('invalid_filter')
        if availability not in (None, 'local_unverified', 'not_local', 'unsupported_format', 'identity_mismatch'):
            raise BridgeError('invalid_filter')
        return self._list('attachments', limit=limit, cursor=cursor,
                          filters={'message_id': message_id, 'kind': kind, 'representation': representation, 'availability': availability})

    def attachment_ref(self, attachment_id: str, *, generation: int | None = None) -> tuple:
        """Return (AttachmentRef, generation) for one archived attachment; raises attachment_not_found."""
        valid_id(attachment_id, 'attachment')
        with self._snapshot(generation=generation) as (db, row, _):
            record = self._record(db, row['generation'], 'attachments', attachment_id)
            if record.get('file_identity') is not None:
                record['file_identity'] = tuple(record['file_identity'])
            return AttachmentRef(**record), row['generation']
