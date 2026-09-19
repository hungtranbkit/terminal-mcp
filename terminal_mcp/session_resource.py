"""Session resource health: how much context/quota a live agent pane has left.

WHY THIS EXISTS

A dispatcher (ChatGPT, the queue loop, an operator) deciding whether to hand a
session one more large task needs two completely different numbers, and until
now could only get them by looking at a screenshot:

  CONTEXT  -- how full THIS session's context window is right now. This is the
              one that decides whether more work fits, and it is the only input
              to `recommended_action` below.
  QUOTA    -- how much of the subscription/rate-limit window the ACCOUNT has
              spent, and when it resets. Says nothing about whether this
              session can hold another task.

Conflating them is the exact failure `ai_context_window.py` already documents
at length for the transcript-derived path: a "usage %" that answers a question
nobody asked. That module stays the authority for transcript-derived context
(structured provider data, verified denominator). THIS module is the pane-
derived path -- the only one available for a session whose transcript this
controller cannot read -- and it is deliberately the more suspicious of the
two, because its input is characters a TUI drew on a screen.

CONSERVATIVE PARSING, AND WHAT "CONSERVATIVE" COSTS

Every number here is either read off a labelled footer field or it is None.
Nothing is inferred from a percentage, nothing is back-computed, nothing is
defaulted. A footer that is absent, partially drawn, mid-redraw, or in a shape
this module has not been shown yields nulls and `observed: False` -- never a
zero, never a stale carry-forward, and never a guess. A wrong reassuring
number here would be acted on by an autonomous dispatcher; a null is merely
uninformative, and the caller already has to handle "unknown" because a plain
shell pane has no footer at all.

The observed Claude Code footer this was written against (real sample,
2026-09-19):

  [Opus 5 (1M context)] | Context ██████████ 96% (in: 2, cache: 955k) | \
      Usage ███░░░░░░░ 30% (resets in 49m)

ROLLOVER IS A RECOMMENDATION, NOT AN ACTION

`recommended_action` and the `rollover` block are hooks: deterministic, and
they never kill, compact or restart anything. In particular a dirty working
tree can never be rolled over on this module's word -- `rollover.allowed` is
False with an explicit reason, because the cost of being wrong is somebody
else's uncommitted work.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .redaction import redact_text

# --------------------------------------------------------------------------
# Policy vocabulary. These strings reach the MCP API and an autonomous
# caller branches on them, so they are stable identifiers, not prose.
# --------------------------------------------------------------------------
TIER_NORMAL = "NORMAL"
TIER_WATCH = "WATCH"
TIER_PREPARE_ROLLOVER = "PREPARE_ROLLOVER"
TIER_FINISH_AND_ROLLOVER = "FINISH_CURRENT_AND_ROLLOVER"
TIER_CHECKPOINT_ONLY = "CHECKPOINT_ONLY"
TIER_UNKNOWN = "UNKNOWN"

CONTEXT_TIERS = (TIER_NORMAL, TIER_WATCH, TIER_PREPARE_ROLLOVER,
                 TIER_FINISH_AND_ROLLOVER, TIER_CHECKPOINT_ONLY, TIER_UNKNOWN)

# context.status -- the severity label, separate from the tier so a UI can
# colour a bar without knowing the rollover policy's vocabulary.
STATUS_BY_TIER = {
    TIER_NORMAL: "NORMAL",
    TIER_WATCH: "WATCH",
    TIER_PREPARE_ROLLOVER: "PREPARE_ROLLOVER",
    TIER_FINISH_AND_ROLLOVER: "CRITICAL",
    TIER_CHECKPOINT_ONLY: "CHECKPOINT_ONLY",
    TIER_UNKNOWN: "UNKNOWN",
}

ACTION_UNKNOWN = "UNKNOWN"
ACTION_CONTINUE = "CONTINUE"
ACTION_CONTINUE_WATCH = "CONTINUE_WATCH"
ACTION_PREPARE_ROLLOVER = "PREPARE_ROLLOVER"
ACTION_FINISH_AND_ROLLOVER = "FINISH_CURRENT_AND_ROLLOVER"
ACTION_CHECKPOINT_ONLY = "CHECKPOINT_ONLY"
# Same rollover intent as the two above, with the one extra precondition a
# dirty tree imposes. A caller that only ever reads `recommended_action`
# still cannot be told to roll over uncommitted work by accident.
ACTION_CHECKPOINT_BEFORE_ROLLOVER = "CHECKPOINT_BEFORE_ROLLOVER"

ACTION_BY_TIER = {
    TIER_NORMAL: ACTION_CONTINUE,
    TIER_WATCH: ACTION_CONTINUE_WATCH,
    TIER_PREPARE_ROLLOVER: ACTION_PREPARE_ROLLOVER,
    TIER_FINISH_AND_ROLLOVER: ACTION_FINISH_AND_ROLLOVER,
    TIER_CHECKPOINT_ONLY: ACTION_CHECKPOINT_ONLY,
    TIER_UNKNOWN: ACTION_UNKNOWN,
}

ROLLOVER_TIERS = (TIER_PREPARE_ROLLOVER, TIER_FINISH_AND_ROLLOVER, TIER_CHECKPOINT_ONLY)

# Quota severity, deliberately its OWN small vocabulary so it can never be
# mistaken for a context tier in a log or a dashboard cell.
QUOTA_NORMAL = "NORMAL"
QUOTA_WARNING = "WARNING"
QUOTA_CRITICAL = "CRITICAL"
QUOTA_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ContextPolicy:
    """The four boundaries from the policy table, as percentages of the
    context window. Defaults are the documented ones; config.py overrides
    them (see SessionHealthConfig) and validates monotonicity there."""
    watch_percent: float = 70.0
    prepare_rollover_percent: float = 85.0
    finish_rollover_percent: float = 92.0
    checkpoint_only_percent: float = 97.0


DEFAULT_CONTEXT_POLICY = ContextPolicy()


def classify_context(percent: float | None, policy: ContextPolicy | None = None) -> tuple[str, str]:
    """(tier, status) for a context fill percentage, or UNKNOWN for None.

    Boundary ownership is fixed by the only reading consistent with the
    policy table's own `<70` / `>92` / `>97` bounds: the low edge of each
    band belongs to the HIGHER tier. So 70 is WATCH (not NORMAL), 85 is
    PREPARE_ROLLOVER, 92 is still PREPARE_ROLLOVER (`>92` starts the next
    band), and 97 is still FINISH_CURRENT_AND_ROLLOVER."""
    policy = policy or DEFAULT_CONTEXT_POLICY
    if percent is None:
        return TIER_UNKNOWN, STATUS_BY_TIER[TIER_UNKNOWN]
    if percent < policy.watch_percent:
        tier = TIER_NORMAL
    elif percent < policy.prepare_rollover_percent:
        tier = TIER_WATCH
    elif percent <= policy.finish_rollover_percent:
        tier = TIER_PREPARE_ROLLOVER
    elif percent <= policy.checkpoint_only_percent:
        tier = TIER_FINISH_AND_ROLLOVER
    else:
        tier = TIER_CHECKPOINT_ONLY
    return tier, STATUS_BY_TIER[tier]


def classify_quota(percent: float | None, *, warning_percent: float = 70.0,
                   critical_percent: float = 90.0) -> str:
    """Quota severity. Never feeds `recommended_action` -- a spent quota
    window means wait or switch account, never "this session is full"."""
    if percent is None:
        return QUOTA_UNKNOWN
    if percent >= critical_percent:
        return QUOTA_CRITICAL
    if percent >= warning_percent:
        return QUOTA_WARNING
    return QUOTA_NORMAL


# --------------------------------------------------------------------------
# Footer parsing.
#
# Every pattern below is anchored on its own LABEL. A bare percentage
# anywhere in pane output is never read as context or quota -- that is the
# whole difference between a measurement and a coincidence.
#
# The gap classes exclude '%' (so a pattern cannot skip over one percentage
# to reach a later one) and are length-bounded (so a "Context" in ordinary
# prose cannot reach a percentage further down the pane). Newlines ARE
# allowed inside the gap, because a footer wider than the pane genuinely
# wraps -- the same real tmux row-wrapping behaviour status.py's completion
# marker already had to be fixed for.
# --------------------------------------------------------------------------
_FOOTER_SCAN_LINES = 12
# 32 characters is generous for what a real footer puts between its label and
# its number -- the observed `Context ██████████ 96%` uses 12, a wrap adds one
# newline -- and tight enough that the word "context" in an ordinary English
# sentence cannot reach a percentage mentioned later in that sentence. Widening
# this trades a false null (harmless) for a false reading (acted upon).
_GAP = r"[^%]{0,32}?"
# Never read a digit out of the middle of something else: `-5%` is not five
# percent, and `196%` is not 96 percent.
_NUMBER = r"(?<![-\d.])(\d{1,3}(?:\.\d+)?)"

_CONTEXT_REMAINING = re.compile(
    rf"\bcontext\b{_GAP}{_NUMBER}\s*%\s*(?:remaining|left|free)", re.IGNORECASE)
_CONTEXT_USED = re.compile(rf"\bcontext\b{_GAP}{_NUMBER}\s*%", re.IGNORECASE)
_USAGE_USED = re.compile(rf"\busage\b{_GAP}{_NUMBER}\s*%", re.IGNORECASE)

# `(in: 2, cache: 955k)` -- the only token counts the footer states outright.
# Deliberately NOT summed with anything derived from the percentage.
_TOKEN_FIELD = re.compile(
    r"\b(in|out|cache|cached|cache read|cache write)\s*:\s*"
    r"(\d+(?:\.\d+)?)\s*([km])?\b", re.IGNORECASE)
_TOKEN_MULTIPLIER = {"k": 1_000, "m": 1_000_000}
# Token counts that describe the prompt the model is holding. `out` is
# excluded: output tokens are not part of the context the next turn starts
# with, and adding them would inflate `used_tokens` past what was observed.
_CONTEXT_TOKEN_FIELDS = {"in", "cache", "cached", "cache read", "cache write"}

# `resets in 49m`, `resets in 1h 5m`, `resets in 2h`, `reset in 90 min`.
_RESET_IN = re.compile(
    r"\breset(?:s|ting)?\s+in\s+(?:(\d{1,3})\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(\d{1,4})\s*m(?:in(?:ute)?s?)?)?", re.IGNORECASE)

# `[Opus 5 (1M context)]`, `[Sonnet 5]`, `claude-opus-5[1m]`. The bracketed
# window statement is the same "provider stated its own window" signal
# ai_context_window.parse_variant_window trusts, in the footer's spelling.
_MODEL_BRACKET = re.compile(r"\[\s*([^\[\]|]{1,80}?)\s*\]")
# REAL false positive, found live 2026-09-19 right after deploy: a pane that
# happened to be displaying this project's own source (`h["Mcp-Session-Id"]
# = sid`) reported `model: "Mcp-Session-Id"`. "Any bracketed text containing a
# letter" is not a model field -- brackets are the single most common
# punctuation in a terminal. A model name is now only accepted when it
# matches one of the known families outright, AND only when a labelled
# Context/Usage percentage was observed in the same footer region (see
# parse_session_resources): a model name with no resource fields beside it is
# not a status footer, it is text that happens to be on screen.
_MODEL_NAME = re.compile(
    r"""^(?:
          (?:claude[-\s])?(?:opus|sonnet|haiku)(?:[-\s]?\d+(?:[-.]\d+)*)?   # Claude
        | (?:gpt|o)[-\s]?\d+(?:[-.]\d+)*(?:[-\s]?codex)?                   # OpenAI/Codex
        | codex(?:[-\s](?:cli|mini|max))?                                   # bare Codex
        )
        (?:\s*\[\s*\d{1,4}\s*[km]\s*\])?$                                # optional [1m]
    """, re.IGNORECASE | re.VERBOSE)
_MODEL_WINDOW = re.compile(r"\(\s*(\d{1,4})\s*([km])\s*(?:context|ctx|window)?\s*\)",
                           re.IGNORECASE)


def _percent(raw: str) -> float | None:
    """A percentage, or None when it is not one. Out-of-range is treated as
    unparseable rather than clamped: a footer reporting 150% has not told us
    the context is full, it has told us this pattern matched the wrong
    thing."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 100.0:
        return None
    return round(value, 1) if value % 1 else int(value)


