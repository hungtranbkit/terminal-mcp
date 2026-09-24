from __future__ import annotations

import re
import time

from . import adapters
from .adapters import normalize_command, select_adapter
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
        r"waiting for input",
        # Claude Code's own multi-choice selection widget (numbered options,
        # arrow-key/Tab navigation, Enter to accept the highlighted choice).
        # These three are the widget's UI-chrome strings, live-verified
        # against a real attended session and already carried by
        # adapters._WAITING_PATTERNS, which is where this set was found to
        # be missing them: a session sitting on a real permission menu
        # reported state=UNKNOWN/input_required=False from classify_status
        # while the very same pane content made adapters refuse the send as
        # TARGET_AWAITING_APPROVAL. The two pattern sets disagreeing about
        # the same observed dialog is exactly the class of bug the removal
        # note just below documents, in the other direction.
        r"enter to select",
        r"tab/arrow keys to navigate",
        r"esc to cancel",
        # REMOVED (2026-09-07, real false positive found LIVE against a
        # real attended session, `window2`): this list used to also
        # include bare r"\bapprove\b" and r"\bpermission\b" word-boundary
        # matches -- a real duplicate of the same bug already found and
        # fixed in adapters.py's own _WAITING_PATTERNS (see that
        # module's own docstring for the full root-cause writeup; the
        # exact same reasoning applies here verbatim). An ordinary
        # composer line about this project's own subject matter (a
        # permissions/roles feature) wrongly reported classify_status's
        # `state` as WAITING_INPUT/`input_required` as True for a
        # completely idle, ordinary session -- confirmed live via
        # terminal_status against window2 itself. Removed rather than
        # narrowed, same reasoning as adapters.py: no real, observed
        # Claude/Codex dialog on record uses the bare word "approve" or
        # "permission" outside a numbered-menu/y-n shape already caught
        # by the patterns above.
    )
)
_AGENT_COMMANDS = {"claude", "codex"}

ACTIVE_COMMANDS = {"claude", "codex", "python", "python3", "pytest", "node", "npm", "bash", "zsh"}


# The shell counterpart of detect_agent_ui_state() below: same principle --
# the pane's own bottom line is direct evidence, activity_epoch is a proxy.
# A shell pane whose LAST non-empty line is a bare prompt, with nothing typed
# after it, has handed control back and the command is over. Without this a
# finished bash pane reports RUNNING for a full 60s purely because "bash" is in
# ACTIVE_COMMANDS. Measured 2026-09-21: ages 0s..60s all RUNNING, flipping only
# at 61s, while the prompt sat visible the whole time -- so
# terminal_turn(send_wait) on a shell burned up to a minute after the work was
# done, which reads as "Terminal MCP is slow / hanging".
#
# Deliberately narrow. A line with a command echoed after the prompt does NOT
# match (\s*$ requires the prompt to end the line), so it cannot fire while a
# command is still on screen waiting to run. A bare ">" is NOT accepted: in bash
# that is a continuation prompt for an unterminated quote, where the shell is
# waiting for input, not idle. An unrecognised prompt does not match and falls
# back to the old timer -- degrading to today's behaviour, never a wrong answer.
_SHELL_PROMPT_READY = re.compile(
    r"^(?:\S+@\S+:\S*[$#]|PS [A-Za-z]:\\\S*>|[$#])\s*$"
)
_SHELLS = {"bash", "zsh", "sh", "fish", "dash"}


