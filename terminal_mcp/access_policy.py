"""Which operational data an authenticated operator may read -- written down
once, so no guard has to guess.

WHY THIS FILE EXISTS
--------------------
Audited 2026-09-12 after operators reported being blocked from ordinary
debugging. The finding was not what anyone expected: `_read_guard` is a
Cloudflare Access identity check and nothing more, so reads were already
default-open. Nobody was being DENIED the audit log -- there was no way to
ask for it. The audit log had no dashboard route and no HTTP API at all, and
its one MCP tool could not filter by time, actor, action or result. An
operator debugging an incident had a 50-row reverse-chronological list and
no way to narrow it.

That is a worse failure than an over-strict rule, because it looks like
security. A missing surface and a deny-all guard are indistinguishable from
the outside, and both get "fixed" by someone eventually turning something off
that mattered. So the tiers below are explicit, and the things that must
NEVER be served are named rather than left to each caller's judgement.

THE THREE TIERS
---------------
SECRET            Never served to anyone, by any surface, at any role. Not
                  redacted-on-a-flag: absent. A token, password, passphrase,
                  private key, cookie or bearer has no read path here.
SENSITIVE_METADATA  Served to an authenticated operator, with secret-shaped
                  content redacted and long text fingerprinted. Pane previews
                  and prompt previews live here: an operator debugging a
                  stuck agent genuinely needs to see what it was sent, and a
                  sha256 plus a redacted 240-char preview is how they get it
                  without the raw text becoming a retrievable store.
OPERATIONAL       Served to any authenticated operator, unredacted. Who did
                  what, when, to which node and session, allowed or denied,
                  why, how long it took, which policy decided. None of it is
                  secret, all of it is what debugging is made of.

`allowed` and `deny_reason` come back TOGETHER so a refusal can say which
field and which tier stopped it -- a generic 403 teaches an operator nothing
and trains them to escalate instead of read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

TIER_SECRET = "SECRET"
TIER_SENSITIVE = "SENSITIVE_METADATA"
TIER_OPERATIONAL = "OPERATIONAL"

ROLE_ANONYMOUS = "anonymous"    # no verified identity
ROLE_OPERATOR = "operator"      # a verified Access identity
ROLE_OWNER = "owner"            # reserved; identical read rights to operator

# Identity is the boundary, not role. This project has no user store to build
# a real role hierarchy on (see cf_access.AccessIdentity), and inventing one
# would be a fiction that reads like a guarantee. So: authenticated operators
# read operational data, anonymous callers read nothing.
READING_ROLES = (ROLE_OPERATOR, ROLE_OWNER)


# --------------------------------------------------------------------------
# The policy table. One row per field family an API can return.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicyRow:
    field: str
    tier: str
    rationale: str


POLICY_TABLE: tuple[PolicyRow, ...] = (
    # -- SECRET: no read path exists. ---------------------------------------
    PolicyRow("password", TIER_SECRET, "replayable credential"),
    PolicyRow("passphrase", TIER_SECRET, "replayable credential"),
    PolicyRow("token", TIER_SECRET, "replayable credential"),
    PolicyRow("bearer", TIER_SECRET, "replayable credential"),
    PolicyRow("api_key", TIER_SECRET, "replayable credential"),
    PolicyRow("private_key", TIER_SECRET, "replayable credential"),
    PolicyRow("cookie", TIER_SECRET, "session-stealing material"),
    PolicyRow("secret_env", TIER_SECRET, "process environment may hold any of the above"),
    PolicyRow("authorization", TIER_SECRET, "the header most likely to hold a bearer token"),

    # -- SENSITIVE_METADATA: operator may read, redacted/fingerprinted. -----
    PolicyRow("text_preview", TIER_SENSITIVE,
              "what an agent was actually sent; redacted, capped, and paired "
              "with a sha256 so an operator can correlate without the store "
              "becoming a way to retrieve the original"),
    PolicyRow("text_sha256", TIER_SENSITIVE, "fingerprint, not content"),
    PolicyRow("last_output", TIER_SENSITIVE, "pane text, ANSI-stripped and redacted"),
    PolicyRow("keys", TIER_SENSITIVE, "key names sent, e.g. C-c"),
    PolicyRow("host_key_fingerprint", TIER_SENSITIVE,
              "a hash, and exactly what pinning compares -- an operator needs "
              "it to verify a host"),
    PolicyRow("public_key_id", TIER_SENSITIVE, "the public half is meant to be handed out"),

    # -- OPERATIONAL: the substance of debugging. ---------------------------
    PolicyRow("timestamp", TIER_OPERATIONAL, "when"),
    PolicyRow("actor", TIER_OPERATIONAL, "who -- the verified Access identity"),
    PolicyRow("action", TIER_OPERATIONAL, "what tool/route was invoked"),
    PolicyRow("session", TIER_OPERATIONAL, "which session"),
    PolicyRow("node_id", TIER_OPERATIONAL, "which machine"),
    PolicyRow("binding", TIER_OPERATIONAL, "which binding"),
    PolicyRow("result", TIER_OPERATIONAL, "allow/deny/outcome"),
    PolicyRow("reason", TIER_OPERATIONAL, "WHY it was denied -- the field an operator needs most"),
    PolicyRow("correlation_id", TIER_OPERATIONAL, "ties a submission across surfaces"),
    PolicyRow("trace_id", TIER_OPERATIONAL, "ties a turn across nodes"),
    PolicyRow("parent_turn_id", TIER_OPERATIONAL, "loop-protection lineage"),
    PolicyRow("depth", TIER_OPERATIONAL, "loop-protection depth"),
    PolicyRow("latency_ms", TIER_OPERATIONAL, "how long it took"),
    PolicyRow("policy_source", TIER_OPERATIONAL, "which rule decided -- grant, policy, default"),
    PolicyRow("policy_version", TIER_OPERATIONAL, "which generation of that rule"),
    PolicyRow("source_transport", TIER_OPERATIONAL, "dashboard/mcp/node"),
    PolicyRow("server_version", TIER_OPERATIONAL, "which build"),
    PolicyRow("origin", TIER_OPERATIONAL, "where the call came from"),
    PolicyRow("auth_status", TIER_OPERATIONAL, "authenticated / NEEDS_AUTH / MISSING / DRIFT"),
    PolicyRow("auth_source", TIER_OPERATIONAL, "how the credential is held, never what it is"),
    PolicyRow("capability", TIER_OPERATIONAL, "what this node can do"),
    PolicyRow("readiness", TIER_OPERATIONAL, "PASS/WARN/FAIL"),
    PolicyRow("last_verified_at", TIER_OPERATIONAL, "when it was last proven"),
)

_BY_FIELD = {row.field: row for row in POLICY_TABLE}

# Field NAMES that always resolve to SECRET even when not listed above, so a
# field added later defaults to closed rather than open. Matches whole
# underscore-separated segments; `_ref`/`_status`/`_at`-style suffixes name
# metadata ABOUT a secret and are allowed.
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:secret|password|passwd|passphrase|token|apikey|api_key|private_key|"
    r"privatekey|credential|auth|authorization|bearer|cookie|session_key)(?:$|_)",
    re.IGNORECASE)
# `_reason` was added after the policy filter silently deleted
# `auth_status_reason` -- the sentence explaining WHY a node's auth could not
# be confirmed -- because the name starts with the `auth` segment. A field
# ending in `_reason` holds a human-readable explanation, never a credential,
# and dropping it makes a status less useful precisely when it is worst.
_METADATA_SUFFIX = re.compile(
    r"_(?:ref|path|file|env_name|status|state|source|url|ok|at|id|count|version|reason)$",
    re.IGNORECASE)


def tier_for(field: str) -> str:
    """Which tier a field belongs to. Unknown fields default to SECRET when
    their NAME looks like a credential, OPERATIONAL otherwise.

    Defaulting an unknown credential-shaped name to SECRET is the important
    half: a field added next year without a policy row must not be served
    because nobody remembered to classify it.
    """
    row = _BY_FIELD.get(field)
    if row is not None:
        return row.tier
    if _SECRET_NAME.search(field) and not _METADATA_SUFFIX.search(field):
        return TIER_SECRET
    return TIER_OPERATIONAL


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    tier: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "tier": self.tier, "field": self.field}


def may_read(field: str, *, role: str) -> Decision:
    """May this role read this field? With the reason, always.

    A refusal names the field and the tier. "Forbidden" on its own teaches an
    operator nothing and trains them to escalate instead of read, which is
    how a system ends up with its guards turned off.
    """
    tier = tier_for(field)
    if tier == TIER_SECRET:
        return Decision(False, f"{field} is SECRET: no read path exists for any role",
                        tier=tier, field=field)
    if role not in READING_ROLES:
        return Decision(False,
                        f"{field} is {tier} and requires a verified identity; "
                        f"this request is {role}",
                        tier=tier, field=field)
    return Decision(True, f"{field} is {tier}; readable by {role}", tier=tier, field=field)


def filter_record(record: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Strip what this role may not see, keeping everything it may.

    Drops SECRET fields outright rather than masking them: a key present as
    "<REDACTED>" still tells an attacker the field exists and is populated,
    and more importantly it tempts the next person to add a debug flag that
    unmasks it.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if may_read(key, role=role).allowed:
            out[key] = value
    return out


def role_for_identity(identity: Any, *, access_configured: bool) -> str:
    """The role a request gets.

    When Cloudflare Access is not configured the guard is a no-op and hands
    back no identity -- but the deployment has then chosen to protect this
    service at the network edge (see cf_access.py). Treating that as
    anonymous would lock every self-hosted operator out of their own audit
    log, which is precisely the over-restriction this audit was called to
    fix. Configured-and-verified and not-configured are both OPERATOR;
    configured-and-unverified never reaches here, because the guard already
    refused it.
    """
    if not access_configured:
        return ROLE_OPERATOR
    return ROLE_OPERATOR if identity is not None else ROLE_ANONYMOUS


def policy_table() -> list[dict[str, Any]]:
    """The table itself, served so an operator can see the rules rather than
    infer them from what happens to be missing."""
    return [{"field": row.field, "tier": row.tier, "rationale": row.rationale}
            for row in POLICY_TABLE]
