# Ghi chú / Ý tưởng (Notes / Ideas)

A cross-project, local-first store for things worth keeping: an idea, a
link, a screenshot, a piece of ChatGPT analysis. The user is mid-chat, says
**"lưu lại"** / **"ghi chú cái này"** / **"đưa vào kho ý tưởng"**, and the
model calls one tool. Later the same material is findable — from ChatGPT
(`note_search`) or from a browser (`/dashboard/notes`).

It is deliberately **not** tied to MESFlow, SubsVid or any other project.
`project_id`/`project_name` are optional and can be attached later, because
a note is usually captured before anyone knows where it belongs.

## Design in one paragraph

SQLite + FTS5 for text and metadata (`notes_store.py`), the real
filesystem for attachment bytes (`notes_service.py`), and one shared
service instance behind both surfaces — the `note_*` MCP tools
(`mcp_app.py`) and the `/dashboard/notes` page plus its JSON routes
(`dashboard.py`). No Redis, no Postgres, no vector database, no embedding
service: V1 is fully local and deterministic, and search returns the same
ranking every time. Images are never base64 blobs in the database — the DB
holds only metadata plus a `storage_path`.

| Concern | Where |
| --- | --- |
| Schema, CRUD, filters, FTS5/bm25 search | `terminal_mcp/notes_store.py` |
| Attachment bytes, MIME sniffing, path confinement | `terminal_mcp/notes_service.py` |
| MCP tools (`note_*`) | `terminal_mcp/mcp_app.py` |
| Web page + JSON routes | `terminal_mcp/dashboard.py` (`NOTES_HTML`) |
| Config section | `terminal_mcp/config.py` (`NotesConfig`) |

## Data model

`notes`

| Field | Notes |
| --- | --- |
| `id` | `note_<uuid4 hex>`, stable |
| `title` | derived from the content if omitted (never blank) |
| `summary` | short human summary |
| `original_content` | what the user wanted kept, verbatim |
| `analysis` | the model's useful explanation — a separate field on purpose |
| `source_url` | nullable |
| `source_chat`, `source_session` | free-text provenance, provider-independent |
| `type` | `idea` \| `reference` \| `todo` \| `research` \| `prompt` \| `design` \| `other` |
| `status` | `new` \| `reviewing` \| `planned` \| `applied` \| `archived` |
| `tags` | JSON array; case-insensitive dedupe, first spelling wins |
| `project_id`, `project_name` | nullable, settable later |
| `created_at`, `updated_at` | ISO-8601 UTC |
| `applied_at`, `applied_ref` | stamped by `note_mark_applied`, cleared if the status moves back |
| `deleted_at` | soft delete; the note leaves every listing *and* the search index |

`note_attachments`: `id`, `note_id`, `filename` (display only), `mime_type`,
`size`, `sha256`, `storage_path`, `width`, `height`, `created_at`.

The schema is created by a tracked, ordered migration
(`NOTES_MIGRATIONS` + `schema.apply_migrations`, `PRAGMA user_version`), so
reopening an existing DB applies nothing and future changes append a
`Migration(2, ...)` rather than an untracked `ALTER TABLE`.

**Status is shown in Vietnamese in the UI** (`Mới` / `Đang xem` / `Sẽ làm` /
`Đã áp dụng` / `Lưu trữ`); the stored values stay the English enum above.

## Snapshot philosophy

A note is designed to outlive its source. Save the URL *and* the content
you care about (`original_content`), *and* your reading of it (`analysis`),
*and* a screenshot if there is one. When the page 404s a year later, the
note is still complete. V1 does not drive a browser to capture screenshots
itself (this project has no browser service); it accepts a screenshot that
ChatGPT or the user attaches.

## MCP tools

All local to the controller — an idea has no node, so nothing here is
routed per-node. Every tool answers with a dict; errors are a stable
`error` code plus a `message`, never an exception.

