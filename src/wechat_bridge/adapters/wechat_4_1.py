"""Explicit column mappings observed on 4.1.15 builds 270102 and 270135.

Source usernames and membership come from structured records. Unknown formats
retain diagnostics rather than inferred relationships.
"""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import plistlib
import re
import hashlib
import heapq
from dataclasses import replace

from ..core.clock import now_utc
from ..core.config import Config
from ..core.errors import BridgeError
from ..core.validate import require_int_range
from ..source.discovery import discover, MESSAGE_SHARD
from ..source.keys import load_manifest, verify_keys
from ..source.reader import open_source
from .identity import IdentityLookup, load_identity_key
from .models import Contact, Conversation, Membership, SourceRef
from . import moments as moments_module
from .schema import SchemaDescriptor, MSG_TABLE, inspect_schema
from .wire import parse_fields

ADAPTER_VERSION = "wechat-4.1.15-270102-270135-v4"
# 270135 (auto-update, 14 Oct 2026) was accepted on evidence that every required
# table and column is identical to 270102; see docs/compatibility.md.
SUPPORTED_BUILDS = frozenset({"270102", "270135", "270390"})  # local experiment: 270390 (4.2.0) keys verified OK
INFO_PLIST = Path("/Applications/WeChat.app/Contents/Info.plist")
CONTACT = {"id": "INTEGER", "username": "TEXT", "nick_name": "TEXT", "remark": "TEXT"}
ROOM = {"id": "INTEGER", "username": "TEXT", "owner": "TEXT", "ext_buffer": "BLOB"}
MEMBER = {"room_id": "INTEGER", "member_id": "INTEGER"}
MESSAGE = {"local_id": "INTEGER", "server_id": "INTEGER", "local_type": "INTEGER",
           "real_sender_id": "INTEGER", "create_time": "INTEGER", "message_content": "TEXT",
           "WCDB_CT_message_content": "INTEGER"}
RESOURCE_INFO = {"message_id": "INTEGER", "chat_id": "INTEGER", "message_local_id": "INTEGER",
                 "message_svr_id": "INTEGER", "message_local_type": "INTEGER", "message_create_time": "INTEGER", "packed_info": "BLOB"}
RESOURCE_DETAIL = {"resource_id": "INTEGER", "message_id": "INTEGER", "type": "INTEGER",
                   "size": "INTEGER", "data_index": "TEXT", "packed_info": "BLOB", "status": "INTEGER"}
HARDLINK = {"md5": "TEXT", "file_name": "TEXT", "file_size": "INTEGER", "dir1": "INTEGER", "dir2": "INTEGER", "type": "INTEGER"}
# Moments (sns/sns.db) is an optional database: it is read only when its key is verified and every table the reader needs has
# the columns below. Table names with a numeric suffix (SnsTopItem_1, SnsMessage_tmp3) are matched by pattern and must be unique.
SNS_TIMELINE = {"tid": "INTEGER", "user_name": "TEXT", "content": "TEXT"}
SNS_TOP_ITEM = {"tid": "INTEGER", "username": "TEXT", "summary": "TEXT", "create_time": "INTEGER", "is_read": "INTEGER"}
SNS_MESSAGE = {"local_id": "INTEGER", "create_time": "INTEGER", "type": "INTEGER", "feed_id": "INTEGER", "is_unread": "INTEGER",
               "from_username": "TEXT", "from_nickname": "TEXT", "to_username": "TEXT", "content": "TEXT",
               "del_status": "INTEGER", "is_relative_me": "INTEGER"}
SNS_PAGE = 25   # rows per read: 25 x MAX_CONTENT_BYTES (bytes, measured on the BLOB cast) stays inside the reader's 8 MiB result limit
MAX_FEED_TIDS = 200000   # bound on the feed-summary bookkeeping held in memory
KNOWN_CONTACT_COLUMNS = frozenset("id username local_type alias encrypt_username flag delete_flag verify_flag remark remark_quan_pin remark_pin_yin_initial nick_name pin_yin_initial quan_pin big_head_url small_head_url head_img_md5 chat_room_notify is_in_chat_room description extra_buffer chat_room_type".split())
KNOWN_MESSAGE_COLUMNS = frozenset("local_id server_id local_type sort_seq real_sender_id create_time status upload_status download_status server_seq origin_source source message_content compress_content packed_info_data WCDB_CT_message_content WCDB_CT_source".split())
KNOWN_SESSION_COLUMNS = frozenset("username type unread_count unread_first_msg_srv_id unread_first_pat_msg_local_id unread_first_pat_msg_sort_seq is_hidden summary draft status last_timestamp sort_timestamp last_clear_unread_timestamp last_msg_locald_id last_msg_type last_msg_sub_type last_msg_sender last_sender_display_name last_msg_ext_type".split())
KNOWN_HARDLINK_COLUMNS = frozenset("md5_hash md5 type file_name file_size modify_time dir1 dir2 _rowid_ extra_buffer".split())
MIN_TIME = -62135596800
MAX_TIME = 253402300799
TIME_VALID = f"(typeof(create_time)='integer' AND create_time BETWEEN {MIN_TIME} AND {MAX_TIME})"
TIME_SORT = f"(CASE WHEN {TIME_VALID} THEN create_time ELSE 0 END)"
# Total order of messages: (sort, database_id, table, local_id). A message without a valid time sorts first, below every
# valid time; the sentinel is an ordinary integer so SQLite and Python compare it the same way.
NO_TIME = MIN_TIME - 1
SORT_KEY = f"(CASE WHEN {TIME_VALID} THEN create_time ELSE {NO_TIME} END)"
# Source type codes each decoded message type can come from (a superset: decoding decides the final type).
RAW_TYPE_SQL = {"text": "local_type=1", "system": "local_type=10000", "image": "(local_type & 4294967295)=3",
                "file": "(local_type & 4294967295)=49", "reply": "(local_type & 4294967295)=49",
                "voice": "(local_type & 4294967295)=34", "video": "(local_type & 4294967295)=43"}
