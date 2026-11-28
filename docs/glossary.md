# Glossary

Terms in alphabetical order. Each entry links to the document that owns the
fact.

**Account root.** The WeChat data folder for one signed-in account. The bridge
reads its `db_storage` directory and its media folders, and never writes to it.
See [data-flow.md](data-flow.md).

**Adapter.** The code that interprets source rows for one WeChat build. The only
adapter covers 4.1.15 builds 270102 and 270135, 4.2.0 build 270390 and 4.2.1 build 270601. It turns rows into messages,
replies, contacts, groups and attachment references. See
[architecture.md](architecture.md#packages).

**Attachment ref.** A record that ties a message to a file or image resource,
with a kind, a representation (original or thumbnail) and an availability. The
bytes come from WeChat's media folders on request. See
[mcp.md](mcp.md#attachment-delivery).

**Availability.** An attachment's state: `local_unverified`, `not_local`,
`unsupported_format` or `identity_mismatch`. See [mcp.md](mcp.md#tools).

**Build.** WeChat's `CFBundleVersion` number, such as 270102. The adapter
accepts only listed builds. See [compatibility.md](compatibility.md).

**Coverage.** What the `status` tool reports about the source: message table
count, message tables no conversation maps to, conversations without local
messages, database count, and flags such as `complete_phone_history: false`.
See [mcp.md](mcp.md#tools).

**Cursor.** An opaque, authenticated paging token bound to a method, filters
and limit. It resumes from a keyset position in the live data, so rows written
after the first page can appear later. See [mcp.md](mcp.md#paging).

**Identity secret.** The 32-byte key in `identity.json` that derives every
opaque ID. Deleting it changes all IDs and invalidates cursors. See
[architecture.md](architecture.md#runtime-state).

**Incarnation.** One life of a source database: a relative path plus a salt. A
new salt is a new incarnation with a new database ID. A same-salt rewrite keeps
its ID and awaits key reverification. See
[architecture.md](architecture.md#key-contracts).

**Key manifest.** `keys.json`, the verified per-database keys. See
[setup.md](setup.md).

**LLDB capture.** The one-time step that runs a signed copy of WeChat under
Apple's debugger to obtain database keys. See [setup.md](setup.md).

**Source acceptance.** The check that the bridge's output matches what WeChat
shows. It needs a full-resolution original image and independently confirmed
samples, and it is blocked. Results are best effort until it passes. See
`prove` in [architecture.md](architecture.md#key-contracts).

**MCP.** Model Context Protocol. The bridge serves thirteen read-only tools to a
client over stdio. See [mcp.md](mcp.md).

**Opaque ID.** An identifier of the form `<kind>_<64 hex>`, derived by keyed
HMAC-SHA256. It stays stable while the identity secret and the source
incarnation stay the same. See [mcp.md](mcp.md#tools).

**Plan.** A one-hour setup record under `setup/<plan-id>/` that names the app
copy, the databases and the recovery state. See [setup.md](setup.md#procedure).

**Proof (`prove`).** The maintainer command that checks private, user-confirmed
samples against the source. The public report omits content and identifiers.
See [architecture.md](architecture.md#key-contracts).

**Representation.** `original`, `thumbnail`, `source` or `decoded`. The server
keeps the label with the bytes and never substitutes one for another. See
[mcp.md](mcp.md#attachment-delivery).

**Shard.** One `message/message_<n>.db` database. The number of required
databases grows with the shard count. See
[architecture.md](architecture.md#key-contracts).

**State directory.** The private folder for keys, identity, inventory, setup
plans and locks. The default is `~/Library/Application Support/WechatBridge`. See
[architecture.md](architecture.md#runtime-state).

**Text index.** WeChat's own full-text index, `message/message_fts.db`. The
bridge reads its plain-text content tables for `search_messages` and never
queries the FTS5 tables themselves. Messages WeChat does not index, such as
system messages, are not searchable. See [mcp.md](mcp.md#search).

**Verified keys.** Saved keys that authenticated a schema read of their
database. Ordinary reads use only these. Verification does not check every
page. See [architecture.md](architecture.md#key-contracts).

**WAL / SHM.** SQLite's write-ahead log (`-wal`) and shared-memory index
(`-shm`) beside a database. SQLite appends recent writes to the WAL before it folds them into the main file.
SQLCipher merges it on read, and the bridge never merges pages itself. See
[architecture.md](architecture.md#key-contracts).

**WeChat.disabled.** The temporary app copy that setup disables after capture,
kept under `setup/<plan-id>/` as a recovery artifact. See
[uninstall.md](uninstall.md).

**WXGF.** WeChat's newer proprietary image container. The bridge cannot decode
it and returns the raw file with a label. See
[compatibility.md](compatibility.md).
