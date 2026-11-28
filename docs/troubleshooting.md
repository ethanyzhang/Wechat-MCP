# Troubleshooting

Find what you saw, read what it means, and take the listed step. The CLI prints
errors as `{"error": "<code>"}`. MCP tools return the same code with `isError`
set. Source: `BridgeError` in `src/wechat_bridge/core/errors.py`.

One rule covers every row: **an ordinary query never needs setup.** Search,
`serve` and every tool use the keys already saved in `<state-dir>/keys.json`. Run
`setup` only when a row below says so, and only after `verify-keys` fails.
[setup.md](setup.md) holds the procedure.

`<state-dir>` is `~/Library/Application Support/WechatBridge` unless you set
`--state-dir` or `WECHAT_BRIDGE_STATE_DIR`.

## Start here

Two commands that read no messages give most answers:

```sh
uv run wechat-bridge doctor       # environment, WeChat version and build
uv run wechat-bridge verify-keys  # saved key coverage per database
```

The MCP `status` tool adds the live picture: `ready_for_local_queries` (or
`false` with `source_error`), `text_index_status`, `moments_status`, `coverage`
and the saved key count. It prints counts and no message text.

## Setup and key errors

| You see | What it means | Safe next step |
| --- | --- | --- |
| `invalid_private_state` | `config.json` is missing or unreadable. | Run `uv run wechat-bridge discover`. It reads no messages. |
| `setup_required` | `keys.json` does not exist. No capture has finished on this Mac. | Run the setup procedure once. If you had keys before, stop: do not recreate the file, and see [setup.md](setup.md#when-to-run-setup). |
| `key_coverage_incomplete`, or `verify-keys` exits 2 with `"ready": false` | A required database has no verified key. WeChat may have created a new message shard. | Run `verify-keys` again and read `issues`. If a required database still shows `missing_key`, run setup once. Saved keys stay in place. |
| `invalid_key_manifest` | `keys.json` failed validation. | Do not edit or delete it. Stop and report. Deleting it forces a new capture. |
| `unsupported_build` | The installed WeChat is not 4.1.15 build 270102 or 270135, 4.2.0 build 270390, or 4.2.1 build 270601. | Wait for a project update. Setup cannot fix this and no downgrade is needed. See [compatibility.md](compatibility.md). |
| `source_version_unavailable` | The bridge could not read `/Applications/WeChat.app/Contents/Info.plist`. | Check that WeChat is installed in `/Applications`, then run `doctor`. |
| `unsupported_schema` | The database layout differs from the tested build. | Run `inspect-schema` and report the code. Do not run setup. |
| `setup_prerequisites_missing` | The Mac is not arm64, or the command runs as root. | Run `setup prepare` as your normal user on an Apple silicon Mac. |
| `setup_command_failed` | An Apple tool (`lldb`, `codesign`, `ditto` or the system `python3`) failed. | Run `xcode-select --install`, then start again from `setup prepare`. |
| `client_exit_required` | A WeChat process is running. | Quit WeChat yourself (Cmd+Q), confirm with `pgrep -x WeChat`, then rerun `setup run`. Do not kill the process. |
| `setup_plan_stale`, `invalid_setup_plan` | The plan expired after one hour, WeChat or its databases changed, or the plan id is wrong. | Run `setup prepare` again and use the new `plan_id`. |
| `setup_busy` | Another `setup run` or `setup recover` holds `.setup.lock`. | Wait for it to finish, then retry. The kernel frees the lock when the holder exits. |
| `setup_recovery_required` | An earlier plan never finished recovery. | Run `setup recover --plan-id <that plan>`. The plan id is in `<state-dir>/setup/active.json`. |
| `setup_copy_exists`, `invalid_setup_copy` | The plan directory already holds a copy of the app. | Run `setup recover` for that plan, then `setup prepare`. |
| `process_inspection_unknown` | The process check failed. | Retry once. If it repeats, stop and report. |
| `status: capture_timeout`, `capture_process_ended`, `capture_failed`, `source_changed`, `partial_coverage` | `setup run` ended without full coverage. | See the status table in [setup.md](setup.md#failure-codes-and-actions). Retry no more than once, and never in a loop. |
| `recovery_status: manual_restore_required` | The temporary WeChat copy was not proved gone. | Quit the extra WeChat window yourself, then run `setup recover --plan-id <id>`. |

## Discovery and source errors

| You see | What it means | Safe next step |
| --- | --- | --- |
| `account_selection_required` | More than one account directory exists. | Stop. This needs an explicit private configuration; see [architecture.md](architecture.md#runtime-state). |
| `source_not_found` | No WeChat account directory exists on this Mac. | Sign in to WeChat once, then run `discover`. |
| `required_source_missing` (in `issues`) | WeChat has not created a required database for this account. | Stop and report. Opening WeChat and loading your chats may create the files, after which `discover` can run again. |
| `source_inventory_limit` | More than 4096 entries sit under `db_storage`. The walk stops rather than run unbounded. | Stop and report. The limit is in `source/discovery.py`. |
| `source_symlink` | A symlink sits inside the WeChat data folder. The bridge refuses to follow it. | Find and remove the symlink only if you created it. Otherwise report it. |
| `source_unreadable`, `source_header_invalid` | A database or the data folder cannot be read, or a file has a truncated header. | Check that macOS has granted your terminal access to WeChat's app data, then retry. |
| `source_busy`, `query_timeout`, `result_too_large` | WeChat held a lock longer than 2 seconds, a query ran past 5 seconds, or a result passed 8 MiB. | Retry. WeChat releases its write lock when it finishes writing. If it repeats, retry with a narrower filter or a smaller `limit`, then report it. |
| `source_changed` | A database file was replaced while the bridge opened it. | Retry. The next call opens the new file. |

## Query errors

| You see | What it means | Safe next step |
| --- | --- | --- |
| `text_index_unavailable` | WeChat's own text index (`message/message_fts.db`) is absent, has no verified key, has an unexpected layout or cannot be read. `status.text_index_status` says which. Search needs it; other tools do not. | If `text_index_status` is `key_missing`, run `verify-keys`. Otherwise report the status value. Use `get_messages` or `recent_messages` meanwhile. |
| `moments_unavailable` | `sns/sns.db` is absent, has no verified key, has an unexpected layout or cannot be read. `status.moments_status` says which. | If `moments_status` is `key_missing`, run `verify-keys`. Otherwise report the status value. Message tools are unaffected. |
| `message_not_found` for an ID you used before | The message is gone from the source, or its database got a new incarnation (a new salt), which changes its message IDs. | Search or list again to get the current ID. |
| `operation_cancelled` | You interrupted a CLI command. | Run it again. |
| `archive_not_ready`, `archive_refresh_in_progress`, `search_not_ready`, `archive_query_failed`, `cursor_expired`, `invalid_generation` | These codes came from the archive that earlier versions used. The current code never returns them, so an older `serve` process is still answering. | Restart the MCP client so it starts `serve` from the current code. `pgrep -fl 'wechat-bridge.*serve'` lists running servers. |

## MCP tool errors

Codes and causes are listed in [mcp.md](mcp.md#errors-and-trust). The ones
people ask about most:

| You see | What it means | Safe next step |
| --- | --- | --- |
| `invalid_cursor` | The cursor was altered, you changed a filter or `limit` between pages, or the cursor came from an earlier version. | Start again without a cursor and keep the filters fixed while paging. |
| `invalid_arguments`, `invalid_filter` | An unknown argument, or a `kind`, `type`, `representation` or `availability` outside the allowed values. | Check the argument table in [mcp.md](mcp.md#tools). |
| `content_too_large` | A reply or attachment passed a size bound (8 MiB attachment maximum). | Lower `max_bytes`, or accept that the bridge has no chunked transfer. |
| `internal_error` | An unexpected failure. The bridge hides the cause so no private path or text leaks. | Run `doctor` and ask for `status`, then retry once. Report it with the command you ran, and no message content. |
| The assistant lists no `wechat_bridge` tools | The client has not loaded the server, or the path moved. | Run `claude mcp list` or `codex mcp list`, check the path to `.venv/bin/wechat-bridge`, and start a new session. |

## Symptoms without an error code

| You see | What it means | Safe next step |
| --- | --- | --- |
| A new message appears in `get_messages` but not in search | Search reads WeChat's own text index, and WeChat may not have indexed the message yet. | Search again later, or read the conversation with `get_messages`. |
| Search returns no results | Matching is a literal substring, after Unicode normalization and case folding. It is not fuzzy or semantic. The query is at most 256 characters. System messages are not in WeChat's text index, so they are never found. | Try a shorter word. Check the filters (`conversation_id`, `start`, `end`, `type`). Check `text_index_status` and `coverage` in `status`. |
| Later pages show messages newer than the first page, or a `sort=recent` conversation is missing from later pages | Paging reads live data with no pinned snapshot. Rows written after the first page can appear in later pages, and a conversation that gets new messages moves ahead of the pages already read. | Expected. Start again without a cursor for a fresh ordering. |
| A message you saw on your phone is absent | Messages that never synced to this Mac do not exist locally. `complete_phone_history` is always `false`. | Nothing to fix. The bridge reports what the Mac holds. |
| An image arrives as a labeled thumbnail or source bytes | The original is WXGF (unsupported), animated, HEVC, or not downloaded to this Mac. The bridge never swaps a thumbnail in for an original. | Read `availability` and `diagnostic_code` on the attachment. See [compatibility.md](compatibility.md). |
| WeChat updated and nothing works | The new build may be unsupported. | Run `doctor`. If `wechat_build` is not 270102, 270135, 270390 or 270601, wait for a project update. |

## What not to do

- Do not delete `keys.json` or `identity.json` to "reset". Deleting
  `identity.json` changes every stable ID and invalidates every cursor.
  `archive.sqlite3` (with `-wal` and `-shm`) and `adapter-schemas.json`, left
  by earlier versions, are unused and safe to delete.
- Do not quit WeChat by process name, use `sudo`, or re-sign the installed app.
- Do not paste keys, `config.json`, or message content into an issue. See
  [SECURITY.md](../.github/SECURITY.md).
