from pathlib import Path

import pytest

from terminal_mcp.replay_guard import (
    FUTURE_TIMESTAMP,
    HeartbeatReplayGuard,
    INVALID_NONCE,
    LEGACY_ACCEPTED,
    MISSING_HEADERS,
    OK,
    REPLAYED_NONCE,
    STALE_TIMESTAMP,
)


def test_accepts_fresh_nonce_once_and_persists_replay(tmp_path: Path):
    path = tmp_path / "replay.db"
    guard = HeartbeatReplayGuard(path, max_skew_seconds=60, retention_seconds=180, require_headers=True)
    first = guard.check_and_record("node-a", "1000", "abcdefghijklmnop", now=1001)
    assert first.accepted and first.verdict == OK

    after_restart = HeartbeatReplayGuard(path, max_skew_seconds=60, retention_seconds=180, require_headers=True)
    replay = after_restart.check_and_record("node-a", "1000", "abcdefghijklmnop", now=1002)
    assert not replay.accepted and replay.verdict == REPLAYED_NONCE


def test_nonce_scope_is_per_node(tmp_path: Path):
    guard = HeartbeatReplayGuard(tmp_path / "replay.db", max_skew_seconds=60, retention_seconds=180, require_headers=True)
    assert guard.check_and_record("node-a", "1000", "abcdefghijklmnop", now=1001).accepted
    assert guard.check_and_record("node-b", "1000", "abcdefghijklmnop", now=1001).accepted


@pytest.mark.parametrize(
    ("timestamp", "nonce", "now", "verdict"),
    [
        ("900", "abcdefghijklmnop", 1000, STALE_TIMESTAMP),
        ("1100", "abcdefghijklmnop", 1000, FUTURE_TIMESTAMP),
        ("1000", "too-short", 1000, INVALID_NONCE),
    ],
)
def test_rejects_bad_replay_metadata(tmp_path: Path, timestamp, nonce, now, verdict):
    guard = HeartbeatReplayGuard(tmp_path / "replay.db", max_skew_seconds=60, retention_seconds=180, require_headers=True)
    result = guard.check_and_record("node-a", timestamp, nonce, now=now)
    assert not result.accepted and result.verdict == verdict


def test_rollout_mode_accepts_missing_headers_but_enforce_mode_does_not(tmp_path: Path):
    observe = HeartbeatReplayGuard(tmp_path / "observe.db", require_headers=False)
    result = observe.check_and_record("node-a", None, None, now=1000)
    assert result.accepted and result.verdict == LEGACY_ACCEPTED

    enforce = HeartbeatReplayGuard(tmp_path / "enforce.db", require_headers=True)
    result = enforce.check_and_record("node-a", None, None, now=1000)
    assert not result.accepted and result.verdict == MISSING_HEADERS


def test_database_permissions_are_private(tmp_path: Path):
    path = tmp_path / "replay.db"
    HeartbeatReplayGuard(path)
    assert path.stat().st_mode & 0o777 == 0o600
