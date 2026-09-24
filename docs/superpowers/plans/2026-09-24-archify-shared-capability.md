# Shared Archify Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a source-backed, durable Archify generation service and dashboard page that persists safe HTML diagrams outside selected repositories.

**Architecture:** Focused `archify_*` modules own configuration/policy, evidence authoring, runtime execution, persistence, and orchestration. `dashboard.py` only registers authenticated routes and renders a standalone page; `server_http.py` owns process-lifetime construction and shutdown.

**Tech Stack:** Python 3.11, SQLite/WAL, `concurrent.futures`, Starlette routes through MCPServer, vanilla dashboard HTML/CSS/JS, Node.js 18+, official `tt-a1i/archify` CLI.

**Spec:** `docs/superpowers/specs/2026-09-24-archify-shared-capability-design.md`

## Global Constraints

- Archify runtime and generated artifacts are shared Terminal MCP state, never stored per project.
- Empty Archify allowed roots reuse Repo Read's effective allowed roots and never widen them.
- Source facts must be proven by inspected files; a prompt may rank facts but cannot create them.
- Generation never downloads dependencies and never emits substitute or fake HTML.
- Subprocesses use argument arrays, no shell, bounded output, and a 120-second default timeout.
- Selected project paths and artifact paths are resolved and containment-checked after symlink resolution.
- Existing dashboard behavior and route guards remain unchanged.

## Review Focus

- A project symlink or `..` selection escaping an allowed root must return `PROJECT_NOT_ALLOWED` without reading it; Task 1 tests both forms.
- A completed job whose database artifact path is tampered must return `ARTIFACT_MISSING`, never serve another file; Task 3 tests this.
- A process restart with `running` and `queued` rows must fail the orphan and resume the queued job exactly once; Task 3 tests this.
- A prompt naming a nonexistent module must not put that module into IR or HTML; Task 2 tests this.
- A missing/broken runtime must keep history readable, disable generation, and expose a clear state; Tasks 2 and 4 test service and route behavior.

---

### Task 1: Configuration and allowed-project policy

**Files:**
- Create: `terminal_mcp/archify_policy.py`
- Modify: `terminal_mcp/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_archify_policy.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ArchifyConfig`, `ArchifyProjectPolicy`, `ProjectInfo`, `resolve_project(path)`, and `discover_projects()`.
- Consumed by: Tasks 2-4.

- [ ] **Step 1: Write failing config and path-policy tests**

```python
def test_archify_roots_must_be_absolute_and_not_filesystem_root():
    with pytest.raises(ValueError, match="archify.allowed_roots"):
        validate_config({"archify": {"allowed_roots": ["/"]}})

def test_project_selection_rejects_dot_dot_and_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / ".git").mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    policy = ArchifyProjectPolicy((str(allowed),))
    with pytest.raises(ArchifyPolicyError, match="PROJECT_NOT_ALLOWED"):
        policy.resolve_project(allowed / ".." / "outside")
    with pytest.raises(ArchifyPolicyError, match="PROJECT_NOT_ALLOWED"):
        policy.resolve_project(allowed / "escape")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_archify_policy.py tests/test_config.py -k archify`
Expected: collection/import failure because the Archify policy and config do not exist.

- [ ] **Step 3: Implement strict configuration and canonical discovery**

```python
@dataclass(frozen=True)
class ArchifyConfig:
    enabled: bool = True
    runtime_dir: str = ""
    allowed_roots: tuple[str, ...] = ()
    max_projects: int = 200
    max_discovery_depth: int = 2
    max_files: int = 500
    max_source_bytes: int = 4 * 1024 * 1024
    max_prompt_chars: int = 4000
    timeout_seconds: float = 120.0
    max_output_bytes: int = 64 * 1024
    history_limit: int = 100
    workers: int = 1
```

