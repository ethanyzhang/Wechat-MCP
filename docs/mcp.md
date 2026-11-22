# MCP server reference

`wechat-bridge serve` runs a stdio MCP server built on the pinned official
Python SDK (`mcp==2.3.0`). Stdout carries protocol only; diagnostics go to
stderr. There is no network listener. Source: `src/wechat_bridge/mcp/`.

## Launching

```sh
uv run wechat-bridge serve             # with background archive watching
uv run wechat-bridge serve --no-watch  # deterministic session, no watcher
```

A first archive import needs `wechat-bridge archive-refresh`. The server never
imports or decrypts at query time. With watching enabled and an active
generation present at startup, a background thread polls DB/WAL metadata and
reconciles every 60 seconds while the source changes, backing off to hourly
while it is untouched. If no generation exists when `serve` starts, no
watcher starts; run `archive-refresh`, then restart `serve`. On disconnect the
watcher receives a stop signal and is joined for up to 8 seconds. A slow native
read can outlive the join, and the next import reclaims an unfinished
generation.

Register the server with a client through the project's virtualenv entry point
and an explicit state directory. Codex:

```sh
codex mcp add wechat_bridge -- <repo>/.venv/bin/wechat-bridge \
  --state-dir "$HOME/Library/Application Support/WechatBridge" serve
```

Claude Code (local scope, available only in the folder where you run it; use
`--scope user` only if every Claude Code session should be able to read your
chats):

```sh
claude mcp add --scope local wechat_bridge -- <repo>/.venv/bin/wechat-bridge \
  --state-dir "$HOME/Library/Application Support/WechatBridge" serve
```

Each registered client starts its own `serve` process. Concurrent `serve`
sessions coordinate on `.archive-watcher.lock` so only one runs an active
watcher per state directory; the others retry the lock every 30 seconds and
take over if the holder exits. Refreshes from the watcher and from a manual
`archive-refresh` are serialized by `.archive-import.lock` in the state
directory. A manual refresh that finds the import lock held returns
`archive_refresh_in_progress` instead of waiting. The watcher treats that
result as `deferred` and tries again on its next cycle.

A watcher refresh that fails with `setup_required`, `key_coverage_incomplete`,
`unsupported_build` or `archive_account_mismatch` is not retried every cycle.
The watcher waits until `keys.json` changes or an hour passes, because only
those events can clear the failure and each attempt records a failed
generation.

The `.venv` entry point is an editable install, so it runs whatever branch the
repository has checked out. Switching branches changes the code that the next
`serve` start runs. It also changes any module that a running server has not
imported yet.

## Tools

