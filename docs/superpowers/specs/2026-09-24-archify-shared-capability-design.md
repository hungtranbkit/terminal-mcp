# Shared Archify Capability Design

## Purpose

Terminal MCP will expose Archify as a shared, durable dashboard capability. An
operator can discover repositories within the existing allowed-root boundary,
select a repository and one of Archify's five diagram types, add an optional
focus prompt, start a background job, return later, and open the persisted
self-contained HTML diagram in the dashboard.

The first acceptance path is a source-backed architecture diagram of this
`terminal-mcp` repository. The feature must never invent a package, module, or
relationship that source inspection did not find.

## Scope

The feature includes:

- allowed-project discovery;
- source-evidence collection with file, byte, and time limits;
- deterministic typed Archify IR authoring for architecture, workflow,
  sequence, dataflow, and lifecycle views;
- an optional prompt used only to rank/filter verified evidence and title the
  result, never to add unverified nodes;
- a shared Archify runtime dependency probe;
- durable asynchronous jobs with restart recovery;
- persisted JSON IR, metadata, and rendered HTML;
- dashboard list/create/status/preview flows;
- API, persistence, security, renderer, and browser tests;
- configuration and operator documentation.

It does not add an LLM provider, install Archify from a request, edit target
repositories, expose arbitrary files, or synthesize a diagram when the real
Archify runtime is unavailable.

## Architecture

### Configuration and shared runtime

`ArchifyConfig` is part of the main Terminal MCP configuration. It contains an
`enabled` flag, optional absolute `runtime_dir`, optional `allowed_roots`,
artifact/database locations, and bounded limits. Empty `allowed_roots` reuses
the effective Repo Read roots so Archify never introduces a wider project
boundary.

The runtime is shared by the Terminal MCP process, not installed into or under
any selected project. `ArchifyRuntime` resolves, in order:

1. configured `runtime_dir`;
2. `TERMINAL_MCP_ARCHIFY_HOME`;
3. the managed shared cache directory beside Terminal MCP state.

A runtime is ready only when Node.js is version 18 or newer, the runtime's
`bin/archify.mjs` exists, and `doctor` succeeds within the configured timeout.
Status reports `disabled`, `runtime_missing`, `node_missing`, `node_too_old`,
`doctor_failed`, or `ready` with a human-actionable message. Generation never
downloads dependencies and never substitutes fake HTML.

### Project discovery and source evidence

`ArchifyProjectPolicy` canonicalizes configured roots and selected paths with
`Path.resolve()`. It rejects `/`, relative roots, missing/non-directory paths,
symlink escapes, nested path traversal, and selections not beneath an allowed
root. Discovery scans only a bounded depth for directories containing `.git`
or a recognized project manifest. It returns stable display data without
enumerating arbitrary host paths.

`SourceInspector` reads a bounded set of non-secret source/config files while
skipping `.git`, dependency/vendor/build/cache directories and the existing
credential filename/glob deny-list. It records repo-relative paths and facts:

- project/package/module names proven by manifests and source paths;
- import/dependency edges proven by language import syntax;
- entry points proven by packaging or executable declarations;
- state names/transitions proven by enum/constants and explicit transition
  maps;
- file evidence attached to every emitted node and relationship.

The inspector never follows symlinks outside the project, never reads more
than the configured file/byte caps, and returns a clear `insufficient_evidence`
failure when a requested diagram type cannot be supported. The prompt may
boost facts whose names/paths contain prompt terms and may become a subtitle;
it cannot create facts.

### Typed IR

`ArchifyAuthor` maps the evidence graph into the five current Archify schemas:

- `architecture`: verified packages/modules as components and imports as
  connections;
- `workflow`: verified entry points and their reachable import/call path as
  lanes/nodes/edges;
- `sequence`: a bounded verified caller-to-callee chain as participants and
  messages;
- `dataflow`: verified source/config/transport/store facts and their explicit
  edges; otherwise the job fails as insufficient evidence;
- `lifecycle`: verified state constants and explicit transition pairs;
  otherwise the job fails as insufficient evidence.

New workflows use schema version 2. Other types use the current schema version
accepted by the shared runtime. IDs are generated deterministically from
verified fact keys. Metadata records the inspected commit when Git can provide
one, the exact relative source files, truncation flags, and the prompt.

### Durable jobs and artifacts