# Attachment-bearing messages (decoded type image or file) come only from these source types.
ATTACHMENT_TYPE_SQL = "((local_type & 4294967295) IN (3,49))"
# WeChat's own full-text index (message/message_fts.db): only its plain content tables are read; its tokenizer is not
# available outside WeChat, so the FTS5 virtual tables themselves are never queried.
TEXT_INDEX_DB = "message/message_fts.db"
TEXT_INDEX_TABLE = re.compile(r"message_fts_v4_[0-9]+_content")
TEXT_INDEX_COLUMNS = {"id": "INTEGER", **{f"c{i}": "" for i in range(7)}}
# Indexed text read per row, in characters: 200 rows x 8192 characters x 4 bytes stays inside the reader's 8 MiB result
# limit. A match past this prefix of a longer message is not found.
TEXT_INDEX_PREFIX = 8192
MAX_UNREAD = 1_000_000


def session_activity(row, columns):
    """Unread count, last-activity UTC time, its integer sort key and diagnostic codes from one SessionTable row.

    A missing column gives `session_unread_unavailable`; a value that is not a plausible integer
    (a bool, a negative count, a time outside 1..MAX_TIME seconds such as milliseconds) gives
    `session_activity_invalid`. Neither ever guesses a value.
    """
    codes = []
    unread = last = None
    if "unread_count" not in columns or "last_timestamp" not in columns:
        codes.append("session_unread_unavailable")
    if "unread_count" in columns:
        value = row.get("unread_count")
        if type(value) is int and 0 <= value <= MAX_UNREAD:
            unread = value
        elif value is not None:
            codes.append("session_activity_invalid")
    stamp = None
    if "last_timestamp" in columns:
        value = row.get("last_timestamp")
        if type(value) is int and 0 < value <= MAX_TIME:
            last = value
            stamp = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=value)).isoformat()
        elif value is not None and value != 0:
            codes.append("session_activity_invalid")
    return unread, stamp, (last if last is not None else -(1 << 63)), tuple(dict.fromkeys(codes))


def select_adapter(build: str, schemas: tuple[SchemaDescriptor, ...]):
    """Bind an adapter to a supported build after checking every required table, column and rowid alias.

    Unknown extra columns set parse_status to partial and are never dropped silently.
    Raises unsupported_build or unsupported_schema.
    """
    if build not in SUPPORTED_BUILDS:
        raise BridgeError("unsupported_build")
    by_path = {s.relative_path: s for s in schemas}
    expected = {"contact/contact.db": {"contact": CONTACT, "chat_room": ROOM},
                "session/session.db": {"SessionTable": {"username": "TEXT", "type": "INTEGER"}},
                "message/message_resource.db": {"ChatName2Id": {"user_name": "TEXT"}, "MessageResourceInfo": RESOURCE_INFO, "MessageResourceDetail": RESOURCE_DETAIL},
                "hardlink/hardlink.db": {"dir2id": {"username": "TEXT"}, "file_hardlink_info_v4": HARDLINK, "image_hardlink_info_v4": HARDLINK}}
    for path, mapping in expected.items():
        schema = by_path.get(path)
        if schema is None or any(not schema.table(t) or not schema.table(t).has(cols) or not schema.table(t).has_rowid for t, cols in mapping.items()):
            raise BridgeError("unsupported_schema")
    contact_schema = by_path["contact/contact.db"]
    if contact_schema.table("contact").rowid_alias != "id" or contact_schema.table("chat_room").rowid_alias != "id":
        raise BridgeError("unsupported_schema")
    shards = [s for s in schemas if MESSAGE_SHARD.fullmatch(s.relative_path)]
    if not shards:
        raise BridgeError("unsupported_schema")
    for schema in shards:
        if not schema.table("Name2Id") or not schema.table("Name2Id").has({"user_name": "TEXT"}) or not schema.table("Name2Id").has_rowid:
            raise BridgeError("unsupported_schema")
        if any(not t.has(MESSAGE) or t.rowid_alias != "local_id" for t in schema.tables if MSG_TABLE.fullmatch(t.name)):
            raise BridgeError("unsupported_schema")
        if any(t.name.startswith("Msg_") and not MSG_TABLE.fullmatch(t.name) for t in schema.tables):
            raise BridgeError("unsupported_schema")
    adapter = Wechat41Adapter(build, schemas)
    contact = by_path["contact/contact.db"]
    if (not contact.table("chatroom_member") or not contact.table("chatroom_member").has(MEMBER)
            or any(n not in KNOWN_CONTACT_COLUMNS for n, _ in contact.table("contact").columns)):
        adapter.parse_status = "partial"
    known = {"contact/contact.db": {"contact": KNOWN_CONTACT_COLUMNS, "chat_room": frozenset(ROOM), "chatroom_member": frozenset(MEMBER)},
             "session/session.db": {"SessionTable": KNOWN_SESSION_COLUMNS},
             "message/message_resource.db": {"ChatName2Id": frozenset(("user_name", "update_time")),
                 "MessageResourceInfo": frozenset("message_id chat_id sender_id message_local_type message_create_time message_local_id message_svr_id message_origin_source packed_info".split()),
                 "MessageResourceDetail": frozenset("resource_id message_id type size create_time access_time status data_index packed_info".split())},
             "hardlink/hardlink.db": {"dir2id": frozenset(("username",)), "file_hardlink_info_v4": KNOWN_HARDLINK_COLUMNS, "image_hardlink_info_v4": KNOWN_HARDLINK_COLUMNS}}
    for schema in schemas:
        for table in schema.tables:
            allowed = (KNOWN_MESSAGE_COLUMNS if MSG_TABLE.fullmatch(table.name) else frozenset(("user_name", "is_session"))) if MESSAGE_SHARD.fullmatch(schema.relative_path) and (MSG_TABLE.fullmatch(table.name) or table.name == "Name2Id") else known.get(schema.relative_path, {}).get(table.name)
            if allowed is not None and (not table.has_rowid or any(n not in allowed for n, _ in table.columns)):
                adapter.parse_status = "partial"
            if MSG_TABLE.fullmatch(table.name) and "packed_info_data" in dict(table.columns) and dict(table.columns)["packed_info_data"] != "BLOB":
                adapter.parse_status = "partial"
    return adapter