def _footer_region(output: str) -> str:
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    return "\n".join(lines[-_FOOTER_SCAN_LINES:])


def _last_percent(pattern: re.Pattern[str], text: str) -> float | None:
    """The LAST labelled match in the footer region. A pane holds scrollback:
    an older footer redraw further up is stale by definition."""
    matches = pattern.findall(text)
    for raw in reversed(matches):
        value = _percent(raw if isinstance(raw, str) else raw[0])
        if value is not None:
            return value
    return None


def _parse_model(text: str) -> tuple[str | None, int | None]:
    """(model, max_tokens) from a bracketed footer model field.

    Strict allowlist: a bracketed field is a model only if, once any stated
    window is removed, what remains is a recognized model name. Anything else
    -- a dict key, a log level, an index, a key hint -- yields (None, None),
    and an unrecognized model is reported as unknown rather than echoed back
    as if it had been identified."""
    for raw in reversed(_MODEL_BRACKET.findall(text)):
        candidate = raw.strip()
        if not candidate or len(candidate) > 80:
            continue
        window = None
        window_match = _MODEL_WINDOW.search(candidate)
        if window_match:
            window = int(window_match.group(1)) * _TOKEN_MULTIPLIER[window_match.group(2).lower()]
        name = _MODEL_WINDOW.sub("", candidate).strip(" ·|-")
        if not _MODEL_NAME.match(name):
            continue
        return (redact_text(name) or None), window
    return None, None


