import json

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dispatch_settings_dashboard import register, save_config_atomic, validate_config
from terminal_mcp.mcp_app import build_mcp


def test_validate_good_and_bad_overlap():
    good = validate_config({"project_id": "novaretail", "dispatcher_session": "codex1", "workers": ["codex2"]})
    assert good["mode"] == "SINGLE_PROJECT"
    try:
        validate_config({"dispatcher_session": "codex1", "workers": ["codex1"]})
    except ValueError as exc:
        assert "dispatcher" in str(exc)
    else:
        raise AssertionError("overlap accepted")


def test_atomic_save_roundtrip(tmp_path):
    path = tmp_path / "dispatch.json"
    cfg = validate_config({"dispatcher_session": "codex1", "workers": ["codex2"], "allowed_roots": ["/tmp/work"]})
    save_config_atomic(cfg, path)
    assert json.loads(path.read_text()) == cfg


def test_routes_get_and_post_guard(tmp_path):
    path = tmp_path / "dispatch.json"
    server = build_mcp(TerminalService(AppConfig(PermissionsConfig(True, True), ())))
    deny = lambda request: (JSONResponse({"denied": True}, status_code=403), None)
    allow = lambda request: (None, None)
    register(server, lambda r: (None, None), deny, config_path=path)
    client = TestClient(server.streamable_http_app())
    assert client.get("/dashboard/ops/dispatch-settings").status_code == 200
    assert client.get("/dashboard/api/ops/dispatch-settings").status_code == 200
    assert client.post("/dashboard/api/ops/dispatch-settings", json={}).status_code == 403
    assert not path.exists()

    server = build_mcp(TerminalService(AppConfig(PermissionsConfig(True, True), ())))
    register(server, lambda r: (None, None), allow, config_path=path)
    client = TestClient(server.streamable_http_app())
    payload = {"project_id": "novaretail", "dispatcher_session": "codex1", "workers": ["codex2"],
               "mode": "SINGLE_PROJECT", "allowed_roots": ["/tmp/work"], "bindings": {"codex2": "novaretail"}}
    response = client.post("/dashboard/api/ops/dispatch-settings", json=payload)
    assert response.status_code == 200 and response.json()["saved"] is True
    assert json.loads(path.read_text()) == payload
