# Data flow

This page follows one message from WeChat's encrypted database on disk to a
tool response in your assistant. Each step names the owning package, the bound
that limits it, and what it reads or writes. For package rules and dependency
direction, see [architecture.md](architecture.md).

## Overview

```
 WeChat data folder (never written)             private <state-dir> (owner-only)
 ------------------------------------           -----------------------------------
 db_storage/**/*.db, *.db-wal, *.db-shm         keys.json   identity.json   inventory.json
        |                                              |            |             |
        | 1. discover   (source/discovery)             |            |             |
        |    names + 16-byte salt headers -------------+------------+------> inventory.json
        |                                              |
        | 2. verify keys (source/keys, reader)  <------+  keys.json
        |    one schema read per database              |
        v                                              |
 3. read (source/reader)                               |
    SQLCipher, read-only, query_only, authorizer       |
        |                                              |
        v                                              |
 4. adapt (adapters/)  <------ identity.json: opaque stable IDs
    rows -> Message, Reply, AttachmentRef records
        |
        v
 5. query (search/repository)   in-memory ID caches, no files
    one fresh adapter per call; decodes only the rows of the page
        |
        v
 6. respond (mcp/service, mcp/server)  ---> stdio ---> MCP client
        ^
        | attachment bytes: attachments/ reads media files, no-follow, pinned
        | beneath the account's media roots
 WeChat media files (never written)
```

Steps 1 and 2 first run as explicit commands (`discover`, `verify-keys`).
After that, every MCP tool call and every CLI `search` runs steps 1 to 6: the
adapter it opens rediscovers the databases and checks the saved keys (without
changing `keys.json`) before it reads. There is no import step and no private
copy of the history.

## Step by step

### 1. Discovery

- **Owner:** `source/discovery.py`, called from `cli.discover`.
- **Does:** walks `<account>/db_storage`, reads the first 16 bytes of each
  `.db` file (the SQLCipher salt), and records a database ID per relative path
  and salt. It decrypts nothing and reads no message.
- **Output:** `<state-dir>/inventory.json` and counts such as `database_count`
  and `replaced_count`. The file is rewritten only when a database incarnation
  is new or changed, so the discovery that every query runs writes nothing in
  the common case.
- **Bounds:** 4096 directory entries (`source_inventory_limit`), no symlinks
  (`source_symlink`), relative paths with no `..`.
- **Required set:** four fixed databases plus every `message/message_<n>.db`
  shard.

### 2. Key verification

- **Owner:** `source/keys.py`, using `source/reader.py`.
- **Does:** matches each saved key in `keys.json` to a database by account,
  path, salt and incarnation, then authenticates one schema read. It does not
  check every page.
- **Output:** a coverage report. Verified keys that the registry accepts merge
  into `keys.json` atomically. A failure never removes an existing key.
- **Bounds:** a missing `keys.json` returns `setup_required`; incomplete
  required coverage returns `key_coverage_incomplete`. Neither starts a
  debugger.
- **Where keys come from:** one-time `setup/` capture, never at query time.

### 3. Read

- **Owner:** `source/reader.py` and `source/native.py`.
- **Does:** opens one SQLCipher database with read-only flags, runs
  `PRAGMA query_only=ON`, and installs an authorizer that allows only listed
  statements, pragmas and functions. Only one statement runs at a time.
  SQLCipher merges the WAL; the bridge never merges pages itself.
- **Bounds:** 2 s busy timeout, 5 s query deadline, at most 200 rows and 8 MiB
  per query (`result_too_large`).
- **Read-only guarantee:** source files are opened read-only, and SQLite may
  coordinate through the `-shm` file, which is not a write to database
  content. Writable SQLCipher engines exist only under `tests/support/`.

### 4. Adapt

- **Owner:** `adapters/` (`wechat_4_1`, `schema`, `messages`, `replies`,
  `identity`, `wire`).
- **Does:** checks the installed build (270102 or 270135 of 4.1.15; 270390 of 4.2.0; 270601 of 4.2.1), observes
  each schema against a whitelist, decodes message content (including
  Zstandard text, XML and protobuf), and derives opaque IDs
  (`<kind>_<64 hex>`) with keyed HMAC-SHA256 from the secret in `identity.json`.
  `attachments/mapping` turns resource and hardlink rows into attachment
  references.
- **Bounds:** 1 MiB per message payload, schema and table whitelists, batches
  of 200 messages, strict wire-format limits.
- **Rule:** unmapped or malformed records keep their source identity and are
  reported. The adapter does not drop them.

### 5. Query

- **Owner:** `search/repository.py`, `search/tokenize.py`, `search/cursor.py`.
- **Does:** opens a fresh adapter for each call. Timelines merge the keysets
  of the relevant message tables in one total order (creation time, database,
  table, row), with time, sender and type filters pushed into SQL, and decode
  candidates in small batches until the page is full. Search reads the
  plain-text content tables of WeChat's own index (`message/message_fts.db`),
  matches the NFKC-normalized, casefolded query as a literal substring in
  Python, and confirms each hit on the decoded message. In-memory caches map
  opaque IDs back to source rows; each is keyed by the stat signature of the
  databases it came from. Cursors are authenticated and bound to the method,
  filters and limit, and resume from a keyset position in the live data.
- **Bounds:** query of at most 256 characters, 50 records by default and 200 at
  most, 100 combined context neighbors, 8 MiB per result page
  (`content_too_large`), plus the reader bounds of step 3 for every statement.
- **Writes:** nothing. The caches live only in the `serve` process.

### 6. MCP response

- **Owner:** `mcp/service.py` (bytes and status, independent of protocol) and
  `mcp/server.py` (thirteen typed read-only tools over stdio).
- **Does:** validates arguments, calls the repository, resolves attachments on
  demand, and encodes one compact JSON reply. Stdout carries protocol only.
- **Attachments:** `attachments/resolver` opens the media file with a
  no-follow descriptor pinned beneath the account's media roots and checks
  size and SHA-256. Supported images are decoded. A thumbnail
  never satisfies an original.
- **Bounds:** attachments of 1 MiB by default and 8 MiB at most, 1 MiB for a
  metadata reply, 12 MiB for a binary reply on the wire.
- **Errors:** `{"error": "<code>"}`. Unexpected failures become
  `internal_error` with no chain or path.

## Where read-only holds

| Boundary | Guarantee | Enforced by |
| --- | --- | --- |
| WeChat databases | Opened read-only; no write, no DDL, no `ATTACH` | Flags, `query_only`, authorizer in `source/reader.py` |
| WeChat media | Read through no-follow descriptors; never modified | `attachments/resolver.py` |
| Private state | Owner-only, symlink-rejecting, atomic writes | `core/config.py` |
| Query time | Source reads through the read-only reader only; no disk writes beyond an `inventory.json` update for a new or changed incarnation | `search/repository.py`, `source/discovery.py` |
| MCP surface | Thirteen read-only tools; no SQL, shell or arbitrary path; `send_message` exists only with `serve --enable-send` | `mcp/server.py` tool set |

Setup is the one exception to "never touches WeChat". It launches a signed
copy of the app under LLDB, once. See [setup.md](setup.md).

## Related

- [architecture.md](architecture.md): packages, locks, runtime state.
- [mcp.md](mcp.md): tool arguments, limits, paging, errors.
- [glossary.md](glossary.md): terms used on this page.
- [troubleshooting.md](troubleshooting.md): the error you hit at a given step.