| Tool | What it does |
| --- | --- |
| `note_create` | save a note (optionally with attachments in the same call) |
| `note_get` | one note in full, with its attachments |
| `note_search` | ranked full-text search (title + summary + original_content + analysis + tags) |
| `note_list` | browse/filter/paginate with no text query |
| `note_update` | partial update — only the fields you pass |
| `note_delete` | soft by default; `hard=true` also unlinks the image files |
| `note_restore` | undo a soft delete |
| `note_add_attachment` | attach an image by `data_base64` or `source_path` |
| `note_remove_attachment` | detach one image and delete its file |
| `note_link_to_project` | attach the note to a project later |
| `note_mark_applied` | status → `applied`, stamp `applied_at`, record where it landed |
| `note_facets` | the type/status/tag/project values that actually exist |

### Example: the whole ChatGPT workflow

The user pastes a screenshot and a link, ChatGPT analyses it, the user says
"lưu ý tưởng này":

```json
// note_create
{
  "title": "Landing page MESFlow — hero + social proof",
  "original_content": "https://example.com/inspiration\nUser: nhìn cái hero này hay, chữ to, 1 CTA duy nhất",
  "analysis": "Pattern: hero một cột, H1 ngắn, 1 CTA, social proof ngay dưới fold. Áp cho MESFlow thì thay proof bằng số máy đang chạy realtime.",
  "source_url": "https://example.com/inspiration",
  "source_chat": "chatgpt:thread-42",
  "type": "idea",
  "tags": ["mesflow", "landing"],
  "attachments_base64": [
    {"filename": "hero.png", "data_base64": "iVBORw0KGgoAAAANSUhEUg..."}
  ]
}
```

```json
// response (abridged)
{
  "id": "note_9f2c1e77...",
  "title": "Landing page MESFlow — hero + social proof",
  "status": "new",
  "type": "idea",
  "tags": ["mesflow", "landing"],
  "attachment_count": 1,
  "attachments": [
    {"id": "att_4b1e...", "filename": "hero.png", "mime_type": "image/png",
     "size": 84213, "width": 1440, "height": 900, "sha256": "9c1f...",
     "url": "/dashboard/api/notes/attachment?id=att_4b1e..."}
  ],
  "attachment_results": [{"id": "att_4b1e...", "...": "..."}]
}
```

Six weeks later — *"trước đây tôi có lưu ý tưởng nào về landing page MESFlow không?"*:

```json
// note_search  {"query": "landing page MESFlow"}
{
  "query": "landing page MESFlow",
  "ranked": true,
  "total": 1,
  "items": [
    {"id": "note_9f2c1e77...", "rank_position": 1, "score": 8.4127,
     "title": "Landing page MESFlow — hero + social proof",
     "excerpt": "…hero một cột, H1 ngắn, 1 CTA, social proof ngay dưới fold…",
     "tags": ["mesflow", "landing"], "status": "new",
     "attachments": [{"url": "/dashboard/api/notes/attachment?id=att_4b1e..."}]}
  ]
}
```

Order by `rank_position` (1 = best). `score` is bm25, sign-flipped so
higher is more relevant; with only one matching document bm25's idf term
is 0, which is why `rank_position` — not `score` — is the signal to trust.
Search is diacritics-insensitive: `y tuong` finds `ý tưởng`.

When it gets used:

```json
// note_mark_applied
{"note_id": "note_9f2c1e77...", "applied_ref": "commit 8f54fc7", "project_name": "MESFlow"}
```

### Attachments: the honest transport story

This MCP runtime has **no binary channel**, so there are exactly three real
ways bytes get in, and all three are tested:

1. **`data_base64`** — in-band string (a `data:` URI is accepted too).
   Decoded server-side and written straight to a file. Size-capped on the
   encoded length *before* the decode allocates.
2. **`source_path`** — an absolute path to a file already on this host.
   **Refused outright (`ATTACHMENT_SOURCE_DISABLED`) unless the operator
   configures `notes.attachment_source_roots`**, and then only inside those
   roots. Symlinks are resolved before the check.
3. **The dashboard's own upload form** — multipart, for a human dragging a
   screenshot onto the page.

