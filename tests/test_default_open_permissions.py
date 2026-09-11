"""Default-open session access: a new session is manageable immediately.

The closed default that preceded this produced one recurring outcome -- a
brand-new session stuck behind a permission nobody had granted yet -- which is
worse than useless for a session manager. So: ABSENCE OF A GRANT RECORD MEANS
ALLOW. A record exists only because someone deliberately changed something,
and its useful shape is now an explicit revoke: an optional lock, never a
prerequisite.

The perimeter is unchanged and is still tested here: sensitive names, denied
patterns and the global switches all still refuse, whatever the default says.
"""
from __future__ import annotations

import time
import uuid

import pytest

from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig, SessionLifecycleConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore


def _service(tmp_path, *, terminal_input=True, denied=(), access=None) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, terminal_input),
        (), 200, 100,                       # no whitelist at all, anywhere
        InputPolicyConfig(allowed_session_patterns=(), denied_session_patterns=tuple(denied)),
        session_access=access or SessionAccessConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),),
                                                 protected_sessions=()),
    )
    return TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))


def _live(factory, prefix: str) -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    factory(name, "bash -lc 'sleep 30'")
    time.sleep(0.25)
    return name


# -- the default itself ------------------------------------------------------

def test_shipped_defaults_are_open():
    access = SessionAccessConfig()
    assert access.default_read is True
    assert access.default_input is True


def test_a_session_with_no_grant_row_is_readable_and_writable(tmp_path):
    svc = _service(tmp_path)
    assert svc.grants.get("brand-new") is None          # nobody granted anything
    described = svc.describe_session_permissions("brand-new")
    assert described["effective"] == {"read": True, "input": True}
    assert described["source"] == "default_policy"
    assert described["requested"] == {"read": None, "input": None}


def test_a_newly_created_session_is_immediately_manageable(tmp_path, tmux_session_factory):
    """The whole point: create it, use it. No grant call in between."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-new")
    assert "error" not in svc.terminal_status(name)
    assert "error" not in svc.terminal_tail(name)
    assert svc.terminal_send_text(name, "echo hi", dry_run=True).get("would_send") is True
    assert "error" not in svc.terminal_send_keys(name, ["Up"])


def test_a_discovered_session_is_listed_and_effective_without_a_grant(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-discover")
    row = next(r for r in svc.terminal_list_sessions()["sessions"] if r["name"] == name)
    assert row["effective_read"] is True
    assert row["effective_input"] is True
    assert row["allowed"] == row["effective_read"]      # deprecated alias agrees


def test_bind_and_supervisor_paths_no_longer_need_an_allowlist(tmp_path, tmux_session_factory):
    from terminal_mcp.supervisor import SupervisorService, SupervisorStore
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-bind")
    assert "error" not in svc.terminal_bind("open-binding", name)
    supervisor = SupervisorService(svc, SupervisorStore(tmp_path / "sup.db"))
    assert "error" not in supervisor.watch(session=name)


# -- durability --------------------------------------------------------------

def test_rename_does_not_touch_permissions(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-rename")
    before = svc.describe_session_permissions(name)["effective"]
    svc.grants.rename_session(name, name + "-new")       # no-op: there is no row
    assert svc.describe_session_permissions(name + "-new")["effective"] == before


def test_a_restart_does_not_lose_access(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-restart")
    assert svc.describe_session_permissions(name)["effective"]["input"] is True
    reopened = _service(tmp_path)
    assert reopened.describe_session_permissions(name)["effective"]["input"] is True


def test_a_legacy_allowed_false_cannot_block_anything(tmp_path, tmux_session_factory):
    """`allowed` is a deprecated alias of effective read. There is no path by
    which it decides anything, so a caller that still reads it can be wrong
    about intent but can never cause a refusal."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-legacy")
    from terminal_mcp.permissions import session_allowed
    assert session_allowed(name, svc.config) is False    # matches no pattern
    assert svc.describe_session_permissions(name)["effective"]["read"] is True


