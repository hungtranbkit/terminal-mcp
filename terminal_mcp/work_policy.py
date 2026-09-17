"""Canonical Work Policy: the rules a worker loads instead of being told them.

The problem this solves is that operating rules have been living in chat
history. A new session, a controller restart, or a different agent starts
without them and has to be re-taught. This module makes the rules a
versioned artefact in the project itself, so any `-work` session can load
them before it executes anything.

Design commitments:

* **The file is the human-readable source of truth.** The text below is the
  bootstrap copy shipped with the code; once written into a project it is a
  normal file people can read and edit. We never silently overwrite an
  edited file -- drift is reported, not erased.
* **Subset loading.** A worker that needs three sections loads three
  sections. Putting the whole policy in context to read one rule is exactly
  the token waste the policy itself forbids.
* **Bindings are recorded, not assumed.** A task records the version and
  hash it actually loaded, so "which rules was this run under" is answerable
  afterwards rather than inferred.
* **A running task keeps its rules.** Changing the policy mid-flight would
  change the contract under a worker that already planned against it. New
  tasks get the new version; running ones are told drift exists and continue
  unless explicitly replanned.
* **Opt-in.** Policy applies to `-work` sessions. A normal terminal session
  is untouched, which is the backward-compatibility guarantee.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .project_knowledge import canonical_root, scrub_knowledge

WORK_POLICY_VERSION = "1.0.0"
POLICIES_DIRNAME = ".projectflow/policies"
POLICY_FILENAME = "WORK_POLICY.md"
PROJECT_POLICY_FILENAME = "PROJECT_POLICY.md"

# Precedence, highest first. This is the rule that decides every conflict,
# and it is ordered by how current and how specific each source is: code
# cannot be out of date about itself, and a policy file cannot know what the
# user just said. The guard clause at the end is not a loophole in the
# ordering -- it is the one thing an instruction cannot override, because a
# release guard exists precisely to survive the moment someone is in a hurry.
PRECEDENCE: tuple[str, ...] = (
    "current code and config (source of truth about what the system does)",
    "explicit current user instruction",
    "project WORK_POLICY (+ PROJECT_POLICY delta)",
    "Knowledge Map",
    "historical memory / past chat",
)
SAFETY_CAVEAT = (
    "A user instruction that conflicts with a safety or release guard does "
    "not remove the guard: keep the guard, say plainly that it applies, and "
    "offer the safe path."
)

# Canonical section keys, in file order. Workers ask for these by name.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("precedence", "Policy Precedence"),
    ("planner_contract", "Bug Planner to Executor Contract"),
    ("difficulty", "Difficulty Triage"),
    ("spec_levels", "Bug Spec Levels"),
    ("completeness", "Spec Completeness Gate"),
    ("needs_redefine", "NEEDS_REDEFINE Flow"),
    ("modes", "Execution Modes"),
    ("token_budget", "Soft Token Budget"),
    ("file_budget", "File and Search Budget"),
    ("knowledge", "Knowledge Map Usage"),
    ("context_pack", "Module Context Pack"),
    ("similar_bugs", "Similar Bug Retrieval"),
    ("runbooks", "Procedural Memory and Runbook Registry"),
    ("verification", "Verification Output Discipline"),
    ("release", "Release Levels"),
    ("developer_assist", "Developer Assist"),
    ("human_hints", "Human Hints"),
    ("telemetry", "Telemetry"),
    ("compatibility", "Backward Compatibility"),
    ("secrets", "Secrets"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)
_TITLE_TO_KEY = {title.lower(): key for key, title in SECTIONS}

CANONICAL_POLICY = f"""<!-- WORK_POLICY_VERSION: {WORK_POLICY_VERSION} -->
# Work Policy

Canonical operating rules for Work sessions. A `-work` session loads this
before executing a task, so the rules do not depend on chat history.

- **WORK_POLICY_VERSION:** {WORK_POLICY_VERSION}
- **Applies to:** sessions whose name ends in `-work`, and the tasks they run.
- **Does not apply to:** ordinary terminal sessions, which behave exactly as
  they did before this file existed.