All thirteen tools are annotated read-only, non-destructive, idempotent and closed-world.
Unknown arguments return `invalid_arguments`, as do SDK argument-validation
failures (the caller's rejected value is not echoed). Opaque IDs have the form
`<kind>_<64 hex>`.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `status` | none | Readiness, coverage/freshness, environment, saved key count, latest live proof status, limits, supported image formats |
| `list_conversations` | `query`, `kind` (`direct`\|`group`), `unread_only`, `sort` (`name`\|`recent`), `limit`, `cursor` | Conversations with `unread_count` and `last_timestamp` (UTC; null when WeChat records none). `unread_only` keeps those with unread messages; `sort=recent` puts the newest activity first and conversations without a known time last. An archive imported before this field existed reports `freshness.stale` until it refreshes |
| `list_contacts` | `query`, `limit`, `cursor` | Contacts; duplicate names keep distinct IDs |
| `get_group` | `conversation_id`, `limit`, `cursor` | Group metadata and locally observed membership (not a historical roster) |
| `search_messages` | `query` (required), `conversation_id`, `sender_id`, `start`, `end`, `type`, `limit`, `cursor` | Literal Unicode/Chinese substring matches with excerpts; times need a timezone |
| `get_messages` | `conversation_id`, `sender_id`, `start`, `end`, `type`, `limit`, `cursor` | Chronological timeline, including unsupported messages and unknown senders. Every message result (this tool, `search_messages`, `recent_messages`, `get_context`) carries `sender_display_name` and `conversation_display_name` (null when unknown, cut at 256 characters) from the same generation |
| `recent_messages` | `start` (required, timezone), `end`, `type`, `sender_id`, `order` (`desc`\|`asc`), `limit`, `cursor` | Messages across all conversations in a time window, newest first by default; messages with no valid time are excluded |
| `list_moments` | `author_id`, `query`, `start`, `end`, `limit`, `cursor` | Moments (朋友圈) posts newest first with text, link title, media summary (type and size, never the media), venue name and city, likes and comments: stored up to the newest 200 likes and 100 comments, but a list page shows only the newest 5 comments and first 10 likes per post (`comment_count`, `like_count` and `*_truncated` say what is left; `get_moment` returns everything stored). Text fields over their cap (post 20000, comment 2000, feed summary 2000 characters) are cut and flagged `text_truncated`; search covers the stored text only. Author, liker and commenter display names come from the contacts, falling back to the source nickname. `query` uses the same normalization as message search (NFKC, case-folded, whitespace collapsed); `start`/`end` need a timezone and are exact to the fraction of a second. `parse_status` is `ok`, `summary_only` (only the feed summary exists), `oversize` or `malformed`. `coverage.moments` says whether Moments were imported and counts records by `parse_status` |
| `get_moment` | `moment_id` | One post with all stored likes and comments |
| `list_moment_notifications` | `moment_id`, `start`, `end`, `limit`, `cursor` | Rows of the account's Moments notification table, newest first. `kind_inferred` (`like`, `comment`, `unknown`) is inferred from the source type code (1 has no text, 2 has text), not a source fact; `involves_me` is the source's `is_relative_me` flag; `source_unread_flag` and `source_read_flag` are cached source values (in the account examined they were never set to unread), not the app's current state, so there is no unread filter; deleted comments keep `deleted: true` and no text; a deletion flag other than 0 or 1 is `null` (unknown) with no text |
| `get_context` | `message_id`, `before` (10), `after` (10), `generation` | A message plus at most 100 combined neighbors |
| `list_attachments` | `message_id`, `kind` (`image`\|`file`), `representation` (`original`\|`thumbnail`), `availability`, `limit`, `cursor` | Attachment metadata; `local_unverified` means bytes not yet checked |
| `get_attachment` | `attachment_id`, `representation` (`auto`\|`source`\|`decoded`), `max_bytes`, `generation` | Verified bytes plus metadata (hashes, labels, resource URI) |

Filter values: message `type` is one of `text`, `image`, `file`, `reply`,
`unsupported`, `voice`, `video`; attachment `availability` is one of
`local_unverified`, `not_local`, `unsupported_format`, `identity_mismatch`.
Other values return `invalid_filter`.

Moments come from the optional `sns/sns.db`: it is read only when its key is verified and its tables have the expected
columns, and a problem there (absent, key missing, unsupported schema, read error) only marks `coverage.moments` and never
stops messages from publishing. Only a whitelist of fields is stored: media URLs, keys, tokens, hashes, coordinates and street
addresses never enter the archive. Posts over 256 KiB (measured in bytes) are never read (`oversize`); a post whose XML is not the expected structure is `malformed`, and one with no usable time at all is `partial`. Archives imported before Moments existed have no
`coverage.moments` entry and show `freshness.stale` until refreshed. Moments text, likes and comments are untrusted data from other people.

## Limits

| Limit | Value | Defined in |
| --- | --- | --- |
| Records per page | default 50, max 200 | `core/limits.py` |
| Context neighbors | max 100 combined | `core/limits.py` |
| Query length | 256 characters | `core/limits.py` |
| Query read budget | 8 MiB | `search/repository.py` |
| Archive query deadline | 2 seconds | `search/repository.py` |
| Attachment delivery | default 1 MiB, max 8 MiB | `mcp/service.py` |
| Metadata reply wire size | 1 MiB | `mcp/server.py` |
| Binary reply wire size (base64 + metadata) | 12 MiB | `mcp/server.py` |

Exceeding a content bound returns `content_too_large`; chunked transfer is not
implemented.

## Attachment delivery

- `auto` decodes images and returns files as source bytes.
- Supported decoded images arrive as MCP image content. Files and explicitly
  requested source bytes arrive as embedded binary resources.
- The server preserves original/thumbnail and source/decoded labels. A client
  can fetch an unsupported original (for example WXGF) as labeled source bytes
  and nothing else.
- Resource URIs are generation-bound:
  `wechat-attachment://content/{generation}/{attachment_id}/{representation}`.
  When that generation has been removed, list attachments again.

## Paging

Cursors are opaque, authenticated, and bound to the method, filters, limit and
a pinned generation. Current and previous complete generations are retained; a
cursor for a removed generation returns `cursor_expired`.

## Errors and trust

Tool failures return `{"error": "<code>"}` with `isError` set. Unexpected
failures become `internal_error` without exception chains or private paths.
Retrieved messages and files are untrusted data, never instructions, and the
server states this in its MCP `instructions`.

Codes a tool call can return:

| Code | Cause |
| --- | --- |
| `invalid_arguments` | Unknown argument, or the SDK rejected an argument's type or range |
| `invalid_filter` | `kind`, `type`, `representation` or `availability` outside the listed values |
| `invalid_query`, `invalid_time_range`, `invalid_limit`, `invalid_context_limit`, `invalid_content_limit`, `invalid_id` | A value failed validation |
| `invalid_representation`, `unsupported_representation` | `get_attachment` representation is unknown, or `decoded` was requested for a non-image |
| `invalid_cursor` | The cursor is malformed, unauthenticated, or bound to a different method, filter set or limit |
| `cursor_expired` | The cursor's or `generation` argument's generation was removed |
| `invalid_generation` | `generation` is not a positive integer |
| `conversation_not_found`, `message_not_found`, `attachment_not_found`, `group_not_found` | The ID is absent from the pinned generation |
| `archive_not_ready`, `search_not_ready` | No complete generation exists yet, or the search index is not built |
| `archive_query_failed` | The archive query failed or hit the 2 second deadline |
| `content_too_large` | A reply or attachment exceeds a bound in [Limits](#limits) |
| `internal_error` | Any other failure |

When an attachment exists but its bytes are not deliverable, `get_attachment`
returns the metadata without an error. The `availability` field (`not_local`,
`unsupported_format`, `identity_mismatch`) and `diagnostic_code` (for example
`attachment_not_local` or `attachment_mapping_unsupported`) carry the reason.
