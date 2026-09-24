from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from terminal_mcp.archify_policy import ArchifyProjectPolicy
from terminal_mcp.archify_runtime import ArchifyRuntimeError
from terminal_mcp.archify_service import ArchifyService, ArchifyServiceError
from terminal_mcp.archify_store import ArchifyStore
from terminal_mcp.config import ArchifyConfig


class FakeRuntime:
    def __init__(self, *, ready=True, failure: ArchifyRuntimeError | None = None,
                 gate: threading.Event | None = None):
        self.ready = ready
        self.failure = failure
        self.gate = gate
        self.calls = 0

    def status(self):
        return {"ready": self.ready, "state": "ready" if self.ready else "runtime_missing",
                "message": "ready" if self.ready else "Install shared Archify runtime."}

    def render(self, diagram_type, input_path, output_path, *, repo_root=None):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=3)
        if self.failure is not None:
            raise self.failure
        output_path.write_text("<!doctype html><title>Archify</title>", encoding="utf-8")
        return {"output_bytes": output_path.stat().st_size, "stdout": "ok", "stderr": ""}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "projects" / "terminal-mcp"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='terminal-mcp'\n", encoding="utf-8")
    (root / "dashboard.py").write_text("import service\n", encoding="utf-8")
    (root / "service.py").write_text("VALUE=1\n", encoding="utf-8")
    return root


def make_service(tmp_path, repo, *, runtime=None, store=None, workers=1):
    store = store or ArchifyStore(tmp_path / "archify.db")
    policy = ArchifyProjectPolicy((str(repo.parent),))
    config = ArchifyConfig(allowed_roots=(str(repo.parent),), workers=workers,
                           max_files=20, max_source_bytes=100_000)
    return ArchifyService(config, policy, runtime or FakeRuntime(), store,
                          artifact_root=tmp_path / "artifacts")


def wait_job(service, job_id, status="completed", timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id)
        if job["status"] == status:
            return job
        if job["status"] == "failed" and status != "failed":
            pytest.fail(f"job failed: {job}")
        time.sleep(0.02)
    pytest.fail(f"job did not reach {status}: {service.get_job(job_id)}")


def test_job_is_persisted_before_worker_can_finish(tmp_path, repo):
    gate = threading.Event()
    service = make_service(tmp_path, repo, runtime=FakeRuntime(gate=gate))
    service.start()
    try:
        job = service.create_job(str(repo), "architecture", "focus api")
        persisted = service.store.get(job["id"])
        assert persisted["status"] in {"queued", "running"}
        gate.set()
        wait_job(service, job["id"])
    finally:
        gate.set()
        service.close()


def test_completed_job_persists_ir_metadata_html_and_history(tmp_path, repo):
    service = make_service(tmp_path, repo)
    service.start()
    try:
        created = service.create_job(str(repo), "architecture", "focus dashboard")
        completed = wait_job(service, created["id"])
        html = service.html_path(created["id"])
        assert html.read_text(encoding="utf-8").startswith("<!doctype html>")
        assert completed["html_path"] == f"{created['id']}/diagram.html"
        assert (html.parent / "diagram.json").is_file()
        assert (html.parent / "metadata.json").is_file()
    finally:
        service.close()

    reopened = make_service(tmp_path, repo, store=ArchifyStore(tmp_path / "archify.db"))
    assert reopened.list_jobs()[0]["id"] == created["id"]
    assert reopened.get_job(created["id"])["status"] == "completed"
    reopened.close()


def test_restart_fails_running_and_resumes_queued_once(tmp_path, repo):
    store = ArchifyStore(tmp_path / "archify.db")
    running = store.create(str(repo), repo.name, "architecture", "running")
    store.mark_running(running["id"])
    queued = store.create(str(repo), repo.name, "architecture", "queued")
    runtime = FakeRuntime()
    service = make_service(tmp_path, repo, runtime=runtime, store=store)

    service.start()
    try:
        assert service.get_job(running["id"])["error_code"] == "interrupted"
        wait_job(service, queued["id"])
        assert runtime.calls == 1
    finally:
        service.close()


def test_tampered_artifact_name_cannot_escape_store(tmp_path, repo):
    outside = tmp_path / "outside.html"
    outside.write_text("secret", encoding="utf-8")
    service = make_service(tmp_path, repo)
    service.start()
    try:
        created = service.create_job(str(repo), "architecture", "")
        wait_job(service, created["id"])
        with sqlite3.connect(service.store.path) as connection:
            connection.execute("UPDATE archify_jobs SET html_path='../../outside.html' WHERE id=?",
                               (created["id"],))
        with pytest.raises(ArchifyServiceError, match="ARTIFACT_MISSING"):
            service.html_path(created["id"])
    finally:
        service.close()


def test_missing_runtime_prevents_creation_but_keeps_history_readable(tmp_path, repo):
    service = make_service(tmp_path, repo, runtime=FakeRuntime(ready=False))

    assert service.status()["runtime"]["state"] == "runtime_missing"
    assert service.list_jobs() == []
    with pytest.raises(ArchifyServiceError, match="ARCHIFY_UNAVAILABLE"):
        service.create_job(str(repo), "architecture", "")
    service.close()


def test_runtime_failure_is_persisted_with_bounded_detail(tmp_path, repo):
    runtime = FakeRuntime(failure=ArchifyRuntimeError(
        "GENERATION_TIMEOUT", "timed out", detail="x" * 100_000))
    service = make_service(tmp_path, repo, runtime=runtime)
    service.start()
    try:
        created = service.create_job(str(repo), "architecture", "")
        failed = wait_job(service, created["id"], status="failed")
        assert failed["error_code"] == "GENERATION_TIMEOUT"
        assert len(failed["error_detail"]) <= 4096
    finally:
        service.close()


def test_worker_revalidates_project_if_path_becomes_symlink_before_execution(tmp_path, repo):
    gate = threading.Event()
    runtime = FakeRuntime(gate=gate)
    service = make_service(tmp_path, repo, runtime=runtime, workers=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pyproject.toml").write_text("[project]\nname='outside'\n", encoding="utf-8")
    (outside / "a.py").write_text("import b\n", encoding="utf-8")
    (outside / "b.py").write_text("VALUE=1\n", encoding="utf-8")
    service.start()
    try:
        first = service.create_job(str(repo), "architecture", "")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and runtime.calls != 1:
            time.sleep(0.01)
        assert runtime.calls == 1
        second = service.create_job(str(repo), "architecture", "")
        parked = repo.with_name("terminal-mcp-parked")
        repo.rename(parked)
        repo.symlink_to(outside, target_is_directory=True)
        gate.set()

        failed = wait_job(service, second["id"], status="failed")
        assert failed["error_code"] == "PROJECT_NOT_ALLOWED"
        assert runtime.calls == 1
    finally:
        gate.set()
        service.close()


@pytest.mark.parametrize("diagram_type", ["bogus", "ARCHITECTURE", ""])
def test_invalid_diagram_type_is_rejected_before_persistence(tmp_path, repo, diagram_type):
    service = make_service(tmp_path, repo)
    with pytest.raises(ArchifyServiceError, match="INVALID_DIAGRAM_TYPE"):
        service.create_job(str(repo), diagram_type, "")
    assert service.list_jobs() == []
    service.close()