The stored type is decided by the **file's own magic bytes**, never by the
filename or a declared content-type; a declared type that disagrees is
refused (`ATTACHMENT_MIME_MISMATCH`) rather than quietly corrected. The
on-disk name is the attachment's own uuid, so no caller-supplied string
ever becomes part of a path.

### Error codes

`NOTE_NOT_FOUND` (404) · `NOTE_DELETED` (410) · `EMPTY_NOTE` ·
`INVALID_TYPE` · `INVALID_STATUS` · `INVALID_TAGS` · `INVALID_SORT` ·
`PROJECT_REQUIRED` · `NOTHING_TO_UPDATE` · `ATTACHMENT_NOT_FOUND` (404) ·
`ATTACHMENT_FILE_MISSING` (410) · `ATTACHMENT_TOO_LARGE` (413) ·
`ATTACHMENT_MIME_NOT_ALLOWED` (415) · `ATTACHMENT_MIME_MISMATCH` (415) ·
`ATTACHMENT_EMPTY` · `ATTACHMENT_BAD_BASE64` ·
`ATTACHMENT_TRANSPORT_REQUIRED` · `ATTACHMENT_SOURCE_DISABLED` (403) ·
`ATTACHMENT_SOURCE_NOT_ALLOWED` (403) · `ATTACHMENT_SOURCE_NOT_ABSOLUTE` ·
`ATTACHMENT_SOURCE_NOT_FOUND` · `ATTACHMENT_SOURCE_NOT_A_FILE` ·
`ATTACHMENT_PATH_OUTSIDE_STORE` (403) · `NOTES_DISABLED` (503).

## Web UI — `/dashboard/notes`

Vietnamese, desktop + mobile (one breakpoint at 860px; the board collapses
to a single column, the drawer goes full-screen).

- A big search bar (*"Tìm trong các ý tưởng đã lưu…"*), debounced.
- Filters: **Loại / Trạng thái / Tag / Dự án / Từ ngày / Đến ngày**, plus
  sort (mới nhất by default) and a "hiện cả lưu trữ" toggle. Dropdown
  values come from `/dashboard/api/notes/facets` — real data, not a
  hardcoded list.
- Three views: **Thư viện** (gallery — the image leads), **Danh sách**
  (compact, for research/text), **Bảng** (Kanban: Mới → Đang xem → Sẽ làm →
  Đã áp dụng, drag-and-drop on desktop, per-card buttons on touch).
- Detail drawer: every field editable, attachment add (drag-and-drop or
  file picker) and remove, *"Đánh dấu đã áp dụng"*, delete with confirm.

### Routes

| Route | Method | Guard |
| --- | --- | --- |
| `/dashboard/notes` | GET | `_read_guard` |
| `/dashboard/api/notes` | GET | `_read_guard` — list, or search when `?q=` |
| `/dashboard/api/notes/facets` | GET | `_read_guard` |
| `/dashboard/api/notes/note?id=` | GET | `_read_guard` |
| `/dashboard/api/notes/attachment?id=` | GET | `_read_guard` |
| `/dashboard/api/notes/create` | POST | `_mutation_guard` |
| `/dashboard/api/notes/update` | POST | `_mutation_guard` |
| `/dashboard/api/notes/mark-applied` | POST | `_mutation_guard` |
| `/dashboard/api/notes/delete` | POST | `_mutation_guard` |
| `/dashboard/api/notes/restore` | POST | `_mutation_guard` |
| `/dashboard/api/notes/attachment/upload` | POST | `_mutation_guard` |
| `/dashboard/api/notes/attachment/remove` | POST | `_mutation_guard` |

`_mutation_guard` = `dashboard.mutations_enabled` + same-origin CSRF check +
Cloudflare Access JWT (when configured). Exactly the boundary every other
dashboard mutation already goes through. **Every route above additionally
passes `_notes_auth_guard`** — see "Authentication" below.

## Authentication (application layer, not just the edge)

**The Notes surface requires a real session in this process.** Every route in
the table above — the page, the JSON API, and attachment serving — is behind
`notes.require_auth`, which defaults to **true**.

