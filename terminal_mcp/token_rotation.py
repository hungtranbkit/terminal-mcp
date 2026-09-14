"""Rotating a node token end to end, without hand-editing either side.

blg_a3cc401d8275, acceptance criterion 1. node_credentials.py can mint,
grace and revoke a credential; that alone still leaves an operator
editing a file on the controller and another one on the node. This
module is the part that makes rotation an *operation* rather than a
procedure.

Why two phases
--------------
A node token authenticates BOTH directions, and they cut over at
different moments:

    inbound   node -> controller   controller verifies (node_credentials)
    outbound  controller -> node   controller presents, node verifies

Switching the controller's outbound copy the instant a new token is
minted would break every controller->node call until the node happened
to pick it up. Switching the node first would break inbound. So the
handoff is explicit:

    1. rotate()   new token is ACTIVE for inbound; the old one drops to
                  an OPEN-ENDED grace (it stays valid until the rotation
                  is confirmed, not until a stopwatch runs out -- a node
                  that was offline for the window must not be locked
                  out). The plaintext is staged, 0600, for exactly one
                  reader.
    2. collect()  the node, authenticating with the token it still has,
                  fetches its replacement over that already-authenticated
                  channel and adopts it. No restart: see node_agent's
                  AgentCredential.
    3. confirm()  the node's next heartbeat arrives signed with the NEW
                  token. That is proof the handoff landed, so now -- and
                  only now -- the controller moves its own outbound copy
                  over and REVOKES the old token.

Step 3 is what makes AC3 ("old token is provably rejected afterwards")
true by construction rather than by a timer: the old credential is not
left to expire, it is revoked at the moment the new one is demonstrably
in use.

What is never written down
--------------------------
The staged plaintext is the one copy that exists between mint and
pickup, in a 0600 file under the credential store's own directory, and
it is deleted on confirm or revoke. Nothing else -- no audit row, no log
line, no API response but collect()'s own -- ever carries a token. Every
other surface identifies a credential by its `token_id` fingerprint.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import node_credentials
from .node_credentials import NodeCredentialStore, token_fingerprint

_log = logging.getLogger(__name__)

# pending-rotation states, as reported to an operator
PENDING = "PENDING_NODE_PICKUP"
DELIVERED = "DELIVERED_AWAITING_CONFIRMATION"
NONE = "NONE"

# How long a claim with no token behind it stays authoritative. Long
# enough that minting (one sqlite transaction) can never be mistaken for
# a crash, short enough that a real crash does not block rotation for the
# rest of the day.
STALE_CLAIM_SECONDS = 120.0

# audit actions -- all of them carry a token_id, never a token
AUDIT_ADOPT = "node_token_adopt"
AUDIT_ROTATE = "node_token_rotate"
AUDIT_DELIVER = "node_token_deliver"
AUDIT_CONFIRM = "node_token_rotation_confirmed"
AUDIT_REVOKE = "node_token_revoke"

# `token` is the new plaintext, or None meaning "stop using any token for
# this node" (revocation).
OutboundApplier = Callable[[str, "str | None"], None]


@dataclass(frozen=True)
class PendingRotation:
    node_id: str
    token_id: str
    created_at: str
    delivered_at: str | None

    @property
    def state(self) -> str:
        return DELIVERED if self.delivered_at else PENDING

    def to_dict(self) -> dict[str, Any]:
        return {"token_id": self.token_id, "state": self.state,
                "created_at": self.created_at, "delivered_at": self.delivered_at}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TokenRotationService:
    """Composes the credential store with the two places a controller
    keeps its outbound copy (connection_store's 0600 token file and the
    per-node env var), so a caller has one idempotent operation per
    intent instead of four coordinated ones."""

    def __init__(self, credentials: NodeCredentialStore, *, connection_store: Any = None,
                 audit: Any = None, apply_outbound: OutboundApplier | None = None,
                 state_dir: str | Path | None = None) -> None:
        self.credentials = credentials
        self.connection_store = connection_store
        self.audit = audit
        self._apply_outbound = apply_outbound
        base = Path(state_dir) if state_dir is not None else credentials.path.parent / "pending-rotations"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            base.chmod(0o700)
        self.state_dir = base

    # -- operator intents --------------------------------------------------

    def adopt_existing(self, node_id: str, token: str | None = None, *,
                       actor: str | None = None) -> dict[str, Any]:
        """Bring a node that predates this feature under management.

        With no token, reads the one the controller already holds for
        this node -- so adopting is a no-input operation for every node
        enrolled the old way. Idempotent: adopting twice reports the same
        token_id and changes nothing.
        """
        plaintext = token or self._current_outbound_token(node_id)
        if not plaintext:
            return {"ok": False, "error": "NO_EXISTING_TOKEN",
                    "detail": "this controller holds no token for that node -- enroll it instead"}
        already = self.credentials.is_managed(node_id)
        record = self.credentials.adopt(node_id, plaintext)
        self._record(AUDIT_ADOPT, node_id, "ALREADY_MANAGED" if already else "ADOPTED",
                     record.token_id, actor)
        return {"ok": True, "already_managed": already, **self.status(node_id)}

    def rotate(self, node_id: str, *, grace_seconds: int | None = None, force: bool = False,
               actor: str | None = None) -> dict[str, Any]:
        """Mint a replacement token and stage it for the node to collect.

        Idempotent while a rotation is in flight: calling it again
        returns the SAME pending rotation rather than minting a second
        one. That is not politeness -- rotating twice before the node has
        picked up the first replacement would push the token the node is
        actually holding out of grace and lock it out. `force=True` is
        the deliberate escape hatch for when that is what you want.
        """
        if not self.credentials.is_managed(node_id):
            return {"ok": False, "error": "NODE_NOT_MANAGED",
                    "detail": "adopt this node's current token first"}
        # Claim the staging slot BEFORE minting anything. Reading it first
        # and then writing would be a check-then-act: two operators (or
        # two dashboard tabs) clicking rotate at the same instant would
        # each mint a token, and the second would push the one the node
        # is actually holding out of grace. The exclusive create is the
        # mutex, so it holds across processes and not just threads.
        if not self._claim_pending(node_id) and not force:
            return {"ok": True, "rotated": False,
                    "detail": "a rotation is already in flight for this node",
                    **self.status(node_id)}
        try:
            record, plaintext = self.credentials.rotate(node_id, grace_seconds=grace_seconds)
        except Exception:
            self._clear_pending(node_id)
            raise
        self._write_pending(node_id, record.token_id, plaintext)
        self._record(AUDIT_ROTATE, node_id, "ROTATED", record.token_id, actor,
                     detail=f"grace={'until_confirmed' if grace_seconds is None else grace_seconds}")
        _log.info("node token rotated node_id=%s new_token_id=%s grace=%s",
                  node_id, record.token_id, grace_seconds)
        return {"ok": True, "rotated": True, **self.status(node_id)}

    def revoke(self, node_id: str, *, token_id: str | None = None, reason: str = "manual_revoke",
               actor: str | None = None) -> dict[str, Any]:
        """Refuse a credential from now on. Idempotent.

        Revoking everything for a node also discards any staged rotation
        and tells the controller to stop presenting a token outbound --
        a revoked credential that the controller itself keeps using would
        be a revocation in name only.
        """
        if not self.credentials.is_managed(node_id):
            return {"ok": False, "error": "NODE_NOT_MANAGED",
                    "detail": "this node has no credential records to revoke"}
        records = self.credentials.revoke(node_id, token_id=token_id, reason=reason)
        pending = self._read_pending(node_id)
        if token_id is None:
            self._clear_pending(node_id)
            self._outbound(node_id, None)
        elif pending is not None and pending.token_id == token_id:
            self._clear_pending(node_id)
        self._record(AUDIT_REVOKE, node_id, "REVOKED", token_id or "all", actor, detail=reason)
        _log.info("node token revoked node_id=%s token_id=%s reason=%s", node_id, token_id or "all", reason)
        return {"ok": True, "revoked": [record.to_dict() for record in records], **self.status(node_id)}

    def status(self, node_id: str) -> dict[str, Any]:
        """Everything an operator needs to see, and nothing that could
        leak: statuses, fingerprints and timestamps only."""
        pending = self._read_pending(node_id)
        return {
            "node_id": node_id,
            "managed": self.credentials.is_managed(node_id),
            "active_token_id": self.credentials.active_token_id(node_id),
            "pending_rotation": pending.to_dict() if pending else None,
            "pending_state": pending.state if pending else NONE,
            "credentials": [record.to_dict() for record in self.credentials.list_for(node_id)],
        }

    # -- the node's own half ------------------------------------------------

    def collect(self, node_id: str) -> dict[str, Any]:
        """Hand the staged replacement to the node that asked for it.

        The CALLER must already have authenticated the request with a
        token this store accepts for this node -- that is the whole
        authorization: only the holder of the current (or still-in-grace)
        credential can collect its successor. Repeatable until confirmed,
        because a node that crashed between receiving and writing the new
        token must be able to ask again.
        """
        pending = self._read_pending(node_id)
        if pending is None:
            return {"ok": False, "error": "NO_PENDING_ROTATION"}
        plaintext = self._read_pending_secret(node_id)
        if plaintext is None:
            # The record exists but the secret is gone (a half-deleted
            # state dir). Refuse rather than pretend.
            return {"ok": False, "error": "PENDING_ROTATION_UNREADABLE"}
        if pending.delivered_at is None:
            self._write_pending(node_id, pending.token_id, plaintext,
                                created_at=pending.created_at, delivered_at=_now_iso())
            self._record(AUDIT_DELIVER, node_id, "DELIVERED", pending.token_id, "node")
        return {"ok": True, "token": plaintext, "token_id": pending.token_id}

    def confirm(self, node_id: str, presented_token_id: str | None) -> bool:
        """Called on every authenticated inbound request. Cheap and
        silent unless the presented token IS the staged one, which is the
        proof that the node has adopted it.

        Only then does the controller move its own outbound copy and
        revoke the old credential -- so a rotation that the node never
        completed leaves both sides exactly as they were.
        """
        if not presented_token_id:
            return False
        pending = self._read_pending(node_id)
        if pending is None or pending.token_id != presented_token_id:
            return False
        plaintext = self._read_pending_secret(node_id)
        if plaintext is None:
            return False
        self._outbound(node_id, plaintext)
        superseded = [record.token_id for record in self.credentials.list_for(node_id)
                      if record.status == node_credentials.GRACE]
        for token_id in superseded:
            self.credentials.revoke(node_id, token_id=token_id, reason="rotation_confirmed")
        self._clear_pending(node_id)
        self._record(AUDIT_CONFIRM, node_id, "CONFIRMED", presented_token_id, "node",
                     detail=f"superseded={','.join(superseded) or 'none'}")
        _log.info("node token rotation confirmed node_id=%s token_id=%s superseded=%s",
                  node_id, presented_token_id, superseded or "none")
        return True

    def refresh_hint(self, node_id: str, presented_token_id: str | None) -> dict[str, Any] | None:
        """What to tell a node on its heartbeat response: there is a new
        token waiting, identified by fingerprint only. Absent (None) for
        the overwhelmingly common case of nothing to do."""
        pending = self._read_pending(node_id)
        if pending is None or pending.token_id == presented_token_id:
            return None
        return {"available": True, "token_id": pending.token_id,
                "collect_path": f"/dashboard/api/nodes/{node_id}/token/refresh"}

    # -- internals ----------------------------------------------------------

    def _current_outbound_token(self, node_id: str) -> str | None:
        if self.connection_store is not None:
            connection = self.connection_store.get(node_id)
            if connection is not None and connection.token_file:
                token = self.connection_store.read_token(connection.token_file)
                if token:
                    return token
        from .dashboard import node_token_env_var

        return os.environ.get(node_token_env_var(node_id)) or None

    def _outbound(self, node_id: str, token: str | None) -> None:
        if self._apply_outbound is None:
            return
        try:
            self._apply_outbound(node_id, token)
        except Exception:  # noqa: BLE001 -- never let bookkeeping break auth
            _log.exception("failed to apply outbound token change for node_id=%s", node_id)

    def _pending_path(self, node_id: str) -> Path:
        return self.state_dir / f"{NodeCredentialStore._validate(node_id)}.json"

    def _claim_pending(self, node_id: str) -> bool:
        """Reserve this node's staging slot, or report that somebody else
        holds it.

        The claim is written with its timestamp and NO token_id, so it
        reads as "no pending rotation" to everything else while the mint
        is in progress -- and so a claim abandoned by a crash between
        claiming and minting can be told apart from one that is simply a
        few milliseconds old. Only the abandoned kind is taken over;
        treating every contentless claim as stale would make the
        exclusive create decorative.
        """
        target = self._pending_path(node_id)
        payload = json.dumps({"claimed_at": _now_iso()}).encode()
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
            return True
        except FileExistsError:
            if not self._claim_is_abandoned(node_id):
                return False
            self._write_raw(node_id, payload)
            return True

    def _claim_is_abandoned(self, node_id: str) -> bool:
        if self._read_pending(node_id) is not None:
            return False  # a real staged rotation, not a claim
        try:
            age = time.time() - self._pending_path(node_id).stat().st_mtime
        except OSError:
            return True
        return age > STALE_CLAIM_SECONDS

    def _read_pending(self, node_id: str) -> PendingRotation | None:
        try:
            raw = json.loads(self._pending_path(node_id).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not raw.get("token_id"):
            return None
        return PendingRotation(node_id=node_id, token_id=str(raw["token_id"]),
                               created_at=str(raw.get("created_at") or ""),
                               delivered_at=raw.get("delivered_at"))

    def _read_pending_secret(self, node_id: str) -> str | None:
        try:
            raw = json.loads(self._pending_path(node_id).read_text())
        except (OSError, ValueError):
            return None
        token = raw.get("token") if isinstance(raw, dict) else None
        if not token or token_fingerprint(str(token)) != raw.get("token_id"):
            return None
        return str(token)

    def _write_pending(self, node_id: str, token_id: str, token: str, *,
                       created_at: str | None = None, delivered_at: str | None = None) -> None:
        self._write_raw(node_id, json.dumps({"token_id": token_id, "token": token,
                                             "created_at": created_at or _now_iso(),
                                             "delivered_at": delivered_at}).encode())

    def _write_raw(self, node_id: str, payload: bytes) -> None:
        target = self._pending_path(node_id)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)

    def _clear_pending(self, node_id: str) -> None:
        with contextlib.suppress(OSError):
            self._pending_path(node_id).unlink()

    def _record(self, action: str, node_id: str, result: str, token_id: str | None,
                actor: str | None, *, detail: str = "") -> None:
        """Every audit row this feature writes goes through here, which is
        why none of them can contain a token: the only credential-shaped
        value it accepts is a token_id fingerprint."""
        if self.audit is None:
            return
        reason = f"token_id={token_id or 'none'}"
        if detail:
            reason = f"{reason} {detail}"
        with contextlib.suppress(Exception):
            self.audit.record(action=action, session=None, result=result, node_id=node_id,
                              actor=actor or "system", reason=reason, source_transport="dashboard")
