"""deploy/node-heartbeat-relay.py -- the bridge that lets a node which
cannot be repointed without destroying its sessions still report as online.

The two behaviours worth pinning are both refusals: it must never post a
heartbeat it did not actually verify, because doing so would report a dead
node as healthy -- strictly worse than the offline state it is fixing.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

RELAY_PATH = Path(__file__).resolve().parent.parent / "deploy" / "node-heartbeat-relay.py"


def _load():
    spec = importlib.util.spec_from_file_location("node_heartbeat_relay", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


relay = _load()

_STATIC = {"platform": "windows", "session_backend": "windows_pty",
           "shell_capabilities": ["powershell", "cmd"], "wsl_available": True,
           "capabilities": []}


class _Recorder:
    """Stands in for urlopen. Serves canned GET/POST bodies and records
    every request, so a test can assert on what was NOT sent."""

    def __init__(self, responses: dict, fail: set[str] | None = None):
        self.responses = responses
        self.fail = fail or set()
        self.posted: list[tuple[str, dict]] = []
        self.requested: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        for pattern in self.fail:
            if pattern in url:
                raise OSError("simulated unreachable")
        if request.method == "POST" and "/heartbeat" in url:
            self.posted.append((url, json.loads(request.data.decode())))
        body = None
        for pattern, payload in self.responses.items():
            if pattern in url:
                body = payload
                break
        if body is None:
            body = {}

        class _Response:
            status = 200

            def read(self_inner):
                return json.dumps(body).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Response()


def _responses(node_id="dell-5530"):
    return {
        "/v1/health": {"status": "ok", "node_id": node_id, "version": "0.12.0",
                       "agent_generation": "abc123"},
        "/v1/metrics": {"cpu_percent": 42.0, "cpu_count": 8},
        "/v1/sessions": {"sessions": [{"name": "win1"}, {"name": "win2"}, {"name": "wtest"}]},
        "/v1/capabilities/refresh": {"agent_types": ["shell", "claude"], "agent_version": "0.12.0"},
    }


def _run(monkeypatch, recorder, node_id="dell-5530"):
    monkeypatch.setattr(relay.urllib.request, "urlopen", recorder)
    return relay.relay_once(node_id=node_id, node_endpoint="http://node:8790",
                            controller_url="http://ctrl:8766", token="t", static=_STATIC)


def test_relays_real_node_state(monkeypatch):
    recorder = _Recorder(_responses())
    assert _run(monkeypatch, recorder) is True
    assert len(recorder.posted) == 1
    url, body = recorder.posted[0]
    assert url == "http://ctrl:8766/dashboard/api/nodes/dell-5530/heartbeat"
    # Session count is the node's own real listing, not a configured guess.
    assert body["tmux_session_count"] == 3
    assert body["metrics"]["cpu_percent"] == 42.0
    assert body["platform"] == "windows"
    assert body["session_backend"] == "windows_pty"
    assert body["agent_types"] == ["shell", "claude"]


def test_refuses_when_endpoint_reports_a_different_node(monkeypatch):
    """Pointed at the wrong host, it must not report that host's health
    under this node's name -- that would mark a node online using a
    completely different machine's metrics."""
    recorder = _Recorder(_responses(node_id="some-other-node"))
    assert _run(monkeypatch, recorder) is False
    assert recorder.posted == []


@pytest.mark.parametrize("broken", ["/v1/health", "/v1/metrics", "/v1/sessions",
                                    "/v1/capabilities/refresh"])
def test_posts_nothing_when_any_pull_fails(monkeypatch, broken):
    """A node that has actually gone away must age out to offline exactly
    as it would with no relay running."""
    recorder = _Recorder(_responses(), fail={broken})
    assert _run(monkeypatch, recorder) is False
    assert recorder.posted == []


def test_token_is_taken_from_the_environment_not_the_command_line():
    """A bearer token passed as an argument is visible in every process
    listing on the host; this tool takes only the variable's NAME."""
    source = RELAY_PATH.read_text(encoding="utf-8")
    assert '"--token-env"' in source
    assert '"--token"' not in source
