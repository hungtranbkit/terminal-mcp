from terminal_mcp.node_client import RemoteNodeClient


def test_remote_set_permissions_sends_json_body_not_query_params():
    client = RemoteNodeClient("http://node.test:8790", "token")
    captured = {}

    def fake_request(method, path, *, params=None, body=None, timeout_seconds=None):
        captured.update(method=method, path=path, params=params, body=body)
        return {"ok": True}

    client._request = fake_request
    result = client.set_permissions(
        "worker", read=True, input=True, expected_revision=7, actor="test",
    )
    assert result == {"ok": True}
    assert captured == {
        "method": "POST",
        "path": "/v1/sessions/worker/permissions",
        "params": None,
        "body": {"read": True, "input": True, "expected_revision": 7, "actor": "test"},
    }
