from __future__ import annotations

import re
import time

from .models import SessionInfo


WAIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do you want to continue",
        r"what should claude do instead",
        r"press enter",
        r"enter password",
        r"password\s*:\s*$",
        r"\[y/n\]",
        r"\[Y/n\]",
        r"continue\?\s*$",
        r"\bapprove\b",
        r"\bpermission\b",
        r"waiting for input",
    )
)
ACTIVE_COMMANDS = {"claude", "codex", "python", "python3", "pytest", "node", "npm", "bash", "zsh"}


def detect_waiting_input(output: str) -> tuple[bool, str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    recent = lines[-12:]
    # A prompt must be very near the current pane bottom to avoid matching old logs.
    for offset, line in enumerate(reversed(recent[-4:])):
        for pattern in WAIT_PATTERNS:
            if pattern.search(line):
                return True, f"recent prompt matched {pattern.pattern!r} at bottom offset {offset}"
    return False, "no high-confidence input prompt in the last four non-empty lines"


def classify_status(session: SessionInfo, output: str, now: int | None = None) -> tuple[str, bool, str]:
    waiting, reason = detect_waiting_input(output)
    if waiting:
        return "WAITING_INPUT", True, reason
    if session.pane_dead:
        return "IDLE", False, "tmux reports the active pane is dead"
    age = max(0, (now if now is not None else int(time.time())) - session.activity_epoch)
    command = session.pane_current_command.casefold()
    if command in ACTIVE_COMMANDS and age <= 60:
        return "RUNNING", False, f"current command is {command!r}; tmux activity age is {age}s"
    if command in {"bash", "zsh", "sh", "fish"} and age > 60:
        return "IDLE", False, f"shell pane has no tmux activity for {age}s"
    return "UNKNOWN", False, f"current command is {command or 'unknown'!r}; activity age is {age}s; {reason}"


# ---------------------------------------------------------------------------
# Supervisor extension: layers DONE/ERROR on top of classify_status() above
# rather than re-deriving RUNNING/IDLE/WAITING_INPUT/UNKNOWN. Kept in this
# module (not supervisor.py) because it is squarely "extend existing session
# classification" — the same heuristic family as WAIT_PATTERNS above, not a
# separate detector. Patterns are the same conservative, bottom-of-pane-only
# shape already used above and in the sibling projectflow-watch tool.
# ---------------------------------------------------------------------------

ERROR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"traceback \(most recent call last\)",
        r"\bexception\b.*:",
        r"\bfatal:\s",
        r"\bpanic:\s",
        r"npm err!",
        r"^\s*error\b[:\s]",
        r"\d+\s+failed\b",
        r"non-zero exit status",
        r"exited with code [1-9]",
        r"^\s*✗",
    )
)
DONE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bfinal report\b",
        r"\btask complete\b",
        r"\ball tests passed\b",
        r"✅.*\b(done|complete|passed)\b",
    )
)
SUPERVISOR_STATES = (
    "RUNNING", "IDLE", "WAITING_INPUT",
    "COMPLETION_CANDIDATE", "VERIFIED_DONE",
    "ERROR", "UNKNOWN",
)
# "DONE" is no longer a value classify_supervisor_state (or anything built
# on it) ever produces -- it is legacy-only, available strictly through
# to_legacy_state()/to_legacy_event_type() below, never as the primary
# state model. A raw consumer (a watch row, an event, supervisor_status's
# counts) must handle COMPLETION_CANDIDATE (unverified: prose/marker
# evidence seen, not yet corroborated) and VERIFIED_DONE (corroborated:
# quiet window held, no regression, any configured nonce/verifier passed)
# as genuinely different things -- prose alone was never proof, and
# treating it as interchangeable with "DONE" is exactly the false-positive
# risk this two-state split exists to eliminate.
LEGACY_DONE_STATES = ("COMPLETION_CANDIDATE", "VERIFIED_DONE")


