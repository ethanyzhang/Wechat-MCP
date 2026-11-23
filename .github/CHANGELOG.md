# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims
to follow [Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

### Changed

- The adapter supports WeChat 4.2.0 build 270390 besides 4.1.15 builds 270102 and 270135. The version and build must now be a
  supported pair (`SUPPORTED_VERSIONS`), so 4.1.15 with 270390 or 4.2.0 with 270102 is `unsupported_build`.
- The MCP server reports its package version instead of a hard-coded one.
- Every MCP tool and the CLI `search` now read WeChat's encrypted databases at call time through the read-only source reader
  and the adapter. The private archive (`archive.sqlite3`), its generations, importer, watcher and the bridge's own search index
  are removed, so results no longer lag the source by a refresh. Each call opens a fresh adapter; in-memory caches, keyed by the
  stat signature of the databases they come from, map opaque IDs back to source rows. Opaque IDs are unchanged.
- Every list result is `{source: "live", observed_at, items, next_cursor}`; message results also carry `local_history_status`
  (`no_local_message_table` for a conversation without a local message table, as an empty result). `get_context`, `get_group`
  and `get_moment` keep their `message`, `group` and `moment` field next to `source` and `observed_at`.
- `status` reports `ready_for_local_queries`, `adapter_version`, `adapter_status`, `text_index_status`, `moments_status` and
  `coverage` (`message_tables`, `unmapped_message_tables`, `conversations_without_local_messages`, `database_count` and flags),
  or `ready_for_local_queries: false` with `source_error` when the source cannot be opened.
- `search_messages` and `search` read the content tables of WeChat's own text index (`message/message_fts.db`, bound as an
  optional database like `sns/sns.db`) and match literal substrings in Python. Messages WeChat does not index, such as system
  messages, are not searchable, and a message WeChat has not indexed yet appears in `get_messages` before it appears in search.
- Cursors use format version 2 and carry no generation. A cursor resumes from its keyset position in the live data, so rows
  written after the first page may appear in later pages and `sort=recent` conversations can move between pages. Cursors from
  earlier versions return `invalid_cursor`.
- The attachment resource URI is `wechat-attachment://content/{attachment_id}/{representation}`.
- A query can fail with `source_busy` while WeChat holds a write lock for longer than the reader's 2 second busy timeout.
- `discover`, which every query now runs, rewrites `inventory.json` only when a database incarnation is new or changed.
  `adapter-schemas.json` is no longer written.

### Added

- Read-only Moments tools `list_moments`, `get_moment` and `list_moment_notifications` over the optional `sns/sns.db`, with a field
  whitelist. They fail with `moments_unavailable` when `sns.db` is absent, unkeyed, unsupported or unreadable, and `status`
  reports why in `moments_status`.
- Error code `text_index_unavailable`: WeChat's text index cannot be read, so search is unavailable; other tools still work.
- `serve --enable-send` registers `send_message`, a text-only send through the open WeChat app (Accessibility, no keystrokes,
  per-call approval, attempt log, throttle). Off by default; see docs/mcp.md, Sending.
- `list_conversations` returns `unread_count` and `last_timestamp`, and takes
  `unread_only` and `sort=recent` (newest activity first). A session table without
  the columns reports `session_unread_unavailable` instead of a silent null.
- `recent_messages`: messages across all conversations in a time window, newest
  first by default.
- Message results carry `sender_display_name` and `conversation_display_name`.

### Removed

- CLI commands `archive-refresh`, `archive-status`, `search-index` and `archive-watch` (with `--debounce-seconds` and
  `--reconcile-seconds`), and `serve --no-watch`. `search` stays and runs live.
- The `generation` argument of `get_context` and `get_attachment`, the `generation` field of attachment metadata, the
  generation segment of the resource URI, and the result fields `generation`, `snapshot_published_at`, `coverage` and
  `freshness` (coverage is reported by `status`).
- Error codes that only the archive produced: `archive_not_ready`, `archive_refresh_in_progress`, `archive_refresh_failed`,
  `archive_query_failed`, `archive_write_failed`, `archive_account_mismatch`, `unsupported_archive_schema`, `search_not_ready`,
  `cursor_expired`, `invalid_generation`, `source_shard_missing`, `message_table_mapping_incomplete`, `moments_discard_failed`,
  `invalid_watch_interval`, `invalid_batch` and `invalid_record_kind`.
