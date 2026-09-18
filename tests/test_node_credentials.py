"""blg_a3cc401d8275 -- node token rotation and revocation.

Before this, a node had one token with no identity, no version and no
revocation path. Rotating meant hand-editing two files on two machines and
restarting the node -- done for real on 2026-09-09, when macbook was
rotated and dell-5530 was deferred because the restart would have cost six
live sessions. A credential you cannot afford to rotate is one you cannot
revoke either.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import node_credentials as nc
from terminal_mcp.node_credentials import NodeCredentialStore

LEGACY = "a" * 64


@pytest.fixture
def store(tmp_path):
    return NodeCredentialStore(tmp_path / "creds.db")


# ---------------------------------------------------------------------------
# rotation preserves connectivity -- the acceptance criterion
# ---------------------------------------------------------------------------

def test_rotation_keeps_the_node_working_throughout(store):
    """The dell-5530 case: rotate without a simultaneous restart. The old
    token must keep authenticating until the node picks up the new one."""
    store.adopt("dell-5530", LEGACY)
    assert store.verify("dell-5530", LEGACY).accepted

    record, fresh = store.rotate("dell-5530", grace_seconds=900)

    assert store.verify("dell-5530", fresh).accepted, "the new token must work immediately"
    assert store.verify("dell-5530", LEGACY).accepted, "the old one must work during grace"
    assert store.verify("dell-5530", LEGACY).detail.endswith("grace window")
    assert store.active_token_id("dell-5530") == record.token_id


def test_grace_window_closes_and_then_refuses(store):
    store.adopt("n1", LEGACY)
    store.rotate("n1", grace_seconds=60)
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    result = store.verify("n1", LEGACY, now=later)
    assert result.verdict == nc.EXPIRED
    assert result.accepted is False


def test_zero_grace_is_an_immediate_cutover(store):
    """For a token believed compromised, there is no window."""
    store.adopt("n1", LEGACY)
    _record, fresh = store.rotate("n1", grace_seconds=0)
    assert store.verify("n1", LEGACY).verdict == nc.REVOKED
    assert store.verify("n1", fresh).accepted


def test_rotating_twice_does_not_widen_the_accepted_set(store):
    """Two grace windows at once would mean every click of rotate leaves
    one more live credential behind."""
    store.adopt("n1", LEGACY)
    _r1, second = store.rotate("n1", grace_seconds=900)
    _r2, third = store.rotate("n1", grace_seconds=900)
    assert store.verify("n1", LEGACY).verdict == nc.REVOKED, "the oldest must be gone"
    assert store.verify("n1", second).accepted, "the previous one is in grace"
    assert store.verify("n1", third).accepted
    accepted = [c for c in store.list_for("n1") if c.status in (nc.ACTIVE, nc.GRACE)]
    assert len(accepted) == 2


# ---------------------------------------------------------------------------
# revocation
# ---------------------------------------------------------------------------

def test_revoked_token_is_rejected_with_its_own_verdict(store):
    """A revoked credential still in use is an incident, not a typo, so it
    must be distinguishable from an unknown token."""
    store.adopt("n1", LEGACY)
    store.revoke("n1", reason="leaked in a paste")
    result = store.verify("n1", LEGACY)
    assert result.verdict == nc.REVOKED
    assert result.accepted is False
    assert "leaked in a paste" in result.detail


def test_revoke_all_is_the_default(store):
    store.adopt("n1", LEGACY)
    _record, fresh = store.rotate("n1", grace_seconds=900)
    store.revoke("n1")
    assert store.verify("n1", LEGACY).verdict == nc.REVOKED
    assert store.verify("n1", fresh).verdict == nc.REVOKED
    assert store.active_token_id("n1") is None


def test_one_token_can_be_revoked_without_disturbing_the_current_one(store):
    store.adopt("n1", LEGACY)
    record, fresh = store.rotate("n1", grace_seconds=900)
    old_id = [c.token_id for c in store.list_for("n1") if c.status == nc.GRACE][0]
    store.revoke("n1", token_id=old_id, reason="old credential leaked")
    assert store.verify("n1", LEGACY).verdict == nc.REVOKED
    assert store.verify("n1", fresh).accepted, "the live token must be untouched"


def test_duplicate_revoke_is_idempotent(store):
    store.adopt("n1", LEGACY)
    first = store.revoke("n1", reason="first")
    second = store.revoke("n1", reason="second")
    assert all(c.status == nc.REVOKED for c in first + second)
    # The original reason and timestamp survive -- a second revoke must not
    # rewrite the record of the first.
    assert [c.revoked_reason for c in store.list_for("n1")] == ["first"]


def test_duplicate_adopt_is_idempotent(store):
    first = store.adopt("n1", LEGACY)
    second = store.adopt("n1", LEGACY)
    assert first.token_id == second.token_id
    assert len(store.list_for("n1")) == 1


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("", nc.UNKNOWN), ("wrong", nc.UNKNOWN), ("b" * 64, nc.UNKNOWN),
])
def test_unknown_tokens_are_refused(store, token, expected):
    store.adopt("n1", LEGACY)
    assert store.verify("n1", token).verdict == expected


def test_a_token_valid_for_another_node_is_unknown_here(store):
    """Saying "wrong node" would confirm the token is valid somewhere."""
    store.adopt("n1", LEGACY)
    result = store.verify("n2", LEGACY)
    assert result.verdict == nc.UNKNOWN
    assert "not recognised for any node" in result.detail
    assert result.token_id is None, "must not leak which credential it matched"


def test_unrecognised_status_is_refused(store):
    store.adopt("n1", LEGACY)
    with store._connection() as connection:
        connection.execute("UPDATE node_credentials SET status = 'WEIRD'")
    assert store.verify("n1", LEGACY).accepted is False


# ---------------------------------------------------------------------------
# no plaintext anywhere
# ---------------------------------------------------------------------------

def test_the_database_never_holds_a_token(tmp_path):
    store = NodeCredentialStore(tmp_path / "creds.db")
    store.adopt("n1", LEGACY)
    _record, fresh = store.rotate("n1")
    raw = (tmp_path / "creds.db").read_bytes()
    assert LEGACY.encode() not in raw
    assert fresh.encode() not in raw


def test_records_carry_no_secret_field(store):
    store.adopt("n1", LEGACY)
    payload = str(store.list_for("n1")[0].to_dict())
    assert LEGACY not in payload
    # The fingerprint identifies the credential without being one.
    assert nc.token_fingerprint(LEGACY) in payload
    assert len(nc.token_fingerprint(LEGACY)) == 12
    assert nc.token_fingerprint(LEGACY) != nc.token_hash(LEGACY)


def test_verify_result_is_safe_to_log(store):
    store.adopt("n1", LEGACY)
    assert LEGACY not in str(store.verify("n1", LEGACY).to_dict())


# ---------------------------------------------------------------------------
# durability, restart, concurrency
# ---------------------------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "creds.db"
    store = NodeCredentialStore(path)
    store.adopt("n1", LEGACY)
    _record, fresh = store.rotate("n1", grace_seconds=900)
    store.revoke("n1", token_id=nc.token_fingerprint(LEGACY), reason="leaked")

    reopened = NodeCredentialStore(path)
    assert reopened.verify("n1", LEGACY).verdict == nc.REVOKED
    assert reopened.verify("n1", fresh).accepted
    assert reopened.active_token_id("n1") == nc.token_fingerprint(fresh)


def test_concurrent_rotations_leave_exactly_one_active(tmp_path):
    path = tmp_path / "creds.db"
    NodeCredentialStore(path).adopt("n1", LEGACY)
    barrier = threading.Barrier(6)

    def rotate(_index):
        barrier.wait()
        return NodeCredentialStore(path).rotate("n1", grace_seconds=900)[1]

    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(rotate, range(6)))

    final = NodeCredentialStore(path)
    active = [c for c in final.list_for("n1") if c.status == nc.ACTIVE]
    assert len(active) == 1, f"exactly one ACTIVE token must survive, got {len(active)}"
    assert sum(1 for t in tokens if final.verify("n1", t).accepted) >= 1


def test_expire_grace_never_changes_an_auth_outcome(store):
    store.adopt("n1", LEGACY)
    store.rotate("n1", grace_seconds=60)
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    before = store.verify("n1", LEGACY, now=later).accepted
    store.expire_grace(now=later)
    assert store.verify("n1", LEGACY, now=later).accepted == before is False


# ---------------------------------------------------------------------------
# backward compatibility at the route
# ---------------------------------------------------------------------------

def test_unmanaged_node_falls_back_to_the_legacy_env_var(tmp_path, monkeypatch):
    """No flag day: a node enrolled before this store existed keeps
    working on its env var until it is adopted."""
    from tests.test_windows_onboarding import _client
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_LEGACYNODE", LEGACY)
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    controller.register_remote_node("legacynode", display_name="l", hostname="h",
                                    endpoint="http://192.168.1.9:8790", token=LEGACY)
    response = client.post("/dashboard/api/nodes/legacynode/heartbeat",
                           headers={"Authorization": f"Bearer {LEGACY}"},
                           json={"metrics": {}, "tmux_session_count": 0, "agent_counts": {},
                                 "agent_types": [], "platform": "linux"})
    assert response.status_code == 200


def test_a_managed_node_is_checked_against_the_store(tmp_path, monkeypatch):
    """Once adopted, the store is authoritative -- so a revocation takes
    effect even though the legacy env var is still set."""
    from terminal_mcp.node_credentials import NodeCredentialStore as Store
    from tests.test_windows_onboarding import _client
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_MANAGED", LEGACY)
    credentials = Store(tmp_path / "creds.db")
    client, controller, _onboarding = _client(tmp_path, monkeypatch, credentials=credentials)
    controller.register_remote_node("managed", display_name="m", hostname="h",
                                    endpoint="http://192.168.1.9:8790", token=LEGACY)
    credentials.adopt("managed", LEGACY)

    beat = lambda: client.post("/dashboard/api/nodes/managed/heartbeat",
                               headers={"Authorization": f"Bearer {LEGACY}"},
                               json={"metrics": {}, "tmux_session_count": 0, "agent_counts": {},
                                     "agent_types": [], "platform": "linux"})
    assert beat().status_code == 200

    credentials.revoke("managed", reason="rotated out")
    refused = beat()
    assert refused.status_code == 401
    assert refused.json()["verdict"] == nc.REVOKED
    # The token itself must never be echoed back.
    assert LEGACY not in refused.text
