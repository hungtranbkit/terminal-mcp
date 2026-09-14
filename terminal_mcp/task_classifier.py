"""How much process a change deserves, and which gate it has to pass.

THE COST THIS EXISTS TO REMOVE

A one-line CSS fix does not need the queue engine re-proven. Running a
fifteen-minute full regression before a user can look at a spacing change
spends the user's time and the agent's budget on evidence nobody asked for,
and it delays the only feedback that actually settles the question -- the
user looking at it.

So a change is classified, and the classification decides the gate.

WHY THE EXCLUSIONS COME FIRST

FAST_FIX is defined by what it is NOT. Anything touching auth, payment,
schema, credentials, session consistency or broad infrastructure is escalated
regardless of how small the diff looks, because "small" and "low risk" are
different properties and the expensive mistakes live exactly where they
disagree. The exclusion check runs before any fast-path match, so a one-line
change to a permission check can never be classified as a CSS tweak.

DEPLOY LEVELS ARE SEPARATE FROM CLASSIFICATION

"Deploy it so I can look" is a PREVIEW. It is not a production release, and
this module never infers one from the other. Production stays an explicit
decision with its own approval, no matter how green the gate was.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

FAST_FIX = "FAST_FIX"
NORMAL = "NORMAL"
SAFE = "SAFE"

# Soft budgets. Hints for how much exploration is proportionate -- never a
# hard stop, because stopping early with insufficient evidence produces a
# guess, and a guess costs far more than the tokens it saved.
SMALL = "SMALL"
MEDIUM = "MEDIUM"
LARGE = "LARGE"

PREVIEW = "PREVIEW"
STAGING = "STAGING"
PRODUCTION = "PRODUCTION"

# -- exclusions, checked FIRST ------------------------------------------------
# Each entry is (reason, pattern). Matched against the change description AND
# every changed path, because either can reveal the real blast radius.
EXCLUSIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("auth/authorization", re.compile(
        r"(?i)\b(auth|authoriz|authentic|permission|grant|rbac|acl|login|session[_ ]?token"
        r"|access[_ ]?control|cf[_-]?access"
        r"|đăng nhập|đăng xuất|phân quyền|quyền truy cập|xác thực|uỷ quyền|ủy quyền)\b")),
    ("payment", re.compile(r"(?i)\b(payment|billing|invoice|charge|stripe|refund|checkout)\b")),
    ("database/schema/migration", re.compile(
        r"(?i)\b(migration|schema|alter table|drop table|truncate|backfill|reindex"
        r"|sqlite|postgres|mysql)\b")),
    ("destructive data", re.compile(
        r"(?i)\b(delete|purge|wipe|destroy|rm -rf|force[- ]?push|reset --hard"
        r"|xoá|xóa|huỷ bỏ|hủy bỏ|ghi đè|dọn sạch)\b")),
    ("credentials/secrets", re.compile(
        # A bare "token" is a credential signal, EXCEPT in the token-budget
        # vocabulary this system uses about itself -- "giảm token budget" is
        # a cost decision, not a change to how credentials are handled.
        r"(?i)\b(credential|secret|password|api[_ ]?key|private[_ ]?key|token file|keychain"
        r"|mật khẩu|khoá bí mật|khóa bí mật|chứng chỉ|thông tin đăng nhập"
        r"|token(?!\s*(?:budget|usage|count|limit|efficiency|spend|estimate)))\b")),
    ("session consistency/concurrency", re.compile(
        r"(?i)\b(concurren|race condition|lock|lease|idempoten|transaction|atomic)\b")),
    ("security-sensitive", re.compile(
        r"(?i)\b(csrf|xss|injection|sandbox|redact|sanitiz|tls|certificate|firewall)\b")),
    ("broad infrastructure", re.compile(
        r"(?i)\b(systemd|nginx|cloudflared|tunnel|dns|node[- ]?agent|reboot|kernel)\b")),
)

# -- fast-path signals --------------------------------------------------------
# Deliberately narrow, and only consulted once nothing above matched.
FAST_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("visual/layout", re.compile(
        r"(?i)\b(css|style|spacing|margin|padding|align|overlap|layout|responsive"
        r"|mobile|overflow|colou?r|font|icon|badge|position)\b")),
    ("label/copy", re.compile(r"(?i)\b(label|wording|copy|text|typo|title|tooltip|placeholder)\b")),
    ("visibility", re.compile(r"(?i)\b(hidden|visib|show|hide|collapse|expand|toggle)\b")),
    ("simple frontend state", re.compile(
        r"(?i)\b(render|rerender|state not updating|button|click handler|dropdown|tab)\b")),
    ("non-destructive config", re.compile(
        r"(?i)\b(interval|timeout|default value|feature flag|threshold)\b")),
)

# Paths that are frontend-only in this project. A change confined to these is
# a candidate for the fast gate; anything outside widens it.
UI_ONLY_PATH_HINTS = ("dashboard.py", "webterm_assets.py", ".css", ".html", ".svg")


@dataclass
class Classification:
    mode: str
    budget: str
    reasons: list[str] = field(default_factory=list)
    escalated_from: str | None = None
    exclusions_hit: list[str] = field(default_factory=list)
    gate_procedure: str | None = None
    deploy_level: str = PREVIEW
    requires_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "budget": self.budget, "reasons": list(self.reasons),
                "escalated_from": self.escalated_from,
                "exclusions_hit": list(self.exclusions_hit),
                "gate_procedure": self.gate_procedure,
                "deploy_level": self.deploy_level,
                "requires_approval": self.requires_approval}


def classify(description: str, *, changed_paths: Sequence[str] = (),
             requested_mode: str | None = None) -> Classification:
    """Decide the mode, the gate and the deploy level for one change.

    `requested_mode` may only ESCALATE. A caller asking for FAST_FIX on a
    change that touches authentication gets NORMAL or SAFE and is told why --
    the whole point of the exclusion list is that it is not negotiable by the
    party in a hurry.
    """
    text = " ".join([description or "", *[str(p) for p in changed_paths]])
    hits = [reason for reason, pattern in EXCLUSIONS if pattern.search(text)]

    if hits:
        # Security and data-integrity concerns get SAFE; the rest get NORMAL.
        severe = {"auth/authorization", "payment", "credentials/secrets",
                  "security-sensitive", "database/schema/migration", "destructive data"}
        mode = SAFE if severe & set(hits) else NORMAL
        return Classification(
            mode=mode, budget=LARGE if mode == SAFE else MEDIUM,
            reasons=[f"excluded from FAST_FIX: {', '.join(hits)}"],
            escalated_from=FAST_FIX if requested_mode == FAST_FIX else None,
            exclusions_hit=hits,
            gate_procedure="test_gate",
            deploy_level=STAGING if mode == NORMAL else PRODUCTION,
            requires_approval=mode == SAFE)

    signals = [reason for reason, pattern in FAST_SIGNALS if pattern.search(text)]
    ui_only = bool(changed_paths) and all(
        any(hint in str(path) for hint in UI_ONLY_PATH_HINTS) for path in changed_paths)

    if requested_mode in (NORMAL, SAFE):
        # Asking for MORE process is always allowed.
        return Classification(
            mode=requested_mode, budget=MEDIUM if requested_mode == NORMAL else LARGE,
            reasons=["caller requested a stricter mode"],
            gate_procedure="test_gate",
            deploy_level=STAGING if requested_mode == NORMAL else PRODUCTION,
            requires_approval=requested_mode == SAFE)

    if signals and (ui_only or not changed_paths):
        return Classification(
            mode=FAST_FIX, budget=SMALL,
            reasons=[f"low-risk signal: {', '.join(signals)}"]
                    + (["change is confined to frontend paths"] if ui_only else []),
            gate_procedure="test_ui_fast",
            deploy_level=PREVIEW, requires_approval=False)

    if signals and not ui_only:
        return Classification(
            mode=NORMAL, budget=MEDIUM,
            reasons=[f"low-risk signal ({', '.join(signals)}) but the change reaches "
                     f"beyond frontend paths"],
            gate_procedure="test_gate", deploy_level=STAGING, requires_approval=False)

    return Classification(
        mode=NORMAL, budget=MEDIUM,
        reasons=["no fast-path signal matched; treating as an ordinary change"],
        gate_procedure="test_gate", deploy_level=STAGING, requires_approval=False)


def investigation_plan(classification: Classification) -> list[dict[str, str]]:
    """The ordered steps a worker should take, cheapest evidence first.

    Published as data rather than prose so a prompt builder and the UI show
    the same plan, and so "did it follow the order" is checkable.
    """
    plan = [
        {"step": "knowledge", "action": "load the relevant Project Knowledge subset",
         "why": "a map of where to look costs a fraction of re-deriving it"},
        {"step": "git_delta", "action": "read the delta since the module's last verified commit",
         "why": "the code is the source of truth; the map only says where"},
        {"step": "targeted_search", "action": "search the exact UI text, error, symbol or route",
         "why": "an exact string finds the site faster than reading files"},
        {"step": "read_narrow", "action": "read only the matching functions and their direct callers",
         "why": "reading a whole file to change three lines is the main avoidable cost"},
    ]
    if classification.mode != FAST_FIX:
        plan.append({"step": "expand", "action": "widen only where evidence demands it",
                     "why": "guessing to save tokens costs more than the tokens saved"})
    plan.append({"step": "gate",
                 "action": f"work_procedures(procedure_id=\"{classification.gate_procedure}\") "
                           f"-- or just \"test\"; do not compose the command",
                 "why": "a registered procedure is called, not re-derived: it reuses a "
                        "green result, returns one line, and costs a reading of the "
                        "script only if it fails"})
    plan.append({"step": "deploy",
                 "action": f"{classification.deploy_level.lower()} deploy"
                           + (" after explicit approval" if classification.requires_approval else ""),
                 "why": "preview is for looking; production is a separate decision"})
    return plan


# The policy text handed to a worker. Kept here so the prompt and the
# classifier cannot drift apart.
AGENT_KNOWLEDGE_POLICY = """\
Before exploring the repository broadly, inspect the relevant Project
Knowledge (MODULE_MAP, DEBUG_MAP, DATA_FLOW) and the registered procedures.
The knowledge map is a map. The current code and git history are the
source of truth and override anything stored there: verify the git delta
for the paths you are about to touch before changing them. Explore
progressively -- knowledge, then delta, then an exact search, then only the
functions involved -- and widen only when the evidence requires it. Run tests,
builds, deploys and smokes THROUGH the registry -- `work_procedures` with the
operation name ("test", "build", "deploy", "smoke", "health") -- instead of
composing the command yourself; it registers what this repository already has
on first use, reuses a still-valid green result, and answers in one line.
Inspect a procedure's script only if it fails. After the task, update reusable knowledge only if you learned
something a future task would need. Never write a secret into knowledge: name
the environment variable, never its value.\
"""
