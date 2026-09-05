from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .adapters import (DELIVERY_BLOCKED, DELIVERY_ERROR, DELIVERY_SUBMIT_CONFIRMED, DELIVERY_TEXT_SENT,
                       DELIVERY_UNKNOWN, TARGET_WAITING, _sent_text_echoed, select_adapter,
                       to_legacy_submit_status)
from .audit import AuditStore
from .bindings import Binding, BindingStore, valid_binding_name
from .config import AppConfig
from .grants import SessionGrant, SessionGrantStore
from .killed_sessions import KilledSessionStore
from .lease import DEFAULT_LEASE_TTL_SECONDS, PaneLeaseStore
from .lifecycle import SessionLifecycleService, resolve_cwd
from .metrics import record_delivery_outcome
from .models import SessionIdentity
from .permissions import (SENSITIVE_SESSION_WORDS, input_session_allowed,
                          require_input, require_read, require_session_lifecycle, session_allowed,
                          session_input_denied_by_pattern, valid_session_name)
from .redaction import redact_ansi_safe, redact_text
from .session_backend import SessionBackend
from .session_registry import SessionRegistryStore
from .status import classify_status
from .tmux import SEND_TEXT_ENTER_SETTLE_SECONDS, TmuxClient, TmuxError, iso_timestamp


