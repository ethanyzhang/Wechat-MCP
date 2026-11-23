# Design: query WeChat's databases live, remove the archive

Status: implemented on branch `ethan/live-source`, based on `ethan/combined` at `4b76b4a`.

## Problem

Every MCP query today reads `archive.sqlite3`, a private copy that a watcher rebuilds by a full
import of all of WeChat's databases (`archive/watcher.py:26`, "Every refresh is a full import that
rewrites the whole archive"). On the owner's account that copy is 532 MB, a refresh takes about 75 s
of CPU, and a refresh fails with `source_changed` whenever WeChat writes during the import
(`archive/importer.py:163-166`). Results therefore lag the source by minutes, sometimes longer.

The archive was justified by three needs. Measured on the owner's account (WeChat 4.2.0 build
270390, 2026-11-22), none of them requires a copy:

| Need | Finding |
| --- | --- |
| Substring search | WeChat keeps its own FTS5 index in `message/message_fts.db` (tables `message_fts_v4_0..3`). Its `_content` tables hold the plain text of 33,179 messages: all 27,754 text messages and 4,271 replies (the archive has 27,754 and 4,270), plus app-message titles. Text equals the bridge's decoded text for 27,751 text messages; for replies it is the title followed by the quote. The newest indexed message equals the newest archived text message (20:32:48 CST). `MATCH` fails (`no such tokenizer: MMFtsTokenizer`), so the content tables are read as a text store. Reading all 33,181 rows through the reader in 200-row pages took 231 ms, and NFKC/casefold matching in Python 33 ms. A sample of 100 rows mapped back through the index's own `name2id` (session and sender columns) to the same message row (local_id, sort_seq, type, time) in every case. |
| Speed of other reads | Measured live through the existing adapter: `open_adapter` 40 to 75 ms, all 10,764 contacts 270 ms, all 344 conversations 295 ms, every message ID in all 136 tables (local_id only, no decode) 210 ms, all 9,088 Moments parsed 612 ms, all notifications 26 ms. Decoding all 39,360 messages takes 11.2 s, so no query may decode messages outside its page. |
| Consistent reads | SQLite gives each read transaction a consistent snapshot of one database. The archive never claimed an account-wide snapshot either (`account_wide_atomic_snapshot: false`). |

## Decision

Delete the archive, its importer, watcher, generations and private FTS index. Every MCP tool
answers from WeChat's databases at query time through the existing `source/` reader (read-only,
`query_only`, authorizer, deadlines) and the existing adapter decoders. A query writes no records:
`discover` now writes `inventory.json` only when a database incarnation is new or changed (it used
to rewrite it on every call; incarnation IDs are part of every message ID, so they must persist),
and the `adapter-schemas.json` write is removed (nothing reads it).

## Layering

```
core ← source ← adapters ← search ← mcp
                         ← attachments
```

