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