class PaneLockRegistry:
    """P0-3: one lock per canonical pane identity (falling back to session
    name if identity can't be resolved), shared by every send path -- plain
    terminal_send_text/terminal_send_bound calls and Supervisor v2's
    execute_send all go through the *same* TerminalService instance in this
    process, so a registry owned by TerminalService is automatically shared
    across all of them. Guarantees two sends targeting the same pane can
    never interleave their text/Enter keystrokes, and (combined with the
    idempotency-key claim, which happens before the lock is even needed)
    that concurrent duplicate requests never both proceed to send."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


SENSITIVE_COMMANDS = {"ssh", "mysql", "psql", "sudo", "passwd"}

# Kill/Reopen reopen-metadata classification (terminal_kill_session): a
# pane_current_command that matches one of THESE at kill time is
# recognized as "just a plain shell" (agent_type="shell", no launcher
# token needed to reopen it safely) -- never an arbitrary command, never
# used as a launcher itself, only as a classification label. Anything
# else that doesn't match a configured session_lifecycle.launch_commands
# value either is simply unrecognized (agent_type=None, incomplete
# metadata) -- never guessed at.
SHELL_COMMAND_NAMES = {
    "bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh",
    # Windows shells (multi-node Windows support) -- a WindowsSessionBackend
    # session's own pane_current_command reports whichever of these was
    # actually launched (see windows_backend.py's WindowsSessionBackend.
    # shell), with or without the ".exe" suffix depending on how it was
    # configured/reported.
    "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
}

# Post-send submission verification (terminal_send_text/terminal_send_bound,
# press_enter=True only). tmux.send_text already adds a fixed settle delay
# between the text and Enter keystrokes -- this is a *second*, independent
# layer on top: a best-effort check, after the send, of whether the pane
# shows evidence Enter actually submitted rather than just having been
# typed. SEND_VERIFY_LINES is how much of the tail we look at;
# SEND_VERIFY_TIMEOUT_SECONDS bounds the total time spent polling for a
# transition (never unbounded); SEND_VERIFY_POLL_INTERVAL_SECONDS is the
# gap between polls -- short enough that a fast-confirming send doesn't pay
# the full timeout, long enough not to hammer tmux.
SEND_VERIFY_LINES = 20
SEND_VERIFY_TIMEOUT_SECONDS = 0.6
SEND_VERIFY_POLL_INTERVAL_SECONDS = 0.05
# Live-tested against the real Codex CLI (not just a synthetic fixture):
# the base 0.6s window is fine for a simple shell but too short for an
# LLM-backed agent CLI to visibly start responding -- a real send that
# genuinely worked ("pong" came back correctly) was still reported
# unconfirmed because no new output had appeared yet within 0.6s. That is
# the safe failure direction (never a false CONFIRMED), but it is
# needlessly pessimistic for adapters known to need longer (Codex, Claude).
# Applies to *both* the initial check and the post-recovery re-check for
# those adapters only -- every other target keeps the original,
# already-tested 0.6s window unchanged.
RECOVERY_VERIFY_TIMEOUT_SECONDS = 3.0
# P0 Part A: adapters (adapters.py) known to need the wider verification
# window above -- an LLM-backed CLI genuinely takes longer to visibly
# respond than a plain shell. Only affects *timeout*, not whether recovery
# itself is attempted (that is decided per-attempt by the selected
# adapter's own stuck_composer_evidence/safe_recovery_allowed, not by this
# set) -- see _send_text_and_verify_locked.
WIDE_VERIFY_ADAPTERS = {"codex", "claude"}
# P0 Part B: how long a send waits for another process's already-held
# pane lease before giving up and failing safely (PANE_BUSY) -- bounded so
# a request never blocks indefinitely on someone else's send, generous
# enough that a genuinely short-lived, benign race (two callers happening
# to target the same pane moments apart) serializes cleanly instead of
# needlessly failing one of them.
PANE_LEASE_WAIT_SECONDS = 5.0
PANE_LEASE_POLL_INTERVAL_SECONDS = 0.1


class TerminalService:
    def __init__(self, config: AppConfig, tmux: SessionBackend | None = None,
                 bindings: BindingStore | None = None,
                 audit: AuditStore | None = None,
                 grants: SessionGrantStore | None = None,
                 leases: PaneLeaseStore | None = None,
                 killed_sessions: KilledSessionStore | None = None,
                 session_registry: SessionRegistryStore | None = None) -> None:
        # `tmux` accepts ANY SessionBackend (session_backend.py) -- a
        # TmuxClient (the default, Linux) or a WindowsSessionBackend
        # (windows_backend.py, injected explicitly by windows_agent.py).
        # Every permission/audit/redaction/kill-reopen-metadata/reliable-
        # submission behavior below is written entirely in terms of that
        # narrow, already-generic surface and runs completely unchanged
        # regardless of which backend this is -- see session_backend.py's
        # own module docstring for why.
        self.config = config
        self.tmux = tmux or TmuxClient()
        self.bindings = bindings or BindingStore()
        self.audit = audit or AuditStore()
        self.grants = grants or SessionGrantStore()
        # Kill/Reopen reopen-metadata store -- see killed_sessions.py and
        # terminal_kill_session/terminal_reopen_session below.
        self.killed_sessions = killed_sessions or KilledSessionStore()
        # Persistent Session Registry -- durable record of every session
        # ever discovered/created, independent of the tmux/Windows-backend
        # process's own lifetime (session_registry.py's own module
        # docstring has the full rationale/design). This process's own
        # node has no name from its own point of view -- REGISTRY_LOCAL_
        # NODE_ID is a private, per-process convention (mirrors
        # controller.py's LOCAL_NODE_ID); a controller merging this with
        # other nodes' registries rewrites it to that node's real id,
        # exactly like it already does for plain session rows.
        self.session_registry = session_registry or SessionRegistryStore()
        # P0 Part B: durable, cross-process pane lease -- defaults to the
        # shared on-disk store (same file for every TerminalService in
        # every process on this host, HTTP/STDIO/dashboard/Supervisor
        # alike) unless a caller injects an isolated one (tests). See
        # lease.py for why this exists on top of _pane_locks below, which
        # only ever serializes *within this one process*.
        self.leases = leases or PaneLeaseStore()
        self._pane_locks = PaneLockRegistry()
        # Session create/detach/delete -- see lifecycle.py's module
        # docstring for why this is composed here rather than folded into
        # this already-large class: one small, independently-testable
        # object, shared by both the dashboard routes and the MCP tools.
        self.lifecycle = SessionLifecycleService(config, self.tmux)

    # Private, per-process convention for this TerminalService's OWN node
    # in the Persistent Session Registry -- see session_registry.py's own
    # module docstring and this class's __init__ comment on
    # self.session_registry for why this is never this node's real,
    # controller-assigned id.
    REGISTRY_LOCAL_NODE_ID = "local"

    def _reconcile_session_registry(self, items: list[Any], grants_by_session: dict[str, SessionGrant]) -> None:
        """Called from terminal_list_sessions/dashboard_list_sessions --
        the ONE place this runs, reusing the exact tmux.list_sessions()
        result those methods already fetched (zero extra tmux calls).
        Upserts an ACTIVE record for every currently-live session, then
        marks any record that WAS active but isn't in `items` this time
        as MISSING -- the reconcile loop the whole registry depends on to
        ever notice a session vanished (a tmux-server restart, an out-of-
        band kill) rather than just silently going stale forever.

        Never raises: a real, currently-broken git binary or an
        unreadable cwd must never break session listing itself, which is
        the one path that has to stay usable no matter what (an operator
        reading `tmux ls`-equivalent data should never be blocked by a
        registry bug)."""
        try:
            seen: set[str] = set()
            bindings_by_session: dict[str, list[str]] = {}
            for binding in self.bindings.list():
                bindings_by_session.setdefault(binding.session, []).append(binding.name)
            launch_commands_by_type = dict(self.config.session_lifecycle.launch_commands)
            for item in items:
                seen.add(item.name)
                grant = grants_by_session.get(item.name)
                cwd = None
                if item.pane_current_path:
                    resolved, error = resolve_cwd(item.pane_current_path, self.config)
                    if error is None:
                        cwd = str(resolved)
                agent_type = self._classify_agent_type(item.pane_current_command)
                launcher = launch_commands_by_type.get(agent_type) if agent_type else None
                binding_names = tuple(bindings_by_session.get(item.name, ()))
                self.session_registry.upsert_seen(
                    self.REGISTRY_LOCAL_NODE_ID, item.name, backend_type=self._registry_backend_type(),
                    cwd=cwd, agent_type=agent_type, launch_command=launcher, launcher_type=agent_type,
                    read_granted=bool(grant and grant.read_enabled), input_granted=bool(grant and grant.input_enabled),
                    binding_names=binding_names,
                )
            self.session_registry.mark_missing(self.REGISTRY_LOCAL_NODE_ID, seen)
        except Exception:  # noqa: BLE001 -- see docstring: never let a registry bug break listing itself
            pass

    def _registry_backend_type(self) -> str:
        # tmux.py's TmuxClient has a `.binary` attribute; windows_backend.py's
        # WindowsSessionBackend does not (see webterm.py's own identical
        # dispatch pattern) -- reused here rather than a new isinstance
        # check against a class this module would otherwise never import.
        return "tmux" if hasattr(self.tmux, "binary") else "windows_pty"

    def _desktop_metadata_for(self, session: str) -> dict[str, Any]:
        """Task item 7 (dashboard: "Desktop visible/Headless"): {} on any
        backend that has no such concept at all (tmux -- duck-typed via
        the same `get_desktop_metadata` presence check every other cross-
        backend dispatch in this file already uses, never an isinstance
        against a class this module doesn't import), never raises."""
        getter = getattr(self.tmux, "get_desktop_metadata", None)
        if getter is None:
            return {}
        try:
            return getter(session) or {}
        except Exception:  # noqa: BLE001 -- desktop metadata is informational only, never fatal to a listing
            return {}

    def terminal_desktop_capability(self) -> dict[str, Any]:
        """Session-independent probe (task item 4/6): can a NEW session
        on THIS node even be created with show_on_desktop=True right now
        -- {} on a backend with no such concept (tmux)."""
        getter = getattr(self.tmux, "desktop_capability", None)
        if getter is None:
            return {}
        try:
            return getter() or {}
        except Exception:  # noqa: BLE001
            return {}

    def resolve_identity(self, session: str) -> SessionIdentity | None:
        try:
            info = self.tmux.get_session(session)
        except TmuxError:
            return None
        return None if info is None else SessionIdentity.from_session_info(info)

    # -- P0 HOTFIX: canonical authorization decisions --------------------
    # A single pair of functions every read/input path in this file (and
    # supervisor.py's session-kind watch()) now goes through, replacing
    # ad-hoc "session_allowed(...) or ..." duplicated separately across
    # _guard/_input_guard/terminal_bind/_resolve_binding/_binding_result/
    # terminal_input_context/terminal_list_sessions/dashboard_list_
    # sessions. Root cause of the live promptflow bug this exists to fix:
    # terminal_list_sessions/dashboard_list_sessions compute "allowed OR
    # granted" for their *display* fields, but every actual read/input
    # tool (terminal_status/terminal_capture/terminal_input_context/
    # terminal_bind/terminal_send_text/terminal_send_keys) gated on
    # session_allowed/input_session_allowed alone via _guard/_input_guard
    # -- a session authorized only via an active dashboard grant (never
    # in the static config.yaml whitelist, which is the entire point of a
    # grant) reported read_allowed=true/input_allowed=true in the list
    # response while every actual operation still returned ACCESS_DENIED.
    # These two functions are now the ONLY place that decision is made;
    # every call site below defers to them rather than re-deriving it.

    def _read_authorized_with_grant(self, session: str, grant: SessionGrant | None) -> bool:
        """True iff the static read whitelist authorizes `session`, OR an
        active grant's read_enabled does. `grant` is a parameter (rather
        than looked up here) so a caller iterating many sessions (the list
        endpoints) can pass in one bulk SessionGrantStore.list() fetch
        instead of one query per session -- see _read_authorized below for
        the single-session convenience wrapper every other call site uses.
        Sensitive-worded names are refused even with a grant, as defense
        in depth: grant_session_read already refuses to grant one in the
        first place, so this should be unreachable, not a new hole."""
        if session_allowed(session, self.config):
            return True
        if any(word in session.casefold() for word in SENSITIVE_SESSION_WORDS):
            return False
        return bool(grant and grant.read_enabled)

    def _read_authorized(self, session: str) -> bool:
        return self._read_authorized_with_grant(session, self.grants.get(session))

    def _input_authorized_with_grant(self, session: str, grant: SessionGrant | None, *,
                                     revalidate_identity: bool = True,
                                     current_identity: SessionIdentity | None = None) -> tuple[bool, str | None]:
        """Returns (authorized, specific_error). The hard deny floor
        (session_input_denied_by_pattern) is checked first and can never
        be overridden by a grant -- exactly the same absolute floor
        grant_session_input itself already enforces at grant time, re-
        checked here at use time too (a deny pattern added to config.yaml
        after a grant already exists must still take effect immediately).
        Otherwise: the static input policy OR an active grant whose
        read_enabled AND input_enabled are both true (a read-only grant
        never authorizes input -- matches grant_session_input's own "read
        must already be granted" invariant, and revoking read alone
        already clears input too, see SessionGrantStore.set_read).

        Identity revalidation (P0-2's invariant: a revoked/expired grant
        fails closed immediately, and a same-name-recreated session --
        e.g. after a tmux-server restart -- never inherits an old grant's
        input authorization) now ALWAYS runs, one way or the other:
        `current_identity`, when given, is used directly (zero extra tmux
        cost -- see below); otherwise this resolves it itself via a fresh
        tmux round-trip (`revalidate_identity=False` is kept only as an
        explicit escape hatch for a caller with neither, and should not be
        used for anything that claims to report real access).

        REAL BUG FIXED HERE (found live: a dashboard-granted session named
        "mesflow" reported effective_input=true after this host's tmux
        server was restarted and every session recreated under the same
        names with new session_id/pane_id/created_epoch -- the grant's
        OLD pinned identity no longer matched, so every actual send
        [terminal_send_text, always identity-revalidated] failed with
        IDENTITY_MISMATCH despite the dashboard/terminal_list_sessions
        claiming input was available). Root cause: the list/discovery
        endpoints (terminal_list_sessions/dashboard_list_sessions) used to
        pass revalidate_identity=False outright, skipping the identity
        check entirely "to avoid a tmux subprocess call per granted
        session" -- but every one of those call sites ALREADY has the
        session's current SessionInfo in hand from the same bulk
        tmux.list_sessions() call that produced the row being built, and
        that struct already carries session_id/pane_id/created_epoch, the
        exact fields this check needs -- so passing it in as
        `current_identity` makes the listing identity-aware (and
        therefore truthful: effective_input now means what it says) at
        literally zero extra cost, never a new tmux round-trip."""
        if session_input_denied_by_pattern(session, self.config):
            return False, None
        if input_session_allowed(session, self.config):
            return True, None
        if not grant or not grant.read_enabled or not grant.input_enabled:
            return False, None
        current = current_identity
        if current is None:
            if not revalidate_identity:
                return True, None
            current = self.resolve_identity(session)
        if current is None or not grant.pinned_session_id:
            return False, "IDENTITY_MISMATCH"
        pinned = SessionIdentity(name=session, session_id=grant.pinned_session_id,
                                 pane_id=grant.pinned_pane_id or "", created_epoch=grant.pinned_created_epoch or 0)
        if not pinned.matches(current):
            return False, "IDENTITY_MISMATCH"
        return True, None

    def _input_authorized(self, session: str) -> tuple[bool, str | None]:
        return self._input_authorized_with_grant(session, self.grants.get(session))

    def _bind_authorized(self, session: str) -> bool:
        """Canonical bind-target authorization: the same 'sensitive names
        never bindable, even with an exact whitelist entry' floor
        binding_session_allowed already enforced, plus (new) an active
        read grant as an alternate path to the same read-level
        authorization terminal_status/terminal_tail/etc. now accept.
        Input through a resulting binding stays separately gated by the
        binding's own input_enabled flag plus _input_guard/
        _check_binding_identity on every actual send, exactly as before
        -- this only decides whether the session may be bound/observed
        through a binding at all."""
        if any(word in session.casefold() for word in SENSITIVE_SESSION_WORDS):
            return False
        return self._read_authorized(session)

    def _audit_result(self, response: dict[str, Any], *, action: str,
                      session: str | None, binding: str | None = None,
                      text: str | None = None, keys: list[str] | None = None,
                      press_enter: bool = False, origin: str | None = None,
                      trace_id: str | None = None, parent_turn_id: str | None = None,
                      depth: int | None = None) -> dict[str, Any]:
        error = response.get("error")
        if error:
            result = "BLOCKED"
        elif response.get("dry_run"):
            result = "DRY_RUN"
        elif response.get("submit_status") == "SUBMIT_UNCONFIRMED":
            # Distinct from plain "SENT": text delivery itself succeeded,
            # but the Enter's submission could not be confirmed within the
            # verification window -- see _send_text_and_verify. Keeping this
            # as its own audit category (not folded into "SENT" or an
            # error) is what makes exactly this class of intermittent bug
            # discoverable from the audit log instead of only from a user
            # report.
            result = "SENT_UNCONFIRMED"
        else:
            result = "SENT"
        # submit_reason (only set on the unconfirmed path) carries no
        # prompt content -- it is a fixed short phrase describing *why*
        # verification was inconclusive, same redaction posture as every
        # other reason string already passed through here.
        self.audit.record(action=action, binding=binding, session=session, text=text,
                          keys=keys, press_enter=press_enter, result=result,
                          reason=error or response.get("reason") or response.get("submit_reason"),
                          correlation_id=response.get("correlation_id"),
                          origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth)
        record_delivery_outcome(response)
        return response

    def _input_guard(self, session: str) -> dict[str, Any] | None:
        """P0 HOTFIX: now defers to _input_authorized (static policy OR an
        active, identity-revalidated grant) instead of input_session_
        allowed alone -- this is the actual fix for the reported bug:
        every input tool (terminal_send_text/terminal_send_keys/
        terminal_send_bound) calls through here."""
        if (error := require_input(self.config)) is not None:
            return {"error": error, "session": session}
        authorized, specific_error = self._input_authorized(session)
        if not authorized:
            return {"error": specific_error or "ACCESS_DENIED", "session": session}
        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        if info is None:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        # P0 audit finding #14: a pane in tmux copy-mode (a human manually
        # scrolled it, or an errant key sequence entered it) intercepts
        # EVERY keystroke -- text or key -- for its own scrollback/search/
        # selection UI; none of it ever reaches the underlying program's
        # pty, regardless of what pane_current_command reports (the
        # foreground process is unaffected and unaware, so this is
        # invisible to every other check here). Without this, a send in
        # this state silently comes back as generic DELIVERY_UNKNOWN with
        # no indication of why, indefinitely, until something exits copy-
        # mode out of band. Refused outright and uniformly for every input
        # path (terminal_send_text/_bound and terminal_send_keys alike --
        # an allowlisted key is exactly as swallowed by tmux's copy-mode
        # keytable as free text would be, so there is no safer subset to
        # carve out here) with a clear, specific diagnostic instead.
        # Deliberately never auto-exits copy-mode itself -- that would be
        # exactly the kind of blind, unrequested recovery action this
        # project's send guards otherwise avoid; resolving it (e.g.
        # `tmux attach` and pressing `q`, or `tmux send-keys -X cancel`
        # run directly by an operator) is an out-of-band action, not
        # something this guarded pipeline performs on a caller's behalf.
        if info.pane_in_mode:
            return {"error": "PANE_IN_COPY_MODE", "session": session,
                    "reason": "the target pane is in tmux copy-mode (scrollback/search/selection) -- "
                              "every keystroke would be intercepted by tmux itself and never reach the "
                              "underlying program; an operator must exit copy-mode out of band (e.g. "
                              "attach and press 'q', or run `tmux send-keys -t <session> -X cancel` "
                              "directly) before input can proceed again"}
        command = info.pane_current_command.casefold()
        allowed = {item.casefold() for item in self.config.input_policy.allowed_sensitive_commands}
        if command in SENSITIVE_COMMANDS and command not in allowed:
            return {"error": "SENSITIVE_TARGET", "session": session, "current_command": command}
        return None

    def _guard(self, session: str) -> dict[str, Any] | None:
        """P0 HOTFIX: now defers to _read_authorized (static policy OR an
        active read grant) instead of session_allowed alone -- this is
        the actual fix for the reported bug: every read tool
        (terminal_tail/terminal_capture/terminal_status/
        terminal_input_context) calls through here. input_action was
        never actually passed True by any caller (input has its own,
        separate _input_guard) -- dropped as dead code, not a behavior
        change."""
        if (permission_error := require_read(self.config)) is not None:
            return {"error": permission_error, "session": session}
        if not self._read_authorized(session):
            return {"error": "ACCESS_DENIED", "session": session}
        return None

    def terminal_list_sessions(self) -> dict[str, Any]:
        """Full tmux session inventory (every real session, not only
        statically-whitelisted ones) -- matches dashboard_list_sessions'
        own discovery scope so any MCP client (Claude Code, ChatGPT via
        the tunnel, ...) can discover the same sessions an operator can
        see and grant from the dashboard. Discovery itself is NOT access:
        `name`/`attached`/`windows`/`created`/`activity` are tmux
        metadata, never pane content, so listing them for a non-
        whitelisted/non-granted session leaks nothing a caller couldn't
        already see by running `tmux ls` themselves on this host -- the
        actual content/control tools (terminal_tail/terminal_capture/
        terminal_status/terminal_send_text/terminal_send_keys/
        terminal_bind and everything built on them) enforce the exact same
        canonical authorization this listing reports (_read_authorized/
        _input_authorized, below) -- discovery never bypasses or widens
        anything; it now simply agrees with what those tools will
        actually do, which it previously did not (see the P0 HOTFIX note
        on _read_authorized_with_grant for the bug this fixes).

        `allowed` is kept ONLY as compatibility/derived metadata -- the
        static config.yaml whitelist result, exactly as before -- and must
        never be read as an independent, possibly-contradictory gate: a
        session can perfectly validly show `allowed=false` alongside
        `read_allowed=true`/`input_allowed=true` (an active dashboard
        grant, never in the static whitelist -- that is the entire point
        of a grant), and every actual read/input tool honors read_allowed/
        input_allowed exactly, via the same canonical _read_authorized/
        _input_authorized_with_grant this method itself calls -- see the
        P0 HOTFIX note above _read_authorized_with_grant for the bug this
        fixes. `read_granted`/`input_granted` reflect an explicit per-
        session dashboard grant (see grants.py), if any -- read live off
        SessionGrantStore on every call, so a grant/revoke issued from the
        dashboard is reflected here immediately, no restart needed.
        `read_allowed`/`input_allowed` are the actual, CURRENT effective
        capability (statically allowed OR granted, and for input also
        gated on the global terminal_input permission) -- the single
        field a caller should check before attempting a read or a send; a
        caller that only wants "can I do X right now" never needs to
        reason about allowed vs. *_granted separately. `effective_read`/
        `effective_input` are exact aliases of read_allowed/input_allowed
        kept for naming parity with dashboard_list_sessions -- same
        values, added rather than renaming the originals to preserve
        backward compatibility for existing callers."""
        if (error := require_read(self.config)) is not None:
            return {"error": error, "sessions": []}
        try:
            items = self.tmux.list_sessions()
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc), "sessions": []}
        grants_by_session = {grant.session: grant for grant in self.grants.list()}
        sessions = []
        for item in items:
            grant = grants_by_session.get(item.name)
            read_granted = bool(grant and grant.read_enabled)
            input_granted = bool(grant and grant.input_enabled)
            read_allowed = self._read_authorized_with_grant(item.name, grant)
            input_allowed = bool(
                self.config.permissions.terminal_input
                and self._input_authorized_with_grant(
                    item.name, grant, current_identity=SessionIdentity.from_session_info(item))[0]
            )
            row = {
                "name": item.name, "allowed": session_allowed(item.name, self.config), "attached": item.attached,
                "windows": item.windows, "created": iso_timestamp(item.created_epoch),
                "activity": iso_timestamp(item.activity_epoch),
                "read_allowed": read_allowed, "read_granted": read_granted,
                "input_allowed": input_allowed, "input_granted": input_granted,
                "effective_read": read_allowed, "effective_input": input_allowed,
            }
            row.update(self._desktop_metadata_for(item.name))
            sessions.append(row)
        self._reconcile_session_registry(items, grants_by_session)
        return {"sessions": sessions}

    def terminal_tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        """Return sanitized recent output. `ansi` is keyword-only, defaults to
        False, and is never set by the MCP tool wrapper — only the dashboard's
        terminal-style renderer opts in to get colour/style escape sequences
        back (still redaction-safe; see redact_ansi_safe). Every other caller
        keeps today's exact plain-text behavior unchanged."""
        if error := self._guard(session):
            return error
        return self._tail_payload(session, lines, ansi=ansi)

    def _tail_payload(self, session: str, lines: int | None, *, ansi: bool) -> dict[str, Any]:
        """The actual tail read + redact, with no guard of its own -- every
        caller (terminal_tail's static-whitelist path, terminal_tail_granted's
        dashboard-grant path) checks its own authorization first and calls
        this only once authorized, so the read logic itself exists in
        exactly one place."""
        requested = self.config.default_tail_lines if lines is None else lines
        if requested < 1:
            return {"error": "INVALID_LINES", "session": session}
        effective = min(requested, self.config.max_capture_lines)
        try:
            output_lines = self.tmux.capture_lines(session, effective, ansi=ansi)
            redact = redact_ansi_safe if ansi else redact_text
            return {
                "session": session,
                "lines_requested": requested,
                "output": redact("\n".join(output_lines)),
                "truncated": requested > self.config.max_capture_lines,
                # P0-9: `output` is text the *watched program* printed, not
                # an instruction from this tool or from terminal-mcp itself
                # -- a caller (human or an external model driving these
                # tools) must treat it as untrusted evidence to read, never
                # as directives to follow. untrusted_fields names exactly
                # which key(s) in this response carry that content.
                "untrusted_output": True, "untrusted_fields": ["output"], "content_source": "session",
            }
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}

    def terminal_capture(self, session: str, start_line: int | None = None) -> dict[str, Any]:
        if error := self._guard(session):
            return error
        if start_line is not None and start_line < 0:
            return {"error": "INVALID_START_LINE", "session": session}
        try:
            captured = self.tmux.capture_lines(session, self.config.max_capture_lines)
            start = start_line or 0
            sliced = captured[start : start + self.config.max_capture_lines]
            truncated = start + len(sliced) < len(captured)
            return {
                "session": session,
                "start_line": start_line,
                "output": redact_text("\n".join(sliced)),
                "lines_returned": len(sliced),
                "truncated": truncated,
                "max_capture_lines": self.config.max_capture_lines,
                "untrusted_output": True, "untrusted_fields": ["output"], "content_source": "session",
            }
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}

    def terminal_status(self, session: str) -> dict[str, Any]:
        if error := self._guard(session):
            return error
        return self._status_payload(session)

    def _status_payload(self, session: str) -> dict[str, Any]:
        """The actual status read + classify + redact, with no guard of its
        own -- see _tail_payload's docstring for why this split exists."""
        try:
            info = self.tmux.get_session(session)
            if info is None:
                return {"session": session, "exists": False, "allowed": True, "state": "UNKNOWN", "input_required": False, "reason": "session does not exist", "last_output": ""}
            output = "\n".join(self.tmux.capture_lines(session, 80))
            state, input_required, reason = classify_status(info, output)
            last_output = "\n".join(output.splitlines()[-20:])
            return {
                "session": session,
                "exists": True,
                "allowed": True,
                "state": state,
                "input_required": input_required,
                "reason": reason,
                "last_output": redact_text(last_output),
                "untrusted_output": True, "untrusted_fields": ["last_output"], "content_source": "session",
            }
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "session": session, "reason": str(exc)}

    def _acquire_pane_lease(self, lock_key: str, owner_id: str) -> bool:
        """P0 Part B: bounded serialize-then-fail-safe. A concurrent
        attempt to the same pane (any process, this one included) that
        already holds the lease means this attempt waits briefly for it to
        finish (the common case: a real send+verify cycle is a few seconds
        at most) rather than failing immediately on a benign, short-lived
        race -- but never waits unboundedly: past PANE_LEASE_WAIT_SECONDS
        this gives up and the caller fails safely (PANE_BUSY) instead of
        blocking a request indefinitely on another process's send."""
        if self.leases.acquire(lock_key, owner_id, ttl_seconds=DEFAULT_LEASE_TTL_SECONDS):
            return True
        deadline = time.monotonic() + PANE_LEASE_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(PANE_LEASE_POLL_INTERVAL_SECONDS)
            if self.leases.acquire(lock_key, owner_id, ttl_seconds=DEFAULT_LEASE_TTL_SECONDS):
                return True
        return False

    # Evidence-code vocabulary for the `evidence` receipt field (P6/P2,
    # docs/prompt-submission.md) -- deliberately just ONE honest code per
    # case rather than a richer taxonomy (INPUT_CLEARED/AGENT_RUNNING/
    # TURN_CREATED/PROMPT_ECHOED) this project's adapters cannot actually
    # distinguish today: every adapter's submit_ack_evidence ultimately
    # answers one yes/no question -- "did genuine pane output move past the
    # pre-Enter baseline" -- so OUTPUT_CHANGED is the only claim that is
    # always true of what was actually checked. Never invents evidence a
    # caller did not really observe.
    _EVIDENCE_OUTPUT_CHANGED = "OUTPUT_CHANGED"
    _EVIDENCE_RECOVERY = "RECOVERY_ESCAPE_ENTER"
    _EVIDENCE_TEXT_SENT = "TEXT_SENT"

    def _enrich_receipt(self, result: dict[str, Any]) -> dict[str, Any]:
        """Adds a small set of additive, backward-compatible receipt fields
        (P6) on top of whatever _send_text_and_verify_locked/the pane-lease
        short-circuits above already produced -- every existing field
        (sent/enter_sent/delivery_state/submit_status/correlation_id/error/
        submit_reason/...) is completely unchanged; nothing here can alter
        a caller's existing behavior, only add fields a caller that knows
        to look for them can use.

          submission_id   -- alias of correlation_id, the vocabulary this
                             upgrade's design doc (docs/prompt-submission.md)
                             uses; kept as a second key rather than a rename
                             so no existing caller/test reading
                             `correlation_id` needs to change.
          evidence         -- a short list of the evidence codes above; []
                             when delivery_state proves nothing (BLOCKED/
                             ERROR/DELIVERY_UNKNOWN).
          activation_attempts -- 0 (Enter never sent), 1 (sent once), or 2
                             (the one bounded Escape+Enter recovery retry
                             also ran) -- never more; see the "never resend
                             a prompt" rule this module already enforces.
          stage            -- set only on a failure/ambiguous outcome, one
                             of WRITE/ACTIVATE/ACCEPTANCE (P8 diagnostics) --
                             omitted entirely on a confirmed or plain
                             TEXT_SENT result, since there is nothing to
                             diagnose there.
        """
        if "correlation_id" in result:
            result.setdefault("submission_id", result["correlation_id"])
        delivery_state = result.get("delivery_state")
        enter_sent = bool(result.get("enter_sent"))
        recovered = bool(result.get("recovery_attempted"))
        result.setdefault("activation_attempts", (2 if recovered else 1) if enter_sent else 0)
        if delivery_state == DELIVERY_TEXT_SENT:
            result.setdefault("evidence", [self._EVIDENCE_TEXT_SENT])
        elif delivery_state == DELIVERY_SUBMIT_CONFIRMED:
            evidence = [self._EVIDENCE_OUTPUT_CHANGED]
            if recovered:
                evidence.append(self._EVIDENCE_RECOVERY)
            result.setdefault("evidence", evidence)
        else:
            result.setdefault("evidence", [])
        error = result.get("error")
        if delivery_state in (DELIVERY_UNKNOWN, DELIVERY_BLOCKED, DELIVERY_ERROR) or error:
            if not result.get("sent"):
                # Text itself was never confirmed written -- PANE_BUSY,
                # SESSION_NOT_FOUND, TARGET_AWAITING_APPROVAL, or a plain
                # tmux-layer ERROR all land here.
                result.setdefault("stage", "WRITE")
            elif not enter_sent:
                # Text landed; Enter was withheld (IDENTITY_CHANGED_MID_
                # SEND) or never applicable to this failure.
                result.setdefault("stage", "ACTIVATE")
            else:
                # Enter was sent (once or with recovery) but no adapter
                # evidence confirmed the target actually processed it.
                result.setdefault("stage", "ACCEPTANCE")
        return result

    def _send_text_and_verify(self, session: str, text: str, press_enter: bool, *,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        """P0-3/P0-4/P0 Part B wrapper around _send_text_and_verify_locked:
        claims an idempotency key (if given) before anything else,
        acquires the durable cross-process pane lease (lease.py -- the
        *only* thing that serializes correctly across HTTP/STDIO/
        dashboard/Supervisor v2 each potentially being a different OS
        process), serializes on the pane's own in-process lock too (a
        same-process fast path that never needs to touch sqlite), and
        persists the final result under the claimed idempotency key.

        idempotency_key semantics: the *first* caller to successfully claim
        a given key is the only one that ever actually sends -- a repeat
        call with the same key (a retry, a duplicate request, or a call
        made again after a process restart, since the claim is durable on
        disk) returns the original stored result instead of sending again.
        A concurrent caller that loses the claim race while the winner is
        still mid-send gets an honest DUPLICATE_IN_PROGRESS -- unless that
        claim has since gone stale (the claimant crashed before storing a
        result; see AuditStore.claim_idempotency_key), in which case this
        call reclaims it and actually sends, rather than reporting
        DUPLICATE_IN_PROGRESS forever for an attempt that will never finish.

        Different concurrent attempts to the same pane (no shared
        idempotency key -- two genuinely different sends racing for the
        same target) serialize on the pane lease up to a bounded wait, then
        fail safely (PANE_BUSY, delivery_state BLOCKED) rather than risk
        interleaving keystrokes from two processes into the same pane.
        """
        if idempotency_key is not None:
            if not self.audit.claim_idempotency_key(idempotency_key):
                existing = self.audit.get_idempotent_result(idempotency_key)
                if existing is not None:
                    return existing
                return {"session": session, "error": "DUPLICATE_IN_PROGRESS", "idempotency_key": idempotency_key}
        identity = self.resolve_identity(session)
        lock_key = f"{identity.session_id}:{identity.pane_id}" if identity is not None else f"name:{session}"
        correlation_id = uuid.uuid4().hex
        if not self._acquire_pane_lease(lock_key, correlation_id):
            result = self._enrich_receipt({
                "session": session, "error": "PANE_BUSY", "correlation_id": correlation_id,
                "delivery_state": DELIVERY_BLOCKED, "submit_status": to_legacy_submit_status(DELIVERY_BLOCKED),
                "submit_reason": "another process is currently holding the send lease for this pane"})
            if idempotency_key is not None:
                self.audit.store_idempotent_result(idempotency_key, result)
            return result
        try:
            with self._pane_locks.get(lock_key):
                result = self._send_text_and_verify_locked(session, text, press_enter, correlation_id=correlation_id)
        finally:
            self.leases.release(lock_key, correlation_id)
        result = self._enrich_receipt(result)
        if idempotency_key is not None:
            self.audit.store_idempotent_result(idempotency_key, result)
        return result

    def _send_text_and_verify_locked(self, session: str, text: str, press_enter: bool, *,
                                     correlation_id: str) -> dict[str, Any]:
        """Send `text` (and, if requested, Enter) through the tmux layer,
        then make a bounded, best-effort attempt to confirm Enter actually
        *submitted* rather than merely having been typed. This is the fix
        for the intermittent "text fully typed but does not execute until
        a human presses Enter" bug: `sent: True` alone was never proof of
        submission (tmux send-keys succeeding only proves the bytes were
        written to the pty, not that the receiving program acted on them),
        so callers must not treat it as one.

        Every call gets a fresh `correlation_id` (P0 Part A.5): a caller
        that lost the response to a call it did *not* give an
        idempotency_key for cannot use it to safely determine "was this
        already sent", but it still ties this exact attempt's audit-log
        row and result together for post-hoc reconciliation. A caller that
        wants "never duplicate on retry" must pass idempotency_key -- see
        _send_text_and_verify above.

        Returns `sent` (the tmux-level *text* send succeeded -- true the
        moment the text bytes are written, independent of what happens to
        any following Enter), `enter_sent` (whether an Enter keystroke was
        actually transmitted at all -- False for a BLOCKED abort before
        Enter, see below), and the authoritative `delivery_state` (one of
        adapters.DELIVERY_STATES):
          - TEXT_SENT: press_enter was False. Nothing to confirm.
          - SUBMIT_CONFIRMED: press_enter was True and the target's own
            AgentAdapter (adapters.py, selected by pane_current_command)
            reports adapter-specific evidence this exact Enter was
            processed -- never a bare "the pane changed" check; a live-
            redrawing Ink UI's own spinner/cursor/timer tick is explicitly
            excluded for the adapters known to have that failure mode.
          - DELIVERY_UNKNOWN: Enter was sent but no adapter evidence
            confirms it within the verification window. Deliberately
            conservative: never silently upgraded to CONFIRMED.
          - BLOCKED: the pinned session identity or pane_current_command
            changed between the text-send and the point Enter would have
            been sent (P0 Part A.3) -- Enter is withheld entirely rather
            than risk it landing on a pane that is no longer the one this
            attempt started against. Never retargets by session name.
          - ERROR: the tmux layer itself failed partway through (session
            vanished, capture failed).
        `submit_status` (the pre-existing field) is kept, now strictly
        *derived* from delivery_state (adapters.to_legacy_submit_status)
        for every existing caller that only ever reads that field.

        Never blindly auto-retries Enter -- a second bare Enter risks a
        genuine double submission (e.g. accepting a destructive
        confirmation prompt twice), which is strictly worse than an honest
        unconfirmed status a caller (a human, or Supervisor v2's
        execute_send) can act on deliberately. The one exception: exactly
        one bounded Escape-then-Enter recovery sequence, gated strictly on
        the selected adapter's own stuck_composer_evidence (this attempt's
        before/after pair looks like its specific known composer-swallow
        signature) AND safe_recovery_allowed (the target is not already
        evidencing active work, where Escape could genuinely interrupt real
        work) -- never a generic "anything unconfirmed" trigger, and never
        for an adapter (GenericShellAdapter, ClaudeAdapter) whose
        stuck_composer_evidence always returns False.

        Verification method: capture the pane's tail exactly as it looks
        right after the text lands but *before* Enter is sent (the
        unambiguous "typed, not yet submitted" reference for this specific
        text), then poll after Enter until that exact snapshot changes.
        Deliberately NOT a substring/marker match against the sent text --
        a real target's own confirmation output can legitimately echo the
        submitted text back (e.g. "SUBMITTED: <text>"), which would falsely
        look like "still pending" under a marker-suffix check. Comparing
        against the precise pre-Enter snapshot has no such false positive.
        `correlation_id` is generated once by the outer wrapper (_send_
        text_and_verify) and reused as this attempt's pane-lease owner id
        too (see lease.py) -- generating it here instead would create a
        second, different id for the same attempt, breaking that link.
        """

        # P0 Part A.3: resolve identity + pane_current_command *immediately
        # before* the text send -- the first of the two revalidation points.
        # A caller-level pin (a binding/grant's pinned identity) is checked
        # by the caller before this method is ever reached; this is a
        # second, narrower check scoped to the send itself, catching a
        # target destroyed/recreated under the same name in the brief
        # window between that caller-level check and the actual send.
        try:
            info_before = self.tmux.get_session(session)
        except TmuxError:
            info_before = None
        if info_before is None:
            return {"sent": False, "enter_sent": False, "characters": len(text), "press_enter": press_enter,
                    "correlation_id": correlation_id, "delivery_state": DELIVERY_ERROR,
                    "submit_status": to_legacy_submit_status(DELIVERY_ERROR),
                    "error": "SESSION_NOT_FOUND", "session": session}
        identity_before = SessionIdentity.from_session_info(info_before)
        command_before = info_before.pane_current_command or ""
        adapter = select_adapter(command_before)

        # URGENT bugfix (real report: text lands in the composer but never
        # submits until a human presses Enter -- for BOTH Claude and Codex):
        # root cause is that this method never actually checked WHAT the
        # target was showing before committing to send -- can_submit_now/
        # identify_target_state existed on every adapter but neither was
        # ever consulted here. A target showing a menu/approval/confirmation
        # prompt (adapters.py's TARGET_WAITING -- "do you want to
        # continue"/"[y/n]"/"waiting for input"/etc, the SAME shared pattern
        # set both adapters already use for status classification) is not
        # its normal prompt composer: Enter there interacts with THAT
        # prompt (e.g. accepts/declines it) instead of submitting a new
        # message, and the caller's actual text was never delivered to the
        # composer at all -- exactly the reported symptom, and worse, a
        # stray Enter could silently answer an unrelated approval prompt.
        # Checked only for press_enter=True (a text-only append has nothing
        # to "submit" and stays exactly as permissive as before) and
        # checked BEFORE anything is sent -- neither the text nor the Enter
        # -- rather than typing into an ambiguous state and only deciding
        # about Enter afterward. Never depends on tmux/OS focus (the
        # ostensible cause in the bug report): this reads the pane's own
        # content directly, the same way every other status check in this
        # project already does, regardless of what has UI focus anywhere.
        if press_enter:
            try:
                pre_send_snapshot = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
            except TmuxError:
                pre_send_snapshot = None
            if pre_send_snapshot is not None and adapter.identify_target_state(pre_send_snapshot) == TARGET_WAITING:
                return {
                    "sent": False, "enter_sent": False, "characters": len(text), "press_enter": press_enter,
                    "correlation_id": correlation_id, "delivery_state": DELIVERY_BLOCKED,
                    "submit_status": to_legacy_submit_status(DELIVERY_BLOCKED), "agent_type": adapter.name,
                    "error": "TARGET_AWAITING_APPROVAL",
                    "submit_reason": ("the target's current output looks like a menu/approval/confirmation "
                                      "prompt, not its normal prompt composer -- sending would risk answering "
                                      "that prompt instead of submitting a new message, so neither the text nor "
                                      "Enter was sent; resolve the pending prompt first, then retry"),
                }

        self.tmux.send_text(session, text, press_enter=False)
        result: dict[str, Any] = {"sent": True, "enter_sent": False, "characters": len(text),
                                  "press_enter": press_enter, "correlation_id": correlation_id,
                                  "agent_type": adapter.name}
        if not press_enter:
            result["delivery_state"] = DELIVERY_TEXT_SENT
            result["submit_status"] = to_legacy_submit_status(DELIVERY_TEXT_SENT)
            return result
        try:
            typed_snapshot = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
        except TmuxError:
            typed_snapshot = None
        # Same fixed settle window tmux.send_text itself uses for a
        # press_enter=True call -- imported, not duplicated, so there is
        # exactly one place that value is decided.
        time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)

        # P0 Part A.3: the *second* revalidation point, immediately before
        # the Enter keystroke. Abort (never send Enter, never retarget by
        # name) if either the pinned tmux identity or the foreground
        # command has moved on since the text landed -- e.g. the process
        # that was about to receive this Enter has already exited (command
        # changed) or the session name now answers for an entirely
        # different pane (identity changed). The text itself was already
        # sent and stays sent; only the Enter is withheld.
        try:
            info_at_enter = self.tmux.get_session(session)
        except TmuxError:
            info_at_enter = None
        identity_at_enter = None if info_at_enter is None else SessionIdentity.from_session_info(info_at_enter)
        command_at_enter = (info_at_enter.pane_current_command or "") if info_at_enter is not None else ""
        if (identity_at_enter is None or not identity_before.matches(identity_at_enter)
                or command_at_enter != command_before):
            result["delivery_state"] = DELIVERY_BLOCKED
            result["submit_status"] = to_legacy_submit_status(DELIVERY_BLOCKED)
            result["error"] = "IDENTITY_CHANGED_MID_SEND"
            result["submit_reason"] = ("the pinned session identity or foreground command changed between "
                                       "the text send and the Enter send -- Enter was withheld")
            return result

        self.tmux.send_keys(session, ["Enter"])
        result["enter_sent"] = True
        if typed_snapshot is None:
            # No reliable pre-Enter baseline to diff against -- verification
            # itself is compromised (a capture failure right after a
            # successful send is unusual but possible, e.g. a fast-closing
            # session). Report unknown rather than guess either way.
            result["delivery_state"] = DELIVERY_UNKNOWN
            result["submit_status"] = to_legacy_submit_status(DELIVERY_UNKNOWN)
            result["submit_reason"] = "could not capture a pre-submit baseline to verify against"
            return result

        # `adapter` was already selected from command_before, above -- reused
        # here rather than recomputed from command_at_enter, which the
        # identity-match check just above already guarantees is identical
        # (this method returns before reaching here otherwise).
        verify_timeout = (RECOVERY_VERIFY_TIMEOUT_SECONDS if adapter.name in WIDE_VERIFY_ADAPTERS
                          else SEND_VERIFY_TIMEOUT_SECONDS)

        _, after, reason = self._poll_for_submission(session, typed_snapshot, timeout=verify_timeout)

        # Escape+Enter recovery: gated strictly on this specific adapter's
        # own evidence for this specific attempt -- see the adapter
        # docstrings (adapters.py) for exactly what each requires. Never a
        # generic "anything unconfirmed" trigger. Checked *before* (takes
        # precedence over) the base submit_ack_evidence check below: a
        # composer that redrew without genuine progress can still look
        # "confirmed" under a bare diff, which is exactly the false
        # positive recovery exists to catch -- so an adapter proving its
        # specific stuck-composer signature overrides that bare diff,
        # rather than only kicking in when the bare check already failed.
        needs_recovery = (
            after is not None
            and adapter.stuck_composer_evidence(typed_snapshot, after)
            and adapter.safe_recovery_allowed(after)
        )
        if needs_recovery:
            # URGENT bugfix hardening (per explicit review): identity-pin
            # plus "no WORKING evidence" is NOT sufficient by itself to
            # safely retry Escape+Enter -- a quiet/unchanged screen or a
            # missing spinner only proves the target isn't visibly busy,
            # never that THIS attempt's own draft is still the one sitting
            # in the composer. Before any recovery keystroke, positively
            # re-verify, from a FRESH capture taken right here (not the
            # now-several-ms-old `after` the needs_recovery decision above
            # was made from):
            #   1. identity/foreground command still match this attempt's
            #      pinned values (symmetric with the two P0 Part A.3 checks
            #      already done before the text-send and the first Enter);
            #   2. the composer still shows the known stuck-composer
            #      pattern (not, e.g., now confirmed, or now showing active
            #      work that started between polls); AND
            #   3. this exact attempt's own sent text is still visibly
            #      present (_sent_text_echoed) -- proof the pending draft
            #      is genuinely still OURS, not a different/later one (a
            #      real user's own edit, or another caller's send) that
            #      happened to also leave no WORKING evidence.
            # Any of these failing withholds recovery entirely (no Escape,
            # no Enter, no retarget by name) rather than risk submitting
            # someone else's pending text.
            try:
                info_at_recovery = self.tmux.get_session(session)
                recheck_snapshot = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
            except TmuxError:
                info_at_recovery = None
                recheck_snapshot = None
            identity_at_recovery = (None if info_at_recovery is None
                                    else SessionIdentity.from_session_info(info_at_recovery))
            command_at_recovery = ((info_at_recovery.pane_current_command or "")
                                   if info_at_recovery is not None else "")
            identity_ok = (identity_at_recovery is not None and identity_before.matches(identity_at_recovery)
                          and command_at_recovery == command_before)
            draft_still_pending = (
                identity_ok and recheck_snapshot is not None
                and adapter.stuck_composer_evidence(typed_snapshot, recheck_snapshot)
                and _sent_text_echoed(recheck_snapshot, text)
            )
            if not draft_still_pending:
                result["delivery_state"] = DELIVERY_UNKNOWN
                result["submit_status"] = to_legacy_submit_status(DELIVERY_UNKNOWN)
                result["submit_reason"] = (
                    "recovery was withheld -- could not positively re-verify, immediately before "
                    "retrying, that this attempt's own draft is still the one pending in the composer"
                )
                return result
            pre_recovery_snapshot = recheck_snapshot  # diff the recovery's own effect against *this*, not the original
            self.tmux.send_keys(session, ["Escape"])
            time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)
            self.tmux.send_keys(session, ["Enter"])
            result["recovery_attempted"] = True
            _, after2, _ = self._poll_for_submission(session, pre_recovery_snapshot,
                                                      timeout=RECOVERY_VERIFY_TIMEOUT_SECONDS)
            confirmed2 = after2 is not None and adapter.submit_ack_evidence(pre_recovery_snapshot, after2, text)
            if confirmed2:
                result["delivery_state"] = DELIVERY_SUBMIT_CONFIRMED
                result["submit_status"] = to_legacy_submit_status(DELIVERY_SUBMIT_CONFIRMED)
                result["submit_reason"] = "confirmed after Escape+Enter recovery"
            else:
                result["delivery_state"] = DELIVERY_UNKNOWN
                result["submit_status"] = to_legacy_submit_status(DELIVERY_UNKNOWN)
                result["submit_reason"] = "still unconfirmed after Escape+Enter recovery"
            return result

        confirmed = after is not None and adapter.submit_ack_evidence(typed_snapshot, after, text)
        if confirmed:
            result["delivery_state"] = DELIVERY_SUBMIT_CONFIRMED
            result["submit_status"] = to_legacy_submit_status(DELIVERY_SUBMIT_CONFIRMED)
            return result
        result["delivery_state"] = DELIVERY_UNKNOWN
        result["submit_status"] = to_legacy_submit_status(DELIVERY_UNKNOWN)
        result["submit_reason"] = reason
        return result

    def _poll_for_submission(self, session: str, typed_snapshot: list[str], *,
                             timeout: float = SEND_VERIFY_TIMEOUT_SECONDS) -> tuple[bool, list[str] | None, str]:
        """The base verification loop: poll until the pane's tail differs
        from `typed_snapshot` (captured right after the text landed, before
        Enter) or `timeout` elapses (SEND_VERIFY_TIMEOUT_SECONDS for every
        caller except RECOVERY_ELIGIBLE_COMMANDS, which use the wider
        RECOVERY_VERIFY_TIMEOUT_SECONDS instead -- see the caller). NOT a
        substring/marker match against the sent text here -- a real
        target's own confirmation output can legitimately echo the
        submitted text back (e.g. "SUBMITTED: <text>"), which would falsely
        look like "still pending" under a marker-suffix check; comparing
        against the precise pre-Enter snapshot has no such false positive.
        (RECOVERY_ELIGIBLE_COMMANDS layers an additional, narrowly-scoped
        genuine-progress check on top of this in the caller -- seeing this
        loop alone report "confirmed" is not sufficient proof for those.)
        Returns (confirmed, last_capture, reason)."""
        deadline = time.monotonic() + timeout
        after: list[str] | None = None
        while True:
            try:
                after = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
            except TmuxError:
                return False, None, "post-send capture failed"
            if after != typed_snapshot:
                return True, after, "confirmed"
            if time.monotonic() >= deadline:
                return False, after, "the pane looked identical to its pre-Enter state throughout the verification window"
            time.sleep(SEND_VERIFY_POLL_INTERVAL_SECONDS)

    def terminal_send_text(self, session: str, text: str, press_enter: bool = False,
                           dry_run: bool = False, idempotency_key: str | None = None, *,
                           origin: str | None = None, trace_id: str | None = None,
                           parent_turn_id: str | None = None, depth: int = 0) -> dict[str, Any]:
        """idempotency_key (P0-4, optional): if provided, a repeat call
        with the same key never sends twice -- it returns the original
        stored result instead, durable across a process restart. Manual/
        dashboard callers can generate one (e.g. a UUID) for this
        guarantee; omitted entirely, behavior is unchanged from before.

        origin/trace_id/parent_turn_id/depth (P11 loop-protection metadata,
        docs/prompt-submission.md): schema preparation for a future agent-
        bridge (e.g. a ChatGPT-Web adapter turn re-entering a Codex/Claude
        session) -- unused by every current caller (every MCP tool,
        dashboard, Supervisor v2 all omit them, so behavior is completely
        unchanged). `depth` is the one value actually enforced today: a
        caller passing depth greater than config.max_agent_bridge_depth is
        refused fail-closed, before anything is sent, rather than silently
        allowing an unbounded agent-to-agent forwarding chain. The other
        three are recorded to the audit log (never exposed to any tool
        schema) purely for future cross-system trace reconciliation."""
        action = "send_text"
        if depth > self.config.max_agent_bridge_depth:
            response = {"error": "AGENT_BRIDGE_DEPTH_EXCEEDED", "session": session, "depth": depth,
                        "max_agent_bridge_depth": self.config.max_agent_bridge_depth}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter,
                                      origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth)
        if error := self._input_guard(session):
            return self._audit_result(error, action=action, session=session, text=text, press_enter=press_enter)
        if not self.config.input_policy.allow_send_text:
            response = {"error": "ACTION_NOT_ALLOWED", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if "\x00" in text:
            response = {"error": "INVALID_TEXT", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if len(text) > self.config.input_policy.max_text_length:
            response = {"error": "INPUT_TOO_LARGE", "session": session, "max_text_length": self.config.input_policy.max_text_length}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if dry_run:
            response = {"session": session, "would_send": True, "dry_run": True,
                        "characters": len(text), "press_enter": press_enter}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        try:
            response = {"session": session,
                        **self._send_text_and_verify(session, text, press_enter, idempotency_key=idempotency_key)}
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter,
                                  origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth)

    def terminal_send_keys(self, session: str, keys: list[str],
                           confirm_sensitive: bool = False) -> dict[str, Any]:
        action = "send_keys"
        if error := self._input_guard(session):
            return self._audit_result(error, action=action, session=session, keys=keys)
        # Permission-model normalization (P9, docs/prompt-submission.md):
        # raw key sends are a distinct capability from text/prompt
        # submission (terminal_send_text/_bound) -- a deployment can now
        # disable this specific path while send_prompt keeps working.
        # Defaults to True: every existing config.yaml is unaffected.
        if not self.config.permissions.allow_send_keys:
            response = {"error": "SEND_KEYS_DISABLED", "session": session}
            return self._audit_result(response, action=action, session=session, keys=keys)
        allowed = set(self.config.input_policy.allow_keys)
        sensitive = set(self.config.input_policy.sensitive_keys_require_confirmation)
        invalid = [key for key in keys if key not in allowed and key not in sensitive]
        if not keys or invalid:
            response = {"error": "KEY_NOT_ALLOWED", "session": session, "invalid_keys": invalid,
                        "allowed_keys": sorted(allowed | sensitive)}
            return self._audit_result(response, action=action, session=session, keys=keys)
        requested_sensitive = [key for key in keys if key in sensitive]
        if requested_sensitive and not confirm_sensitive:
            response = {"error": "CONFIRMATION_REQUIRED", "session": session,
                        "sensitive_keys": requested_sensitive}
            return self._audit_result(response, action=action, session=session, keys=keys)
        if len(keys) > 100:
            response = {"error": "TOO_MANY_KEYS", "session": session}
            return self._audit_result(response, action=action, session=session, keys=keys)
        response = self._send_keys_leased(session, keys)
        return self._audit_result(response, action=action, session=session, keys=keys)

    def _send_keys_leased(self, session: str, keys: list[str]) -> dict[str, Any]:
        """P0 Part B: raw key sends (terminal_send_keys) share the exact
        same durable cross-process pane lease as terminal_send_text -- "any
        text/key send" (see lease.py) means both, not just the verified
        text-composition path. No adapter/verification involved here (this
        is the same unverified raw send terminal_send_keys always was);
        the lease's only job for this caller is stopping two processes'
        raw key sequences from interleaving into the same pane."""
        identity = self.resolve_identity(session)
        lock_key = f"{identity.session_id}:{identity.pane_id}" if identity is not None else f"name:{session}"
        correlation_id = uuid.uuid4().hex
        if not self._acquire_pane_lease(lock_key, correlation_id):
            return {"session": session, "error": "PANE_BUSY", "correlation_id": correlation_id,
                    "reason": "another process is currently holding the send lease for this pane"}
        try:
            with self._pane_locks.get(lock_key):
                self.tmux.send_keys(session, keys)
                return {"session": session, "sent": True, "keys": keys, "correlation_id": correlation_id}
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        finally:
            self.leases.release(lock_key, correlation_id)

    def terminal_exit_copy_mode(self, *, session: str | None = None,
                                binding: str | None = None) -> dict[str, Any]:
        """Explicitly cancel tmux copy-mode for one authorized target.

        This is intentionally separate from terminal_send_text/send_keys:
        input remains blocked while an operator is scrolling/searching, and
        a caller must explicitly request this state-changing tmux operation.
        No key is ever written to the underlying pane PTY.
        """
        action = "exit_copy_mode"
        if (session is None) == (binding is None):
            response = {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
            return self._audit_result(response, action=action, session=session, binding=binding)

        stored = None
        if binding is not None:
            stored, error = self._resolve_binding(binding)
            if error:
                return self._audit_result(error, action=action, session=error.get("session"), binding=binding)
            session = stored.session
            if not stored.input_enabled:
                response = {"error": "BINDING_INPUT_DISABLED", "binding": binding, "session": session}
                return self._audit_result(response, action=action, session=session, binding=binding)

        if (permission_error := require_input(self.config)) is not None:
            response = {"error": permission_error, "session": session}
            if binding is not None:
                response["binding"] = binding
            return self._audit_result(response, action=action, session=session, binding=binding)
        authorized, specific_error = self._input_authorized(session)
        if not authorized:
            response = {"error": specific_error or "ACCESS_DENIED", "session": session}
            if binding is not None:
                response["binding"] = binding
            return self._audit_result(response, action=action, session=session, binding=binding)

        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
            return self._audit_result(response, action=action, session=session, binding=binding)
        if info is None:
            response = {"error": "SESSION_NOT_FOUND", "session": session}
            return self._audit_result(response, action=action, session=session, binding=binding)
        identity = SessionIdentity.from_session_info(info)

        if stored is not None and (identity_error := self._check_binding_identity(binding, stored)) is not None:
            return self._audit_result(identity_error, action=action, session=session, binding=binding)
        command = info.pane_current_command.casefold()
        allowed_sensitive = {item.casefold() for item in self.config.input_policy.allowed_sensitive_commands}
        if command in SENSITIVE_COMMANDS and command not in allowed_sensitive:
            response = {"error": "SENSITIVE_TARGET", "session": session, "current_command": command}
            return self._audit_result(response, action=action, session=session, binding=binding)
        if not info.pane_in_mode:
            response = {"session": session, "copy_mode_exited": False, "status": "NOT_IN_COPY_MODE"}
            if binding is not None:
                response["binding"] = binding
            self.audit.record(action=action, binding=binding, session=session, result="NOOP",
                              reason="NOT_IN_COPY_MODE", source_transport="mcp")
            return response

        lock_key = f"{identity.session_id}:{identity.pane_id}"
        correlation_id = uuid.uuid4().hex
        if not self._acquire_pane_lease(lock_key, correlation_id):
            response = {"error": "PANE_BUSY", "session": session, "correlation_id": correlation_id,
                        "reason": "another process is currently holding the send lease for this pane"}
            return self._audit_result(response, action=action, session=session, binding=binding)
        try:
            with self._pane_locks.get(lock_key):
                current = self.tmux.get_session(session)
                current_identity = None if current is None else SessionIdentity.from_session_info(current)
                if current_identity is None or not identity.matches(current_identity):
                    response = {"error": "IDENTITY_MISMATCH", "session": session,
                                "reason": "the session/pane changed before copy-mode could be cancelled",
                                "correlation_id": correlation_id}
                    return self._audit_result(response, action=action, session=session, binding=binding)
                if not current.pane_in_mode:
                    response = {"session": session, "copy_mode_exited": False,
                                "status": "NOT_IN_COPY_MODE", "correlation_id": correlation_id}
                    if binding is not None:
                        response["binding"] = binding
                    self.audit.record(action=action, binding=binding, session=session, result="NOOP",
                                      reason="NOT_IN_COPY_MODE", source_transport="mcp",
                                      correlation_id=correlation_id)
                    return response

                self.tmux.exit_copy_mode(session)
                after = self.tmux.get_session(session)
                after_identity = None if after is None else SessionIdentity.from_session_info(after)
                if after_identity is None or not identity.matches(after_identity):
                    response = {"error": "IDENTITY_MISMATCH", "session": session,
                                "reason": "the session/pane changed while copy-mode was being cancelled",
                                "correlation_id": correlation_id}
                    return self._audit_result(response, action=action, session=session, binding=binding)
                if after.pane_in_mode:
                    response = {"error": "COPY_MODE_EXIT_FAILED", "session": session,
                                "reason": "tmux accepted cancel but the pane remains in copy-mode",
                                "correlation_id": correlation_id}
                    return self._audit_result(response, action=action, session=session, binding=binding)
                response = {"session": session, "copy_mode_exited": True,
                            "status": "COPY_MODE_EXITED", "correlation_id": correlation_id}
                if binding is not None:
                    response["binding"] = binding
                self.audit.record(action=action, binding=binding, session=session, result="SUCCEEDED",
                                  reason="COPY_MODE_EXITED", source_transport="mcp",
                                  correlation_id=correlation_id)
                return response
        except TmuxError as exc:
            response = {"error": "COPY_MODE_EXIT_FAILED", "session": session,
                        "reason": str(exc), "correlation_id": correlation_id}
            return self._audit_result(response, action=action, session=session, binding=binding)
        finally:
            self.leases.release(lock_key, correlation_id)

    def _binding_result(self, binding: Binding) -> dict[str, Any]:
        # P0 HOTFIX: `allowed` here is bind-target authorization (static
        # whitelist OR an active read grant, via _bind_authorized) -- kept
        # under its pre-existing name for compatibility, same "derived
        # metadata, not an independent gate" posture as terminal_list_
        # sessions' own `allowed` field.
        allowed = self._bind_authorized(binding.session)
        try:
            exists = self.tmux.get_session(binding.session) is not None
        except TmuxError:
            exists = False
        return {
            "binding": binding.name, "session": binding.session,
            "session_exists": exists, "allowed": allowed,
            "read_enabled": binding.read_enabled, "input_enabled": binding.input_enabled,
            "effective_input": (self.config.permissions.terminal_input and binding.input_enabled
                                and self._input_authorized(binding.session)[0]),
            "created_at": binding.created_at, "updated_at": binding.updated_at,
        }

    def terminal_bind(self, binding: str, session: str, replace: bool = False,
                      read_enabled: bool = True, input_enabled: bool = False) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        if not self._bind_authorized(session):
            return {"error": "ACCESS_DENIED", "binding": binding, "session": session}
        try:
            info = self.tmux.get_session(session)
            if info is None:
                return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session}
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session, "reason": str(exc)}
        # P0-2: pin identity at bind (and rebind) time -- a bind/rebind is
        # the explicit "pin to whatever this session is right now" action.
        stored, changed = self.bindings.put(
            binding, session, read_enabled=read_enabled,
            input_enabled=input_enabled, replace=replace,
            pinned_session_id=info.session_id, pinned_pane_id=info.pane_id,
            pinned_created_epoch=info.created_epoch,
        )
        if not changed:
            return {"error": "BINDING_EXISTS", "binding": binding, "session": stored.session}
        result = self._binding_result(stored)
        result["replaced"] = replace
        return result

    def terminal_get_binding(self, binding: str) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        stored = self.bindings.get(binding)
        if stored is None:
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return self._binding_result(stored)

    def terminal_list_bindings(self) -> list[dict[str, Any]]:
        results = []
        for binding in self.bindings.list():
            row = self._binding_result(binding)
            row["state"] = "RUNNING" if row["session_exists"] else "MISSING"
            results.append(row)
        return results

    def terminal_unbind(self, binding: str) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        if not self.bindings.delete(binding):
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return {"binding": binding, "unbound": True}

    def _resolve_binding(self, binding: str) -> tuple[Binding | None, dict[str, Any] | None]:
        if not valid_binding_name(binding):
            return None, {"error": "INVALID_BINDING", "binding": binding}
        stored = self.bindings.get(binding)
        if stored is None:
            return None, {"error": "BINDING_NOT_FOUND", "binding": binding}
        if not self._bind_authorized(stored.session):
            return None, {"error": "ACCESS_DENIED", "binding": binding, "session": stored.session}
        return stored, None

    def terminal_tail_bound(self, binding: str, lines: int = 200) -> dict[str, Any]:
        stored, error = self._resolve_binding(binding)
        if error:
            return error
        if not stored.read_enabled:
            return {"error": "READ_DISABLED", "binding": binding, "session": stored.session}
        return {"binding": binding, **self.terminal_tail(stored.session, lines)}

    def terminal_status_bound(self, binding: str) -> dict[str, Any]:
        stored, error = self._resolve_binding(binding)
        if error:
            return error
        if not stored.read_enabled:
            return {"error": "READ_DISABLED", "binding": binding, "session": stored.session}
        if (permission_error := require_read(self.config)) is not None:
            return {"error": permission_error, "binding": binding, "session": stored.session}
        try:
            if self.tmux.get_session(stored.session) is None:
                return {"binding": binding, "session": stored.session, "exists": False,
                        "allowed": True, "state": "MISSING", "input_required": False,
                        "reason": "session does not exist", "last_output": ""}
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "binding": binding, "session": stored.session, "reason": str(exc)}
        return {"binding": binding, **self.terminal_status(stored.session)}

    def _check_binding_identity(self, binding: str, stored: Binding) -> dict[str, Any] | None:
        """P0-2/P0 Part B.3: refuse to send through a binding whose pinned
        identity no longer matches what currently answers to its session
        name -- the name may have been recycled onto an unrelated tmux
        session, or its pane replaced. Returns None if the send may
        proceed.

        A binding with no pin yet (created before P0-2 existed, or whose
        identity couldn't be resolved at bind time) now fails closed for
        INPUT instead of lazily adopting whatever identity happens to be
        live at first-send time: silently trusting "whatever answers to
        this name right now" is exactly the unpinned-identity risk P0
        Part B's revalidation work exists to close, and grandfathering it
        in for input specifically was a real gap, not a defensible
        default. Read (terminal_tail_bound/terminal_status_bound) is
        unaffected -- neither calls this method, so an old, never-rebound
        binding stays fully readable; only *sending* through it now
        requires an explicit rebind (terminal_bind with replace=True, which
        pins the session's current identity) to migrate off the unpinned
        state, once, per binding."""
        current = self.resolve_identity(stored.session)
        if stored.pinned_session_id is None:
            return {
                "error": "BINDING_NOT_PINNED", "binding": binding, "session": stored.session,
                "reason": "this binding predates identity pinning and has never been rebound -- "
                          "input is refused until it is explicitly rebound (terminal_bind with "
                          "replace=True) to pin its current tmux identity; read is unaffected",
            }
        pinned = SessionIdentity(name=stored.session, session_id=stored.pinned_session_id,
                                 pane_id=stored.pinned_pane_id or "",
                                 created_epoch=stored.pinned_created_epoch or 0)
        if current is None or not pinned.matches(current):
            return {
                "error": "IDENTITY_MISMATCH", "binding": binding, "session": stored.session,
                "reason": "the session/pane this binding was created for no longer matches "
                          "what currently answers to that session name -- rebind explicitly "
                          "to accept the new target",
            }
        return None

    def terminal_send_bound(self, binding: str, text: str, press_enter: bool = False,
                            dry_run: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
        action = "send_bound"
        stored, error = self._resolve_binding(binding)
        if error:
            return self._audit_result(error, action=action, session=error.get("session"), binding=binding,
                                      text=text, press_enter=press_enter)
        if not self.config.permissions.terminal_input:
            response = {"error": "INPUT_DISABLED", "binding": binding, "session": stored.session}
            return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                      text=text, press_enter=press_enter)
        if not stored.input_enabled:
            response = {"error": "BINDING_INPUT_DISABLED", "binding": binding, "session": stored.session}
            return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                      text=text, press_enter=press_enter)
        # Apply action/session/target validation here while recording exactly one bound event.
        if error := self._input_guard(stored.session):
            response = {"binding": binding, **error}
        elif not self.config.input_policy.allow_send_text:
            response = {"error": "ACTION_NOT_ALLOWED", "binding": binding, "session": stored.session}
        elif "\x00" in text:
            response = {"error": "INVALID_TEXT", "binding": binding, "session": stored.session}
        elif len(text) > self.config.input_policy.max_text_length:
            response = {"error": "INPUT_TOO_LARGE", "binding": binding, "session": stored.session,
                        "max_text_length": self.config.input_policy.max_text_length}
        elif dry_run:
            response = {"binding": binding, "session": stored.session, "would_send": True,
                        "dry_run": True, "characters": len(text), "press_enter": press_enter}
        elif (identity_error := self._check_binding_identity(binding, stored)) is not None:
            response = identity_error
        else:
            try:
                response = {"binding": binding, "session": stored.session,
                            **self._send_text_and_verify(stored.session, text, press_enter,
                                                         idempotency_key=idempotency_key)}
            except TmuxError as exc:
                response = {"error": "SESSION_NOT_FOUND", "binding": binding,
                            "session": stored.session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                  text=text, press_enter=press_enter)

    def terminal_list_input_audit(self, limit: int = 50, binding: str | None = None,
                                  session: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            return {"error": "INVALID_LIMIT", "events": []}
        return {"events": self.audit.list(limit, binding, session)}

    def terminal_input_context(self, session: str | None = None,
                               binding: str | None = None) -> dict[str, Any]:
        if (session is None) == (binding is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        stored = None
        if binding is not None:
            stored, error = self._resolve_binding(binding)
            if error:
                return error
            session = stored.session
        if error := self._guard(session):
            return error
        try:
            info = self.tmux.get_session(session)
            if info is None:
                return {"error": "SESSION_NOT_FOUND", "session": session}
            lines = self.tmux.capture_lines(session, 20)
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        effective = (self.config.permissions.terminal_input and self._input_authorized(session)[0]
                     and (stored is None or stored.input_enabled)
                     and info.pane_current_command.casefold() not in SENSITIVE_COMMANDS
                     and not info.pane_in_mode)
        return {"binding": binding, "session": session, "current_command": info.pane_current_command,
                "status": "RUNNING" if not info.pane_dead else "DEAD",
                "last_output": redact_text("\n".join(lines)), "effective_input": effective,
                "pane_in_mode": info.pane_in_mode}

    # -- dashboard-only per-session grants -----------------------------
    # GRANTING/REVOKING (grant_session_read/grant_session_input below,
    # and everything that reads/writes an actual grant) is still
    # deliberately NEVER exposed as an MCP tool (see mcp_app.py -- there
    # is no supervisor_grant_*/terminal_grant_* tool wrapper). DISCOVERY
    # is shared: terminal_list_sessions (above, the MCP tool surface) and
    # dashboard_list_sessions (below) both show the full tmux inventory
    # plus each session's grant/capability state -- an MCP client (Claude
    # Code, ChatGPT via the tunnel) can see the exact same sessions and
    # authorization state an operator sees on the dashboard, including a
    # session it cannot yet read or control. That is discovery only:
    # every content/control method (terminal_tail/terminal_status/
    # terminal_send_text/terminal_bind and the *_granted methods below)
    # still enforces session_allowed/input_session_allowed/the per-
    # session grant exactly as before -- seeing a session listed never
    # grants anything, only supervisor_watch/grant_session_read/
    # grant_session_input do, and only the latter two are dashboard-only.

    def dashboard_list_sessions(self) -> dict[str, Any]:
        """Same full-inventory discovery as terminal_list_sessions (the
        MCP tool surface) above, in the shape the dashboard's own JS
        already expects -- session NAME/attached/windows/created/activity
        are tmux metadata, not pane CONTENT, so surfacing them for a non-
        whitelisted session leaks nothing a viewer couldn't already see by
        running `tmux ls` themselves on this host. `allowed` is the
        existing static-whitelist result (unchanged meaning); `grant` is
        this session's current dashboard grant, if any; `effective_read`/
        `effective_input` fold both together -- a whitelisted session is
        unaffected (already true via `allowed`), a granted-but-not-
        whitelisted one becomes readable/sendable through the *_granted
        methods below, nothing else changes for it."""
        if (error := require_read(self.config)) is not None:
            return {"error": error, "sessions": []}
        try:
            items = self.tmux.list_sessions()
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc), "sessions": []}
        grants_by_session = {grant.session: grant for grant in self.grants.list()}
        sessions = []
        for item in items:
            grant = grants_by_session.get(item.name)
            grant_read = bool(grant and grant.read_enabled)
            grant_input = bool(grant and grant.input_enabled)
            allowed = session_allowed(item.name, self.config)
            effective_input = bool(
                self.config.permissions.terminal_input
                and self._input_authorized_with_grant(
                    item.name, grant, current_identity=SessionIdentity.from_session_info(item))[0]
            )
            row = {
                "name": item.name, "allowed": allowed, "attached": item.attached,
                "windows": item.windows, "created": iso_timestamp(item.created_epoch),
                "activity": iso_timestamp(item.activity_epoch),
                "grant": {"read_enabled": grant_read, "input_enabled": grant_input},
                "effective_read": self._read_authorized_with_grant(item.name, grant),
                "effective_input": effective_input,
            }
            row.update(self._desktop_metadata_for(item.name))
            # UX gap fix: an operator needs to know WHY the input-grant
            # control is (or would be) blocked, not just that it is --
            # only computed when it's actually relevant (statically input-
            # whitelisted sessions are never grant-blocked; a session
            # already effectively input-capable has nothing to explain).
            # Reuses item.pane_current_command already fetched by this
            # same list_sessions() call -- no extra per-session tmux
            # round-trip, same N+1 discipline as the rest of this route.
            if not allowed and not effective_input:
                row["input_block_reason"] = self._input_grant_block_reason(
                    item.name, pane_current_command=item.pane_current_command)
            # Kill/Reopen UX (item 11 of the design this backs): a real,
            # currently-observed preview of whether a Kill of THIS session
            # would capture complete-enough metadata for an automatic
            # Reopen -- reuses the exact same classification
            # terminal_kill_session itself applies at kill time, so the
            # dashboard can warn BEFORE the (irreversible) kill rather than
            # only after. Only computed while session_lifecycle is even
            # enabled -- the one thing that ever makes this relevant.
            if self.config.session_lifecycle.enabled:
                row["kill_reopen_ready"] = self._reopen_would_be_complete(item)
            sessions.append(row)
        # Read-only convenience for the "Xóa session" UI (never the actual
        # enforcement -- terminal_delete_session's own protected_sessions
        # check is what actually refuses a delete, regardless of what a
        # client does or doesn't disable in its own UI based on this list).
        self._reconcile_session_registry(items, grants_by_session)
        return {"sessions": sessions,
               "session_lifecycle_enabled": self.config.session_lifecycle.enabled,
               "protected_sessions": list(self.config.session_lifecycle.protected_sessions),
               "web_terminal_enabled": self.config.dashboard.web_terminal_enabled}

    def _reopen_would_be_complete(self, info: Any) -> bool:
        """Preview-only version of _capture_reopen_metadata's own
        completeness decision -- never mutates anything, never persisted;
        purely for the dashboard's pre-Kill warning (item 11)."""
        agent_type = self._classify_agent_type(info.pane_current_command)
        if agent_type is None:
            return False
        if agent_type == "shell":
            return True
        if not info.pane_current_path:
            return False
        _resolved, error = resolve_cwd(info.pane_current_path, self.config)
        return error is None

    def terminal_web_terminal_access(self, session: str) -> dict[str, Any]:
        """Authorization + existence resolution for the web terminal
        (xterm.js over a WebSocket, attaching a browser directly to an
        existing tmux session's real pty -- webterm.py). This is the ONE
        place that decision is made; both dashboard.py's /dashboard/ws/
        terminal and webauth_dashboard.py's /app/ws/terminal call this
        and nothing else to decide whether to spawn a WebTerminalProcess
        at all, and whether it opens read-only or interactive -- reuses
        the exact same canonical _read_authorized_with_grant/
        _input_authorized_with_grant every other read/input surface in
        this file already goes through, so "can this viewer open a web
        terminal, and can they type into it" can never diverge from what
        terminal_tail/terminal_send_text would themselves decide for the
        same caller and session.

        Returns either {"error": ...} (refuse outright -- the caller must
        never construct a WebTerminalProcess) or {"exists": True,
        "input": bool, "attached": bool} (open -- `input` decides
        interactive vs. tmux `-r` read-only; there is no separate "read"
        key because reaching this point at all already proves read access:
        ACCESS_DENIED is returned before existence is even checked).

        Never creates, mutates, or attaches to anything itself -- purely a
        decision, exactly like _guard/_input_guard themselves; unlike
        those, it also confirms the session currently EXISTS (SESSION_
        NOT_FOUND), which the caller cannot skip: the web terminal must
        fail closed on a since-deleted/renamed session rather than let
        tmux's own attach-session error surface as some generic pty
        failure."""
        if not self.config.dashboard.web_terminal_enabled:
            return {"error": "WEB_TERMINAL_DISABLED"}
        if (error := require_read(self.config)) is not None:
            return {"error": error}
        if not valid_session_name(session):
            return {"error": "INVALID_SESSION_NAME"}
        grant = self.grants.get(session)
        if not self._read_authorized_with_grant(session, grant):
            return {"error": "ACCESS_DENIED"}
        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc)}
        if info is None:
            return {"error": "SESSION_NOT_FOUND"}
        input_enabled = bool(
            self.config.permissions.terminal_input
            and self._input_authorized_with_grant(session, grant)[0]
        )
        return {"exists": True, "input": input_enabled, "attached": info.attached}

    def grant_session_read(self, session: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        if (error := require_read(self.config)) is not None:
            return {"error": error, "session": session}
        if not valid_session_name(session):
            return {"error": "INVALID_SESSION", "session": session}
        if enabled:
            if any(word in session.casefold() for word in SENSITIVE_SESSION_WORDS):
                return {"error": "SENSITIVE_SESSION_NOT_GRANTABLE", "session": session}
            try:
                exists = self.tmux.get_session(session) is not None
            except TmuxError as exc:
                return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
            if not exists:
                return {"error": "SESSION_NOT_FOUND", "session": session}
        grant = self.grants.set_read(session, enabled, granted_by=granted_by)
        self.session_registry.touch_grant(self.REGISTRY_LOCAL_NODE_ID, session,
                                          read_granted=grant.read_enabled, input_granted=grant.input_enabled)
        return {"session": session, "read_enabled": grant.read_enabled, "input_enabled": grant.input_enabled}

    def _input_grant_block_reason(self, session: str, *, pane_current_command: str | None = None) -> str | None:
        """Read-only superset of the eligibility checks grant_session_
        input's enable=True branch enforces before actually mutating --
        factored out so the dashboard can tell an operator WHY input
        can't (yet) be granted for a session, using the exact same
        decision grant_session_input itself would make, never a second,
        possibly-divergent copy of this logic (the "keep dashboard
        permission calculations consistent" requirement this exists for).
        Returns None if input could be granted for this session right now
        (independent of whether read is already granted -- callers that
        care about that ordering check it separately, same as
        grant_session_input does).

        `pane_current_command`: pass this to skip this call's own tmux
        round-trip when the caller already has fresh SessionInfo for
        every session in one listing pass (dashboard_list_sessions) --
        omit it (the default) to have this look the session up itself,
        which then also doubles as the SESSION_NOT_FOUND check a single-
        session caller (grant_session_input) needs anyway."""
        if (error := require_input(self.config)) is not None:
            return error
        if not valid_session_name(session):
            return "INVALID_SESSION"
        if any(word in session.casefold() for word in SENSITIVE_SESSION_WORDS):
            return "SENSITIVE_SESSION_NOT_GRANTABLE"
        if session_input_denied_by_pattern(session, self.config):
            return "ACCESS_DENIED"
        if pane_current_command is None:
            try:
                info = self.tmux.get_session(session)
            except TmuxError:
                return "SESSION_NOT_FOUND"
            if info is None:
                return "SESSION_NOT_FOUND"
            pane_current_command = info.pane_current_command
        command = pane_current_command.casefold()
        allowed_sensitive = {item.casefold() for item in self.config.input_policy.allowed_sensitive_commands}
        if command in SENSITIVE_COMMANDS and command not in allowed_sensitive:
            return "SENSITIVE_TARGET"
        return None

    def grant_session_input(self, session: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        """Read must already be granted -- enforced by SessionGrantStore.
        set_input itself (returns None if not), not just checked here, so
        this can never race a concurrent read-revoke into granting input
        without read. Pins the session's CURRENT identity (session_id/
        pane_id/created_epoch) at the moment input is granted, exactly
        like terminal_bind already does for bindings -- re-checked at
        every send in terminal_send_text_granted, never silently carried
        forward to a same-named session that gets recreated later."""
        if (error := require_input(self.config)) is not None:
            return {"error": error, "session": session}
        if not valid_session_name(session):
            return {"error": "INVALID_SESSION", "session": session}
        existing = self.grants.get(session)
        if existing is None or not existing.read_enabled:
            return {"error": "READ_GRANT_REQUIRED", "session": session}
        if not enabled:
            grant = self.grants.set_input(session, False, granted_by=granted_by)
            self.session_registry.touch_grant(self.REGISTRY_LOCAL_NODE_ID, session,
                                              read_granted=grant.read_enabled, input_granted=grant.input_enabled)
            return {"session": session, "read_enabled": grant.read_enabled, "input_enabled": grant.input_enabled}
        if (reason := self._input_grant_block_reason(session)) is not None:
            return {"error": reason, "session": session}
        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        if info is None:  # vanished between the check above and here
            return {"error": "SESSION_NOT_FOUND", "session": session}
        grant = self.grants.set_input(session, True, granted_by=granted_by,
                                      pinned_session_id=info.session_id, pinned_pane_id=info.pane_id,
                                      pinned_created_epoch=info.created_epoch)
        if grant is None:  # read was revoked concurrently between the check above and here
            return {"error": "READ_GRANT_REQUIRED", "session": session}
        self.session_registry.touch_grant(self.REGISTRY_LOCAL_NODE_ID, session,
                                          read_granted=grant.read_enabled, input_granted=grant.input_enabled)
        return {"session": session, "read_enabled": grant.read_enabled, "input_enabled": grant.input_enabled}

    def _require_read_grant(self, session: str) -> dict[str, Any] | None:
        if (error := require_read(self.config)) is not None:
            return {"error": error, "session": session}
        grant = self.grants.get(session)
        if grant is None or not grant.read_enabled:
            return {"error": "READ_RESTRICTED", "session": session}
        return None

    def terminal_status_granted(self, session: str) -> dict[str, Any]:
        if error := self._require_read_grant(session):
            return error
        return self._status_payload(session)

    def terminal_tail_granted(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        if error := self._require_read_grant(session):
            return error
        return self._tail_payload(session, lines, ansi=ansi)

    def terminal_send_text_granted(self, session: str, text: str, press_enter: bool = False,
                                   dry_run: bool = False, idempotency_key: str | None = None, *,
                                   origin: str | None = None, trace_id: str | None = None,
                                   parent_turn_id: str | None = None, depth: int = 0) -> dict[str, Any]:
        """Parallel to terminal_send_text, for a dashboard-granted (not
        statically-whitelisted) session -- reuses the exact same guarded
        low-level send primitive (_send_text_and_verify: pane lock,
        idempotency, submit verification), only the authorization check
        differs. Re-verifies the grant's pinned identity against the
        session's CURRENT tmux identity right now, at send time, not just
        at grant time -- a session destroyed and recreated under the same
        name since the grant was issued gets IDENTITY_MISMATCH, never a
        silent send to the new, unvetted pane. Unlike a binding, this does
        NOT lazily adopt an unpinned identity: input grants are always
        pinned at grant time (grant_session_input requires the session to
        exist then), so a missing pin here would only mean the grant row
        is stale/corrupt -- treated as a mismatch, never guessed past."""
        action = "send_text_granted"
        if depth > self.config.max_agent_bridge_depth:
            response = {"error": "AGENT_BRIDGE_DEPTH_EXCEEDED", "session": session, "depth": depth,
                        "max_agent_bridge_depth": self.config.max_agent_bridge_depth}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter,
                                      origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth)
        if (error := require_input(self.config)) is not None:
            return self._audit_result({**error, "session": session}, action=action, session=session,
                                      text=text, press_enter=press_enter)
        grant = self.grants.get(session)
        if grant is None or not grant.read_enabled or not grant.input_enabled:
            response = {"error": "GRANT_REQUIRED", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if not self.config.input_policy.allow_send_text:
            response = {"error": "ACTION_NOT_ALLOWED", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if "\x00" in text:
            response = {"error": "INVALID_TEXT", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if len(text) > self.config.input_policy.max_text_length:
            response = {"error": "INPUT_TOO_LARGE", "session": session, "max_text_length": self.config.input_policy.max_text_length}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        # P0 HOTFIX: reuses the canonical _input_authorized_with_grant
        # (the same identity-revalidation this method always did inline)
        # instead of duplicating the pin-comparison here -- GRANT_REQUIRED
        # above already covers "no grant at all", so by this point the
        # only way this can fail is the identity mismatch case.
        authorized, specific_error = self._input_authorized_with_grant(session, grant)
        if not authorized:
            response = {
                "error": specific_error or "ACCESS_DENIED", "session": session,
                "reason": "the session this input grant was issued for no longer matches what "
                          "currently answers to that session name -- re-grant explicitly to accept "
                          "the new target",
            }
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if info is None:
            response = {"error": "SESSION_NOT_FOUND", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        # P0 audit finding #14: same copy-mode refusal as _input_guard --
        # this path (grant-based send) doesn't route through _input_guard
        # at all, so it needs its own check to get the same guarantee.
        if info.pane_in_mode:
            response = {"error": "PANE_IN_COPY_MODE", "session": session,
                       "reason": "the target pane is in tmux copy-mode (scrollback/search/selection) -- "
                                 "every keystroke would be intercepted by tmux itself and never reach the "
                                 "underlying program; an operator must exit copy-mode out of band (e.g. "
                                 "attach and press 'q', or run `tmux send-keys -t <session> -X cancel` "
                                 "directly) before input can proceed again"}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        command = info.pane_current_command.casefold()
        allowed_sensitive = {item.casefold() for item in self.config.input_policy.allowed_sensitive_commands}
        if command in SENSITIVE_COMMANDS and command not in allowed_sensitive:
            response = {"error": "SENSITIVE_TARGET", "session": session, "current_command": command}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if dry_run:
            response = {"session": session, "would_send": True, "dry_run": True,
                        "characters": len(text), "press_enter": press_enter}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        try:
            response = {"session": session,
                        **self._send_text_and_verify(session, text, press_enter, idempotency_key=idempotency_key)}
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter,
                                  origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth)

    # -- Session lifecycle: create/detach/delete -------------------------
    # The ONE implementation shared by the dashboard's "Tạo session"/
    # "Tách"/"Xóa session" routes (dashboard.py, webauth_dashboard.py) and
    # the terminal_create_session/_detach_session/_delete_session MCP
    # tools (mcp_app.py) -- see lifecycle.py's module docstring. This
    # layer owns permission-gating (require_session_lifecycle), audit
    # logging, and the optional grant/binding/initial_prompt composition
    # a create call may ask for; SessionLifecycleService.create/detach/
    # delete own the actual tmux decisions and never audit or authorize
    # anything themselves.

    def terminal_create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                                initial_prompt: str | None = None, grant_mode: str = "none",
                                binding: str | None = None, requested_by: str | None = None,
                                show_on_desktop: bool = False) -> dict[str, Any]:
        """Create a new detached tmux session running a plain shell, or
        Claude/Codex via a server-side-only launcher (config.session_
        lifecycle.launch_commands -- agent_type never becomes a command
        line, only a lookup key). Never auto-grants read/input just
        because creation succeeded: grant_mode is an explicit, separate
        opt-in ("none" the default -- caller gets a session, nothing more;
        "read"/"read_send" additionally call the exact same grant_session_
        read/grant_session_input this class always used, so a grant here
        is refused exactly when a manual dashboard grant would be, e.g. a
        sensitive-worded name or a denied input pattern). initial_prompt,
        when given, is sent ONLY after the session reaches state=READY,
        through terminal_send_text -- the same pane-locked, idempotency-
        aware, adapter-verified submission path every other prompt in
        this project goes through, never a raw send-keys shortcut; if the
        caller's own effective permission doesn't yet cover this session
        (no static whitelist match and grant_mode left at "none"), that
        send comes back ACCESS_DENIED same as it would for any other
        ungranted session -- creating a session never silently bypasses
        that. binding, when given, calls terminal_bind (fails closed on a
        name collision exactly like terminal_bind always has, never
        remaps an existing binding out from under it)."""
        action = "create_session"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason=error)
            return {"error": error, "session": name}
        if grant_mode not in ("none", "read", "read_send"):
            self.audit.record(action=action, session=name, result="BLOCKED", reason="INVALID_GRANT_MODE")
            return {"error": "INVALID_GRANT_MODE", "session": name}
        result = self.lifecycle.create(name, agent_type, cwd, show_on_desktop=show_on_desktop)
        ok = "error" not in result
        self.audit.record(action=action, session=name, result="CREATED" if ok else "BLOCKED",
                          reason=result.get("error") or f"agent_type={agent_type}")
        if not ok:
            return result

        grant_result: dict[str, Any] | None = None
        if grant_mode != "none":
            read_result = self.grant_session_read(name, True, granted_by=requested_by)
            if "error" not in read_result and grant_mode == "read_send":
                input_result = self.grant_session_input(name, True, granted_by=requested_by)
                grant_result = {"read": read_result, "input": input_result}
            else:
                grant_result = {"read": read_result}
        result["grant"] = grant_result

        binding_result: dict[str, Any] | None = None
        if binding:
            binding_result = self.terminal_bind(binding, name)
        result["binding"] = binding_result

        if initial_prompt:
            if result.get("state") == "READY":
                result["initial_prompt_result"] = self.terminal_send_text(name, initial_prompt, press_enter=True)
            else:
                result["initial_prompt_result"] = {
                    "error": "SESSION_NOT_READY", "session": name, "state": result.get("state"),
                }
        return result

    def terminal_detach_session(self, name: str) -> dict[str, Any]:
        """Detach any attached tmux client from `name` -- never kills the
        session/process, never loses output/state. Idempotent: a session
        that isn't currently attached returns its current state, not an
        error."""
        action = "detach_session"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason=error)
            return {"error": error, "session": name}
        if not valid_session_name(name):
            self.audit.record(action=action, session=name, result="BLOCKED", reason="INVALID_SESSION_NAME")
            return {"error": "INVALID_SESSION_NAME", "session": name}
        result = self.lifecycle.detach(name)
        ok = "error" not in result
        self.audit.record(action=action, session=name, result="DETACHED" if ok else "BLOCKED",
                          reason=result.get("error") or result.get("action"))
        return result

    def terminal_delete_session(self, name: str) -> dict[str, Any]:
        """Terminate and remove exactly one tmux session (`kill-session`,
        never `kill-server`) -- the protected set (config.session_
        lifecycle.protected_sessions, always including "terminal-mcp")
        can never be deleted through this path. Idempotent: a session
        already gone returns a success-shaped result, not an error. On an
        actual (or already-confirmed) deletion, cleans up any binding
        pointing at this session (deleted -- a binding to a session that
        no longer exists is never left dangling) and any active grant
        (revoked via the same grant_session_read(False) path a manual
        dashboard revoke uses, which keeps the grant ROW as history but
        marks it disabled, rather than deleting it outright)."""
        action = "delete_session"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason=error)
            return {"error": error, "session": name}
        if not valid_session_name(name):
            self.audit.record(action=action, session=name, result="BLOCKED", reason="INVALID_SESSION_NAME")
            return {"error": "INVALID_SESSION_NAME", "session": name}
        result = self.lifecycle.delete(name, protected_sessions=self.config.session_lifecycle.protected_sessions)
        ok = "error" not in result
        if ok:
            self._cleanup_after_session_gone(name)
            self.session_registry.mark_killed(self.REGISTRY_LOCAL_NODE_ID, name, killed_by="dashboard:delete")
        self.audit.record(action=action, session=name, result="DELETED" if ok else "BLOCKED",
                          reason=result.get("error") or result.get("action"))
        return result

    def _cleanup_after_session_gone(self, session: str) -> None:
        """Called once terminal_delete_session has confirmed `session` no
        longer exists (whether this call killed it or it was already
        gone) -- never leaves a binding pointing at a vanished session,
        and never leaves an active grant for one either. Supervisor watch
        cleanup lives one layer up (mcp_app.py/dashboard.py, which are the
        callers that actually have a SupervisorService reference -- the
        same "coordinate across two composed services at the wiring
        layer" pattern supervisor_watch/supervisor_unwatch already use in
        mcp_app.py for v1/v2 policy purge, not a new precedent)."""
        for binding in self.bindings.list():
            if binding.session == session:
                self.bindings.delete(binding.name)
        grant = self.grants.get(session)
        if grant is not None and (grant.read_enabled or grant.input_enabled):
            self.grants.set_read(session, False, granted_by="system:session_deleted")

    # -- Kill (destructive, with reopen metadata) / Reopen ---------------

    def _classify_agent_type(self, pane_current_command: str) -> str | None:
        command = (pane_current_command or "").casefold()
        for agent_type, launcher in self.config.session_lifecycle.launch_commands:
            if command == Path(launcher).name.casefold():
                return agent_type
        if command in SHELL_COMMAND_NAMES:
            return "shell"
        return None

    def _capture_reopen_metadata(self, info: Any) -> dict[str, Any]:
        """Real, observed pane state at the moment BEFORE a kill -- never
        a guess. `working_directory` is only kept if it re-validates
        against config.session_lifecycle.allowed_cwd_roots (the exact
        same resolve_cwd lifecycle.create() itself uses for a caller-
        supplied cwd) -- an observed path outside the allowed roots
        (a session opened directly on the host outside this project's own
        controls) is simply dropped, not silently trusted."""
        agent_type = self._classify_agent_type(info.pane_current_command)
        working_directory = None
        if info.pane_current_path:
            resolved, error = resolve_cwd(info.pane_current_path, self.config)
            if error is None:
                working_directory = str(resolved)
        return {"agent_type": agent_type, "working_directory": working_directory,
               "observed_command": info.pane_current_command}

    def terminal_kill_session(self, name: str, confirm_name: str, *,
                              requested_by: str | None = None) -> dict[str, Any]:
        """Terminate exactly one tmux session (kill-session, never kill-
        server) -- same protected_sessions refusal and idempotent-already-
        gone shape as terminal_delete_session, which this calls for the
        actual tmux mechanics and binding/grant cleanup. Two things this
        adds on top of a plain delete:

          1. `confirm_name` must exactly equal `name` -- a SECOND,
             server-side-enforced confirmation for a destructive action,
             never trusting a client-side confirm() dialog alone.
          2. On an actual kill (never on an already-gone no-op), captures
             the pane's real, currently-observed command/cwd immediately
             BEFORE killing it, classifies that into a safe agent_type +
             validated working_directory, and persists it
             (killed_sessions.py) so a later terminal_reopen_session can
             recreate a NEW session under the same name without ever
             guessing. `metadata_complete` in the response tells the
             caller (the dashboard) whether that will actually be
             possible, so it can warn BEFORE the kill if not."""
        action = "kill_session"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason=error)
            return {"error": error, "session": name}
        if not valid_session_name(name):
            self.audit.record(action=action, session=name, result="BLOCKED", reason="INVALID_SESSION_NAME")
            return {"error": "INVALID_SESSION_NAME", "session": name}
        if confirm_name != name:
            self.audit.record(action=action, session=name, result="BLOCKED", reason="CONFIRMATION_MISMATCH")
            return {"error": "CONFIRMATION_MISMATCH", "session": name}
        # Protected check is also lifecycle.delete()'s own first move (see
        # its docstring) -- repeated here only so this can short-circuit
        # BEFORE ever querying tmux for metadata to capture, not because
        # the refusal itself would otherwise be missed.
        if name in self.config.session_lifecycle.protected_sessions:
            self.audit.record(action=action, session=name, result="BLOCKED", reason="SESSION_PROTECTED")
            return {"error": "SESSION_PROTECTED", "session": name}
        try:
            info = self.tmux.get_session(name)
        except TmuxError as exc:
            self.audit.record(action=action, session=name, result="BLOCKED", reason="TMUX_ERROR")
            return {"error": "TMUX_ERROR", "reason": str(exc), "session": name}
        metadata = self._capture_reopen_metadata(info) if info is not None else None
        result = self.lifecycle.delete(name, protected_sessions=self.config.session_lifecycle.protected_sessions)
        ok = "error" not in result
        if ok:
            self._cleanup_after_session_gone(name)
            if result.get("action") == "deleted" and metadata is not None:
                record = self.killed_sessions.record(
                    name, agent_type=metadata["agent_type"], working_directory=metadata["working_directory"],
                    observed_command=metadata["observed_command"], killed_by=requested_by,
                )
                result["reopen_metadata"] = {
                    "agent_type": record.agent_type, "working_directory": record.working_directory,
                    "metadata_complete": record.metadata_complete,
                }
            else:
                result["reopen_metadata"] = None
            # Registry: KILLED, never removed -- see session_registry.py's
            # own docstring on why Kill/Delete only ever change `status`.
            # cwd/agent_type reuse the SAME pane-state capture just above
            # (metadata) rather than re-deriving it -- also means a
            # session killed before any reconcile pass ever ran for it
            # (no registry row yet) still gets a real, complete record,
            # not an empty one (see mark_killed's own upsert docstring).
            self.session_registry.mark_killed(
                self.REGISTRY_LOCAL_NODE_ID, name, killed_by=requested_by,
                reopen_metadata=result.get("reopen_metadata"),
                cwd=metadata["working_directory"] if metadata else None,
                agent_type=metadata["agent_type"] if metadata else None,
                backend_type=self._registry_backend_type(),
            )
        self.audit.record(action=action, session=name, result="KILLED" if ok else "BLOCKED",
                          reason=result.get("error") or result.get("action"))
        return result

    def terminal_reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                                grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        """Recreates a NEW tmux session/process under `name` from saved
        Kill metadata (killed_sessions.py) via the exact same
        terminal_create_session this project's Create action already
        uses -- explicitly NOT a resurrection of the killed process's own
        RAM/state; a fresh process, same name/cwd/launcher.

        `agent_type`/`cwd`, when explicitly supplied, OVERRIDE the saved
        metadata field-by-field (never merged/guessed) -- the intended way
        to proceed when saved metadata is incomplete, letting a caller
        pick a safe agent/cwd explicitly rather than this method ever
        inventing one. Refuses with REOPEN_METADATA_INCOMPLETE, naming
        exactly what's still missing, if the effective agent_type is
        unknown, or (for a non-"shell" agent_type) the effective cwd is."""
        action = "reopen_session"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason=error)
            return {"error": error, "session": name}
        if not valid_session_name(name):
            self.audit.record(action=action, session=name, result="BLOCKED", reason="INVALID_SESSION_NAME")
            return {"error": "INVALID_SESSION_NAME", "session": name}
        record = self.killed_sessions.get(name)
        effective_agent_type = agent_type or (record.agent_type if record else None)
        effective_cwd = cwd or (record.working_directory if record else None)
        if effective_agent_type is None:
            self.audit.record(action=action, session=name, result="BLOCKED", reason="AGENT_TYPE_UNKNOWN")
            return {"error": "REOPEN_METADATA_INCOMPLETE", "session": name, "missing": ["agent_type"]}
        if effective_agent_type != "shell" and not effective_cwd:
            self.audit.record(action=action, session=name, result="BLOCKED", reason="CWD_UNKNOWN")
            return {"error": "REOPEN_METADATA_INCOMPLETE", "session": name, "missing": ["working_directory"]}
        used_saved_metadata_only = record is not None and agent_type is None and cwd is None
        result = self.terminal_create_session(name, effective_agent_type, effective_cwd,
                                              grant_mode=grant_mode, requested_by=requested_by)
        result["reopened_from_metadata"] = used_saved_metadata_only
        if "error" not in result:
            self.killed_sessions.clear(name)
        self.audit.record(action=action, session=name, result="REOPENED" if "error" not in result else "BLOCKED",
                          reason=result.get("error"))
        return result

    # -- Persistent Session Registry (recovery, independent of killed_
    # sessions.py -- see session_registry.py's own module docstring) -----

    @staticmethod
    def _registry_record_dict(record: Any) -> dict[str, Any]:
        return {
            "node_id": record.node_id, "session_name": record.session_name, "key": record.key(),
            "display_name": record.display_name, "node_name": record.node_name,
            "backend_type": record.backend_type, "cwd": record.cwd, "repo_root": record.repo_root,
            "git_remote": record.git_remote, "git_branch": record.git_branch, "last_commit": record.last_commit,
            "agent_type": record.agent_type, "launch_command": record.launch_command,
            "launcher_type": record.launcher_type, "created_at": record.created_at,
            "last_seen_at": record.last_seen_at, "last_activity_at": record.last_activity_at,
            "last_known_state": record.last_known_state, "status": record.status,
            "killed_at": record.killed_at, "deleted_at": record.deleted_at, "offline_at": record.offline_at,
            "metadata_complete": record.metadata_complete, "recoverable": record.recoverable,
            "read_granted": record.read_granted, "input_granted": record.input_granted,
            "grant_updated_at": record.grant_updated_at, "binding_names": list(record.binding_names),
            "notes": record.notes, "tags": list(record.tags),
        }

    def terminal_registry_list(self, *, recoverable_only: bool = False) -> dict[str, Any]:
        """Every session this process has ever discovered/created, active
        or not -- task item 4's "Active và Recoverable/History" dashboard
        section is this list, split client-side (or via recoverable_only)
        on `status`/`recoverable`, never a second, separately-maintained
        list. A fresh reconcile pass runs first (same as listing sessions
        itself would) so this is never staler than the live session list
        happens to be."""
        self.terminal_list_sessions()  # side effect: reconciles the registry (see _reconcile_session_registry)
        from .session_registry import RECOVERABLE_STATUSES
        statuses = RECOVERABLE_STATUSES if recoverable_only else None
        records = self.session_registry.list(node_id=self.REGISTRY_LOCAL_NODE_ID, statuses=statuses)
        if recoverable_only:
            records = [r for r in records if r.recoverable]
        return {"records": [self._registry_record_dict(r) for r in records]}

    def terminal_registry_get(self, session_name: str) -> dict[str, Any]:
        record = self.session_registry.get(self.REGISTRY_LOCAL_NODE_ID, session_name)
        if record is None:
            return {"error": "REGISTRY_RECORD_NOT_FOUND", "session": session_name}
        return self._registry_record_dict(record)

    def terminal_registry_search(self, query: str) -> dict[str, Any]:
        """Task item 9's own explicit scenario: find a project by name/
        path/repo even when the session that was working on it is gone
        or renamed (the "quan_ly_ban_hang" case this feature was built
        for)."""
        if not query or not query.strip():
            return {"records": []}
        records = self.session_registry.search(query.strip())
        return {"records": [self._registry_record_dict(r) for r in records]}

    def terminal_registry_reopen(self, session_name: str, *, agent_type: str | None = None,
                                 cwd: str | None = None, grant_mode: str = "none",
                                 requested_by: str | None = None) -> dict[str, Any]:
        """Recreate a session FROM REGISTRY METADATA -- explicitly a fresh
        process, never a resurrection of whatever RAM/state the original
        had (task item 5's own explicit requirement: "Không giả khôi phục
        RAM/process cũ; ghi rõ đây là recreate from metadata"). Reuses
        terminal_create_session exactly like terminal_reopen_session
        (killed_sessions.py-backed) already does -- the only difference
        is WHERE the agent_type/cwd come from: this registry (which has
        an entry for any session ever reconciled here, not only ones
        explicitly Kill'd through the dashboard -- e.g. mesflow/
        promptflow/quan_ly_ban_hang after this host's tmux server itself
        restarted, none of which killed_sessions.db has any record of at
        all since nothing ever called terminal_kill_session for them).

        No adapter-level --resume/--continue/session-id support (task's
        own "nếu agent hỗ trợ resume... thì dùng khi an toàn") -- not
        implemented in this pass: this project's launch_commands config
        maps agent_type to a plain launcher token only, with no verified,
        adapter-specific resume-flag wiring to build on safely yet.
        Documented limitation, not a silent gap."""
        action = "registry_reopen"
        if (error := require_session_lifecycle(self.config)) is not None:
            self.audit.record(action=action, session=session_name, result="BLOCKED", reason=error)
            return {"error": error, "session": session_name}
        if not valid_session_name(session_name):
            return {"error": "INVALID_SESSION_NAME", "session": session_name}
        record = self.session_registry.get(self.REGISTRY_LOCAL_NODE_ID, session_name)
        if record is None:
            return {"error": "REGISTRY_RECORD_NOT_FOUND", "session": session_name}
        effective_agent_type = agent_type or record.agent_type
        effective_cwd = cwd or record.cwd
        if effective_agent_type is None:
            return {"error": "REOPEN_METADATA_INCOMPLETE", "session": session_name, "missing": ["agent_type"]}
        if effective_agent_type != "shell" and not effective_cwd:
            return {"error": "REOPEN_METADATA_INCOMPLETE", "session": session_name, "missing": ["cwd"]}
        result = self.terminal_create_session(session_name, effective_agent_type, effective_cwd,
                                              grant_mode=grant_mode, requested_by=requested_by)
        result["recreated_from_registry"] = True
        if "error" not in result:
            # Reflects ACTIVE immediately rather than waiting for the next
            # ordinary poll's own reconcile pass to notice.
            self.session_registry.upsert_seen(
                self.REGISTRY_LOCAL_NODE_ID, session_name, backend_type=self._registry_backend_type(),
                cwd=effective_cwd, agent_type=effective_agent_type,
            )
        self.audit.record(action=action, session=session_name,
                          result="REOPENED" if "error" not in result else "BLOCKED", reason=result.get("error"))
        return result

    def terminal_registry_purge(self, session_name: str, *, purged_by: str | None = None) -> dict[str, Any]:
        """The ONE hard-delete path for a registry row (task item 6) --
        separate from Kill/Delete of the runtime session, which never
        touches the registry row's existence, only its `status`. Refuses
        an ACTIVE record outright (purging a currently-live session's own
        history makes no sense and almost certainly indicates the caller
        meant Kill, not this)."""
        record = self.session_registry.get(self.REGISTRY_LOCAL_NODE_ID, session_name)
        if record is None:
            return {"error": "REGISTRY_RECORD_NOT_FOUND", "session": session_name}
        if record.status == "ACTIVE":
            return {"error": "SESSION_STILL_ACTIVE", "session": session_name}
        self.session_registry.purge(self.REGISTRY_LOCAL_NODE_ID, session_name, purged_by=purged_by)
        self.audit.record(action="registry_purge", session=session_name, result="PURGED", reason=purged_by)
        return {"session": session_name, "purged": True}

    def terminal_list_killed_sessions(self) -> dict[str, Any]:
        """Sessions Kill has recorded reopen metadata for, most recent
        first -- self-healing: a record whose name now belongs to a real,
        live session again (created some other way since the kill) is
        cleared here rather than ever listed as still reopenable."""
        if (error := require_session_lifecycle(self.config)) is not None:
            return {"error": error, "killed_sessions": []}
        entries = []
        for item in self.killed_sessions.list():
            try:
                exists = self.tmux.get_session(item.name) is not None
            except TmuxError:
                exists = False
            if exists:
                self.killed_sessions.clear(item.name)
                continue
            entries.append({
                "name": item.name, "agent_type": item.agent_type, "working_directory": item.working_directory,
                "metadata_complete": item.metadata_complete, "killed_at": item.killed_at, "killed_by": item.killed_by,
            })
        return {"killed_sessions": entries}