def to_legacy_state(state: str) -> str:
    """Explicit compatibility adapter, never the primary model: collapses
    both new completion states back to the pre-existing "DONE" a caller
    written against the old 6-state vocabulary expects. Call this
    deliberately at an integration boundary -- never let it leak into new
    code as a substitute for checking the real state."""
    return "DONE" if state in LEGACY_DONE_STATES else state


def to_legacy_event_type(event_type: str) -> str:
    """Same adapter for event_type: both new completion event types map
    back to the pre-existing "completed"."""
    return "completed" if event_type in ("completion_candidate", "verified_done") else event_type


def _match_recent(patterns: tuple[re.Pattern[str], ...], output: str, window: int = 20) -> tuple[bool, str]:
    lines = [line for line in output.splitlines() if line.strip()][-window:]
    for offset, line in enumerate(reversed(lines)):
        for pattern in patterns:
            if pattern.search(line):
                return True, f"matched {pattern.pattern!r} at offset {offset} from bottom"
    return False, ""


# ---------------------------------------------------------------------------
# P0-7: structured completion marker. DONE_PATTERNS above is deliberately
# never treated as final proof of a verified completion (see supervisor2.py
# _reconcile_observing_actions's completion-candidate gate) -- it is one
# input to a COMPLETION_CANDIDATE determination, never a direct
# VERIFIED_DONE. An agent that wants stronger, harder-to-spoof-by-accident
# completion evidence can emit this structured marker instead of/alongside
# prose; task_id/nonce let a caller correlate it to a specific attempt (a
# supervisor-issued nonce, once a delivery mechanism to the agent exists,
# would let genuine completion be distinguished from a coincidental or
# copied-in string -- not built here, see the P0 final report's remaining-
# heuristic-limitations section).
# ---------------------------------------------------------------------------

COMPLETION_MARKER_RE = re.compile(
    r"###TERMINAL_MCP_COMPLETION\s+protocol=terminal-mcp-completion/v1\s+([^#\n]*?)###"
)
_MARKER_FIELD_RE = re.compile(r"(\w+)=(\S+)")
COMPLETION_MARKER_REQUIRED_FIELDS = ("task_id", "status", "summary_sha256")


def parse_completion_marker(output: str) -> dict[str, str] | None:
    """Parse the LAST well-formed structured completion marker in `output`,
    if any. Returns its fields as a dict, or None if no marker is present
    or the marker found is missing a required field (never guessed/
    partially trusted -- an ambiguous marker is the same as no marker)."""
    matches = COMPLETION_MARKER_RE.findall(output)
    if not matches:
        return None
    fields = dict(_MARKER_FIELD_RE.findall(matches[-1]))
    if not all(name in fields for name in COMPLETION_MARKER_REQUIRED_FIELDS):
        return None
    if fields.get("status") != "completion_candidate":
        return None
    return fields


def verify_completion_marker(marker: dict[str, str] | None, *, task_id: str, attempt: int,
                             nonce: str | None, nonce_consumed: bool) -> bool:
    """P0-7 phase 2: True only if `marker` (from parse_completion_marker)
    exactly matches the CURRENT, unconsumed attempt's task_id/attempt/
    nonce -- an external caller fetches that nonce via
    supervisor_get_completion_token and is responsible for having the
    agent echo it back. Never guesses: a missing marker, a mismatched
    task_id/attempt (a stale marker from an earlier watch/attempt), or an
    already-consumed nonce (replay of an old marker, e.g. pasted or
    scrolled back into view) all return False -- the caller falls back to
    the ordinary quiet-window promotion instead, never treating a
    non-match as an error."""
    if marker is None or nonce is None or nonce_consumed:
        return False
    return marker.get("task_id") == task_id and marker.get("attempt") == str(attempt) and marker.get("nonce") == nonce