## Policy Precedence

Highest first:

1. Current code and config -- the source of truth about what the system does.
2. Explicit current user instruction.
3. This WORK_POLICY, plus any PROJECT_POLICY delta.
4. Knowledge Map.
5. Historical memory or past chat.

{SAFETY_CAVEAT}

## Bug Planner to Executor Contract

The planner produces a persistent Bug Execution Spec; the worker executes it.
The handoff is the spec, never a chat transcript.

Before editing anything, the worker does a SHORT plan verification against the
current code and reports one of:

- `PLAN_CONFIRMED` -- the spec matches what the code actually does.
- `PLAN_ADJUSTED` -- mostly right; state precisely what changed and why.
- `PLAN_MISMATCH` -- the premise is wrong; stop and hand back with the reason.

After a fix lands, update DEBUG_MAP with the real root cause, so the next bug
in this module starts from evidence rather than from nothing.

## Difficulty Triage

Every spec carries `DIFFICULTY` (EASY / MEDIUM / HARD) and
`DIFFICULTY_CONFIDENCE`. Difficulty is a separate axis from execution mode:
mode is how much process a change needs, difficulty is how much is understood.

Hard signals -- auth/session, concurrency, data consistency, multi-service,
unexplained regression, infra/security -- outrank an easy-looking surface. A
misaligned button that only misaligns after re-login is an auth bug.

## Bug Spec Levels

- `L1_EXACT_FIX` -- module, files and root cause known; the fix is prescribed.
- `L2_DIRECTED_INVESTIGATION` -- area known, cause not; search boundary given.
- `L3_UNKNOWN` -- needs reproduction first; must still carry a reproduction
  path, a search boundary and a maximum scope.

The level is derived from the evidence present, never asserted.

## Spec Completeness Gate

A spec is scored before dispatch: L1 requires >= 90%, L2 >= 75%, L3 >= 40%
plus reproduction, search boundary and max scope. Below threshold the status
is `NEEDS_REDEFINE`.

## NEEDS_REDEFINE Flow

Return `MISSING` (the fields that are absent) and `QUESTIONS_FOR_PLANNER`
(specific, answerable). Do not start a wide investigation to compensate for a
thin spec -- that converts a planning gap into token spend.

## Execution Modes

- `FAST_FIX` -- localized, low-risk, visually verifiable. Preview first.
- `NORMAL` -- ordinary change with a normal test gate.
- `SAFE` -- migrations, auth, permissions, deploy paths, data integrity.
  Full gate and approval; never route these to FAST_FIX.

Exclusions are checked before fast-path signals, so a small-looking change in
a dangerous area stays SAFE.

## Soft Token Budget

Budgets are SMALL / MEDIUM / LARGE by bug class. They are guidance, not a
kill switch: **never hard-stop mid-task**. On crossing a soft limit, report
`TOKEN_BUDGET_STATUS`, say what was learned and what remains, and either
finish or escalate with a reason -- an abandoned task costs more than the
reading it saved.

## File and Search Budget

L1 should need about 5 files and 2 search rounds; L2 and L3 scale up. An L1
that blows its budget was not an L1: hand it back for a better spec rather
than quietly turning it into an open investigation.

## Knowledge Map Usage

Investigation order: knowledge map, then git delta, then exact search, then a
narrow read, and only widen on evidence.

**The map is a map. The current code and git state are the source of truth.**
Confidence is HIGH / MEDIUM / LOW, derived from whether a module's paths have
changed since it was verified -- never a fabricated percentage. A stale entry
is a lead to check, not a fact to rely on.

## Module Context Pack

A bounded briefing per module: purpose, files, entry points, test and smoke
runbooks, known issues, past bugs. It is capped on purpose, and it states
what it does not cover so a thin pack is not mistaken for a complete one.

## Similar Bug Retrieval

Before investigating, check whether this bug has been seen. A strong match is
offered as `REUSED_BUG_SPEC` with its source commit attached; every path it
names is verified against current code before editing. A weaker match is
offered as reading, not as a conclusion.