def open_adapter(config: Config, *, build: str | None = None):
    """Discover, verify the saved keys and return a bound adapter; never runs setup or touches WeChat.

    Raises setup_required, key_coverage_incomplete, unsupported_build or source_version_unavailable.
    """
    if build is None:
        try:
            with INFO_PLIST.open("rb") as stream:
                info = plistlib.load(stream)
            if info.get("CFBundleShortVersionString") not in ("4.1.15", "4.2.0"):  # local experiment
                raise BridgeError("unsupported_build")
            build = info.get("CFBundleVersion")
        except (OSError, plistlib.InvalidFileException):
            raise BridgeError("source_version_unavailable") from None
    inventory = discover(config)
    path = config.state_dir / "keys.json"
    if not path.exists():
        raise BridgeError("setup_required")
    coverage = verify_keys(inventory, load_manifest(path), config)
    if not coverage.ready:
        raise BridgeError("key_coverage_incomplete")
    owners = {d.database_id: d.relative_path for d in inventory.databases}
    entries = {e.database_id: e for e in coverage.verified_entries if owners.get(e.database_id) == e.relative_path}
    schemas = []
    for db in inventory.databases:
        if db.required:
            key = entries[db.database_id]
            with open_source(db, key.raw_key, config=config, profile=key.profile) as reader:
                schemas.append(inspect_schema(reader))
    adapter = select_adapter(build, tuple(schemas))
    adapter.config, adapter.inventory, adapter.coverage = config, inventory, coverage
    adapter._keys = entries
    adapter._dbs = {d.relative_path: d for d in inventory.databases if d.required}
    adapter._bind_sns(inventory, coverage, config)
    adapter._bind_text_index(inventory, coverage, config)
    adapter.identities = IdentityLookup(inventory.account_id, load_identity_key(config))
    return adapter