Implement containment with `candidate.resolve().is_relative_to(root.resolve())`, reject `/`, and discover only manifest/`.git` directories beneath bounded roots. Add documented example config.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `pytest -q tests/test_archify_policy.py tests/test_config.py -k 'archify or repo_read'`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add terminal_mcp/archify_policy.py terminal_mcp/config.py config.example.yaml tests/test_archify_policy.py tests/test_config.py
git commit -m "feat(archify): secure project discovery"
```

### Task 2: Source evidence, typed authoring, and shared runtime

**Files:**
- Create: `terminal_mcp/archify_source.py`
- Create: `terminal_mcp/archify_runtime.py`
- Test: `tests/test_archify_source.py`
- Test: `tests/test_archify_runtime.py`

**Interfaces:**
- Consumes: `ArchifyConfig` and canonical project paths from Task 1.
- Produces: `SourceInspection`, `ArchifyAuthor.build(diagram_type, inspection, prompt)`, `ArchifyRuntime.status()`, and `ArchifyRuntime.render(...)`.
- Consumed by: Task 3.

- [ ] **Step 1: Write failing source-backed authoring tests**

```python
def test_architecture_uses_only_modules_and_imports_found_in_source(repo):
    (repo / "demo").mkdir()
    (repo / "demo" / "api.py").write_text("from demo import store\n")
    (repo / "demo" / "store.py").write_text("VALUE = 1\n")
    inspection = SourceInspector(max_files=20, max_bytes=10000).inspect(repo)
    ir = ArchifyAuthor().build("architecture", inspection, "include imaginary billing")
    labels = {item["label"] for item in ir["components"]}
    assert labels == {"demo.api", "demo.store"}
    assert "billing" not in json.dumps(ir).lower()

def test_lifecycle_without_explicit_states_fails_instead_of_inventing(repo):
    (repo / "main.py").write_text("print('hello')\n")
    with pytest.raises(ArchifyEvidenceError, match="INSUFFICIENT_EVIDENCE"):
        ArchifyAuthor().build("lifecycle", SourceInspector().inspect(repo), "")
```

- [ ] **Step 2: Run source tests and verify RED**

Run: `pytest -q tests/test_archify_source.py`
Expected: import failure because the evidence inspector does not exist.

- [ ] **Step 3: Implement bounded inspection and deterministic IR**

Implement secret/vendor exclusion, language import parsing, entry-point/state/transition evidence, stable IDs, prompt-term ranking, and schema-valid structures for all five types. Every IR node and relationship keeps repo-relative evidence in metadata or source fields accepted by that schema.

- [ ] **Step 4: Run source tests and verify GREEN**

Run: `pytest -q tests/test_archify_source.py`
Expected: all source evidence tests pass.

- [ ] **Step 5: Write failing runtime dependency and argv tests**

```python
def test_missing_shared_runtime_is_reported_without_output(tmp_path):
    runtime = ArchifyRuntime(tmp_path / "missing")
    assert runtime.status()["state"] == "runtime_missing"
    with pytest.raises(ArchifyRuntimeError, match="ARCHIFY_UNAVAILABLE"):
        runtime.render("architecture", tmp_path / "in.json", tmp_path / "out.html")

def test_render_passes_paths_as_discrete_arguments(fake_runtime, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append((argv, kw)) or completed(0))
    ArchifyRuntime(fake_runtime).render("architecture", tmp_path / "a b.json", tmp_path / "out.html")
    assert seen[0][0][2:4] == ["render", "architecture"]
    assert seen[0][1]["shell"] is False
```

- [ ] **Step 6: Run runtime tests and verify RED**

Run: `pytest -q tests/test_archify_runtime.py`
Expected: import failure because the runtime adapter does not exist.

- [ ] **Step 7: Implement runtime probe and renderer**

Use `node --version`, `node <runtime>/bin/archify.mjs doctor`, then `node ... render <type> <ir> <html> --quality standard`, adding `--repo-root` only for architecture. Clamp captured text and convert timeout/nonzero/missing-output failures into stable errors.

- [ ] **Step 8: Run runtime tests and verify GREEN**

Run: `pytest -q tests/test_archify_runtime.py tests/test_archify_source.py`
Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add terminal_mcp/archify_source.py terminal_mcp/archify_runtime.py tests/test_archify_source.py tests/test_archify_runtime.py
git commit -m "feat(archify): author diagrams from source evidence"
```