`ArchifyStore` owns one SQLite database in WAL mode. A job has a random ID,
canonical project path, project label, diagram type, prompt, status
(`queued`, `running`, `completed`, `failed`), timestamps, dependency/evidence
error fields, and relative artifact names. Inserts happen before the worker is
scheduled.

`ArchifyService` uses a bounded process-wide executor. At startup it changes
orphaned `running` jobs to `failed` with code `interrupted`; queued rows remain
queued and are resubmitted. A job writes into a private per-job directory
beneath the shared artifact root. IR and metadata use atomic replacement;
HTML is accepted only after Archify returns success and the output is a regular
file below that exact job directory. Subprocesses use argument arrays, a fixed
working directory, captured bounded output, an explicit timeout, and no shell.

History is ordered newest first and capped per request. No delete endpoint is
added in this increment.

### Dashboard and routes

The standalone `/dashboard/archify` page follows the existing shared dashboard
navigation and authentication conventions. The main dashboard navigation adds
one Archify link without altering existing session behavior.

Routes:

- `GET /dashboard/archify` — page;
- `GET /dashboard/api/archify/status` — configuration/runtime state and limits;
- `GET /dashboard/api/archify/projects` — discovered allowed projects;
- `GET /dashboard/api/archify/jobs?limit=N` — recent durable jobs;
- `GET /dashboard/api/archify/jobs/{job_id}` — one job and metadata;
- `POST /dashboard/api/archify/jobs` — validate and persist a new job;
- `GET /dashboard/api/archify/jobs/{job_id}/html` — same-origin inline preview.

GET routes use `_read_guard`; the POST route uses `_mutation_guard`. The create
request accepts only `project`, an enumerated diagram type, and a UTF-8 prompt
up to the configured character cap. HTML lookup uses the stored job ID and
stored relative artifact name, never a caller-provided path. The response uses
`Content-Security-Policy: sandbox allow-scripts` and no-store headers; the page
previews it in a sandboxed iframe and also offers an explicit open link.

The UI shows dependency health before generation, disables Generate when the
runtime is unavailable, polls active jobs, keeps completed/failed history after
refresh, and displays structured failure messages instead of manufactured
output.

## Error handling

Errors have stable codes: `ARCHIFY_DISABLED`, `ARCHIFY_UNAVAILABLE`,
`PROJECT_NOT_ALLOWED`, `PROJECT_NOT_FOUND`, `INVALID_DIAGRAM_TYPE`,
`PROMPT_TOO_LONG`, `INSUFFICIENT_EVIDENCE`, `GENERATION_TIMEOUT`,
`GENERATION_FAILED`, `ARTIFACT_MISSING`, and `JOB_NOT_FOUND`. API responses do
not include unbounded subprocess output or host paths outside already-selected
allowed projects.

## Security and limits

- No shell command strings; subprocess arguments are discrete values.
- Selected projects and artifacts are resolved and containment-checked after
  following symlinks.
- Secret globs and dependency/build directories are never inspected.
- Defaults: 2 discovery levels, 2,000 candidate entries, 500 inspected files,
  4 MiB source bytes, 4,000 prompt characters, 120 second generation timeout,
  64 KiB captured subprocess output, 100 history rows, and one concurrent job.
- The target repository is read-only; all artifacts live in shared Terminal MCP
  state.
- Runtime installation/update is an explicit operator action outside request
  handling.

## Testing and verification

Tests use real temporary repositories and SQLite stores. A small fake Archify
runtime is permitted only at the subprocess boundary to make timeout/error
tests deterministic; authoring and persistence remain real. A checked-out
official Archify runtime is used for local integration/browser verification.

Coverage includes root validation, symlink/traversal escapes, secret skipping,
source-backed facts, unsupported evidence, runtime status, safe argv,
timeouts, durable restart recovery, history refresh, artifact containment,
route guards, and existing dashboard regression tests.

Browser verification starts a disposable dashboard, selects the real
`terminal-mcp` repository, creates an architecture job, waits for completion,
loads the HTML in the iframe, opens its URL, refreshes, and confirms the job
remains in history. Existing dashboard home/session controls are smoke-checked
before and after.

## Documentation and completion

The feature matrix entry in `docs/REQUIREMENTS.md` is updated in place with
the evidence actually obtained. `docs/CHATGPT_USAGE.md`, `config.example.yaml`,
and the README explain runtime provisioning, routes, limits, state locations,
and dependency failures. Completion requires all relevant tests, full default
pytest, browser verification, a clean review, and a commit on
`feat/archify-ui`.
