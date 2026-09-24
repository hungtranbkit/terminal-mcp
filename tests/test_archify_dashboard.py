from __future__ import annotations

import time

from starlette.testclient import TestClient

from terminal_mcp.archify_policy import ArchifyProjectPolicy
from terminal_mcp.archify_service import ArchifyService
from terminal_mcp.archify_store import ArchifyStore
from terminal_mcp.config import (AppConfig, ArchifyConfig, InputPolicyConfig,
                                 PermissionsConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp


class FakeRuntime:
    def __init__(self, ready=True):
        self.ready = ready

    def status(self):
        return {"ready": self.ready, "state": "ready" if self.ready else "runtime_missing",
                "message": "ready" if self.ready else "Install shared Archify runtime."}

    def render(self, diagram_type, input_path, output_path, *, repo_root=None):
        output_path.write_text("<!doctype html><h1>Real Archify artifact</h1>", encoding="utf-8")
        return {"output_bytes": output_path.stat().st_size}


def _rig(tmp_path, *, runtime=None):
    repo = tmp_path / "projects" / "terminal-mcp"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='terminal-mcp'\n", encoding="utf-8")
    (repo / "dashboard.py").write_text("import service\n", encoding="utf-8")
    (repo / "service.py").write_text("VALUE=1\n", encoding="utf-8")
    archify_config = ArchifyConfig(allowed_roots=(str(repo.parent),), max_files=20,
                                   max_source_bytes=100_000)
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)), archify=archify_config,
    )
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    archify = ArchifyService(
        archify_config, ArchifyProjectPolicy((str(repo.parent),)), runtime or FakeRuntime(),
        ArchifyStore(tmp_path / "archify.db"), artifact_root=tmp_path / "artifacts",
    )
    archify.start()
    server = build_mcp(terminal)
    register_dashboard(server, terminal, archify=archify)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, archify, repo


def _wait(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        body = client.get(f"/dashboard/api/archify/jobs/{job_id}").json()
        if body["job"]["status"] in {"completed", "failed"}:
            return body["job"]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_archify_page_projects_and_navigation_render(tmp_path):
    client, service, repo = _rig(tmp_path)
    try:
        page = client.get("/dashboard/archify")
        assert page.status_code == 200
        assert 'data-global-nav' in page.text
        assert 'id="archifyForm"' in page.text
        assert 'id="previewFrame"' in page.text
        body = client.get("/dashboard/api/archify/projects").json()
        assert body["projects"] == [{"name": "terminal-mcp", "path": str(repo.resolve())}]
        assert 'href="/dashboard/archify"' in client.get("/dashboard").text
    finally:
        service.close()


def test_create_rejects_traversal_and_requires_same_origin(tmp_path):
    client, service, repo = _rig(tmp_path)
    try:
        payload = {"project": str(repo / ".." / ".." / "outside"),
                   "diagram_type": "architecture", "prompt": ""}
        response = client.post("/dashboard/api/archify/jobs", json=payload)
        assert response.status_code == 403
        assert response.json()["error"] == "PROJECT_NOT_ALLOWED"

        no_origin = TestClient(client.app)
        response = no_origin.post("/dashboard/api/archify/jobs", json={
            "project": str(repo), "diagram_type": "architecture", "prompt": ""})
        assert response.status_code == 403
        assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"
    finally:
        service.close()


def test_generate_preview_and_history_survive_page_refresh(tmp_path):
    client, service, repo = _rig(tmp_path)
    try:
        response = client.post("/dashboard/api/archify/jobs", json={
            "project": str(repo), "diagram_type": "architecture", "prompt": "dashboard"})
        assert response.status_code == 202
        job_id = response.json()["job"]["id"]
        assert _wait(client, job_id)["status"] == "completed"

        preview = client.get(f"/dashboard/api/archify/jobs/{job_id}/html")
        assert preview.status_code == 200
        assert preview.headers["content-security-policy"] == "sandbox allow-scripts"
        assert "Real Archify artifact" in preview.text

        history = client.get("/dashboard/api/archify/jobs").json()["jobs"]
        assert history[0]["id"] == job_id
        assert client.get("/dashboard/archify").status_code == 200
        assert client.get("/dashboard/api/archify/jobs").json()["jobs"][0]["status"] == "completed"
    finally:
        service.close()


def test_unavailable_runtime_is_explicit_and_create_is_503(tmp_path):
    client, service, repo = _rig(tmp_path, runtime=FakeRuntime(ready=False))
    try:
        status = client.get("/dashboard/api/archify/status").json()
        assert status["runtime"]["state"] == "runtime_missing"
        response = client.post("/dashboard/api/archify/jobs", json={
            "project": str(repo), "diagram_type": "architecture", "prompt": ""})
        assert response.status_code == 503
        assert response.json()["error"] == "ARCHIFY_UNAVAILABLE"
    finally:
        service.close()


def test_unknown_job_and_invalid_limit_are_bounded(tmp_path):
    client, service, _repo = _rig(tmp_path)
    try:
        assert client.get("/dashboard/api/archify/jobs/missing").status_code == 404
        assert client.get("/dashboard/api/archify/jobs?limit=not-a-number").status_code == 400
    finally:
        service.close()