def _parse_context_tokens(text: str) -> int | None:
    """Sum of the footer's own stated prompt-token fields, or None.

    Never derived from the percentage: `0.96 * window` would be a
    plausible-looking number this module never actually observed."""
    total = 0
    found = False
    for label, raw, suffix in _TOKEN_FIELD.findall(text):
        if label.lower() not in _CONTEXT_TOKEN_FIELDS:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if suffix:
            value *= _TOKEN_MULTIPLIER[suffix.lower()]
        total += value
        found = True
    return int(total) if found else None


def _parse_reset_minutes(text: str) -> int | None:
    for match in reversed(list(_RESET_IN.finditer(text))):
        hours, minutes = match.group(1), match.group(2)
        if hours is None and minutes is None:
            continue
        total = (int(hours) * 60 if hours else 0) + (int(minutes) if minutes else 0)
        # A stated reset of 0 is not observable information; ignore it
        # rather than reporting "resets now".
        if 0 < total <= 60 * 24 * 7:
            return total
    return None


@dataclass(frozen=True)
class ParsedResources:
    """Exactly what the footer said, with no policy applied yet."""
    model: str | None = None
    context_percent: float | None = None
    context_used_tokens: int | None = None
    context_max_tokens: int | None = None
    usage_percent: float | None = None
    usage_reset_in_minutes: int | None = None

    @property
    def observed(self) -> bool:
        return any(value is not None for value in
                   (self.context_percent, self.usage_percent, self.context_used_tokens,
                    self.usage_reset_in_minutes))