def shell_prompt_is_back(output: str) -> bool:
    """True when the bottom of the pane is a bare shell prompt."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    return bool(lines) and bool(_SHELL_PROMPT_READY.match(lines[-1]))


# ---------------------------------------------------------------------------
# Agent-CLI state markers -- the fix for "terminal_status says UNKNOWN for a
# session whose own pane plainly shows what it is doing".
#
# Why the command+activity_epoch rules in classify_status are not enough:
# tmux's #{session_activity} is unreliable for an Ink-rendered agent CLI, in
# BOTH directions (terminal_wall.py's OutputChangeTracker reached the same
# conclusion independently, from the same fleet). Captured LIVE on this host,
# 2026-09-19, against real Claude Code 2.1.277 sessions:
#
#   terminal-mcp-claude-tests -- activity age 663s, yet genuinely WORKING:
#     "* Billowing... (10m 25s . 17.9k tokens)"
#     ">> auto mode on . 1 shell . esc to interrupt . <- for agents ..."
#     pane_current_command="claude" with age>60 falls through every rule
#     below to UNKNOWN, for a session that is unmistakably RUNNING.
#
#   hp-work / hp2 / terminal-mcp-claude-audit -- finished turns, idle:
#     "* Sauteed for 20s . done 7:25 AM"
#     "                    new task? /clear to save 167.7k tokens"
#     "* Brewed for 19s . done 10:06 AM"
#     "* Cogitated for 39m 35s . done 10:05 AM . 1 shell still running"
#     same fall-through to UNKNOWN, for sessions that are unmistakably IDLE.
#
# Deliberately only these two markers, both read off the CLI's OWN chrome
# (never the model's prose), both verified against those captures:
#
#   RUNNING -- "esc to interrupt". The busy footer, and already the one
#     working-evidence marker adapters.py has real Claude AND real Codex
#     evidence for; reused here rather than invented. NOT extended with
#     adapters' looser \bworking\b/\bthinking\b, which an agent's own
#     conversational text reaches trivially.
#   IDLE -- the turn-finished status line "<middle dot> done H:MM AM/PM",
#     and the "new task?" affordance on its own line. The U+00B7 separator
#     and the 12-hour clock are both part of the captures above; the
#     line-start anchor on "new task?" is what keeps it off a sentence that
#     merely contains those words.
#
# Anything else stays UNKNOWN. Codex has no verified finished-turn marker on
# record here, so none is guessed at -- the same standing rule the bare
# \bapprove\b/\bpermission\b patterns above were deleted under.
# ---------------------------------------------------------------------------
AGENT_RUNNING_PATTERNS = (
    re.compile(r"esc to interrupt", re.IGNORECASE),
    # Claude 2.1.278 auto mode omits the interrupt hint while still
    # rendering its empty composer. Match the live spinner's timed chrome,
    # not conversational mentions of "thinking" or the finished "for ...".
    re.compile(r"^\s*[✢✳✶✻✽·*]\s+[^\n…]+…\s+\(\d+(?:h|m|s)\b[^\n]*\)\s*$"),
)
AGENT_IDLE_PATTERNS = (
    re.compile(r"·\s*done\s+\d{1,2}:\d{2}\s*[ap]\.?m\.?\b", re.IGNORECASE),
    re.compile(r"^\s*new task\?", re.IGNORECASE),
)
# The footer/status-line region. In every capture above the finished-turn
# status line sits 6 non-empty lines from the bottom (status line, an
# update/affordance line, a composer-box border, the composer, the other
# border, the mode footer), so 8 covers it with margin without reaching back
# into the conversation transcript above.
AGENT_UI_MARKER_LINES = 8


def detect_agent_ui_state(output: str) -> tuple[str | None, str]:
    """RUNNING/IDLE read from an agent CLI's own footer, or (None, why-not).

    Two separate passes, RUNNING first: a busy footer is the LIVE state and
    must win outright over a "done 10:05 AM" line still visible from the
    PREVIOUS turn. One pass over both pattern sets would instead let
    whichever marker happened to sit lower in the pane decide, which is
    exactly backwards.
    """
    recent = [line for line in output.splitlines() if line.strip()][-AGENT_UI_MARKER_LINES:]
    for offset, line in enumerate(reversed(recent)):
        for pattern in AGENT_RUNNING_PATTERNS:
            if pattern.search(line):
                return "RUNNING", (f"agent CLI busy footer matched {pattern.pattern!r} "
                                   f"at bottom offset {offset}")
    for offset, line in enumerate(reversed(recent)):
        for pattern in AGENT_IDLE_PATTERNS:
            if pattern.search(line):
                return "IDLE", (f"agent CLI finished-turn marker matched {pattern.pattern!r} "
                                f"at bottom offset {offset}")
    return None, "no high-confidence agent CLI state marker near the pane bottom"


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
    command = normalize_command(session.pane_current_command)
    # Checked BEFORE the activity-age rules below, not after: the pane's own
    # footer is direct evidence of what the target is doing right now, while
    # activity_epoch is an unreliable proxy that is wrong in both directions
    # for an Ink-rendered agent CLI (see AGENT_RUNNING_PATTERNS above for the
    # live captures). Ordering it after would leave an idle Claude session
    # that merely redrew within 60s reporting RUNNING on the strength of that
    # redraw, and a working one whose spinner has not moved reporting UNKNOWN.
    # The command is restated in the reason string because that is where
    # terminal_wall.command_from() reads it from.
    ui_state, ui_reason = detect_agent_ui_state(output)
    if ui_state is not None:
        return ui_state, False, f"{ui_reason}; current command is {command or 'unknown'!r}"
    # Shell equivalent of the agent-UI check above, and placed AFTER it so an
    # agent pane is still classified by its own footer first.
    if command in _SHELLS and shell_prompt_is_back(output):
        return "IDLE", False, "shell prompt is back at the bottom of the pane; the command has finished"
    if command in _AGENT_COMMANDS:
        target = select_adapter(command).identify_target_state(output.splitlines())
        if target == adapters.TARGET_RUNNING:
            return "RUNNING", False, f"{command} adapter reports a turn in flight"
        if target == adapters.TARGET_COMPOSER:
            return "IDLE", False, f"{command} is back at its composer; the turn has finished"
    # An agent CLI is a persistent interactive process: its pane command and
    # a recent redraw do not prove that a task is executing. Agent commands
    # reach RUNNING only through their own UI/footer or adapter evidence above.
    if command in ACTIVE_COMMANDS and (command != "codex") and age <= 60:
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
    "RUNNING", "IDLE", "WAITING_INPUT", "BLOCKED", "FAILED",
    "COMPLETION_CANDIDATE", "VERIFYING", "VERIFIED_DONE",
    "ERROR", "UNKNOWN",
)
# P0 Part C: three states added on top of the original seven, none of them
# ever produced by classify_supervisor_state itself (which only knows
# about pane-output patterns) -- they exist purely as targets of
# supervisor.py's own promotion state machine (_handle_completion_candidate)
# once independent verification is involved, for a watch under autonomous
# policy (see supervisor.py's SupervisorService.autonomous_check):
#   VERIFYING -- a COMPLETION_CANDIDATE that has cleared the existing
#     nonce/quiet-window+self-reported-evidence gate for an *autonomous*
#     watch is not promoted straight to VERIFIED_DONE the way a non-
#     autonomous watch's would be; it moves here while an independent
#     verifier (verifier.py -- real git/test-command execution outside the
#     target pane, never prose-derived) actually runs. Durable: written to
#     the watches table before the verifier runs, so a crash mid-
#     verification leaves an observable VERIFYING row a later poll safely
#     re-verifies, rather than a silent gap.
#   FAILED -- an autonomous watch's independent verifier ran and reported
#     a definitive fail (non-zero test exit, dirty worktree where clean
#     was required, commit/worktree mismatch). Terminal: never auto-
#     promotes to VERIFIED_DONE from here; an operator must intervene
#     (fix and re-watch) and the v2 policy is blocked so no further
#     autonomous action is taken against a claim independent verification
#     just rejected.
#   BLOCKED -- an autonomous watch reached the completion gate with no
#     independent verifier configured at all (or one that could not run --
#     e.g. a misconfigured/unreachable worktree). Prohibits exactly the
#     thing P0 Part C exists to prohibit: quiet-window/prose-only
#     promotion to VERIFIED_DONE for a watch that can actually act
#     autonomously. Also terminal until an operator configures a verifier
#     policy (or downgrades the watch out of autonomous policy) and
#     re-watches.
# "DONE" is no longer a value classify_supervisor_state (or anything built
# on it) ever produces -- it is legacy-only, available strictly through
# to_legacy_state()/to_legacy_event_type() below, never as the primary
# state model. A raw consumer (a watch row, an event, supervisor_status's
# counts) must handle COMPLETION_CANDIDATE (unverified: prose/marker
# evidence seen, not yet corroborated) and VERIFIED_DONE (corroborated:
# quiet window held, no regression, any configured nonce/verifier passed)
# as genuinely different things -- prose alone was never proof, and
# treating it as interchangeable with "DONE" is exactly the false-positive
# risk this two-state split exists to eliminate. VERIFYING/BLOCKED/FAILED
# (P0 Part C) are new, distinct states with no legacy equivalent at all --
# to_legacy_state deliberately leaves them exactly as themselves rather
# than folding them into "DONE" (VERIFYING/BLOCKED are explicitly NOT
# done) or inventing a legacy meaning that never existed.
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


# A line that IS a shell prompt plus the command typed at it -- an echo of
# input, never program output. Audited live 2026-09-21 on hp-linux: session
# `urbanflow-hp-relay2` classified ERROR purely because the command the
# operator had typed contained the word EXCEPTION, matching ERROR_PATTERNS'
# r"\bexception\b.*:". Scanning what the user typed for evidence of a
# failure is a category error, so those lines are skipped.
_PROMPT_LINE = re.compile(r"^\S+@\S+:.*[$#]\s|^\s*[$#>]\s+\S|^PS [A-Za-z]:\\.*>\s")

# ERROR looks only at the very bottom of the pane, the same posture
# detect_waiting_input already takes (its window is 4). The shared 20-line
# default let a failure that had already scrolled most of the way out of
# view still mark a session ERROR -- measured on hp 2026-09-21,
# `repoport-public-smoke` matched at offset 15 of 20 on an hours-old error.
ERROR_WINDOW = 5


def _match_recent(patterns: tuple[re.Pattern[str], ...], output: str, window: int = 20,
                  skip_prompt_lines: bool = False) -> tuple[bool, str]:
    lines = [line for line in output.splitlines() if line.strip()][-window:]
    for offset, line in enumerate(reversed(lines)):
        if skip_prompt_lines and _PROMPT_LINE.search(line):
            continue
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

# Real, live-discovered bug (P0 QUEUE + SUPERVISOR LIVE TEST checkpoint,
# 2026-09-07): the marker's own printed form (~150-160 chars on one
# LOGICAL line) routinely exceeds a normal terminal's column width (e.g.
# 80), so a captured pane genuinely WRAPS it across multiple physical
# rows -- each padded with trailing spaces before its own real '\n' (real
# tmux/pyte row-rendering behavior, not a hypothetical). The original
# `[^#\n]*?` middle group excluded '\n', so a wrapped marker was NEVER
# matched at all -- confirmed live: a disposable Claude session
# correctly replied "alpha" and printed the exact expected marker
# (visually confirmed in the pane), yet `parse_completion_marker`
# returned None and the task sat in VERIFYING forever. Fixed by
# excluding only '#' (never '\n') -- the marker's own fields (hex ids,
# integers, a fixed enum value) never contain '#', so this still
# terminates correctly at the real closing '###' and cannot run away
# past it; `_MARKER_FIELD_RE`'s own `\w+=\S+` extraction is already
# whitespace/newline-agnostic. `verify_completion_marker`'s exact task_
# id/attempt/nonce match (not just "some fields were found") remains the
# real defense against a false positive from incidental text landing
# inside a match span.
COMPLETION_MARKER_RE = re.compile(
    r"###TERMINAL_MCP_COMPLETION\s+protocol=terminal-mcp-completion/v1\s+([^#]*?)###"
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
    # ERROR needs the failure to still be LIVE. This function has no clock,
    # but `state` already carries one: classify_status only returns IDLE for
    # a pane sitting at a plain shell with no tmux activity for over a
    # minute -- whatever failed has finished and handed the prompt back, so
    # text still on screen is history, not a running failure. Audited on hp
    # 2026-09-21: ALL FIVE sessions reported ERROR were idle shells, the
    # freshest matching a psql error 44 hours old.
    if state != "IDLE":
        matched, why = _match_recent(ERROR_PATTERNS, output, window=ERROR_WINDOW,
                                     skip_prompt_lines=True)
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