Why this is not left to Cloudflare Access: `cloudflared` connects to this
process over **loopback**, so once a tunnel request arrives it is
indistinguishable from a local one. Edge-only Access therefore says nothing
to the application about the request in front of it — the exact gap
`cf_access.py`'s own docstring warns about — and anything else that can reach
the port (a tailnet peer, another process on the host) reads everything.
Notes hold whatever the operator chose to keep, so that posture is wrong for
them even though it is the historical one for the rest of `/dashboard/*`.

**No third mechanism was added.** The guard accepts either of the two
identities this project already has:

1. a **webauth session cookie** (`webauth.py` — the `/login` path,
   scrypt-hashed local account, 12h sessions, rate-limited), resolved through
   the very same `WebAuthStore` that `/app/*` uses, so `terminal-mcp-webauth`,
   `/logout`, and session expiry already control Notes access with no new
   tooling; or
2. a **verified Cloudflare Access assertion** (`cf_access.py`), when
   `dashboard.cloudflare_access_team_domain` *and*
   `dashboard.cloudflare_access_audience` are configured app-side.

Refusals: a browser hitting the page with no session gets **303 → `/login`**;
every API route answers **401 `LOGIN_REQUIRED`** (JSON, so the page's own
`fetch()` can show "Phiên đăng nhập đã hết" with a link back rather than
following a redirect into HTML). A user whose account is flagged
`must_change_password` gets **403 `PASSWORD_CHANGE_REQUIRED`** — the same rule
`webauth_dashboard._require_session_api` applies. **It fails closed**: a
deployment that forgets to pass the store still refuses everything.

This composes with, and never replaces, the pre-existing guards — CSRF
(`ORIGIN_NOT_ALLOWED`), `dashboard.mutations_enabled`, and
`notes.enabled` (503) are all still checked, and a valid session does not
bypass any of them. It also changes nothing about the other `/dashboard/*`
routes: this adds a boundary to the Notes surface only.

To log in the first time, `server_http.py` writes a one-time bootstrap
password to `~/.local/state/terminal-mcp/webauth-bootstrap.txt` (mode 600) on
first start if no account exists; `terminal-mcp-webauth set-password <user>`
changes it.

### What this does NOT cover: the MCP surface

The `note_*` MCP tools have **no per-tool authentication**, and that is
deliberate. The entire `/mcp` transport — all 214 tools, including
`terminal_send_text`, which types into a live agent session — is protected by
transport-level controls only: loopback binding, `LanCidrGuardMiddleware`'s
CIDR allowlist on any LAN/overlay socket, and the requirement that remote
access arrive through an authenticated HTTPS tunnel. Bolting a notes-specific
credential onto that one tool family would be a second, bespoke mechanism
guarding the *least* dangerous tools on the surface, while
`terminal_send_text` stayed open — security theater, not security.

So the honest statement of residual risk: **anything permitted to speak MCP to
this controller can read and write notes.** On this host that means loopback
plus tailnet peers inside `100.64.0.0/10`. Narrowing it is an MCP-transport
decision for the whole tool surface (per-client tokens or an authenticating
proxy in front of `/mcp`), not something the Notes feature should solve alone.

## Security posture

- **MIME by content, not by name.** A shell script named `.png` is refused.
- **No user-controlled path component.** Files are stored as
  `<attachments_dir>/YYYY/MM/<attachment-uuid>.<ext>`; the original filename
  lives in the DB for display only. Traversal is structurally impossible,
  not filtered.
- **Confined reads, both ways.** `source_path` must resolve inside a
  configured root (after symlink resolution). Serving re-verifies that the
  DB's `storage_path` is still inside the attachment directory, so a
  tampered row cannot turn the endpoint into an arbitrary-file reader — and
  `remove` refuses to unlink anything outside the store.
- **Atomic writes.** Temp file in the same directory, `fsync`, then
  `os.replace`. A crash leaves a stray temp file, never a truncated image a
  note already points at. A failed metadata insert rolls the file back.
- **Serving headers.** `nosniff`, a Content-Type from the allowlist,
  `Content-Security-Policy: default-src 'none'; sandbox`, and there is no
  static mount for the attachment directory anywhere — the only way out is
  the by-id route.
