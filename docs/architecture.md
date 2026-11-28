# Architecture

`wechat-bridge` is a local, read-only, one-directional pipeline from WeChat's
encrypted local databases to a stdio MCP server. Every query reads those
databases at call time; the bridge keeps no copy of the history. This page
describes the code as implemented; update it in the same change as any
structural code change.

```mermaid
flowchart LR
    S[setup/<br/>one-time key capture] -.keys.json.-> K
    D[(WeChat SQLCipher DBs + WAL)] --> K[source/<br/>discovery · keys · reader]
    K --> A[adapters/<br/>builds 270102 · 270135 · 270390 · 270601 mappings]
    A --> I[search/<br/>live repository · cursor · tokenize]
    F[(WeChat media files)] --> T[attachments/<br/>mapping · resolver · images]
    A --> T
    T --> I
    I --> M[mcp/<br/>service · server]
    T --> M
    M --> C[MCP client over stdio]
    M -.serve --enable-send.-> W[send/<br/>optional send_message]
    A --> P[proof.py<br/>acceptance gate]
    T --> P
```

## Packages

All under `src/wechat_bridge/`. Each module's one-line docstring states its
invariant; read it before editing the module.

| Package | Responsibility | Key modules |
| --- | --- | --- |
| `source/` | Lowest layer above `core/`. Inventory DB identities without decrypting; hold exact per-database keys; run read-only, query-only, authorizer-restricted, deadline-bounded SQLCipher transactions. Never merges WAL pages manually. | `discovery`, `keys`, `native` (ctypes bindings), `reader` |
| `adapters/` | Interpret source rows for the supported builds only (`SUPPORTED_VERSIONS` in `adapters/wechat_4_1.py`: 270102 and 270135 of WeChat 4.1.15, 270390 of 4.2.0, 270601 of 4.2.1). Bounded schema observation and identifier whitelisting; decode messages, replies, protobuf wire data; derive stable opaque IDs from a private persisted identity secret. Owns every SQL statement against WeChat, including keyset reads of message tables without decoding (`message_keys`, `local_ids`) and the optional `sns/sns.db` and `message/message_fts.db`, each bound only when its key is verified and its schema matches. Unmapped/malformed records keep their source identity and are reported, never dropped. | `wechat_4_1`, `schema`, `messages`, `replies`, `moments`, `identity`, `wire`, `models` |
| `search/` | The live query layer. Each call opens a fresh adapter, merges per-table keysets in one total order, and decodes only the rows its page needs. In-memory caches, keyed by the stat signature of the databases they come from, map opaque IDs back to source rows. Search reads the content tables of WeChat's own text index and matches NFKC/casefolded literal substrings in Python. Authenticated opaque cursors are bound to method, filters and limit. Writes nothing to disk. | `repository`, `cursor`, `tokenize` |
| `attachments/` | Exact resource/hardlink joins on tested layouts; no-follow descriptor reads pinned beneath the account's media roots with size/digest/streaming SHA-256 checks; static image decoding; Media key material is separate from database keys. | `mapping`, `resolver`, `images`, `models` |
| `mcp/` | `service` produces verified bounded bytes and safe status independently of protocol; `server` exposes thirteen typed read-only tools over stdio (stdout is protocol only). See [mcp.md](mcp.md). | `service`, `server` |
| `send/` | Optional text-only `send_message`, imported only by `serve --enable-send`. Drives the open WeChat app through a compiled Accessibility helper, with a send log and throttle outside the state directory. See [mcp.md](mcp.md#sending-optional-off-by-default). | `tool`, `sender`, `driver`, `macos`, `log` |
| `setup/` | One-time key capture, never called by retrieval. Expiring plan → ad-hoc-signed *copy* of WeChat under Apple LLDB → page-authenticated, natively verified keys → idempotent recovery. See [setup.md](setup.md). | `prepare`, `capture`, `lldb_capture`, `recovery`, `lock`, `system` |
| `proof.py` | Source-acceptance gate: private selection of samples with explicit expected values; redacted public report. | none |
| `cli.py` | `wechat-bridge` entry point; lazy-imports each subsystem per command. | none |

Shared foundation modules live in the `core/` package and import nothing above
it. The top-level modules `cli.py` and `proof.py` sit above the layers they
orchestrate:

| Module | Purpose |
| --- | --- |
| `core/config.py` | Frozen `Config`; owner-only, symlink-rejecting, `O_NOFOLLOW`, size-bounded, atomic (temp + rename + fsync under `flock`) private-state I/O. Rejects state inside the repository (`state_inside_repository`); the guard runs in `create_config` and only means something for a source checkout. |
| `core/errors.py` | `BridgeError(code)`. Public output is a code, never source material. |
| `core/limits.py` | Public query-contract limits (values in [mcp.md](mcp.md#limits)). Other bounds live next to the code that uses them. |
| `core/validate.py` | Shared strict input validators raising a caller-chosen `BridgeError` code. |
| `core/serialize.py` | The single compact JSON encoding for private state and MCP text. |
| `core/clock.py` | UTC ISO-8601 timestamps. |

## Dependency direction

The intended layering (module-level imports follow it, and
`tests/unit/test_layering.py` enforces it):

```
foundation = core/ (config, errors, clock, validate, serialize, limits)
  ← source ← adapters ← attachments ← search ← mcp
  source ← setup
  core ← send
```

`attachments` is also imported by `mcp` and `proof.py`.

Known exceptions, all function-local imports:

| Import | Reason |
| --- | --- |
| `adapters/wechat_4_1.py` → `attachments.mapping` | Attachment references are derived while adapting messages; `attachments.mapping` itself imports `adapters.identity` and `adapters.wire`. |
| `mcp/service.py` → `cli.doctor` | `status()` reuses the CLI's environment report. |
| `mcp/server.py` → `send.tool` | `send_message` is registered only with `serve --enable-send`, so a default server never imports `send/`. |

Add no new upward imports. Move shared code down a layer.

`setup/lldb_capture.py` is a special case: it runs under Apple's system
`/usr/bin/python3` inside LLDB, not the project interpreter. It must stay
standard-library only, import nothing from the package, and remain compatible
with that older Python.

## Runtime state

Private state lives outside the repository, by default in
`~/Library/Application Support/WechatBridge` (override with `--state-dir` or
`WECHAT_BRIDGE_STATE_DIR`). Directories are 0700, files 0600.

| Path | Contents |
| --- | --- |
| `config.json`, `inventory.json` | Selected account root and native library path; discovered DB identities, rewritten only when an incarnation is new or changed |
| `keys.json` | Verified per-database keys. Never print, copy or commit. |
| `identity.json` | Identity secret. Deleting it changes every stable ID. |
| `image-material.json` | Optional explicit image-key candidates; when absent, material is derived from known app-data locations |
| `setup/<plan-id>/`, `setup/active.json` | Setup plans, journals, capture diagnostics; the plan that owns setup now |
| `proofs/<uuid>.json` | Private proof evidence |
| `wechat-ui-<hash>` | Compiled send helper, built only with `serve --enable-send`; one per helper source version, safe to delete |
| `.lock`, `.setup.lock` | `flock` files, described under [Locks](#locks) |

Earlier versions also wrote `archive.sqlite3` (with `-wal` and `-shm`),
`adapter-schemas.json`, `.archive-import.lock` and `.archive-watcher.lock`.
The current code never opens them; the owner can delete them.

The native SQLCipher library resolves to Homebrew's
`/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib` if present, otherwise
`~/.local/share/wechat-bridge/sqlcipher-4.19.0/lib/libsqlcipher.dylib` (built by
`scripts/build_sqlcipher.py`, which checks the source archive's SHA-256). A saved
`config.json` keeps its explicit path.

### Locks

Both are advisory `flock` files in the state directory (mode 0600,
`O_NOFOLLOW`). The kernel releases a lock when its process exits, so a crashed
holder leaves no stale lock.

| File | Taken by | Behaviour on contention |
| --- | --- | --- |
| `.lock` | `core.config.state_lock`: registry, key-manifest and identity-secret read-modify-write (`discover`, `publish_verified_manifest`, identity creation) | Blocks until free |
| `.setup.lock` | `setup.lock.setup_lock`: `setup run` and `setup recover` | Fails at once with `setup_busy`. A separate `setup/active.json` check returns `setup_recovery_required` while an earlier plan is unrecovered |

## Key contracts

- **Source reads:** read-only flags, `query_only`, restrictive authorizer, 2 s
  busy timeout, 5 s query deadline, 8 MiB result cap (`result_too_large`).
  SQLite may coordinate through the `-shm` file (shared-memory coordination,
  not a write to database content).
  Per-database observation times; no atomic account-wide snapshot is claimed.
- **Key verification** (`verify-keys`) authenticates a schema read per database
  using an exact account/path/salt/incarnation match. It does not prove
  full-page integrity. Only verified candidates whose ID the registry accepts
  are merged atomically into the manifest; failures preserve existing keys.
  A missing `keys.json` returns `setup_required`, and incomplete coverage of
  the required databases returns `key_coverage_incomplete`; neither starts a
  debugger.
- **Required databases:** `contact/contact.db`, `session/session.db`,
  `message/message_resource.db`, `hardlink/hardlink.db`, plus every
  `message/message_<n>.db` shard (`REQUIRED` and `MESSAGE_SHARD` in
  `source/discovery.py`). The required count therefore grows with the number of
  message shards. Other databases are discovered and keyed but not required.
  Discovery flags any `.kvdb` path as `auxiliary_storage_unhandled`.
- **Database incarnations:** a database ID belongs to a relative path + salt.
  A new salt is a new incarnation with a new ID (and new message IDs). A
  same-salt rewrite (new inode) keeps its ID and is flagged for reverification;
  the saved key must still open it; the flag (`replaced_count`) clears at the
  next `verify-keys`. If the registry holds a different ID for the same path +
  salt on a row flagged replaced, `verify-keys` restores the verified key's ID
  once that key opens the file, unless another row owns that ID. That run still
  reports `replaced` (exit 2); the next run reports `verified`.
- **Live queries:** every MCP tool and the CLI `search` open a fresh adapter
  (`open_adapter`: discovery, key check, schema check) per call, so new shards,
  new message tables and replaced databases are seen at once. Messages have one
  total order, `(sort, database_id, table, local_id)`, where `sort` is the
  creation time in seconds or a sentinel below every valid time; per-table
  keysets are merged in that order and only the rows of the requested page are
  decoded. Message tables that no conversation maps to are skipped by
  all-conversation queries and counted in `status.coverage`. A row that is
  gone from the source is simply absent; nothing is inferred from absence.
  A query can fail with `source_busy` while WeChat holds a write lock past the
  busy timeout.
- **Caches:** the query repository keeps in-memory maps (contacts and
  conversations, per-shard message IDs and senders, attachments, Moments).
  Each is keyed by the stat signature (database ID, inode, size, mtime of the
  `.db` and its `-wal`) of exactly the databases it was built from and is
  rebuilt lazily by the first query after a change. An ID resolved through a
  cache is re-derived from the decoded row before it is returned. Caches hold
  full adapter records; `public_record` redacts private fields at the response
  boundary. Nothing is written to disk.
- **Optional databases:** `message/message_fts.db` (WeChat's own full-text
  index) and `sns/sns.db` (Moments) are bound only when their key is verified
  and their tables have the expected columns. A problem with either only makes
  search fail with `text_index_unavailable` or the Moments tools fail with
  `moments_unavailable`; `status` reports `text_index_status` and
  `moments_status`. The FTS5 virtual tables are never queried, because their
  tokenizer exists only inside WeChat; only the plain `_content` tables are
  read.
- **Attachments:** files ≤ 200 MiB; images ≤ 50 MiB encoded/decoded and ≤ 40 M
  pixels. Static JPEG/PNG/WebP, legacy XOR and verified V1/V2 DAT decode;
  animated, WXGF and HEVC are unsupported. A thumbnail never stands in for an
  original, and a first frame is never labeled an original. Media key material
  is derived only from documented app-data locations or an owner-only
  `image-material.json` in private state; no debugger runs during reads, and
  database keys are never used as media keys.
- **Acceptance proof** (`prove`): a private selection lists `samples` for
  `message`, `contact`, `group`, `reply`, `original_image` and `ordinary_file`,
  each with explicit expected values; message samples pin conversation and
  database/table/local-row identity. Only `expectation_basis: user_confirmed`
  (set after independent confirmation) can pass; missing checks and
  automatically selected expectations cannot. Production JSON cannot assert
  `synthetic_fixture` ground truth or override the detected build. Detailed
  evidence goes to private `proofs/`; the public report omits sample content,
  identifiers, hashes and paths.
- **Errors:** every failure surfaces as a `BridgeError` code; CLI exit codes
  are listed in the [README](../README.md#commands). MCP codes are in
  [mcp.md](mcp.md#errors-and-trust).

## Tests

`tests/unit/` and `tests/integration/` use synthetic data only. `tests/support/`
provides the writable SQLCipher fixture engine (`encrypted.py`, never imported
by production), hand-authored schemas, sample builders, image generators, a
complete synthetic proof and a controlled setup backend. Native tests need the
SQLCipher library above; the suite has no skipped tests.