def parse_session_resources(output: str) -> ParsedResources:
    """Read the resource footer of an agent pane, or report nothing.

    Safe to call on any pane output at all -- a plain shell, a compiler log,
    an empty capture. Anything not stated in a labelled footer field comes
    back None."""
    text = _footer_region(output)
    if not text:
        return ParsedResources()
    remaining = _last_percent(_CONTEXT_REMAINING, text)
    if remaining is not None:
        # "4% remaining" is the same fact as "96% used" -- a restatement of
        # an observed number, not a derivation from an unrelated one.
        context_percent: float | None = _percent(str(100 - remaining))
    else:
        context_percent = _last_percent(_CONTEXT_USED, text)
    usage_percent = _last_percent(_USAGE_USED, text)
    # The model field is only read from something that IS a resource footer.
    # No labelled percentage means no footer, so any bracketed text here is
    # just pane content -- see _MODEL_NAME for the live false positive this
    # second gate exists to close.
    model, model_window = (_parse_model(text)
                           if (context_percent is not None or usage_percent is not None)
                           else (None, None))
    return ParsedResources(
        model=model,
        context_percent=context_percent,
        context_used_tokens=_parse_context_tokens(text),
        context_max_tokens=model_window,
        usage_percent=usage_percent,
        usage_reset_in_minutes=_parse_reset_minutes(text),
    )