- **Files are `0600`, the DB is `0600`.**
- **Logging is deliberately thin**: ids, types, sizes, counts. Note text and
  filenames of user content are never logged.
- **Single-user, stated plainly.** This dashboard has one operator identity
  (webauth / Cloudflare Access in front of it), so a note has no owner
  column and every authenticated caller sees every note. That is a real
  limitation, not an oversight — a half-built multi-tenant model (an owner
  column nothing enforces) would read like a boundary while enforcing
  nothing. Adding real per-user scoping later is one migration plus a
  filter in `_filter_sql`.

## Configuration

Defaults work with no configuration at all. See the `notes:` block in
`config.example.yaml`.

| Key | Default | Meaning |
| --- | --- | --- |
| `notes.enabled` | `true` | `false` removes the `note_*` tools and makes the routes answer 503 |
| `notes.require_auth` | `true` | application-layer auth on every Notes HTTP route; `false` returns them to the unauthenticated posture of the rest of `/dashboard/*` |
| `notes.attachments_dir` | *(empty)* | empty = beside the notes DB |
| `notes.max_attachment_bytes` | `10485760` | 10 MiB |
| `notes.allowed_mime_types` | png, jpeg, webp, gif | must be types the server can sniff *and* serve |
| `notes.attachment_source_roots` | *(empty)* | empty = `source_path` transport disabled |

Environment overrides (same three-step resolution as every other store in
this project — env var → `XDG_STATE_HOME` → `~/.local/state`):

- `TERMINAL_MCP_NOTES_DB` — the SQLite file.
- `TERMINAL_MCP_NOTES_ATTACHMENTS_DIR` — the image directory.

Default locations:

```
~/.local/state/terminal-mcp/notes.db
~/.local/state/terminal-mcp/notes_attachments/YYYY/MM/<attachment-uuid>.<ext>
```

## Backup and restore

A note has two halves and a backup that covers one of them is not a
backup. Stop nothing — SQLite is in WAL mode, so use its own online backup:

```bash
STATE=~/.local/state/terminal-mcp
OUT=~/backups/notes-$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT"

# 1. The database, consistently (never `cp` a live WAL database).
sqlite3 "$STATE/notes.db" ".backup '$OUT/notes.db'"

# 2. The images.
tar -C "$STATE" -czf "$OUT/notes_attachments.tar.gz" notes_attachments

# 3. Verify before trusting it.
sqlite3 "$OUT/notes.db" "PRAGMA integrity_check; SELECT COUNT(*) FROM notes;"
```

Restore is the reverse: put `notes.db` and `notes_attachments/` back in the
same state directory. If the two halves came from different moments and the
search index looks stale, rebuild it from the table — no data is touched:

```python
from terminal_mcp.notes_store import NotesStore
print(NotesStore().reindex_all(), "notes reindexed")
```

## Running it

Nothing extra to start: the notes surface is part of the normal server.

```bash
# HTTP + dashboard
.venv/bin/terminal-mcp-http          # or: systemctl --user restart terminal-mcp-http
# then open http://127.0.0.1:8766/dashboard/notes

# stdio (the same note_* tools)
.venv/bin/terminal-mcp
```

Tests:

```bash
.venv/bin/python -m pytest tests/test_notes_store.py tests/test_notes_attachments.py \
    tests/test_notes_mcp_tools.py tests/test_notes_dashboard.py tests/test_notes_config.py -q
```

## Limitations (V1)

- Single-user, as described above — one local account gates the whole store;
  there is no per-user scoping of individual notes.
- The MCP tool surface is transport-authenticated only (see above).
- Controller-local: notes are not replicated across nodes (an idea has no
  node; the controller is the one place it lives).
- No semantic/vector search. FTS5 bm25 only — deliberate: V1 must not add a
  cloud or LLM dependency to work. Embeddings could be layered on later
  behind the same `note_search` contract.
- No automatic screenshot capture of a saved URL (no browser service in
  this project); the model or the user attaches the image.
- Attachments are images only (png/jpeg/webp/gif) — the types the UI can
  actually preview.
