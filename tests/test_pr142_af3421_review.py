from terminal_mcp.node_client import NodeClientError, RemoteNodeClient
from tests.test_pr142_capacity_boundaries import Client, _controller, _node


class RecordingClient(Client):
    def __init__(self, cwd, *, mismatch=False):
        super().__init__()
        self.cwd = cwd
        self.mismatch = mismatch
        self.probes = []
        self.launch = None

    def resolve_cwd(self):
        return {"resolved": True, "cwd": self.cwd}

    def repo_evidence(self, cwd):
        self.probes.append(cwd)
        return super().repo_evidence("/different" if self.mismatch else cwd)

    def create_session(self, *args, **kwargs):
        self.launch = args[2]
        return super().create_session(*args, **kwargs)


def nodes(*names):
    rows = [_node(name) for name in names]
    for i, row in enumerate(rows):
        row.tmux_session_count = i
    return rows


def old_client():
    client = RemoteNodeClient("http://fixture.invalid", "fixture-token")

    def request(method, path, **kwargs):
        if (method, path) == ("GET", "/v1/resolve-cwd"):
            raise NodeClientError("legacy route absent", http_status=404)
        if method == "GET" and path.startswith("/v1/repo-evidence?cwd="):
            return {"cwd": "/task", "exists": True, "readable": True}
        raise AssertionError((method, path))

    client._request = request
    return client


def test_binds_per_node_default_after_mismatch():
    primary = RecordingClient("/primary", mismatch=True)
    alternate = RecordingClient("/alternate")
    ctl = _controller(nodes("primary", "alternate"), {"primary": primary, "alternate": alternate})
    result = ctl.terminal_create_session("default-fallback")
    assert result["node_id"] == "alternate"
    assert primary.probes == ["/primary"] and primary.calls == 0
    assert alternate.probes == ["/alternate"] and alternate.launch == "/alternate"


def test_legacy_agent_denied_explicit_and_skipped_in_auto():
    alternate = RecordingClient("/alternate")
    ctl = _controller(nodes("legacy", "alternate"), {"legacy": old_client(), "alternate": alternate})
    for cwd in (None, "/task"):
        denied = ctl.terminal_create_session("legacy-explicit", node="legacy", cwd=cwd)
        assert denied["error"] == "TARGET_VOLUME_UNVERIFIED"
    result = ctl.terminal_create_session("legacy-fallback")
    assert result["node_id"] == "alternate" and alternate.launch == "/alternate"


def test_omitted_cwd_never_dispatches_to_adapter_without_resolver():
    adapter = RecordingClient("/default")
    adapter.resolve_cwd = None
    ctl = _controller(nodes("adapter"), {"adapter": adapter})
    assert ctl.terminal_create_session("adapter", node="adapter").get("error")
    assert adapter.calls == 0


def test_auto_skips_eligible_row_without_client_after_default_resolution_failure():
    alternate = RecordingClient("/alternate")
    ctl = _controller(nodes("legacy", "unconnected", "alternate"),
                      {"legacy": old_client(), "alternate": alternate})
    result = ctl.terminal_create_session("unconnected-fallback")
    assert result["node_id"] == "alternate" and alternate.launch == "/alternate"