# --------------------------------------------------------------------------
# Git state. Read-only, bounded, cached -- terminal_status is polled, and a
# status call must never become a git benchmark.
# --------------------------------------------------------------------------
_GIT_TIMEOUT_SECONDS = 3.0
_git_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def probe_git_state(cwd: str | None, *, cache_seconds: float = 10.0,
                    now: Any = time.monotonic) -> dict[str, Any]:
    """{repo, branch, dirty} for a working directory, all None when unknown.

    `dirty` counts untracked files, which is the conservative direction: an
    unignored new file is unsaved work, and rollover must not discard it.
    Never raises. A path that is not a repo, a missing git, or a timeout all
    produce None rather than a cheerful False."""
    unknown = {"repo": None, "branch": None, "dirty": None}
    if not cwd:
        return dict(unknown)
    cached = _git_cache.get(cwd)
    current = float(now())
    if cached and current - cached[0] < cache_seconds:
        return dict(cached[1])
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        _git_cache[cwd] = (current, dict(unknown))
        return dict(unknown)
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(cwd, "status", "--porcelain", "--untracked-files=normal")
    state = {
        "repo": root.strip() or None,
        "branch": (branch or "").strip() or None,
        # None only when the command itself failed -- an empty successful
        # porcelain is a real, positive "clean".
        "dirty": None if porcelain is None else bool(porcelain.strip()),
    }
    _git_cache[cwd] = (current, dict(state))
    return dict(state)


def reset_git_cache() -> None:
    """Test/operational hook: forget every cached git probe."""
    _git_cache.clear()


# --------------------------------------------------------------------------
# Assembly.
# --------------------------------------------------------------------------
def build_resource_block(*, agent: str | None = None,
                         parsed: ParsedResources | None = None,
                         git: dict[str, Any] | None = None,
                         checkpoint: dict[str, Any] | None = None,
                         policy: ContextPolicy | None = None,
                         quota_warning_percent: float = 70.0,
                         quota_critical_percent: float = 90.0,
                         now: datetime | None = None) -> dict[str, Any]:
    """The normalized `resource` payload terminal_status/batch_inspect expose.

    Shape is stable whether or not anything was observable: a caller always
    finds the same keys and reads null, never a missing path."""
    parsed = parsed or ParsedResources()
    policy = policy or DEFAULT_CONTEXT_POLICY
    git = git or {}
    tier, status = classify_context(parsed.context_percent, policy)

    dirty = git.get("dirty")
    rollover_intent = tier in ROLLOVER_TIERS
    blocked_reason = None
    if rollover_intent:
        if dirty is True:
            blocked_reason = "GIT_DIRTY_CHECKPOINT_REQUIRED"
        elif dirty is None:
            # Unproven is not the same as clean. The hook refuses; the
            # recommendation stays honest about context pressure.
            blocked_reason = "GIT_STATE_UNKNOWN"
    action = ACTION_BY_TIER[tier]
    if blocked_reason == "GIT_DIRTY_CHECKPOINT_REQUIRED" and tier != TIER_CHECKPOINT_ONLY:
        action = ACTION_CHECKPOINT_BEFORE_ROLLOVER

    reset_at = None
    if parsed.usage_reset_in_minutes is not None:
        base = now or datetime.now(timezone.utc)
        reset_at = (base + timedelta(minutes=parsed.usage_reset_in_minutes)).isoformat()

    return {
        "agent": agent,
        "model": parsed.model,
        "observed": parsed.observed,
        "context": {
            "used_tokens": parsed.context_used_tokens,
            "max_tokens": parsed.context_max_tokens,
            "percent": parsed.context_percent,
            "status": status,
            "tier": tier,
        },
        # Quota, kept in its own block with its own vocabulary. It is never
        # an input to recommended_action -- see this module's docstring.
        "usage": {
            "percent": parsed.usage_percent,
            "reset_in_minutes": parsed.usage_reset_in_minutes,
            "reset_at": reset_at,
            "status": classify_quota(parsed.usage_percent,
                                     warning_percent=quota_warning_percent,
                                     critical_percent=quota_critical_percent),
        },
        "git": {
            "repo": git.get("repo"),
            "branch": git.get("branch"),
            "dirty": dirty,
        },
        "recommended_action": action,
        # Foundation only: deterministic hooks plus the checkpoint metadata a
        # rollover would need to preserve. Nothing here acts.
        "rollover": {
            "recommended": rollover_intent,
            "allowed": (blocked_reason is None) if rollover_intent else None,
            "requires_checkpoint": bool(rollover_intent and blocked_reason is not None),
            "blocked_reason": blocked_reason,
            "checkpoint": dict(checkpoint or {}),
        },
        "policy": {
            "watch_percent": policy.watch_percent,
            "prepare_rollover_percent": policy.prepare_rollover_percent,
            "finish_rollover_percent": policy.finish_rollover_percent,
            "checkpoint_only_percent": policy.checkpoint_only_percent,
        },
    }