### Task 3: Durable job store and orchestration

**Files:**
- Create: `terminal_mcp/archify_store.py`
- Create: `terminal_mcp/archify_service.py`
- Test: `tests/test_archify_service.py`

**Interfaces:**
- Consumes: Task 1 policy/config and Task 2 inspector/author/runtime.
- Produces: `ArchifyService.status/projects/create_job/list_jobs/get_job/html_path/start/close`.
- Consumed by: Task 4 dashboard routes and Task 5 process wiring.

- [ ] **Step 1: Write failing durability, recovery, and containment tests**

```python
def test_job_is_persisted_before_worker_runs(service):
    job = service.create_job(str(service.repo), "architecture", "focus api")
    persisted = service.store.get(job["id"])
    assert persisted["status"] in {"queued", "running"}

def test_restart_fails_running_and_resumes_queued_once(store, service_factory):
    running = store.create(...); store.mark_running(running["id"])
    queued = store.create(...)
    service = service_factory(store)
    service.start()
    assert store.get(running["id"])["error_code"] == "interrupted"
    wait_for(lambda: store.get(queued["id"])["status"] == "completed")

def test_tampered_artifact_name_cannot_escape_store(service, tmp_path):
    job = completed_job(service)
    service.store.set_artifact_for_test(job["id"], "../../outside.html")
    with pytest.raises(ArchifyServiceError, match="ARTIFACT_MISSING"):
        service.html_path(job["id"])
```

- [ ] **Step 2: Run service tests and verify RED**

Run: `pytest -q tests/test_archify_service.py`
Expected: import failure because the store/service do not exist.

- [ ] **Step 3: Implement SQLite store and bounded executor service**

Create a WAL database with explicit transitions and newest-first indexes. Write artifacts under `<artifact_root>/<job_id>/`, use atomic JSON/metadata replacement, validate output containment, recover startup rows, and expose only serialized job dictionaries.

- [ ] **Step 4: Run service tests and verify GREEN**

Run: `pytest -q tests/test_archify_service.py`
Expected: all service tests pass, including timeout/failure/restart cases.

- [ ] **Step 5: Commit**

```bash
git add terminal_mcp/archify_store.py terminal_mcp/archify_service.py tests/test_archify_service.py
git commit -m "feat(archify): persist durable generation jobs"
```

### Task 4: Dashboard page, APIs, and safe preview

**Files:**
- Create: `terminal_mcp/archify_dashboard.py`
- Modify: `terminal_mcp/dashboard.py`
- Test: `tests/test_archify_dashboard.py`
- Test: `tests/test_dashboard_navigation.py`

**Interfaces:**
- Consumes: `ArchifyService` from Task 3 and existing dashboard guards/navigation.
- Produces: the seven `/dashboard/archify` routes and standalone UI.
- Consumed by: Task 5 server construction and browser verification.

- [ ] **Step 1: Write failing route/security/history tests**

```python
def test_archify_page_and_projects_are_read_guarded(client):
    assert client.get("/dashboard/archify").status_code == 200
    body = client.get("/dashboard/api/archify/projects").json()
    assert body["projects"][0]["path"] == str(repo.resolve())

def test_create_rejects_traversal_and_missing_runtime(client):
    response = client.post("/dashboard/api/archify/jobs", json={
        "project": str(repo / ".." / "outside"), "diagram_type": "architecture", "prompt": ""},
        headers=same_origin_headers(client))
    assert response.status_code == 403
    assert response.json()["error"] == "PROJECT_NOT_ALLOWED"

def test_html_preview_is_sandboxed_and_uses_stored_artifact(client, completed_job):
    response = client.get(f"/dashboard/api/archify/jobs/{completed_job}/html")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"
```

- [ ] **Step 2: Run dashboard tests and verify RED**

Run: `pytest -q tests/test_archify_dashboard.py tests/test_dashboard_navigation.py -k archify`
Expected: 404 or missing-service failures because routes are not registered.

- [ ] **Step 3: Implement standalone page and thin guarded routes**