`archive/` is deleted. `search/repository.py` is rewritten as the live query layer and keeps its
name and public methods; `search/cursor.py` and `search/tokenize.py` (minus the archive index
helpers) stay. The repository owns query planning, pagination, redaction and the in-memory caches
below. The adapter owns every SQL statement against WeChat: new methods `message_keys` (keys of
one table in total order, filters pushed into SQL, no decoding), `messages_at` (decode given
rows), `local_ids`, `shard_names`, `all_message_tables`, `text_index_rows` and `text_index_names`
(WeChat's FTS content tables, bound like `sns.db` as an optional database).

A fresh adapter is opened per query (40 to 75 ms), so new shards, new message tables and replaced
databases are seen at once; nothing about the adapter's bindings is cached.

## In-memory caches (no files)

Opaque IDs stay HMAC-SHA256 digests (`adapters/identity.py:12`), unchanged, so every ID a client
or the send log already holds stays valid. Digests are one-way, so resolving an ID needs a map
from digest to source coordinates. These maps live only in the `serve` process and hold full
adapter records (with `source_username`, paths and file identities); `public_record` redacts at
the response boundary, as today.

Each cache is keyed by a per-file stat signature (inode, size, mtime of the `.db` and its `-wal`)
of exactly the databases it was built from, so a message write never rebuilds the contact cache.
`archive/reconcile.py`'s tree-wide `source_fingerprint` is deleted with the archive.

| Cache | Built from | Cost measured | Keyed by |
| --- | --- | --- | --- |
| contacts, conversations, `table → conversation` | `contacts()`, `conversations()` | ~0.6 s together | `contact.db`, `session.db` |
| per shard: `message_id → (table, local_id)` and `contact_id → Name2Id rows` | `local_ids`, `shard_names` | 210 ms for 39,361 rows | that shard |
| attachment index `attachment_id → (shard, table, local_id)` | attachment-type rows (`local_type & 0xffffffff` in 3, 49) and `MessageResourceInfo/Detail`; each message contributes its `missing` and `conflict` IDs and one ID per resource row | part of the 0.97 s cold `list_attachments` | all shards, `message_resource.db`, `contact.db`, `session.db` |
| Moments and notifications | `moments()`, `moment_notifications()` | 0.64 s | `sns.db` |

A rebuild happens lazily inside the query that needs it. Resolving an ID through an index only
finds a candidate: the row is decoded and its ID recomputed before it is returned.

## Result envelope and cursors

Every list result is `{source: "live", observed_at, items, next_cursor}`; single-record results
keep their record field (`message`, `moment`, `group`) next to `source` and `observed_at`.
`generation`, `snapshot_published_at`, `coverage` and `freshness` are removed (coverage moves to
`status`). Message results keep `local_history_status`: `no_local_message_table` when the
conversation has no message table, as a normal empty result.

Cursors stay HMAC-signed and bound to method, filters and limit (`search/cursor.py`) but carry no
generation; a cursor resumes from its keyset position in the live data. Rows written after the
first page may appear in later pages, and conversations sorted by `recent` can move between pages
when they get new messages. `cursor_expired` disappears.

Messages have one total order, `(sort, database_id, table, local_id)`, where `sort` is the
create time in seconds or `MIN_TIME - 1` when the time is invalid. Timeline, recent and context
cursors store that key, and each shard's keyset predicate is derived from it. Search cursors store
the rank key instead, `(position, length, time, "table:local_id")` (four items, the cursor limit),
and resume strictly after it in rank order; a candidate dropped on one page is dropped again on
any later page, so none is returned twice.

| Tool | Live plan |
| --- | --- |
| `status` | Readiness (keys, build, required databases), per-database observation times, whether `message_fts.db` and `sns.db` are usable. Record counts become cheap `COUNT(*)` values or are dropped. |
| `list_contacts`, `list_conversations`, `get_group` | Filter, sort and page the cached lists in Python; membership read live per call as today (`memberships`). |
| `get_messages`, `recent_messages` | `message_keys` per table (one conversation's shard tables, or every table) with time, sender and a source-type superset pushed into SQL, heap-merged in total order; candidates are decoded in small batches and kept only if their decoded `type` and `sender_id` match, until `limit + 1` survive. Tables with no known conversation are skipped and counted in `status`. |
| `search_messages` | Read `message_fts_v4_*_content` with session, sender, time and source-type filters in SQL; NFKC/casefold substring match in Python; rank by (position, length, newest, table and local_id) on the indexed text. Each candidate maps through the index's `name2id` to its conversation's table; a local_id present in more than one shard is narrowed by source type and create time, and skipped only if still ambiguous. Candidates are decoded in rank order and kept only if the decoded text contains the query and the type and sender match. Messages WeChat does not index (system messages, 123 on this account) are not searchable. |
| `get_context` | Resolve the anchor through the message ID index, then take `before` and `after` neighbors from `message_keys` over all of the conversation's shard tables. The `generation` argument is removed. |
| `list_attachments`, `get_attachment` | `get_attachment` resolves through the attachment index, decodes the message and recomputes its refs. `list_attachments` with `message_id` decodes that message; without it, walks the attachment index in ID order, decoding each message once, until `limit + 1` refs pass the filters; its cost is measured before merge. The resource URI drops its generation segment: `wechat-attachment://content/{attachment_id}/{representation}`. |
| `list_moments`, `get_moment`, `list_moment_notifications` | Filter and page the cached records. When `sns.db` is absent, unkeyed, unsupported or unreadable they fail with `moments_unavailable`, never an empty list; `status` says why. |
| `send_message` recipient check | `resolve_recipient` reads the cached conversations and contacts. |

Budget: every tool answers within 2 s on the owner's account, cold caches included. Measured on
2026-11-22: the slowest cold call was 1.5 s (`list_moment_notifications`), the slowest warm call
0.6 s (`get_group`); the table is in `docs/compatibility.md`. Reaching it needed two changes found
by profiling: an adapter reuses each SQLCipher connection for the length of one query (388 opens
cost 0.58 s in one `list_attachments` call), and attachment mapping takes a message's conversation
from the decoder's own table map instead of rebuilding the conversation list (0.4 s per call).

## Removed

- `archive/` (store, importer, reconcile, watcher), `search/index.py`, the index helpers in
  `search/tokenize.py` (`terms`, `match_expression`), and `benchmarks/search.py`'s archive setup
  (retargeted at the live repository).
- CLI: `archive-refresh`, `archive-status`, `search-index`, `archive-watch`; `serve --no-watch`.
  `search` stays and runs live.
- Locks `.archive-import.lock`, `.archive-watcher.lock`.
- Error codes that only the archive produced (`archive_not_ready`, `archive_refresh_in_progress`,
  `cursor_expired`, `invalid_generation`, `search_not_ready`, `archive_query_failed`,
  `unsupported_archive_schema`, ...); the docs list each one.
- Tests for removed code; new tests cover each live tool against the synthetic SQLCipher fixtures
  in `tests/support/encrypted.py`, including a fixture `message_fts.db`.

## Migration

The new code never opens `archive.sqlite3`. Deleting the existing file (532 MB) and its `-wal`/
`-shm` is left to the owner; `docs/uninstall.md` and the README say so. Running `serve` processes
of the old code keep working on the old archive until restarted.

## Risks

- **WeChat changes its FTS layout or stops filling it.** `status` reports the text index as
  unavailable and `search_messages` returns `text_index_unavailable`; it never silently returns
  nothing. The adapter checks the `_content` columns like every other table.
- **FTS lag.** Measured equal to the message tables on this account; a message WeChat has not yet
  indexed is missing from search but present in `get_messages`.
- **Cold-cache latency.** The first query after a contact or Moments change pays up to ~0.6 s.
- **Long-running native reads.** Unchanged: the reader's 5 s deadline and 2 s busy timeout.
- **Contention with WeChat.** A query can now fail with `source_busy` while WeChat holds a write
  lock longer than the busy timeout; clients retry. The private copy never contended.
