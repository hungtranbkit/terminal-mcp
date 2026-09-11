"""The one place that says what protocol generation this build speaks.

Why this exists: `AGENT_GENERATION` (node_agent.py) is a random token minted
per process. It answers "did this agent restart?" and nothing else -- two
nodes running completely different code report two random strings that look
exactly as different as two restarts of the same build. Until now there was no
way for a controller to know whether a node understood the same protocol it
does, which is precisely the question that matters before routing work to it.

This was not academic. Audited 2026-09-11, dell-linux was running a commit the
controller had never seen, with the old session-name whitelist still deciding
access, while the controller had retired it -- and nothing anywhere reported a
mismatch.

CONTRACT_VERSION is bumped ONLY for a change that alters what a peer may
assume: a wire-format change, a field whose meaning changes, an authorization
rule that moves. Adding a capability is additive and does NOT bump it --
that is what CAPABILITIES is for.

A node that reports nothing is treated as version 0 / no capabilities, never
as "probably compatible". Degrading loudly is the point.
"""
from __future__ import annotations

CONTRACT_VERSION = 1

# Feature flags a peer may rely on. Additive: a peer must treat an unknown
# capability as absent, never as an error, so a newer node can talk to an
# older controller.
CAP_GRANT_ONLY_ACCESS = "grant_only_access"
"""Access is decided by user grants + session_access defaults. The
session-name whitelist authorizes nothing, and `allowed` is a deprecated
alias of the real read authorization rather than a second, contradictory
answer."""

CAP_SOFT_RECONNECT = "soft_reconnect"
"""Recovery checks whether the runtime session is still alive before
respawning anything, so a node that comes back with its tmux intact is
reconciled rather than duplicated."""

CAP_RECOVERY_TOMBSTONE = "recovery_tombstone"
"""An intentionally killed session is never resurrected by the background
reconcile pass; only an explicit human force does that."""

CAP_KEY_SENDS = "key_sends"
"""Raw key sends (arrows/Tab/Escape) over /v1/sessions/{name}/send-keys,
policy-gated by permissions.allow_send_keys + input_policy.allow_keys."""

CAPABILITIES: frozenset[str] = frozenset({
    CAP_GRANT_ONLY_ACCESS,
    CAP_SOFT_RECONNECT,
    CAP_RECOVERY_TOMBSTONE,
    CAP_KEY_SENDS,
})

LEGACY_CONTRACT_VERSION = 0
"""What a peer that reports nothing is recorded as. Never assumed compatible."""


def describe() -> dict:
    """The handshake payload, reported by /v1/health and every heartbeat.

    The key is `contract_capabilities`, NOT `capabilities`: the heartbeat body
    already carries `capabilities` for PROBED tool/runtime features
    (git/docker/python). Reusing that name silently clobbered the probed list
    the first time this was wired -- two different questions must not share a
    field.
    """
    return {"contract_version": CONTRACT_VERSION, "contract_capabilities": sorted(CAPABILITIES)}


def compatibility(peer_contract_version: int | None,
                  peer_capabilities: object = None) -> dict:
    """How this build should treat a peer reporting those values.

    Returns `status` plus the reason, so a caller can surface WHY rather than
    just refusing. Three outcomes, and none of them is a silent one:

    * `ok`       -- same contract version.
    * `degraded` -- peer is older (or reported nothing). Still routable: every
                    capability is checked individually before it is used, and
                    an absent one simply is not used. This is the case that
                    used to be invisible.
    * `refused`  -- peer speaks a NEWER contract than this build. It may be
                    relying on semantics this code does not implement, and
                    guessing is how a wrong route becomes a wrong write.
    """
    version = LEGACY_CONTRACT_VERSION if peer_contract_version is None else int(peer_contract_version)
    caps = frozenset(peer_capabilities or ())
    missing = sorted(CAPABILITIES - caps)
    if version > CONTRACT_VERSION:
        return {"status": "refused", "peer_contract_version": version,
                "local_contract_version": CONTRACT_VERSION, "missing_capabilities": missing,
                "reason": (f"peer speaks contract v{version}, this build speaks "
                           f"v{CONTRACT_VERSION} -- upgrade this node before routing to it")}
    if version < CONTRACT_VERSION:
        return {"status": "degraded", "peer_contract_version": version,
                "local_contract_version": CONTRACT_VERSION, "missing_capabilities": missing,
                "reason": (f"peer speaks contract v{version}, this build speaks "
                           f"v{CONTRACT_VERSION} -- routable, but capabilities "
                           f"{missing or '[]'} must not be assumed")}
    return {"status": "ok", "peer_contract_version": version,
            "local_contract_version": CONTRACT_VERSION, "missing_capabilities": missing,
            "reason": "same contract version"}


def describe_for_heartbeat() -> dict:
    """Same values as describe(), keyed for NodeRegistry.heartbeat kwargs."""
    return {"contract_version": CONTRACT_VERSION,
            "contract_capabilities": tuple(sorted(CAPABILITIES))}