The page fetches status/projects/jobs, validates form state, posts jobs,
polls queued/running rows, selects completed jobs into a sandboxed iframe,
opens the same stored HTML route in a new tab, and reloads persisted history.
Route code maps stable service error codes to 400/403/404/409/503 responses.

- [ ] **Step 4: Run dashboard tests and verify GREEN**

Run: `pytest -q tests/test_archify_dashboard.py tests/test_dashboard_navigation.py tests/test_dashboard.py`
Expected: all selected tests pass and existing dashboard routes remain green.

- [ ] **Step 5: Commit**

```bash
git add terminal_mcp/archify_dashboard.py terminal_mcp/dashboard.py tests/test_archify_dashboard.py tests/test_dashboard_navigation.py
git commit -m "feat(archify): add dashboard generation workflow"
```

### Task 5: Process wiring, documentation, real runtime, and verification

**Files:**
- Modify: `terminal_mcp/server_http.py`
- Modify: `docs/REQUIREMENTS.md`
- Modify: `docs/CHATGPT_USAGE.md`
- Modify: `README.md`
- Modify: `requirements-lock.txt` only if the normal environment lock changes
- Test: `tests/test_archify_server.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: process-lifetime shared service, truthful docs, and acceptance evidence.

- [ ] **Step 1: Write failing construction/shutdown test**

```python
def test_http_runtime_builds_one_shared_archify_service(tmp_path, monkeypatch):
    built = build_http_components(config_with_archify(tmp_path))
    assert built.archify.store.path.parent == tmp_path / "state"
    assert built.dashboard_archify is built.archify
```

- [ ] **Step 2: Run test and verify RED**

Run: `pytest -q tests/test_archify_server.py`
Expected: failure because server wiring does not construct Archify.

- [ ] **Step 3: Wire one shared service into server startup/shutdown**

Construct database/artifact/runtime paths from configured Terminal MCP state,
pass the same service to `register_dashboard`, call `start()` after route
registration, and register `close()` with process shutdown.

- [ ] **Step 4: Run focused Archify and dashboard tests**

Run: `pytest -q tests/test_archify_*.py tests/test_dashboard.py tests/test_dashboard_navigation.py`
Expected: all tests pass.

- [ ] **Step 5: Update living requirements and operator docs**

Record the feature matrix/status and one detailed feature entry in
`docs/REQUIREMENTS.md`; document dashboard usage and API behavior in
`docs/CHATGPT_USAGE.md`; document shared runtime provisioning and state paths
in `README.md` and `config.example.yaml`. Status must reflect actual browser
and real-Archify evidence, not planned evidence.

- [ ] **Step 6: Provision a disposable official shared runtime and run acceptance generation**

```bash
git clone --depth 1 https://github.com/tt-a1i/archify.git "$TMPDIR/terminal-mcp-archify-runtime"
node "$TMPDIR/terminal-mcp-archify-runtime/archify/bin/archify.mjs" doctor
```

Start a disposable dashboard configured with the runtime above and this worktree as an allowed root. Generate the architecture diagram through the HTTP API, wait for `completed`, fetch HTML, restart the service, and confirm the job still exists.

- [ ] **Step 7: Run browser verification**

Open `/dashboard/archify`, select `terminal-mcp`, choose Architecture, generate,
wait for completion, confirm iframe content and Open action, refresh and confirm
history. Visit `/dashboard` and confirm existing dashboard content and no error
overlay/blank page.

- [ ] **Step 8: Run full quality gates**

Run: `git diff --check`
Expected: exit 0.

Run: `pytest -q`
Expected: exit 0 with no failures.

- [ ] **Step 9: Commit**

```bash
git add terminal_mcp/server_http.py docs/REQUIREMENTS.md docs/CHATGPT_USAGE.md README.md tests/test_archify_server.py
git commit -m "feat(archify): ship shared diagram capability"
```

- [ ] **Step 10: Final branch verification**

Run `git status --short --branch`, inspect `git log --oneline` and the final diff
from the pre-feature base, then perform the required whole-branch review. Fix
Critical/Important findings test-first and rerun the full suite before the final
completion report.
