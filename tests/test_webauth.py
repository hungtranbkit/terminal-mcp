"""Unit tests for WebAuthStore -- the local username/password account/
session/rate-limit store backing the /login,/app/* path (webauth_dashboard.py).
Route-level behavior (cookies, CSRF, forced password change, etc.) is
covered separately in test_webauth_dashboard.py; this file only exercises
the store's own API in isolation.
"""
from __future__ import annotations

import time
from datetime import timedelta

from terminal_mcp.webauth import RATE_LIMIT_THRESHOLD, WebAuthStore


def _store(tmp_path) -> WebAuthStore:
    return WebAuthStore(tmp_path / "webauth.db")


def test_new_store_has_no_users(tmp_path):
    store = _store(tmp_path)
    assert store.has_any_user() is False


def test_create_and_verify_password(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "correct horse battery staple")
    assert store.has_any_user() is True
    user = store.verify_password("admin", "correct horse battery staple")
    assert user is not None
    assert user.username == "admin"
    assert user.must_change_password is False


def test_verify_password_rejects_wrong_password(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "correct horse battery staple")
    assert store.verify_password("admin", "wrong password") is None


def test_verify_password_rejects_unknown_username(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "correct horse battery staple")
    assert store.verify_password("nobody", "correct horse battery staple") is None


def test_password_is_never_stored_in_plaintext(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a very secret password value")
    with open(store.path, "rb") as handle:
        raw = handle.read()
    assert b"a very secret password value" not in raw


def test_create_or_replace_user_can_set_must_change_password(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "bootstrap password value!!", must_change_password=True)
    user = store.verify_password("admin", "bootstrap password value!!")
    assert user is not None
    assert user.must_change_password is True


def test_set_password_returns_false_for_unknown_user(tmp_path):
    store = _store(tmp_path)
    assert store.set_password("nobody", "some new password 12345") is False


def test_set_password_updates_and_clears_must_change_flag(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "old password value 1234", must_change_password=True)
    assert store.set_password("admin", "new password value 5678") is True
    user = store.verify_password("admin", "new password value 5678")
    assert user is not None
    assert user.must_change_password is False
    assert store.verify_password("admin", "old password value 1234") is None


def test_set_password_invalidates_every_existing_session(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "old password value 1234")
    token_a = store.create_session("admin")
    token_b = store.create_session("admin")
    assert store.resolve_session(token_a) is not None
    assert store.resolve_session(token_b) is not None
    store.set_password("admin", "new password value 5678")
    assert store.resolve_session(token_a) is None
    assert store.resolve_session(token_b) is None


def test_create_session_and_resolve_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    token = store.create_session("admin")
    user = store.resolve_session(token)
    assert user is not None
    assert user.username == "admin"


def test_resolve_session_rejects_unknown_token(tmp_path):
    store = _store(tmp_path)
    assert store.resolve_session("not-a-real-token") is None


def test_resolve_session_rejects_empty_token(tmp_path):
    store = _store(tmp_path)
    assert store.resolve_session("") is None


def test_session_hash_only_is_stored_never_the_raw_token(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    token = store.create_session("admin")
    with open(store.path, "rb") as handle:
        raw = handle.read()
    assert token.encode("utf-8") not in raw


def test_resolve_session_rejects_expired_session(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    token = store.create_session("admin", ttl=timedelta(seconds=-1))
    assert store.resolve_session(token) is None


def test_destroy_session_invalidates_only_that_token(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    token_a = store.create_session("admin")
    token_b = store.create_session("admin")
    store.destroy_session(token_a)
    assert store.resolve_session(token_a) is None
    assert store.resolve_session(token_b) is not None


def test_destroy_all_sessions_for_user(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    store.create_or_replace_user("other", "another password 5678")
    admin_token = store.create_session("admin")
    other_token = store.create_session("other")
    store.destroy_all_sessions_for("admin")
    assert store.resolve_session(admin_token) is None
    assert store.resolve_session(other_token) is not None


def test_purge_expired_sessions_removes_only_expired_rows(tmp_path):
    store = _store(tmp_path)
    store.create_or_replace_user("admin", "a password value 1234")
    live_token = store.create_session("admin")
    expired_token = store.create_session("admin", ttl=timedelta(seconds=-1))
    store.purge_expired_sessions()
    assert store.resolve_session(live_token) is not None
    assert store.resolve_session(expired_token) is None


def test_rate_limit_allows_a_few_failures_before_locking(tmp_path):
    store = _store(tmp_path)
    for _ in range(RATE_LIMIT_THRESHOLD - 1):
        store.record_failure("1.2.3.4")
    assert store.seconds_until_allowed("1.2.3.4") == 0.0


def test_rate_limit_locks_after_threshold_failures(tmp_path):
    store = _store(tmp_path)
    for _ in range(RATE_LIMIT_THRESHOLD):
        store.record_failure("1.2.3.4")
    assert store.seconds_until_allowed("1.2.3.4") > 0.0


def test_rate_limit_is_bucketed_per_client_key(tmp_path):
    store = _store(tmp_path)
    for _ in range(RATE_LIMIT_THRESHOLD):
        store.record_failure("1.2.3.4")
    assert store.seconds_until_allowed("5.6.7.8") == 0.0


def test_rate_limit_cleared_by_record_success(tmp_path):
    store = _store(tmp_path)
    for _ in range(RATE_LIMIT_THRESHOLD):
        store.record_failure("1.2.3.4")
    assert store.seconds_until_allowed("1.2.3.4") > 0.0
    store.record_success("1.2.3.4")
    assert store.seconds_until_allowed("1.2.3.4") == 0.0


def test_rate_limit_is_not_permanent_it_is_a_bounded_backoff(tmp_path):
    # Not a real-time-based sleep test (too slow/flaky) -- just confirms
    # the lock has a finite, bounded expiry rather than being open-ended.
    from terminal_mcp.webauth import RATE_LIMIT_BACKOFF_CAP_SECONDS

    store = _store(tmp_path)
    for _ in range(RATE_LIMIT_THRESHOLD + 10):
        store.record_failure("1.2.3.4")
    assert store.seconds_until_allowed("1.2.3.4") <= RATE_LIMIT_BACKOFF_CAP_SECONDS