class Wechat41Adapter:
    """Maps WeChat 4.1.15 tables to records. Reads are bounded and read-only; callers use open_adapter."""
    version = ADAPTER_VERSION

    def __init__(self, build, schemas):
        self.build = build
        self.schemas = schemas
        self._schemas = {s.relative_path: s for s in schemas}
        self.parse_status = "ok"
        self.observations = {}
        self._conversations = None
        self._readers = {}

    def close(self):
        """Close every source connection this adapter opened; the adapter can still open new ones afterwards."""
        readers, self._readers = self._readers, {}
        for reader in readers.values():
            reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    sns_status = "not_present"
    _sns = None
    text_index_status = "not_present"
    _text_index = None

    def _bind_text_index(self, inventory, coverage, config):
        """Bind WeChat's message_fts.db as an optional source for search; a problem leaves search unavailable, nothing else."""
        db = next((d for d in inventory.databases if d.relative_path == TEXT_INDEX_DB), None)
        if db is None:
            return
        entry = next((e for e in coverage.verified_entries if e.database_id == db.database_id and e.relative_path == db.relative_path), None)
        if entry is None:
            self.text_index_status = "key_missing"
            return
        try:
            with open_source(db, entry.raw_key, config=config, profile=entry.profile) as reader:
                schema = inspect_schema(reader)
        except Exception:
            self.text_index_status = "failed:text_index_schema_unreadable"
            return
        tables = tuple(sorted(t.name for t in schema.tables if TEXT_INDEX_TABLE.fullmatch(t.name) and t.has_rowid and t.has(TEXT_INDEX_COLUMNS)))
        names = schema.table("name2id")
        if not tables or not names or not names.has({"username": "TEXT"}) or not names.has_rowid:
            self.text_index_status = "unsupported_schema"
            return
        self._text_index = (db, entry, schema, tables)
        self.text_index_status = "available"

    def text_index_names(self):
        """Map WeChat's text-index row IDs (session and sender columns) to source usernames. Needs text_index_status == available."""
        db, entry, schema, _ = self._text_index
        names, cursor = {}, -1
        with open_source(db, entry.raw_key, config=self.config, profile=entry.profile) as reader:
            while True:
                with reader.transaction() as tx:
                    rows = tx.query("SELECT rowid AS id,username FROM " + schema.quote_table("name2id") + " WHERE rowid>? ORDER BY rowid LIMIT 200", (cursor,), max_rows=200)
                    self.observations[db.database_id] = now_utc()
                for row in rows:
                    if isinstance(row["username"], str) and row["username"] and "\0" not in row["username"]:
                        names[row["id"]] = row["username"]
                if len(rows) < 200:
                    return names
                cursor = rows[-1]["id"]

    def text_index_rows(self, *, sessions=None, senders=None, lower=None, upper=None, type_sql=None):
        """Yield rows of WeChat's text index: text (its first TEXT_INDEX_PREFIX characters), local_id, sort_seq, raw type, session,
        sender and create_time.

        Filters are pushed into SQL: `sessions`/`senders` are text-index row IDs (an empty tuple yields nothing), `lower`/`upper`
        bound create_time in seconds, `type_sql` is one RAW_TYPE_SQL predicate written for the local_type column.
        """
        if sessions == () or senders == ():
            return
        db, entry, schema, tables = self._text_index
        where, params = ["typeof(c0)='text'", "typeof(c1)='integer'", "typeof(c4)='integer'"], []
        for column, values in (("c4", sessions), ("c5", senders)):
            if values is not None:
                where.append(f"{column} IN (" + ",".join("?" for _ in values) + ")"); params += list(values)
        if lower is not None:
            where.append("typeof(c6)='integer' AND c6>=?"); params.append(lower)
        if upper is not None:
            where.append("typeof(c6)='integer' AND c6<=?"); params.append(upper)
        if type_sql is not None:
            where.append("(" + type_sql.replace("local_type", "c3") + ")")
        with open_source(db, entry.raw_key, config=self.config, profile=entry.profile) as reader:
            for table in tables:
                cursor = -1
                while True:
                    with reader.transaction() as tx:
                        rows = tx.query(f"SELECT id,substr(c0,1,{TEXT_INDEX_PREFIX}) AS text,c1 AS local_id,c2 AS sort_seq,c3 AS local_type,c4 AS session,c5 AS sender,c6 AS create_time FROM "
                                        + schema.quote_table(table) + " WHERE id>? AND " + " AND ".join(where) + " ORDER BY id LIMIT 200",
                                        (cursor, *params), max_rows=200)
                        self.observations[db.database_id] = now_utc()
                    yield from rows
                    if len(rows) < 200:
                        break
                    cursor = rows[-1]["id"]

    def _bind_sns(self, inventory, coverage, config):
        """Bind sns/sns.db as an optional source; any problem leaves it unavailable and never affects the message tables."""
        db = next((d for d in inventory.databases if d.relative_path == "sns/sns.db"), None)
        if db is None:
            return
        entry = next((e for e in coverage.verified_entries if e.database_id == db.database_id and e.relative_path == db.relative_path), None)
        if entry is None:
            self.sns_status = "key_missing"
            return
        try:
            with open_source(db, entry.raw_key, config=config, profile=entry.profile) as reader:
                schema = inspect_schema(reader)
        except Exception:
            self.sns_status = "failed:sns_schema_unreadable"
            return
        tables = {}
        for key, pattern, columns in (("timeline", r"SnsTimeLine", SNS_TIMELINE), ("top", r"SnsTopItem_\d+", SNS_TOP_ITEM), ("messages", r"SnsMessage_tmp\d+", SNS_MESSAGE)):
            found = [t for t in schema.tables if re.fullmatch(pattern, t.name) and t.has_rowid and t.has(columns)]
            if len(found) != 1:
                self.sns_status = "unsupported_schema"
                return
            tables[key] = found[0].name
        self._sns = (db, entry, schema, tables)
        self.sns_status = "available"

    def _sns_rows(self, table_key, columns, *, page=SNS_PAGE, checkpoint=None):
        """Keyset-paged rows of one sns table with an explicit column list (never *), one short transaction per page.

        `checkpoint` (cancellation) runs before every page.
        """
        db, entry, schema, tables = self._sns
        quoted = schema.quote_table(tables[table_key])
        cursor = None
        with open_source(db, entry.raw_key, config=self.config, profile=entry.profile) as reader:
            while True:
                if checkpoint:
                    checkpoint()
                with reader.transaction() as tx:
                    where = "WHERE rowid>?" if cursor is not None else ""
                    rows = tx.query(f"SELECT rowid AS bridge_rowid,{columns} FROM {quoted} {where} ORDER BY rowid LIMIT {int(page)}",
                                    (cursor,) if cursor is not None else (), max_rows=page)
                    self.observations[db.database_id] = now_utc()
                if not rows:
                    return
                for row in rows:
                    yield row
                cursor = rows[-1]["bridge_rowid"]

    def moments(self, checkpoint=None):
        """Yield Moment records: full posts from SnsTimeLine, then feed-summary-only posts. Needs sns_status == available.

        The feed table can hold several rows per post; its read flag is used only when all of them agree. Feed bookkeeping is
        bounded (MAX_FEED_TIDS) and summaries are kept only for posts that have no body.
        """
        limit = moments_module.MAX_CONTENT_BYTES
        with_body = set()
        for row in self._sns_rows("timeline", "tid", page=100, checkpoint=checkpoint):
            if type(row["tid"]) is int:
                with_body.add(row["tid"])
        flags, candidates, times = {}, {}, {}
        for row in self._sns_rows("top", f"tid,username,substr(summary,1,{moments_module.MAX_SUMMARY + 1}) AS summary,create_time,is_read", page=100, checkpoint=checkpoint):
            tid = row["tid"]
            if type(tid) is not int:
                continue
            if tid not in flags and len(flags) >= MAX_FEED_TIDS:
                raise BridgeError("moments_feed_limit")
            flags.setdefault(tid, set()).add(row["is_read"] if type(row["is_read"]) is int else None)
            if type(row["create_time"]) is int and row["create_time"] > times.get(tid, row["create_time"] - 1):
                times[tid] = row["create_time"]
            if tid not in with_body and (tid not in candidates or (type(row["create_time"]) is int and row["create_time"] > (candidates[tid][2] or 0))):
                candidates[tid] = (row["username"], row["summary"], row["create_time"] if type(row["create_time"]) is int else None)
        def flag(tid):
            values = flags.get(tid, set())
            return next(iter(values)) if len(values) == 1 else None
        seen = set()
        # length is measured in bytes on the BLOB cast: SQLite's length() on TEXT counts characters, and a multibyte page
        # could otherwise exceed the reader's result limit and drop every Moment.
        body = f"CASE WHEN length(CAST(content AS BLOB))<={limit} THEN content END"
        for row in self._sns_rows("timeline", f"tid,user_name,{body} AS content", checkpoint=checkpoint):
            tid = row["tid"]
            if type(tid) is not int or tid in seen:
                continue
            seen.add(tid)
            yield moments_module.parse_moment(tid, row["user_name"], row["content"] if isinstance(row["content"], str) else None,
                                              self.identities, read_flag=flag(tid), fallback_time=times.get(tid))
        for tid in sorted(candidates):
            username, summary, created = candidates[tid]
            yield moments_module.summary_moment(tid, username, summary, created, self.identities, read_flag=flag(tid))

    def moment_notifications(self, checkpoint=None):
        """Yield MomentNotification records from the sns message table. Needs sns_status == available."""
        db, _, _, tables = self._sns
        columns = ("local_id,create_time,type,feed_id,is_unread,from_username,substr(from_nickname,1,256) AS from_nickname,"
                   f"to_username,substr(content,1,{moments_module.MAX_COMMENT_TEXT + 1}) AS content,del_status,is_relative_me")
        for row in self._sns_rows("messages", columns, checkpoint=checkpoint):
            if type(row["local_id"]) is int:
                yield moments_module.notification(row, self.identities, db.database_id, tables["messages"])

    @contextmanager
    def _open(self, relative):
        """A read-only connection to one required database, opened once and reused until close().

        An adapter serves one query (or one proof run), so its connections live only that long; opening a SQLCipher
        connection costs more than most of the reads made through it.
        """
        if not hasattr(self, "config"):
            raise BridgeError("adapter_not_bound")
        reader = self._readers.get(relative)
        if reader is None:
            db = self._dbs[relative]; entry = self._keys[db.database_id]
            reader = self._readers[relative] = open_source(db, entry.raw_key, config=self.config, profile=entry.profile)
        yield reader

    def _rows(self, relative, table, *, where="", params=()):
        quoted = self._schemas[relative].quote_table(table)
        # Conditions are internal literals; source values are always bound.
        # Keyset paging on rowid in 100-row transactions keeps each read short and bounded.
        cursor = None
        with self._open(relative) as reader:
            while True:
                with reader.transaction() as tx:
                    condition = "rowid>?" if cursor is not None else "1"
                    values = (cursor, *params) if cursor is not None else params
                    rows = tx.query(f"SELECT rowid AS bridge_rowid,* FROM {quoted} WHERE {condition} " + where + " ORDER BY rowid LIMIT 100", values, max_rows=100)
                    self.observations[reader.db.database_id] = now_utc()
                if not rows:
                    break
                for row in rows:
                    yield row
                cursor = rows[-1]["bridge_rowid"]

    def _source(self, relative, table, row, server_id=None):
        db = self._dbs[relative]
        return SourceRef(db.account_id, db.database_id, table, str(row["bridge_rowid"]), server_id,
                         self.observations[db.database_id])

    @staticmethod
    def _username(value):
        if not isinstance(value, str) or not value or "\0" in value:
            raise BridgeError("source_identity_invalid")
        return value

    def contacts(self):
        """Yield contacts in rowid order; raises source_identity_invalid for a malformed username."""
        for row in self._rows("contact/contact.db", "contact"):
            username = self._username(row["username"])
            nickname = row["nick_name"] or None; remark = row["remark"] or None
            yield Contact(self.identities.contact_id(username), username, nickname, remark,
                          remark or nickname, self._source("contact/contact.db", "contact", row),
                          self.parse_status, ("schema_partial",) if self.parse_status == "partial" else ())

    def conversations(self):
        """Yield each session or group once; membership is recorded only when the source table supports it."""
        contacts = {c.source_username: c for c in self.contacts()}
        groups = {self._username(r["username"]): r for r in self._rows("contact/contact.db", "chat_room")}
        member = self._schemas["contact/contact.db"].table("chatroom_member")
        supported = bool(member and member.has(MEMBER) and member.has_rowid)
        seen = set()
        session_columns = frozenset(name for name, _ in self._schemas["session/session.db"].table("SessionTable").columns)
        for relative, table in (("session/session.db", "SessionTable"), ("contact/contact.db", "chat_room")):
            for row in self._rows(relative, table):
                username = self._username(row["username"])
                if username in seen:
                    continue
                seen.add(username)
                group = username.endswith("@chatroom")
                contact = contacts.get(username)
                membership = "recorded" if group and username in groups and supported else "unsupported"
                activity = session_activity(row, session_columns) if table == "SessionTable" else (None, None, -(1 << 63), ())
                yield Conversation(self.identities.conversation_id(username), "group" if group else "direct", username,
                                   contact.display_name if contact else None,
                                   (self._source(relative, table, row),), membership, self.parse_status, activity[3],
                                   activity[0], activity[1], activity[2])

    def conversation(self, conversation_id):
        """Return the single matching conversation; zero or several matches raise conversation_not_found."""
        # An adapter serves one refresh or proof run; per-message lookups must
        # not rebuild the list (and reopen contact.db) every time.
        if self._conversations is None:
            self._conversations = {}
            for c in self.conversations():
                self._conversations.setdefault(c.conversation_id, []).append(c)
        found = self._conversations.get(conversation_id, [])
        if len(found) != 1:
            raise BridgeError("conversation_not_found")
        return found[0]

    def memberships(self, conversation_id):
        """Yield recorded group members. Unparsed nickname data becomes diagnostic codes, never guessed names."""
        try:
            group = self.conversation(conversation_id)
        except BridgeError:
            raise BridgeError("group_not_found") from None
        if group.kind != "group":
            raise BridgeError("group_not_found")
        if group.membership_status != "recorded":
            raise BridgeError("membership_unsupported")
        rooms = list(self._rows("contact/contact.db", "chat_room", where="AND username=?", params=(group.source_username,)))
        if len(rooms) != 1:
            raise BridgeError("group_identity_conflicting")
        room = rooms[0]; diagnostics = ()
        contacts = {int(c.source.local_id): c for c in self.contacts()}
        member_rows = tuple(self._rows("contact/contact.db", "chatroom_member", where="AND room_id=?", params=(room["id"],)))
        recorded_names = {contacts[r["member_id"]].source_username for r in member_rows if r["member_id"] in contacts}
        nicknames = {}
        if room["ext_buffer"]:
            try:
                for field in parse_fields(room["ext_buffer"]):
                    if field.number in (3, 4) and field.wire_type == 0:
                        continue
                    if field.number != 1 or field.wire_type != 2:
                        diagnostics += ("group_nickname_variant_unsupported",)
                        continue
                    fields = parse_fields(field.value)
                    names = [f.value.decode("utf-8") for f in fields if f.number == 1 and f.wire_type == 2]
                    cards = [f.value.decode("utf-8") for f in fields if f.number == 2 and f.wire_type == 2]
                    if len(names) != 1 or names[0] not in recorded_names:
                        diagnostics += ("group_member_mapping_mismatch",)
                    elif len(cards) == 1 and len(cards[0].encode()) <= 256:
                        if names[0] in nicknames and nicknames[names[0]] != cards[0]:
                            nicknames[names[0]] = None
                            diagnostics += ("group_nickname_conflicting",)
                        else:
                            nicknames[names[0]] = cards[0] or None
                    elif cards:
                        diagnostics += ("group_nickname_variant_unsupported",)
                    if any((f.number, f.wire_type) not in ((1, 2), (2, 2), (3, 0), (4, 2)) for f in fields):
                        diagnostics += ("group_nickname_variant_unsupported",)
            except BridgeError as error:
                diagnostics = (error.code,)
                nicknames = {}
            except UnicodeError:
                diagnostics = ("invalid_text_encoding",)
                nicknames = {}
        diagnostics = tuple(sorted(set(diagnostics)))
        for row in member_rows:
            contact = contacts.get(row["member_id"])
            codes = diagnostics + (() if contact else ("member_contact_missing",))
            yield Membership(conversation_id, contact.contact_id if contact else None,
                             nicknames.get(contact.source_username) if contact else None,
                             self.observations[self._dbs["contact/contact.db"].database_id],
                             parse_status="partial" if codes else "ok", diagnostic_codes=codes)

    def public_dict(self):
        """Redacted adapter summary: version, build and counts."""
        return {"adapter_version": self.version, "build": self.build, "schema_count": len(self.schemas),
                "status": self.parse_status, "message_shard_count": sum(bool(MESSAGE_SHARD.fullmatch(s.relative_path)) for s in self.schemas)}

    def message_tables(self, username):
        """(schema, table) pairs holding one conversation's messages, one per shard that has its table."""
        name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        for schema in self.schemas:
            if MESSAGE_SHARD.fullmatch(schema.relative_path) and schema.table(name):
                yield schema, name

    def _resolve_reply(self, reply, username):
        from .replies import resolve_replies
        if reply.target_server_id is None or reply.resolution_status == "unsupported":
            return reply
        # Candidates are only matched, never trusted: several matches stay ambiguous in resolve_replies.
        server = int(reply.target_server_id)
        if not -(1 << 63) <= server < (1 << 63):
            return reply
        candidates = []
        for schema, table in self.message_tables(username):
            with self._open(schema.relative_path) as reader, reader.transaction() as tx:
                rows = tx.query("SELECT local_id FROM " + schema.quote_table(table) + " WHERE server_id=? ORDER BY local_id LIMIT 2", (server,), max_rows=2)
                self.observations[reader.db.database_id] = now_utc()
                for row in rows:
                    source = SourceRef(reader.db.account_id, reader.db.database_id, table, str(row["local_id"]))
                    candidates.append(self.identities.message_id(source))
        key = (reply.account_id, reply.conversation_id, reply.target_server_id)
        return resolve_replies((reply,), {key: tuple(candidates)})[0]

    def all_message_tables(self):
        """Every (schema, table) message table in every shard."""
        return tuple((schema, table.name) for schema in self.schemas if MESSAGE_SHARD.fullmatch(schema.relative_path)
                     for table in schema.tables if MSG_TABLE.fullmatch(table.name))

    def shard_names(self, schema):
        """{Name2Id rowid: username} of one shard; message senders refer to these rows."""
        names, cursor = {}, -1
        with self._open(schema.relative_path) as reader:
            while True:
                with reader.transaction() as tx:
                    rows = tx.query("SELECT rowid,user_name FROM Name2Id WHERE rowid>? ORDER BY rowid LIMIT 200", (cursor,), max_rows=200)
                    self.observations[reader.db.database_id] = now_utc()
                for row in rows:
                    if isinstance(row["user_name"], str) and row["user_name"] and "\0" not in row["user_name"]:
                        names[row["rowid"]] = row["user_name"]
                if len(rows) < 200:
                    return names
                cursor = rows[-1]["rowid"]

    def _decode_rows(self, tx, schema, table, username, rows, observed):
        from .messages import decode_message
        self.identities.add_conversation(schema.database_id, table, username)
        sender_ids = tuple(sorted({r["real_sender_id"] for r in rows if type(r["real_sender_id"]) is int}))
        for sender_id in sender_ids:
            self.identities.clear_sender(schema.database_id, sender_id)
        for start in range(0, len(sender_ids), 200):
            chunk = sender_ids[start:start + 200]
            for sender in tx.query("SELECT rowid,user_name FROM Name2Id WHERE rowid IN (" + ",".join("?" for _ in chunk) + ")", chunk, max_rows=200):
                value = sender["user_name"]
                if isinstance(value, str) and value and "\0" not in value:
                    self.identities.add_sender(schema.database_id, sender["rowid"], value)
        messages = []
        for row in rows:
            server = str(row["server_id"]) if row["server_id"] else None
            source = SourceRef(self.inventory.account_id, schema.database_id, table, str(row["local_id"]), server, observed)
            messages.append(((row["bridge_sort"], schema.database_id, table, row["local_id"]), decode_message(row | {"timestamp_unit": "seconds"}, source, self.identities)))
        return messages

    def _columns(self, schema, table):
        columns = ("local_id,server_id,local_type,real_sender_id,create_time,CAST(message_content AS BLOB) AS message_content,"
                   "WCDB_CT_message_content," + SORT_KEY + " AS bridge_sort")
        if "packed_info_data" in dict(schema.table(table).columns):
            columns += ",packed_info_data"
        return columns

    def message_keys(self, schema, table, *, after=None, descending=False, lower=None, upper=None,
                     senders=None, type_sql=None, batch_size=200):
        """Yield message keys (sort, database_id, table, local_id) of one table in total order, without decoding.

        `after` is a key from any table; the stream starts strictly past it in the chosen direction. `lower`/`upper` bound
        valid times in seconds, `senders` are Name2Id row IDs (an empty tuple yields nothing) and `type_sql` is a
        RAW_TYPE_SQL or ATTACHMENT_TYPE_SQL predicate; every filter runs in SQL.
        """
        require_int_range(batch_size, 1, 200, "invalid_limit")
        if senders == ():
            return
        where, params = [], []
        if lower is not None:
            where.append(f"{TIME_VALID} AND create_time>=?"); params.append(lower)
        if upper is not None:
            where.append(f"{TIME_VALID} AND create_time<=?"); params.append(upper)
        if senders is not None:
            where.append("real_sender_id IN (" + ",".join("?" for _ in senders) + ")"); params += list(senders)
        if type_sql is not None:
            where.append("(" + type_sql + ")")
        later, op = ("<", "DESC") if descending else (">", "ASC")
        position = None
        if after is not None:
            # Past `after` in the total order: this table's own (database_id, table) settles ties on sort.
            mine, theirs = (schema.database_id, table), (after[1], after[2])
            ahead = (mine < theirs) if descending else (mine > theirs)
            position = (after[0], after[3] if mine == theirs else None, ahead)
        quoted = schema.quote_table(table)
        with self._open(schema.relative_path) as reader:
            while True:
                clauses, values = list(where), list(params)
                if position is not None:
                    sort, local_id, ahead = position
                    if local_id is not None:
                        clauses.append(f"({SORT_KEY}{later}? OR ({SORT_KEY}=? AND local_id{later}?))"); values += [sort, sort, local_id]
                    else:
                        clauses.append(f"{SORT_KEY}{later}{'=' if ahead else ''}?"); values.append(sort)
                with reader.transaction() as tx:
                    rows = tx.query(f"SELECT {SORT_KEY} AS bridge_sort,local_id FROM " + quoted + (" WHERE " + " AND ".join(clauses) if clauses else "")
                                    + f" ORDER BY bridge_sort {op},local_id {op} LIMIT ?", (*values, batch_size), max_rows=batch_size)
                    self.observations[schema.database_id] = now_utc()
                for row in rows:
                    yield (row["bridge_sort"], schema.database_id, table, row["local_id"])
                if len(rows) < batch_size:
                    return
                position = (rows[-1]["bridge_sort"], rows[-1]["local_id"], False)

    def messages_at(self, schema, table, username, local_ids):
        """Decode the given local rows of one message table; returns {local_id: (key, Message)} for rows that exist.

        Rows are read 200 at a time; a batch whose payloads exceed the reader's result limit is split in half and retried, so
        only a single row that is itself too large fails (result_too_large).
        """
        found = {}
        ids = sorted({int(i) for i in local_ids})
        pending = [ids[start:start + 200] for start in range(0, len(ids), 200)]
        with self._open(schema.relative_path) as reader:
            while pending:
                chunk = pending.pop(0)
                try:
                    with reader.transaction() as tx:
                        rows = tx.query("SELECT " + self._columns(schema, table) + " FROM " + schema.quote_table(table)
                                        + " WHERE local_id IN (" + ",".join("?" for _ in chunk) + ")", chunk, max_rows=200)
                        observed = now_utc()
                        self.observations[schema.database_id] = observed
                        decoded = self._decode_rows(tx, schema, table, username, rows, observed) if rows else []
                except BridgeError as error:
                    if error.code != "result_too_large" or len(chunk) == 1:
                        raise
                    half = len(chunk) // 2
                    pending[:0] = [chunk[:half], chunk[half:]]
                    continue
                for key, message in decoded:
                    if message.reply:
                        message = replace(message, reply=self._resolve_reply(message.reply, username))
                    found[key[3]] = (key, message)
        return found

    def local_ids(self, schema, table, *, where=None):
        """Yield the local_ids of one message table in order without decoding; `where` is an internal predicate."""
        cursor = None
        quoted = schema.quote_table(table)
        with self._open(schema.relative_path) as reader:
            while True:
                with reader.transaction() as tx:
                    clauses = ([where] if where else []) + (["local_id>?"] if cursor is not None else [])
                    rows = tx.query("SELECT local_id FROM " + quoted + (" WHERE " + " AND ".join(clauses) if clauses else "")
                                    + " ORDER BY local_id LIMIT 200", (cursor,) if cursor is not None else (), max_rows=200)
                    self.observations[schema.database_id] = now_utc()
                for row in rows:
                    yield row["local_id"]
                if len(rows) < 200:
                    return
                cursor = rows[-1]["local_id"]

    def resource_ids(self):
        """Yield (conversation username, message local_id, resource_id or None) for every message resource row; no paths."""
        schema = self._schemas["message/message_resource.db"]
        cursor = -1
        with self._open(schema.relative_path) as reader:
            while True:
                with reader.transaction() as tx:
                    infos = tx.query("SELECT i.message_id AS id,c.user_name AS username,i.message_local_id AS local_id FROM MessageResourceInfo i "
                                     "JOIN ChatName2Id c ON c.rowid=i.chat_id WHERE i.message_id>? ORDER BY i.message_id LIMIT 200", (cursor,), max_rows=200)
                    resources = {}
                    ids = [r["id"] for r in infos]
                    for start in range(0, len(ids), 50):
                        chunk = ids[start:start + 50]
                        after = -1
                        while True:   # a message can own several detail rows; page them by rowid
                            rows = tx.query("SELECT rowid AS rid,message_id,resource_id FROM MessageResourceDetail WHERE rowid>? AND message_id IN ("
                                            + ",".join("?" for _ in chunk) + ") ORDER BY rowid LIMIT 200", (after, *chunk), max_rows=200)
                            for row in rows:
                                resources.setdefault(row["message_id"], []).append(row["resource_id"])
                            if len(rows) < 200:
                                break
                            after = rows[-1]["rid"]
                    self.observations[reader.db.database_id] = now_utc()
                for info in infos:
                    if isinstance(info["username"], str) and type(info["local_id"]) is int:
                        for resource_id in resources.get(info["id"], [None]):
                            yield info["username"], info["local_id"], resource_id
                if len(infos) < 200:
                    return
                cursor = infos[-1]["id"]

    def attachment_refs(self, message):
        """Attachment references for one message; unmapped ones stay explicit."""
        from ..attachments.mapping import attachment_refs
        return attachment_refs(self, message)
