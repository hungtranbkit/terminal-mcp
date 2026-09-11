"""Session permission management: the API ChatGPT was missing.

It could rename a session but had no way to grant or revoke access to one --
that took an SSH session and a config edit. These tests pin the surface that
replaces that, and the invariants that make it safe to hand to an agent.

There is no session-name whitelist anywhere in here. Permission comes from an
explicit user grant or the deployment's default policy, and `source` says
which -- because "read: false" means something very different when a user
revoked it than when a default nobody has touched produced it.
"""
from __future__ import annotations

import time

import pytest

from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig, SessionLifecycleConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore


def _live(tmux_session_factory, prefix: str) -> str:
    """A real, disposable tmux session -- grant_session_read deliberately
    refuses a session that does not exist, because an input grant pins that
    session's identity and there is nothing to pin otherwise."""
    import uuid
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    tmux_session_factory(name, "bash -lc 'sleep 30'")
    time.sleep(0.25)
    return name


def _service(tmp_path, *, default_read=False, default_input=False, terminal_input=True,
             denied=()) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, terminal_input),
        # Deliberately empty: nothing here may depend on a name whitelist.
        (), 200, 100,
        InputPolicyConfig(allowed_session_patterns=(), denied_session_patterns=tuple(denied)),
        session_access=SessionAccessConfig(default_read=default_read, default_input=default_input),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),),
                                                 protected_sessions=()),
    )
    return TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))


# -- reading before writing --------------------------------------------------

def test_describe_separates_requested_from_effective_and_names_the_source(tmp_path):
    svc = _service(tmp_path, default_read=True)
    described = svc.describe_session_permissions("anything-at-all")
    assert described["requested"] == {"read": None, "input": None}   # nobody granted
    assert described["effective"]["read"] is True                    # ...but policy allows
    assert described["source"] == "default_policy"
    assert described["inherited_from_default_policy"] is True
    assert described["revision"] == 0


def test_an_explicit_grant_is_reported_as_explicit(tmp_path, tmux_session_factory):
    svc = _service(tmp_path, default_read=False)
    name = _live(tmux_session_factory, "perm-explicit")
    svc.set_session_permissions(name, read=True, actor="operator")
    described = svc.describe_session_permissions(name)
    assert described["source"] == "explicit_grant"
    assert described["inherited_from_default_policy"] is False
    assert described["requested"]["read"] is True
    assert described["granted_by"] == "operator"


def test_allowed_is_a_deprecated_alias_never_a_second_answer(tmp_path, tmux_session_factory):
    """The contradictory state this replaced: allowed=false beside
    effective_read=true. They cannot disagree now because one IS the other."""
    svc = _service(tmp_path, default_read=False)
    name = _live(tmux_session_factory, "perm-alias")
    for read in (True, False):
        svc.set_session_permissions(name, read=read, actor="operator")
        described = svc.describe_session_permissions(name)
        assert described["allowed"] == described["effective"]["read"]


# -- writing -----------------------------------------------------------------

def test_granting_input_implies_read(tmp_path, tmux_session_factory):
    """Asking for input on an ungranted session is a request for both --
    the store refuses input without read, so anything else would silently
    half-apply."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-implies")
    result = svc.set_session_permissions(name, input=True, actor="chatgpt")
    assert result["effective"] == {"read": True, "input": True}


def test_revoking_read_revokes_input_with_it(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-revoke")
    svc.set_session_permissions(name, read=True, input=True, actor="chatgpt")
    result = svc.set_session_permissions(name, read=False, actor="chatgpt")
    assert result["effective"] == {"read": False, "input": False}


def test_setting_what_is_already_set_is_a_no_op_not_a_failure(tmp_path, tmux_session_factory):
    """A retried call must not double-apply or error."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-idem")
    first = svc.set_session_permissions(name, read=True, actor="a")
    second = svc.set_session_permissions(name, read=True, actor="a")
    assert second["effective"] == first["effective"]
    assert "error" not in second


def test_nothing_to_change_is_refused_rather_than_silently_ignored(tmp_path):
    assert _service(tmp_path).set_session_permissions("s1")["error"] == "NOTHING_TO_CHANGE"