## Procedural Memory and Runbook Registry

Repeated operations -- test, build, deploy, smoke, health check -- run from
registered scripts in `scripts/agent/`. Look up the registry before doing
such an operation by hand. **Reuse existing scripts; never duplicate one.**
Read a script's body only when it fails or is stale. Scripts are idempotent,
fail fast, and print a one-line verdict. Knowledge stores the metadata and a
link, never the script body.

## Verification Output Discipline

On success, print one concise `PASS` line. Pull the full log only on `FAIL`,
and then only the relevant window. Test gates are dependency-aware: run what
the change can affect, cached by commit and path.

## Release Levels

`PREVIEW` -> `STAGING` -> `PRODUCTION` are separate and explicit. A preview
is not a deploy. Production requires the full gate and approval, and the
approval covers one release, not a standing permission.

## Developer Assist

The user is a developer. For a HARD bug the planner analyses FIRST, then asks
1-3 high-value technical questions -- each one specific enough to eliminate a
branch, sent together with the current findings and hypotheses.

Vague questions are forbidden. "Can you give more detail?" wastes the one
resource this section exists to spend carefully.

If no answer arrives, proceed on the best available evidence within the
spec's boundary. Never block waiting.

## Human Hints

`HUMAN_HINTS[]` records developer guidance with its provenance, and
`HUMAN_ASSIST_STATUS` tracks whether help was needed, requested, received or
unavailable. A hint is guidance, not truth: verify it against the current
code, and if it turns out wrong, correct the spec and say so.

## Telemetry

Record per task: token usage (exact when the runtime reports it, otherwise
clearly marked `estimated` or `unknown`), files read, search rounds, runbook
hits and misses, redefine count, and time to first preview.

**Never fabricate precision.** An unavailable number is reported as
unavailable; an invented one corrupts every efficiency decision made from it.

## Backward Compatibility

Ordinary terminal sessions are not Work sessions and this policy does not
apply to them. Work features are opt-in via the `-work` suffix, and existing
sessions, APIs and tools keep working unchanged.

## Secrets

Never store secret values in a policy, spec, knowledge document, runbook or
telemetry record. Environment variable NAMES only. Never print or echo a real
credential. Never copy a private key, password, token or passphrase between
nodes.

## Changelog

- **1.0.0** -- initial canonical policy: planner/executor contract, difficulty
  triage, spec levels and completeness gate, execution modes, soft token and
  file/search budgets, knowledge map usage, context packs, similar-bug
  retrieval, runbook registry, verification discipline, release levels,
  developer assist, human hints, telemetry, compatibility and secrets.