# ---------------------------------------------------------------------------
# P0-7/P0-8 phase 3: trusted verifier hooks. Never executes anything --
# same principle as the completion marker above, generalized: an agent
# that already ran its own tests / checked git status / worked through a
# checklist can print a structured evidence marker reporting the result,
# and a watch can be configured (supervisor_watch's required_verifiers) to
# require one or more kinds of evidence, bound to the same nonce/attempt
# as the completion token, before COMPLETION_CANDIDATE is allowed to
# promote to VERIFIED_DONE at all -- see supervisor.py's
# _handle_completion_candidate / _verifiers_satisfied. A watch with no
# required_verifiers configured (the default) is completely unaffected --
# this is strictly additive, opt-in evidence on top of the existing
# completion-marker/quiet-window promotion, never a replacement for it.
# ---------------------------------------------------------------------------

KNOWN_VERIFIER_KINDS = ("tests", "git_status", "checklist")

EVIDENCE_MARKER_RE = re.compile(
    r"###TERMINAL_MCP_EVIDENCE\s+protocol=terminal-mcp-evidence/v1\s+([^#\n]*?)###"
)
EVIDENCE_MARKER_REQUIRED_FIELDS = ("kind", "task_id", "attempt", "nonce", "status")


def parse_evidence_markers(output: str) -> dict[str, dict[str, str]]:
    """Parse every well-formed evidence marker in `output`, keyed by
    `kind` -- for a given kind, the LAST well-formed marker of that kind
    wins (mirrors parse_completion_marker's "last one wins" rule, so an
    agent can print an early failing attempt and a later passing one and
    only the later one counts). A marker missing a required field, whose
    `status` is not exactly 'pass' or 'fail', or whose `kind` is not one
    of KNOWN_VERIFIER_KINDS, is skipped entirely -- never partially
    trusted or guessed at, the same as an absent marker."""
    result: dict[str, dict[str, str]] = {}
    for match in EVIDENCE_MARKER_RE.findall(output):
        fields = dict(_MARKER_FIELD_RE.findall(match))
        if not all(name in fields for name in EVIDENCE_MARKER_REQUIRED_FIELDS):
            continue
        if fields.get("status") not in ("pass", "fail"):
            continue
        if fields.get("kind") not in KNOWN_VERIFIER_KINDS:
            continue
        result[fields["kind"]] = fields
    return result


def verify_evidence_marker(marker: dict[str, str] | None, *, task_id: str, attempt: int,
                           nonce: str | None, nonce_consumed: bool) -> bool:
    """Same binding check as verify_completion_marker, applied to one
    evidence marker: True only if `marker` (one value from
    parse_evidence_markers) matches the CURRENT, unconsumed attempt's
    task_id/attempt/nonce. This says nothing about pass/fail -- a caller
    checks marker['status'] separately once this confirms the marker is
    genuinely for this attempt, not a stale or copied-in one."""
    if marker is None or nonce is None or nonce_consumed:
        return False
    return marker.get("task_id") == task_id and marker.get("attempt") == str(attempt) and marker.get("nonce") == nonce


def classify_supervisor_state(state: str, reason: str, output: str) -> tuple[str, str]:
    """Normalize a classify_status() result plus ERROR/completion evidence
    to the 7-state supervisor vocabulary. WAITING_INPUT is already high-
    confidence from classify_status and always wins outright — it is never
    overridden by an ERROR/completion marker elsewhere in the same recent
    window. Prose (DONE_PATTERNS) or a structured marker is only ever
    COMPLETION_CANDIDATE here -- this function has no notion of time or
    history, so it cannot itself verify anything; promotion to
    VERIFIED_DONE happens in supervisor.py's polling loop, which tracks a
    quiet window (and any configured nonce/verifier) across multiple calls
    to this classifier. Never inferred from ordinary silence either way —
    that maps to IDLE at the loop level instead (idle_threshold)."""
    if state == "WAITING_INPUT":
        return state, reason
    matched, why = _match_recent(ERROR_PATTERNS, output)
    if matched:
        return "ERROR", why
    matched, why = _match_recent(DONE_PATTERNS, output)
    if matched:
        return "COMPLETION_CANDIDATE", why
    if parse_completion_marker(output) is not None:
        return "COMPLETION_CANDIDATE", "structured completion marker present (protocol=terminal-mcp-completion/v1)"
    if state in ("RUNNING", "IDLE", "UNKNOWN"):
        return state, reason
    return "UNKNOWN", reason

