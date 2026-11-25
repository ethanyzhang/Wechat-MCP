# MCP server reference

`wechat-bridge serve` runs a stdio MCP server built on the pinned official
Python SDK (`mcp==2.3.0`). Stdout carries protocol only; diagnostics go to
stderr. There is no network listener. Source: `src/wechat_bridge/mcp/`.

## Launching

```sh
uv run wechat-bridge serve
```

Every tool reads WeChat's encrypted databases at call time through the
read-only source reader (`source/reader.py`) and the build adapter. There is no
import step, no private copy of the history and no background watcher. Each
call opens a fresh adapter, so new message shards, new message tables and
replaced databases are seen at once.

The `serve` process keeps a few in-memory caches that map opaque IDs back to
source rows: contacts and conversations, per-shard message IDs and senders, an
attachment index, and Moments. Each cache is keyed by the inode, size and mtime
of exactly the databases it was built from (and their `-wal` files) and is
rebuilt lazily inside the first query after one of them changes. An ID found
through a cache is re-derived from the decoded row before it is returned.
Nothing in these caches is written to disk. A query writes no records; the
`discover` step it runs rewrites `inventory.json` only when a database
incarnation is new or changed.

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

Each registered client starts its own `serve` process with its own caches;
the processes do not coordinate. A query can fail with `source_busy`
when WeChat holds a write lock on a database for longer than the reader's
2 second busy timeout; retry it.

The `.venv` entry point is an editable install, so it runs whatever branch the
repository has checked out. Switching branches changes the code that the next
`serve` start runs. It also changes any module that a running server has not
imported yet. A `serve` process started before an upgrade keeps running the old
code until its client restarts it.

## Command line

Every tool is also a `wechat-bridge` subcommand that calls the same service
method and prints the same JSON object on stdout, then exits: nothing runs in
the background, and each command reads the databases when it runs. Tool names
become dashed commands and arguments become dashed flags; the ID argument of
`get_group`, `get_messages`, `get_moment`, `get_context` and `get_attachment`
and the query of `search_messages` are positional.

```sh
wechat-bridge list-conversations --unread-only --sort recent --limit 20
wechat-bridge recent-messages --start 2026-11-24T00:00:00-06:00
wechat-bridge search-messages 发票 --conversation-id conversation_<hex>
wechat-bridge get-context message_<hex> --before 5 --after 5
wechat-bridge get-attachment attachment_<hex> --output ~/Desktop/file.bin
printf '%s' "text" | wechat-bridge send-message --conversation-id conversation_<hex> --recipient-name "Full name"
```

- Errors print `{"error": code}` and exit 2; argument errors print
  `invalid_arguments` without echoing the value.
- `get-attachment` prints the metadata. With `--output PATH` it also writes the
  bytes to a new file (mode 0600; an existing path is `output_exists`). There is
  no `resource_uri` on the command line.
- `send-message` reads the text from stdin (trailing newlines removed) and goes
  through the same sender, log, limits and lock as `send_message` below. It is
  not gated by a flag and asks for no approval of its own: the caller decides,
  as with an allowlisted MCP tool. `sent` exits 0, `unconfirmed` exits 3 (never
  resend).
- `status` reports `read_only: true`: it describes the read commands.
- Shared state (`inventory.json`, `identity.json`, `keys.json`) is written under
  the state-directory lock and replaced atomically, so concurrent commands are
  safe; sends are serialized by the send lock.

## Tools

