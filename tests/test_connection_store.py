"""connection_store.py -- durable metadata for a discovery/remote-connect
node, and its token-file management. Every test here isolates the store
to tmp_path (never the real ~/.local/state/terminal-mcp/connections.db --
see connection_store.py's own module docstring for the exact class of
incident this project has already been bitten by once for a sibling
store, node_registry.py's nodes.db)."""
from __future__ import annotations

import sqlite3
import stat

from terminal_mcp.connection_store import (
    TRANSPORT_AGENT_TOKEN,
    TRANSPORT_CLOUDFLARE_SSH,
    TRANSPORT_LAN_SSH,
    ConnectionStore,
    generate_node_token,
)


def test_save_and_get_roundtrips_every_field(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    saved = store.save("m910", transport_type=TRANSPORT_LAN_SSH, endpoint="http://192.168.1.50:8790",
                       hostname="192.168.1.50", username="pi", port=22,
                       host_key_fingerprint="SHA256:abc", token_file="/tmp/x.token")
    fetched = store.get("m910")
    assert fetched == saved
    assert fetched.transport_type == TRANSPORT_LAN_SSH
    assert fetched.endpoint == "http://192.168.1.50:8790"
    assert fetched.hostname == "192.168.1.50"
    assert fetched.username == "pi"
    assert fetched.port == 22
    assert fetched.host_key_fingerprint == "SHA256:abc"


def test_save_is_idempotent_upsert(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    store.save("m910", transport_type=TRANSPORT_LAN_SSH, endpoint="http://1.2.3.4:8790")
    store.save("m910", transport_type=TRANSPORT_CLOUDFLARE_SSH, endpoint="http://1.2.3.4:9999")
    rows = store.list()
    assert len(rows) == 1
    assert rows[0].transport_type == TRANSPORT_CLOUDFLARE_SSH
    assert rows[0].endpoint == "http://1.2.3.4:9999"


def test_unknown_transport_type_rejected(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    import pytest
    with pytest.raises(ValueError):
        store.save("m910", transport_type="not-a-real-transport", endpoint="http://1.2.3.4:8790")


def test_list_orders_by_node_id(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    store.save("zeta", transport_type=TRANSPORT_AGENT_TOKEN, endpoint="http://1.1.1.1:1")
    store.save("alpha", transport_type=TRANSPORT_AGENT_TOKEN, endpoint="http://2.2.2.2:2")
    assert [c.node_id for c in store.list()] == ["alpha", "zeta"]


def test_delete_removes_row_and_token_file(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    token_file = store.write_token("m910", "super-secret-token")
    store.save("m910", transport_type=TRANSPORT_AGENT_TOKEN, endpoint="http://1.2.3.4:8790", token_file=token_file)
    assert store.delete("m910") is True
    assert store.get("m910") is None
    from pathlib import Path
    assert not Path(token_file).exists()
    assert store.delete("m910") is False  # already gone -- not an error, just False


def test_token_file_is_0600_and_never_in_the_sqlite_row(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    token = "super-secret-token-value-should-never-appear-in-the-db"
    token_file = store.write_token("m910", token)
    mode = stat.S_IMODE(store.tokens_dir.joinpath("m910.token").stat().st_mode)
    assert mode == 0o600
    assert store.read_token(token_file) == token

    saved = store.save("m910", transport_type=TRANSPORT_AGENT_TOKEN, endpoint="http://1.2.3.4:8790",
                       token_file=token_file)
    assert saved.token_file == token_file
    # The raw sqlite file itself never contains the secret token bytes --
    # only the reference PATH does (matching node_registry.py's own
    # auth_token_ref-is-a-reference, not a secret, discipline).
    raw_bytes = (tmp_path / "connections.db").read_bytes()
    assert token.encode() not in raw_bytes


def test_read_token_missing_file_returns_none(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    assert store.read_token(str(tmp_path / "does-not-exist.token")) is None


def test_generate_node_token_is_long_and_random():
    a = generate_node_token()
    b = generate_node_token()
    assert len(a) >= 32
    assert a != b


def test_db_file_itself_is_0600(tmp_path):
    store = ConnectionStore(tmp_path / "connections.db")
    store.save("m910", transport_type=TRANSPORT_AGENT_TOKEN, endpoint="http://1.2.3.4:8790")
    mode = stat.S_IMODE((tmp_path / "connections.db").stat().st_mode)
    assert mode == 0o600
