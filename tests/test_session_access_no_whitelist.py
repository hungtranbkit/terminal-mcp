"""Session access is decided by user grants + a configurable default policy.

The session-name whitelist is gone from every enforcement path. It produced a
state operators reported as a bug -- a row showing `allowed=false` next to
`effective_read=true`/`effective_input=true`, two fields that look like they
must agree -- and it meant granting access to a session required editing
config.yaml and restarting the service, which is not what a per-session grant
is for.

What replaces it:

* explicit grants (grants.db) decide, per session;
* `session_access.default_read`/`default_input` decide when no grant exists;
* `allowed` is a DEPRECATED alias of the real read authorization, so it can
  never contradict `effective_read` again;
* the security boundaries that actually are boundaries -- account/webauth/
  Cloudflare Access, node bearer tokens, the sensitive-name floor, the
  `denied_session_patterns` deny list, and the global permission switches --
  are untouched.
"""
from __future__ import annotations

import time
import uuid

import pytest

from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.permissions import session_allowed


def _config(*, default_read: bool, default_input: bool, denied=()) -> AppConfig:
    # NOTE the whitelist patterns here: deliberately a list that matches
    # NOTHING these tests use. Nothing may depend on them any more.
    return AppConfig(
        PermissionsConfig(True, True), ("nothing-matches-this-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("nothing-matches-this-*",),
                          denied_session_patterns=tuple(denied)),
        session_access=SessionAccessConfig(default_read=default_read, default_input=default_input),
    )


def _service(tmp_path, **kwargs) -> TerminalService:
    return TerminalService(_config(**kwargs), grants=SessionGrantStore(tmp_path / "grants.db"))


def _fresh_name() -> str:
    """A name no config anywhere has ever listed."""
    return f"brandnew-{uuid.uuid4().hex[:10]}"


# -- the reported contradiction is structurally gone ------------------------

def test_allowed_can_never_contradict_effective_read(tmp_path, tmux_session_factory):
    name = _fresh_name()
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path, default_read=False, default_input=False)

    row = next(r for r in service.terminal_list_sessions()["sessions"] if r["name"] == session)
    assert row["allowed"] == row["effective_read"] is False
    assert not session_allowed(session, service.config)  # and the whitelist agrees it never matched

    service.grant_session_read(session, True, granted_by="operator")
    row = next(r for r in service.terminal_list_sessions()["sessions"] if r["name"] == session)
    assert row["allowed"] == row["effective_read"] is True
    # Still not whitelisted -- and that no longer matters to anything.
    assert not session_allowed(session, service.config)


# -- the live workflow the user asked for -----------------------------------

def test_brand_new_session_grant_then_revoke_with_no_restart(tmp_path, tmux_session_factory):
    """A session name that has never appeared in any config: default policy,
    then the user turns read+input on and it works IMMEDIATELY, then turns
    them off and it is refused IMMEDIATELY. No config edit, no restart --
    the same TerminalService instance throughout."""
    name = _fresh_name()
    session = tmux_session_factory(name, "bash -lc 'sleep 30'")
    time.sleep(0.2)
    service = _service(tmp_path, default_read=False, default_input=False)

    # 1. Untouched -> default policy (closed here).
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"

    # 2. User grants read -> readable at once, still cannot send.
    service.grant_session_read(session, True, granted_by="operator")
    assert "error" not in service.terminal_status(session)
    assert "error" not in service.terminal_tail(session)
    assert service.terminal_send_text(session, "x")["error"] in ("ACCESS_DENIED", "GRANT_REQUIRED")

    # 3. User grants input -> sending works at once.
    service.grant_session_input(session, True, granted_by="operator")
    assert service.terminal_send_text(session, "echo ok", dry_run=True).get("would_send") is True
    assert "error" not in service.terminal_send_keys(session, ["Up"])

    # 4. User revokes read -> everything closes at once (input dies with it).
    service.grant_session_read(session, False, granted_by="operator")
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"


def test_open_default_policy_makes_a_brand_new_session_readable_immediately(tmp_path, tmux_session_factory):
    """The other supported shape: a deployment that chooses open reads. A
    never-seen session is readable with no grant and no restart -- which under
    the whitelist required editing config.yaml and bouncing the service."""
    session = tmux_session_factory(_fresh_name(), "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path, default_read=True, default_input=False)
    assert "error" not in service.terminal_status(session)
    # Input is still closed: reading and driving are different decisions.
    assert service.terminal_send_text(session, "x")["error"] in ("ACCESS_DENIED", "GRANT_REQUIRED")


# -- boundaries that must survive -------------------------------------------

def test_sensitive_names_are_refused_whatever_the_policy_says(tmp_path):
    """The one name-based rule kept on purpose. An open default policy must
    not make "prod-database" readable."""
    service = _service(tmp_path, default_read=True, default_input=True)
    for name in ("prod-database", "ssh-tunnel", "root-shell", "my-password-store"):
        assert service._read_authorized(name) is False
        assert service._input_authorized(name)[0] is False


def test_deny_patterns_still_beat_an_explicit_grant(tmp_path, tmux_session_factory):
    """input_policy.denied_session_patterns is a hard floor -- config-level
    DENY, not a whitelist -- and a grant may not override it."""
    name = _fresh_name()
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path, default_read=True, default_input=True, denied=(name,))
    service.grant_session_read(session, True, granted_by="operator")
    service.grant_session_input(session, True, granted_by="operator")
    assert service._input_authorized(session)[0] is False


def test_global_input_switch_still_wins(tmp_path, tmux_session_factory):
    session = tmux_session_factory(_fresh_name(), "bash -lc 'sleep 20'")
    time.sleep(0.2)
    config = _config(default_read=True, default_input=True)
    config = type(config)(**{**config.__dict__, "permissions": PermissionsConfig(True, False)})
    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    assert service.terminal_send_text(session, "x").get("error") is not None


# -- migration ---------------------------------------------------------------

def test_migration_converts_the_old_whitelist_into_real_grants(tmp_path, tmux_session_factory):
    """Upgrading must not silently revoke access. Anything the retired
    whitelist used to authorize becomes an explicit grant, exactly once."""
    name = f"test-migrate-{uuid.uuid4().hex[:8]}"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=False, default_input=False),
    )
    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    assert service._read_authorized(session) is False  # closed before migrating

    summary = service.migrate_whitelist_to_grants()
    assert session in summary["read_granted"]
    assert session in summary["input_granted"]
    assert service._read_authorized(session) is True
    assert service._input_authorized(session)[0] is True

    # Idempotent: a second run adds nothing.
    again = service.migrate_whitelist_to_grants()
    assert session not in again["read_granted"]
    assert session in again["skipped_existing"]


def test_migration_never_resurrects_an_access_the_user_revoked(tmp_path, tmux_session_factory):
    """A user who deliberately revoked read on a still-whitelisted session
    must not have it handed back by the migration."""
    name = f"test-revoked-{uuid.uuid4().hex[:8]}"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=False, default_input=False),
    )
    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    service.grants.set_read(session, False, granted_by="operator")

    service.migrate_whitelist_to_grants()
    assert service._read_authorized(session) is False