"""

# Accepts a semver pre-release/build suffix so a project override marked
# "1.0.0-offlinepos.1" is not silently reported as plain "1.0.0" -- that
# truncation made an override indistinguishable from the canonical version.
_VERSION_RE = re.compile(
    r"WORK_POLICY_VERSION:\s*([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.\-]+)?)")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def policy_dir(cwd: str) -> Path | None:
    """Where policies live -- resolved through the canonical repository root.

    Worktrees of one repository share a policy, for the same reason they
    share a knowledge map: two worktrees operating under silently different
    rules is a bug that is very hard to see from inside either one.
    """
    root = canonical_root(cwd)
    if root is None:
        # Not a git repository (or a bare one): there is no project to hold a
        # policy file. Returning None lets the loader fall back to the
        # built-in text, so a worker outside a repo still has rules -- rather
        # than crashing the run that was trying to obey them.
        return None
    return root / POLICIES_DIRNAME


def parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def split_sections(text: str) -> dict[str, str]:
    """Map canonical section key -> that section's text.

    Unknown headings are kept under a slugged key rather than dropped: a
    project that adds a section should not have it silently disappear from
    the loader that is supposed to be showing people the effective policy.
    """
    out: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text or ""))
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start():end].strip()
        key = _TITLE_TO_KEY.get(title.lower())
        if key is None:
            key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        out[key] = body
    return out


@dataclass(frozen=True)
class PolicyBinding:
    """What a task actually loaded -- recorded on its execution metadata."""

    policy_version: str
    policy_hash: str
    loaded_at: str
    source: str
    override_present: bool = False
    override_version: str | None = None
    effective_hash: str = ""
    sections_loaded: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "loaded_at": self.loaded_at,
            "source": self.source,
            "override_present": self.override_present,
            "override_version": self.override_version,
            "effective_hash": self.effective_hash or self.policy_hash,
            "sections_loaded": list(self.sections_loaded),
        }


@dataclass
class EffectivePolicy:
    """The canonical policy plus any project delta, already merged."""

    version: str
    text: str
    sections: dict[str, str]
    source: str
    override_present: bool = False
    override_version: str | None = None
    override_sections: tuple[str, ...] = ()
    file_version: str | None = None
    builtin_version: str = WORK_POLICY_VERSION
    drift: tuple[str, ...] = field(default_factory=tuple)

    @property
    def policy_hash(self) -> str:
        return _hash(self.text)

    def section(self, key: str) -> str | None:
        return self.sections.get(key)

    def load(self, keys: Sequence[str]) -> str:
        """Only the sections asked for -- the whole point of subset loading.

        A worker deciding FAST_FIX versus SAFE needs `modes` and maybe
        `release`; pulling twenty sections to answer that is the waste the
        policy itself prohibits.
        """
        wanted = [k for k in keys if k in self.sections]
        header = f"WORK_POLICY v{self.version}"
        if self.override_present:
            header += f" + project override v{self.override_version or '?'}"
        missing = [k for k in keys if k not in self.sections]
        body = "\n\n".join(self.sections[k] for k in wanted)
        if missing:
            body += "\n\n[sections not present in this policy: " + ", ".join(missing) + "]"
        return f"{header}\n\n{body}".strip()

    def binding(self, *, sections_loaded: Sequence[str] = ()) -> PolicyBinding:
        return PolicyBinding(
            policy_version=self.version,
            policy_hash=_hash(self.text),
            loaded_at=_now(),
            source=self.source,
            override_present=self.override_present,
            override_version=self.override_version,
            effective_hash=_hash(self.text),
            sections_loaded=tuple(sections_loaded),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "source": self.source,
            "policy_hash": self.policy_hash,
            "override_present": self.override_present,
            "override_version": self.override_version,
            "override_sections": list(self.override_sections),
            "file_version": self.file_version,
            "builtin_version": self.builtin_version,
            "drift": list(self.drift),
            "sections": list(self.sections),
        }


def ensure_policy_file(cwd: str, *, overwrite: bool = False) -> dict[str, Any]:
    """Materialise the canonical policy into the project.

    Writes only when absent, unless `overwrite` is explicit. A project that
    has edited its policy keeps those edits: silently restoring the shipped
    text would delete a deliberate decision, and the version numbers exist so
    drift can be reported instead.
    """
    directory = policy_dir(cwd)
    if directory is None:
        return {"written": False, "path": None, "version": None,
                "reason": "NOT_A_GIT_REPOSITORY",
                "detail": f"{cwd!r} is not inside a git worktree; a project policy "
                          f"needs a project to live in"}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / POLICY_FILENAME
    if path.exists() and not overwrite:
        existing = path.read_text(encoding="utf-8")
        return {"written": False, "path": str(path),
                "version": parse_version(existing), "reason": "already present"}
    text = scrub_knowledge(CANONICAL_POLICY, where=POLICY_FILENAME)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return {"written": True, "path": str(path), "version": WORK_POLICY_VERSION,
            "reason": "overwritten" if overwrite else "created"}


def load_policy(cwd: str) -> EffectivePolicy:
    """Load the effective policy: canonical file (or built-in) plus delta.

    Falls back to the built-in text when a project has no file yet, so a
    worker in a fresh project still operates under the rules instead of
    under none. The fallback is reported in `source`, never disguised.
    """
    directory = policy_dir(cwd)
    path = (directory / POLICY_FILENAME) if directory is not None else None
    drift: list[str] = []

    if path is not None and path.exists():
        text = path.read_text(encoding="utf-8")
        source = str(path)
        file_version = parse_version(text)
        version = file_version or WORK_POLICY_VERSION
        if file_version is None:
            drift.append("policy file has no WORK_POLICY_VERSION marker; "
                         f"assuming built-in {WORK_POLICY_VERSION}")
        elif file_version != WORK_POLICY_VERSION:
            drift.append(f"project policy is v{file_version}, built-in is "
                         f"v{WORK_POLICY_VERSION}; the project file wins")
    else:
        text = CANONICAL_POLICY
        source = "built-in"
        file_version = None
        version = WORK_POLICY_VERSION
        drift.append("no WORK_POLICY.md found; using the built-in policy"
                     + ("" if directory is not None else
                        " (not inside a git worktree)")
                     + " -- run ensure_policy_file to materialise it")

    sections = split_sections(text)
    policy = EffectivePolicy(version=version, text=text, sections=sections,
                             source=source, file_version=file_version,
                             drift=tuple(drift))

    override_path = (directory / PROJECT_POLICY_FILENAME) if directory is not None else None
    if override_path is not None and override_path.exists():
        override_text = override_path.read_text(encoding="utf-8")
        override_sections = split_sections(override_text)
        if not override_sections:
            policy.drift = policy.drift + (
                f"{PROJECT_POLICY_FILENAME} has no '## ' sections; ignored",)
        else:
            # Delta only: a project overrides the sections it names and
            # inherits the rest. Copying the whole policy into the override
            # is how two files drift apart, so we merge rather than replace.
            merged = dict(sections)
            merged.update(override_sections)
            policy.sections = merged
            policy.text = text + "\n\n<!-- project override -->\n" + override_text
            policy.override_present = True
            policy.override_version = parse_version(override_text)
            policy.override_sections = tuple(override_sections)
            duplicated = [k for k in override_sections
                          if override_sections[k].strip() == sections.get(k, "").strip()]
            if duplicated:
                policy.drift = policy.drift + (
                    "override duplicates canonical text for: "
                    + ", ".join(sorted(duplicated)) + " -- keep only the delta",)
    return policy


def binding_is_stale(binding: PolicyBinding | dict[str, Any],
                     current: EffectivePolicy) -> dict[str, Any]:
    """Has the policy moved since this task bound to it?

    Reports; does not act. A task that already planned under v1.0.0 keeps
    running under v1.0.0 -- swapping the rules under a worker mid-flight
    would invalidate a plan it has no way to know changed. The next task
    picks up the new version, and an explicit replan is how a running task
    adopts it deliberately.
    """
    raw = binding.as_dict() if isinstance(binding, PolicyBinding) else dict(binding)
    same = raw.get("effective_hash") == _hash(current.text)
    if same:
        return {"stale": False, "action": "continue"}
    return {
        "stale": True,
        "action": "continue_under_bound_version",
        "bound_version": raw.get("policy_version"),
        "current_version": current.version,
        "note": ("policy changed after this task bound to it; the task continues "
                 "under the version it planned against. New tasks use the current "
                 "version; an explicit reload/replan is required to switch."),
    }


def policy_for_task(cwd: str, *, session: str | None = None,
                    sections: Sequence[str] = ()) -> dict[str, Any]:
    """The auto-load entry point a Work task calls before executing.

    Returns the binding to record plus only the requested sections. A
    non-Work session gets `applies=False` and nothing else: that is the
    backward-compatibility guarantee, enforced here rather than remembered.
    """
    from .work_eligibility import is_work_session

    if session is not None and not is_work_session(session):
        return {"applies": False, "reason": "NOT_WORK_SESSION",
                "note": "ordinary terminal session; Work policy does not apply"}
    policy = load_policy(cwd)
    keys = list(sections) or list(SECTION_KEYS)
    return {
        "applies": True,
        "binding": policy.binding(sections_loaded=keys).as_dict(),
        "version": policy.version,
        "override_present": policy.override_present,
        "drift": list(policy.drift),
        "text": policy.load(keys),
        "available_sections": list(policy.sections),
    }