# -- optimistic concurrency --------------------------------------------------

def test_a_stale_revision_conflicts_instead_of_erasing_the_other_change(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-conflict")
    first = svc.set_session_permissions(name, read=True, actor="a")
    stale = first["revision"] - 1

    svc.set_session_permissions(name, input=True, actor="b")          # someone else moves it

    conflict = svc.set_session_permissions(name, read=False, expected_revision=stale, actor="a")
    assert conflict["error"] == "REVISION_CONFLICT"
    assert conflict["current_revision"] > stale
    assert conflict["current"]["effective"]["input"] is True          # b's change survived


def test_a_matching_revision_applies(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-match")
    current = svc.set_session_permissions(name, read=True, actor="a")
    applied = svc.set_session_permissions(name, input=True,
                                          expected_revision=current["revision"], actor="a")
    assert "error" not in applied
    assert applied["effective"]["input"] is True


def test_a_rename_does_not_bump_the_revision(tmp_path, tmux_session_factory):
    """A rename re-keys a grant; it does not change the permission. Bumping
    would make a concurrent caller's correct revision look stale for a change
    that never happened."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-rename")
    before = svc.set_session_permissions(name, read=True, actor="a")
    svc.grants.rename_session(name, name + "-new")
    after = svc.describe_session_permissions(name + "-new")
    assert after["revision"] == before["revision"]
    assert after["effective"]["read"] is True


# -- floors that no grant may cross -----------------------------------------

def test_a_sensitive_name_cannot_be_granted_at_all(tmp_path):
    svc = _service(tmp_path, default_read=True)
    for name in ("prod-database", "ssh-tunnel", "my-password-vault"):
        assert svc.set_session_permissions(name, read=True)["error"] == "SENSITIVE_SESSION_NOT_GRANTABLE"
        assert svc.describe_session_permissions(name)["effective"]["read"] is False


def test_the_global_input_switch_still_wins_over_a_grant(tmp_path, tmux_session_factory):
    svc = _service(tmp_path, terminal_input=False)
    name = _live(tmux_session_factory, "perm-switch")
    result = svc.set_session_permissions(name, read=True, input=True, actor="a")
    assert result["effective"]["read"] is True
    assert result["effective"]["input"] is False       # switch beats the grant
    assert result["floors"]["global_input_enabled"] is False


def test_a_denied_pattern_beats_a_grant(tmp_path, tmux_session_factory):
    svc = _service(tmp_path, denied=("locked-*",))
    name = _live(tmux_session_factory, "locked")
    result = svc.set_session_permissions(name, read=True, input=True, actor="a")
    assert result["effective"]["input"] is False
    assert result["floors"]["input_denied_by_pattern"] is True


def test_an_invalid_session_name_is_refused(tmp_path):
    assert _service(tmp_path).set_session_permissions("../etc/passwd", read=True)["error"] == "INVALID_SESSION"


# -- no whitelist dependency -------------------------------------------------

def test_permissions_work_with_no_whitelist_configured_at_all(tmp_path, tmux_session_factory):
    """Every config in this file ships an EMPTY allowed_session_patterns. If
    any of this still depended on the whitelist, none of it would work."""
    svc = _service(tmp_path, default_read=False)
    assert svc.config.allowed_session_patterns == ()
    name = _live(tmux_session_factory, "zzz-no-pattern-matches")
    svc.set_session_permissions(name, read=True, input=True, actor="a")
    described = svc.describe_session_permissions(name)
    assert described["effective"] == {"read": True, "input": True}


# -- durability --------------------------------------------------------------

def test_permissions_survive_a_service_restart(tmp_path, tmux_session_factory):
    """Grants live in sqlite, not in process memory -- a controller restart,
    a recovery, or a node reconnect must not quietly reset them."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "perm-restart")
    svc.set_session_permissions(name, read=True, input=True, actor="a")

    reopened = _service(tmp_path)     # fresh service, same store path
    described = reopened.describe_session_permissions(name)
    assert described["effective"] == {"read": True, "input": True}
    assert described["source"] == "explicit_grant"
    assert described["granted_by"] == "a"