- Lock files `.archive-import.lock` and `.archive-watcher.lock`.
- Upgrade note: the new code never reads `archive.sqlite3` (with `-wal` and `-shm`) or `adapter-schemas.json`; delete them from
  the state directory to free the space. A `serve` process started from older code keeps using the old code and archive until
  its client restarts it (`pgrep -fl 'wechat-bridge.*serve'` lists running servers).

## [0.1.2] - 2026-10-24

### Fixed

- The `serve` watcher no longer re-imports an unchanged source every 60 s. The
  periodic reconcile backs off to hourly while the source is untouched, and
  every refresh is followed by idle time that caps the watcher at 20% of a core,
  so a source written steadily or a repeating failure cannot hold it busy.

## [0.1.1] - 2026-10-15

### Changed

- `discover` stops with `source_inventory_limit` after 4096 entries under
  `db_storage`, matching the reconcile walk.
- The CLI prints `{"error": "internal_error"}` and exits 2 on unexpected
  exceptions instead of a traceback.
- Build backend pinned to `hatchling==1.32.4`.

### Removed

- The write-only decoded-image cache (`state/decoded/`). Existing folders can
  be deleted.

## 0.1.0 - Initial release (not published as a tag)

First public release. Experimental. Source acceptance remains blocked (see
Known limitations).

### Added

- Source access: private discovery and redacted diagnostics (`doctor`,
  `discover`), a read-only, query-only, authorizer-restricted SQLCipher reader
  with WAL coverage, key verification and partial-coverage reporting
  (`inspect-schema`, `verify-keys`).
- One-time, explicit, recoverable key capture (`setup prepare`, `setup run`,
  `setup recover`).
  After keys are verified and recovery completes, setup deletes its raw-key
  copies (`captured.json`, `candidates.json`) and reports `key_copies_removed`.
- Adapter for WeChat 4.1.15 builds 270102 and 270135 on macOS 27 (arm64):
  messages (text, Zstandard text, image, file, reply), cross-shard replies,
  contacts, groups and locally observed membership. Unsupported or unmapped
  records are retained explicitly.
- Private, owner-only archive published in atomic, complete generations, with
  bounded full reconciliation, a debounced watcher and freshness/coverage
  diagnostics (`archive-refresh`, `archive-status`, `archive-watch`).
- Generation-scoped literal Unicode/Chinese substring search with bounded
  queries and opaque authenticated paging cursors (`search-index`, `search`).
- Stdio MCP server with nine read-only tools (`status`, `list_conversations`,
  `list_contacts`, `get_group`, `search_messages`, `get_messages`,
  `get_context`, `list_attachments`, `get_attachment`) and bounded, verified
  attachment delivery (`serve`). Concurrent `serve` sessions coordinate on a
  per-state-directory watcher lock.
- Attachment resolution for files (size and digest verified) and static
  JPEG/PNG/WebP, legacy XOR and V1/V2 DAT images. Each result carries an
  original or thumbnail label, and one never stands in for the other.
- Source-acceptance proof tool (`prove`) and a query-latency benchmark.
- Project documentation, contributing guide, security policy and code of
  conduct.

### Known limitations

- Live source acceptance is blocked on a supported full original image and
  independent confirmation of the proof samples.
- WXGF image originals are not decoded; they are returned only as labeled
  source bytes. Animated images and HEVC are unsupported.
- Attachment delivery is capped at 8 MiB per call; no chunked transfer.
- Some attachment metadata variants are unmapped and reported as unsupported.
- Setup leaves the `WeChat.disabled` app copy, and setups from before the
  cleanup change may keep raw-key copies (`setup/<plan>/captured.json` and
  `candidates.json`) in the private state directory. Exclude that directory
  from backups. See [SECURITY.md](SECURITY.md).
- The bridge supports the tested WeChat line and fails closed on other builds
  with `unsupported_build`.

[Unreleased]: https://github.com/xerlxzx/Wechat-MCP/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/xerlxzx/Wechat-MCP/releases/tag/v0.1.1