All thirteen tools are annotated read-only, non-destructive, idempotent and closed-world.
Unknown arguments return `invalid_arguments`, as do SDK argument-validation
failures (the caller's rejected value is not echoed). Opaque IDs have the form
`<kind>_<64 hex>`.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `status` | none | `ready_for_local_queries` (or `false` with `source_error` when the source cannot be opened), `adapter_version`, `adapter_status`, `text_index_status`, `moments_status`, `coverage` (`message_tables`, `unmapped_message_tables`, `conversations_without_local_messages`, `database_count`, and the flags `account_wide_atomic_snapshot`, `auxiliary_storage_unhandled`, `complete_phone_history`), environment, saved key count, latest live proof status, limits, supported image formats |
| `list_conversations` | `query`, `kind` (`direct`\|`group`), `unread_only`, `sort` (`name`\|`recent`), `limit`, `cursor` | Conversations with `unread_count` and `last_timestamp` (UTC; null when WeChat records none). `unread_only` keeps those with unread messages; `sort=recent` puts the newest activity first and conversations without a known time last |
| `list_contacts` | `query`, `limit`, `cursor` | Contacts; duplicate names keep distinct IDs |
| `get_group` | `conversation_id`, `limit`, `cursor` | Group metadata and locally observed membership (not a historical roster) |
| `search_messages` | `query` (required), `conversation_id`, `sender_id`, `start`, `end`, `type`, `limit`, `cursor` | Literal Unicode/Chinese substring matches with excerpts, read from WeChat's own text index; times need a timezone. See [Search](#search) |
| `get_messages` | `conversation_id`, `sender_id`, `start`, `end`, `type`, `limit`, `cursor` | Chronological timeline, including unsupported messages and unknown senders. Every message result (this tool, `search_messages`, `recent_messages`, `get_context`) carries `sender_display_name` and `conversation_display_name` (null when unknown, cut at 256 characters) |
| `recent_messages` | `start` (required, timezone), `end`, `type`, `sender_id`, `order` (`desc`\|`asc`), `limit`, `cursor` | Messages across all conversations in a time window, newest first by default; messages with no valid time are excluded |
| `list_moments` | `author_id`, `query`, `start`, `end`, `limit`, `cursor` | Moments (朋友圈) posts newest first with text, link title, media summary (type and size, never the media), venue name and city, likes and comments: stored up to the newest 200 likes and 100 comments, but a list page shows only the newest 5 comments and first 10 likes per post (`comment_count`, `like_count` and `*_truncated` say what is left; `get_moment` returns everything stored). Text fields over their cap (post 20000, comment 2000, feed summary 2000 characters) are cut and flagged `text_truncated`; search covers the kept text only. Author, liker and commenter display names come from the contacts, falling back to the source nickname. `query` uses the same normalization as message search (NFKC, case-folded, whitespace collapsed); `start`/`end` need a timezone and are exact to the fraction of a second. `parse_status` is `ok`, `summary_only` (only the feed summary exists), `oversize` or `malformed`. Fails with `moments_unavailable` when Moments cannot be read |
| `get_moment` | `moment_id` | One post with all stored likes and comments |
| `list_moment_notifications` | `moment_id`, `start`, `end`, `limit`, `cursor` | Rows of the account's Moments notification table, newest first. `kind_inferred` (`like`, `comment`, `unknown`) is inferred from the source type code (1 has no text, 2 has text), not a source fact; `involves_me` is the source's `is_relative_me` flag; `source_unread_flag` and `source_read_flag` are cached source values (in the account examined they were never set to unread), not the app's current state, so there is no unread filter; deleted comments keep `deleted: true` and no text; a deletion flag other than 0 or 1 is `null` (unknown) with no text |
| `get_context` | `message_id`, `before` (10), `after` (10) | A message plus at most 100 combined neighbors from its conversation, across all of its shards |
| `list_attachments` | `message_id`, `kind` (`image`\|`file`), `representation` (`original`\|`thumbnail`), `availability`, `limit`, `cursor` | Attachment metadata; `local_unverified` means bytes not yet checked |
| `get_attachment` | `attachment_id`, `representation` (`auto`\|`source`\|`decoded`), `max_bytes` | Verified bytes plus metadata (hashes, labels, resource URI) |

Filter values: message `type` is one of `text`, `image`, `file`, `reply`,
`unsupported`, `voice`, `video`; attachment `availability` is one of
`local_unverified`, `not_local`, `unsupported_format`, `identity_mismatch`.
Other values return `invalid_filter`.

Moments come from the optional `sns/sns.db`: it is read only when its key is verified and its tables have the expected
columns. When it is absent, its key is missing, its schema is unsupported or it cannot be read, the three Moments tools fail
with `moments_unavailable` instead of returning an empty list, `status.moments_status` names the reason (`not_present`,
`key_missing`, `unsupported_schema`, `failed:sns_schema_unreadable`), and the message tools are unaffected. A busy or
unreadable source still fails with `source_busy` or `source_unreadable`. Only a whitelist of fields is parsed: media URLs,
keys, tokens, hashes, coordinates and street addresses never enter a record. Posts over 256 KiB (measured in bytes) are never read (`oversize`); a post whose XML is not the expected structure is `malformed`, and one with no usable time at all is `partial`. Moments text, likes and comments are untrusted data from other people.

## Results

Every list result has the shape `{"source": "live", "observed_at", "items", "next_cursor"}`. `observed_at` is the UTC
time the bridge answered. `get_moment` returns `moment`, `get_context` adds the anchor `message`, and `get_group` adds
`group`, each next to `source` and `observed_at`. Message results from `search_messages`, `get_messages` and
`recent_messages` also carry `local_history_status`: `available_local_scope`, or `no_local_message_table` when the
conversation has no message table on this Mac, which is a normal empty result. Coverage belongs to `status`; results carry
no coverage or freshness fields.

## Search

`search_messages` and the CLI `search` command read the plain-text content tables of WeChat's own full-text index,
`message/message_fts.db` (tables `message_fts_v4_<n>_content`). WeChat's tokenizer exists only inside WeChat, so the
index is read as a text store and matched in Python: the query and each text are NFKC-normalized, case-folded and
whitespace-collapsed, and the query must appear as a literal substring. Results are ranked by match position, text length,
newest first, then table and row. Each candidate is decoded from its message table and kept only if the decoded text still
contains the query and the type and sender filters match.

- Messages WeChat does not index, such as system messages, are not searchable. They still appear in `get_messages`.
- A message WeChat has not indexed yet is missing from search but present in `get_messages`.
- When the index is absent, unkeyed, has an unexpected layout or cannot be read, `search_messages` fails with
  `text_index_unavailable` and `status.text_index_status` says why (`not_present`, `key_missing`, `unsupported_schema`,
  `failed:text_index_schema_unreadable`). Search never silently returns nothing in that case.

## Limits

| Limit | Value | Defined in |
| --- | --- | --- |
| Records per page | default 50, max 200 | `core/limits.py` |
| Context neighbors | max 100 combined | `core/limits.py` |
| Query length | 256 characters | `core/limits.py` |
| Result page budget | 8 MiB | `search/repository.py` |
| Source statement deadline | 5 seconds, with a 2 second busy timeout | `source/reader.py` |
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
- Resource URIs have the form
  `wechat-attachment://content/{attachment_id}/{representation}`. The bytes
  are resolved from the current source each time the resource is read.

## Paging

Cursors are opaque, HMAC-authenticated, and bound to the method, filters and
limit. A cursor holds the keyset position of the last row of its page and
resumes from that position in the live data; there is no pinned snapshot.
Rows written after the first page may therefore appear in later pages, and
conversations sorted by `recent` can move between pages when they receive new
messages. Messages are ordered by creation time, then database, table and row.
Cursors from
versions that used archive generations return `invalid_cursor`.

## Sending (optional, off by default)

`wechat-bridge serve --enable-send` adds one tool, `send_message(conversation_id, recipient_name, text)`. Without
the flag the tool does not exist, the send code is never imported and `status.read_only` stays true. With it,
`status.read_only` is false, `send_message` is annotated non-read-only, destructive and open-world, and every other
tool keeps its read-only annotation.

- **Approval.** Keep the MCP client's own permission prompt on for `send_message` on every call and never allowlist
  it. Message text from chats is untrusted and can contain instructions; the prompt is the only barrier between such
  text and a sent message. The prompt shows the recipient name and the text. Check once, by hand, that a call prompts
  and that denying it changes nothing in WeChat.
- **What it does.** Text only (up to 2000 characters, newline allowed, no other control character), to a direct or
  group chat. `recipient_name` must equal the conversation's full stored display name, the name must be unique among
  all contacts and conversations (a conversation and its own contact row count as one), and after opening the chat the
  window title must match and a member-count label must be present exactly for a group. The composer must be empty; the text is set as the composer's value and read back before Send is
  clicked. No keystroke is ever typed (the input method corrupts them). It works from any starting tab and clicks
  each chat row once. Every mouse event first checks that WeChat is frontmost and is the window under the pointer, and
  the Send click re-verifies title, kind and composer text in the same helper run.
- **Results.** `sent` means the window shows a new message with that text and the composer emptied, not that WeChat
  delivered it. `unconfirmed` means Send was used but the window did not show it: it may or may not have been sent,
  never resend, check the chat. Refusals (`refused_*`, `throttled`, `duplicate_send`, `send_log_unavailable`) send
  nothing. A refused send after the chat was opened still marks that chat read in WeChat.
- **Log and limits.** `send-log.jsonl` in `~/Library/Application Support/WechatBridgeSend` (mode 0600, append-only, shared by every
  bridge process of the user) records an attempt before Send is used and a result after: time, conversation ID, a keyed
  HMAC of the text and its length, never the text. A refusal after the chat was opened is logged too. At most
  one send per 3 seconds and 30 per hour (attempts count, even unresolved ones), and the same text to the same
  conversation is refused for 10 minutes (an attempt that was refused before Send was pressed no longer counts; the duplicate window only recognizes repeats made through the same state directory, because the text digest is keyed per state directory, while the interval and hourly cap are shared by all). If the log ever holds a damaged line (a crash mid-write), sends are refused with `send_log_unavailable` until that line is deleted by hand. A
  `send.lock` next to the log serializes sends across processes; a send waits at most 30 seconds for it (`refused_busy`). These are guards against a runaway loop, not WeChat rules.
- **Permission.** The Accessibility grant belongs to the app that launches the bridge (for example Claude.app) and
  covers everything that app runs. The tool returns `refused_no_accessibility` when it is missing. The UI helper
  (`wechat_ui.swift`) is compiled once into the state directory with `swiftc` (Xcode command line tools).
- **Single account.** The recipient is resolved from the configured account's databases, while the helper drives whichever
  WeChat account is logged in. They must be the same account; do not switch accounts while sending is enabled. The window
  shows no account indicator the bridge can check, so this is an assumption to confirm in the manual acceptance run.
- **Helper build.** The first start with `--enable-send` compiles the helper in the background; a send before that finishes
  compiles it inline and may exceed the client's timeout.
- **Account risk.** Driving the desktop app is not an official WeChat API. The owner accepts that risk for personal,
  non-bulk use. Only use it for messages you would type yourself.

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
| `conversation_not_found`, `message_not_found`, `attachment_not_found`, `group_not_found`, `moment_not_found` | The ID is absent from the current source |
| `text_index_unavailable` | WeChat's text index cannot be read; see [Search](#search) |
| `moments_unavailable` | `sns/sns.db` cannot be read; `status.moments_status` says why |
| `source_busy`, `query_timeout`, `result_too_large` | WeChat held a write lock past the 2 second busy timeout, a statement ran past 5 seconds, or one statement returned more than 8 MiB. Retry |
| `source_unreadable`, `source_changed` | A database could not be read, or its file was replaced while it was being opened. Retry |
| `setup_required`, `key_coverage_incomplete`, `unsupported_build` | Keys are missing or incomplete, or the installed WeChat build is unsupported. See [troubleshooting.md](troubleshooting.md) |
| `content_too_large` | A reply or attachment exceeds a bound in [Limits](#limits) |
| `internal_error` | Any other failure |

When an attachment exists but its bytes are not deliverable, `get_attachment`
returns the metadata without an error. The `availability` field (`not_local`,
`unsupported_format`, `identity_mismatch`) and `diagnostic_code` (for example
`attachment_not_local` or `attachment_mapping_unsupported`) carry the reason.