# -- the optional lock -------------------------------------------------------

def test_an_explicit_revoke_still_blocks_immediately(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-revoke")
    svc.set_session_permissions(name, read=False, actor="owner")
    assert svc.terminal_status(name)["error"] == "ACCESS_DENIED"
    assert svc.terminal_send_text(name, "x")["error"] == "ACCESS_DENIED"


def test_a_revoke_can_be_restored(tmp_path, tmux_session_factory):
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-restore")
    svc.set_session_permissions(name, read=False, actor="owner")
    svc.set_session_permissions(name, read=True, input=True, actor="owner")
    assert "error" not in svc.terminal_status(name)


def test_deleting_the_record_returns_the_session_to_the_default(tmp_path, tmux_session_factory):
    """Absence means allow, so removing a row is how a session goes back to
    the default -- distinct from writing read_enabled=0, which means someone
    said no. Keeping those distinguishable is what lets the migration be
    safe."""
    svc = _service(tmp_path)
    name = _live(tmux_session_factory, "open-clear")
    svc.set_session_permissions(name, read=False, actor="owner")
    assert svc.describe_session_permissions(name)["effective"]["read"] is False
    svc.grants.delete(name)
    described = svc.describe_session_permissions(name)
    assert described["effective"] == {"read": True, "input": True}
    assert described["source"] == "default_policy"


# -- the perimeter still holds ----------------------------------------------

def test_sensitive_names_are_still_refused_under_an_open_default(tmp_path):
    svc = _service(tmp_path)
    for name in ("prod-database", "ssh-jump", "team-password-store"):
        assert svc.describe_session_permissions(name)["effective"]["read"] is False


def test_denied_patterns_still_refuse_input(tmp_path, tmux_session_factory):
    svc = _service(tmp_path, denied=("locked-*",))
    name = _live(tmux_session_factory, "locked")
    assert svc.describe_session_permissions(name)["effective"]["input"] is False


def test_the_global_input_switch_still_wins(tmp_path, tmux_session_factory):
    svc = _service(tmp_path, terminal_input=False)
    name = _live(tmux_session_factory, "open-switch")
    described = svc.describe_session_permissions(name)
    assert described["effective"]["read"] is True
    assert described["effective"]["input"] is False


# -- migration ---------------------------------------------------------------

def test_migration_clears_system_written_denies_but_keeps_a_real_one(tmp_path):
    """Every deny row measured on the real fleet was `system:session_deleted`
    -- teardown bookkeeping that only read as "deny" because the default used
    to be closed. Those go. A deny an actual person authored stays, because
    this migration has no business overruling someone locking a session."""
    svc = _service(tmp_path)
    svc.grants.set_read("torn-down", False, granted_by="system:session_deleted")
    svc.grants.set_read("locked-by-owner", False, granted_by="alice@example.com")

    summary = svc.migrate_deny_records_to_default_open()
    assert "torn-down" in summary["cleared"]
    assert [d["session"] for d in summary["preserved_user_denies"]] == ["locked-by-owner"]

    assert svc.describe_session_permissions("torn-down")["effective"]["read"] is True
    assert svc.describe_session_permissions("locked-by-owner")["effective"]["read"] is False


def test_migration_clears_whitelist_migration_rows_that_now_narrow_access(tmp_path):
    """The retired whitelist migration wrote read=1/input=0 for read-only
    pattern matches. Under an open default that row BLOCKS input the default
    would allow -- two sessions on the real fleet were stuck exactly there."""
    svc = _service(tmp_path)
    svc.grants.set_read("was-read-only", True, granted_by="whitelist-migration")
    assert svc.describe_session_permissions("was-read-only")["effective"]["input"] is False

    svc.migrate_deny_records_to_default_open()
    assert svc.describe_session_permissions("was-read-only")["effective"] == {"read": True, "input": True}


def test_migration_is_idempotent(tmp_path):
    svc = _service(tmp_path)
    svc.grants.set_read("torn-down", False, granted_by="system:session_deleted")
    first = svc.migrate_deny_records_to_default_open()
    second = svc.migrate_deny_records_to_default_open()
    assert first["cleared"] == ["torn-down"]
    assert second["cleared"] == []


def test_the_whitelist_migration_is_inert_once_defaults_are_open(tmp_path):
    """It could only ever narrow access now, which is the failure this model
    exists to remove."""
    svc = _service(tmp_path)
    summary = svc.migrate_whitelist_to_grants()
    assert summary.get("skipped")
    assert summary["read_granted"] == []


# -- a grant that outlived the session it was pinned to -----------------------

def _grant_pinned_to_a_dead_identity(service, session: str) -> None:
    """The exact row shape left behind when a granted session is gone:
    read+input granted, pinned to ids that no longer resolve."""
    service.grants.set_read(session, True, granted_by="test")
    service.grants.set_input(session, True, granted_by="test",
                             pinned_session_id="$99999", pinned_pane_id="%99999",
                             pinned_created_epoch=1)


def test_a_stale_identity_pin_falls_back_to_the_default_instead_of_denying(
    tmp_path, tmux_session_factory,
):
    # Live in production as `mesflow`: granted while it ran on the
    # controller, later moved to another node. The controller resolves
    # identity against its OWN tmux, so the pin could never match again and
    # every send was refused with IDENTITY_MISMATCH -- while an identical
    # session with NO grant at all stayed writable. A record must never be
    # worse than no record under a default-open policy.
    service = _service(tmp_path)
    name = _live(tmux_session_factory, "stalepin")
    _grant_pinned_to_a_dead_identity(service, name)

    allowed, reason = service._input_authorized(name)
    assert allowed is True
    assert reason is None


def test_the_stale_pin_is_reported_so_the_row_is_not_silently_inert(
    tmp_path, tmux_session_factory,
):
    service = _service(tmp_path)
    name = _live(tmux_session_factory, "stalepin-desc")
    _grant_pinned_to_a_dead_identity(service, name)

    described = service.describe_session_permissions(name)
    assert described["effective"] == {"read": True, "input": True}
    assert described["stale_identity_pin"] is True
    # The grant is not what decided this, and the answer says so.
    assert described["inherited_from_default_policy"] is True
    assert described["input_block_reason"] is None


def test_a_matching_pin_is_not_reported_stale(tmp_path, tmux_session_factory):
    service = _service(tmp_path)
    name = _live(tmux_session_factory, "livepin")
    identity = service.resolve_identity(name)
    assert identity is not None
    service.grants.set_read(name, True, granted_by="test")
    service.grants.set_input(name, True, granted_by="test",
                             pinned_session_id=identity.session_id,
                             pinned_pane_id=identity.pane_id,
                             pinned_created_epoch=identity.created_epoch)

    described = service.describe_session_permissions(name)
    assert described["stale_identity_pin"] is False
    assert described["source"] == "explicit_grant"
    assert described["effective"] == {"read": True, "input": True}


def test_an_explicit_lock_still_wins_over_a_stale_pin(tmp_path, tmux_session_factory):
    # Falling back to the default must not become a way for a deliberate
    # lock to expire on its own.
    service = _service(tmp_path)
    name = _live(tmux_session_factory, "stalepin-locked")
    _grant_pinned_to_a_dead_identity(service, name)
    service.grants.set_input(name, False, granted_by="test")

    allowed, _ = service._input_authorized(name)
    assert allowed is False
    assert service.describe_session_permissions(name)["effective"]["input"] is False


def test_a_closed_default_keeps_the_old_fail_closed_answer(tmp_path, tmux_session_factory):
    service = _service(tmp_path, access=SessionAccessConfig(default_read=True, default_input=False))
    name = _live(tmux_session_factory, "stalepin-closed")
    _grant_pinned_to_a_dead_identity(service, name)

    allowed, reason = service._input_authorized(name)
    assert allowed is False
    assert reason == "IDENTITY_MISMATCH"
