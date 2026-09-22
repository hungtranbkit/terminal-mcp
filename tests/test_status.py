from terminal_mcp.models import SessionInfo
from terminal_mcp.status import (
    classify_status,
    classify_supervisor_state,
    detect_agent_ui_state,

    shell_prompt_is_back,
    detect_waiting_input,
    parse_completion_marker,
    verify_completion_marker,
)


def info(command="bash", activity=100, dead=False):
    return SessionInfo("test-x", False, 1, 1, activity, 123, command, dead)


def test_waiting_prompt_near_bottom():
    waiting, reason = detect_waiting_input("work\nDo you want to continue? [y/N]")
    assert waiting
    assert "matched" in reason


def test_old_prompt_not_false_positive():
    output = "Do you want to continue? [y/N]\n" + "\n".join(f"line {i}" for i in range(20))
    assert not detect_waiting_input(output)[0]


def test_running_and_idle_classification():
    assert classify_status(info("python", 95), "working", now=100)[0] == "RUNNING"
    assert classify_status(info("bash", 1), "$", now=100)[0] == "IDLE"


def test_ordinary_composer_mentioning_permission_is_not_waiting_input():
    # Real false positive, found LIVE against a real attended session
    # (`window2`, 2026-09-07): status.py's own WAIT_PATTERNS used to
    # duplicate adapters.py's now-fixed bare r"\bpermission\b" bug --
    # this exact reported composer/status-line shape used to make
    # classify_status report WAITING_INPUT/input_required=True for a
    # completely ordinary, idle session.
    output = (
        "hết, JS chỉ render, mọi quyền/luật/audit/transaction nằm ở .NET bridge/service.\n"
        "✻ Worked for 14m 33s · done 2:31 AM\n"
        "                    new task? /clear to save 891k tokens\n"
        "> Làm Role/Permission step 2 custom role web đi\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · install gh for PR status · ← for agents"
    )
    waiting, reason = detect_waiting_input(output)
    assert waiting is False
    state, input_required, _reason = classify_status(info("claude", 5), output, now=100)
    assert state != "WAITING_INPUT"
    assert input_required is False


# ---------------------------------------------------------------------------
# Claude Code 2.1.277 pane captures, taken LIVE from this host on 2026-09-19
# (tmux capture-pane against the real sessions named in each constant). These
# are the exact outputs that used to classify as UNKNOWN. Reproduced verbatim,
# including the middle dots, box-drawing borders and trailing padding tmux
# renders, because the padding and the line positions are part of what the
# classifier reads.
# ---------------------------------------------------------------------------
RULE = "─" * 80

# terminal-mcp-claude-tests: genuinely WORKING, but tmux reported an activity
# age of 663s -- an Ink UI whose spinner had not redrawn while it waited on a
# long shell command. This is the capture that made "agent + stale activity"
# indistinguishable from a mystery.
CLAUDE_WORKING = "\n".join((
    "● Baseline at ~48%. Waiting for completion.",
    "",
    "● Waiting for baseline suite completion · 2m 28s",
    "  ⏎  $ until ! pgrep -f 'pytest -q -p no:cacheprovider --maxfail=40' >/dev/null;",
    "     do sleep 20; done; echo \"BASELINE DONE\"; tail -40",
    "     /tmp/terminal-mcp-main-baseline.log (2m 26s)",
    "     (ctrl+b ctrl+b (twice) to run in background)",
    "",
    "✻ Billowing… (10m 25s · ↓ 17.9k tokens)",
    RULE,
    "❯",
    RULE,
    "  ⏵⏵ auto mode on · 1 shell · esc to interrupt · ⇤ for agents · ⇥ to manage",
))

# hp-work: finished turn, idle for days. Carries BOTH idle markers -- the
# "done 7:25 AM" status line and the "new task?" affordance.
CLAUDE_IDLE_NEW_TASK = "\n".join((
    "  my last message is still the fix if you want a retry to be readable.",
    "",
    "✻ Sautéed for 20s · done 7:25 AM",
    "                                        new task? /clear to save 167.7k tokens",
    RULE,
    "❯ reset coordinator_attempts to 0",
    RULE,
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
))

# terminal-mcp-claude-audit: finished turn with a background shell still
# alive, and NO "new task?" line (an update notice occupies that row instead)
# -- so the "done 10:05 AM" status line alone has to carry the verdict, from
# six non-empty lines above the bottom. "1 shell still running" is on that
# very line and must NOT be read as the agent running.
CLAUDE_IDLE_DONE_ONLY = "\n".join((
    "  Standing by — no further edits until you've merged and smoked efc9bb9.",
    "",
    "✻ Cogitated for 39m 35s · done 10:05 AM · 1 shell still running",
    "                                        ✔ Update installed · Restart to update",
    RULE,
    "❯ viết report AUDIT-TERMINAL-MCP-2026-09-19.md ngay",
    RULE,
    "  ⏵⏵ auto mode on · 1 shell · ⇤ for agents · ⇥ to manage",
))

# Claude Code's own numbered permission widget. Its chrome line was carried by
# adapters._WAITING_PATTERNS but not by this module's WAIT_PATTERNS, so a
# session sitting on a real approval prompt classified as UNKNOWN here while
# adapters refused the send as TARGET_AWAITING_APPROVAL.
CLAUDE_PERMISSION_MENU = "\n".join((
    "  Do you want to make this edit to core.py?",
    "  ❯ 1. Yes",
    "    2. Yes, and don't ask again this session",
    "    3. No, tell Claude what to do differently",
    "  Enter to select · Tab/arrow keys to navigate · Esc to cancel",
))


def test_working_claude_pane_is_running_despite_stale_tmux_activity():
    # The exact regression: pane_current_command="claude" with an activity age
    # far past the 60s ACTIVE_COMMANDS window used to fall through to UNKNOWN.
    assert detect_agent_ui_state(CLAUDE_WORKING)[0] == "RUNNING"
    state, input_required, reason = classify_status(info("claude", activity=0), CLAUDE_WORKING, now=663)
    assert state == "RUNNING"
    assert input_required is False
    assert "esc to interrupt" in reason
    # terminal_wall.command_from() parses the command back out of this string.
    assert "current command is 'claude'" in reason


def test_finished_claude_turn_with_new_task_affordance_is_idle():
    assert detect_agent_ui_state(CLAUDE_IDLE_NEW_TASK)[0] == "IDLE"
    state, input_required, _reason = classify_status(
        info("claude", activity=0), CLAUDE_IDLE_NEW_TASK, now=438_554)
    assert state == "IDLE"
    assert input_required is False


def test_finished_claude_turn_is_idle_from_the_done_status_line_alone():
    assert detect_agent_ui_state(CLAUDE_IDLE_DONE_ONLY)[0] == "IDLE"
    state, _input_required, _reason = classify_status(
        info("claude", activity=0), CLAUDE_IDLE_DONE_ONLY, now=2497)
    assert state == "IDLE"


def test_idle_claude_that_redrew_recently_is_idle_not_running():
    # The activity-age rule alone would say RUNNING here purely because the
    # pane redrew within 60s -- the pane's own footer says the turn is done,
    # and pane evidence must win over the proxy.
    state, _input_required, _reason = classify_status(
        info("claude", activity=95), CLAUDE_IDLE_DONE_ONLY, now=100)
    assert state == "IDLE"


def test_busy_footer_beats_a_previous_turns_done_marker():
    # A working pane can still show the PREVIOUS turn's "done" line inside the
    # marker window. The live busy footer is the current state.
    mixed = CLAUDE_IDLE_DONE_ONLY + "\n  ⏵⏵ auto mode on · esc to interrupt · ⇤ for agents"
    assert detect_agent_ui_state(mixed)[0] == "RUNNING"


def test_claude_permission_menu_is_waiting_input():
    waiting, _reason = detect_waiting_input(CLAUDE_PERMISSION_MENU)
    assert waiting is True
    state, input_required, _reason = classify_status(
        info("claude", activity=0), CLAUDE_PERMISSION_MENU, now=5)
    assert state == "WAITING_INPUT"
    assert input_required is True


# Two more LIVE captures, 2026-09-19, both sessions genuinely stuck on a real
# approval dialog that classify_status reported as UNKNOWN before this change
# -- the worst possible answer for this state, since nothing tells an operator
# or a supervisor that the session is blocked on them.
CLAUDE_READ_PERMISSION_DIALOG = "\n".join((
    " tools follow next session, sandboxed commands at once.",
    "",
    " Allow reads outside the working directories?",
    " ❯ 1. Yes, keep allowing reads outside the working directories",
    "   2. No, block reads outside the working directories from now on",
    "   3. No, ask again next time",
    "",
    " Esc to cancel · Tab to amend",
))

# Codex CLI's own tool-approval menu -- a different CLI, a different footer
# ("enter to submit | esc to cancel"), the same shared chrome string.
CODEX_TOOL_APPROVAL_MENU = "\n".join((
    "  Allow Terminal MCP to run tool \"terminal_mcp.terminal_status\"?",
    " ",
    "  session: nova-claude-long",
    " ",
    "  › 1. Allow                   Run the tool and continue.",
    "    2. Allow for this session  Run the tool and remember this choice for this",
    "                               session.",
    "    3. Always allow            Run the tool and remember this choice for",
    "                               future tool calls.",
    "    4. Cancel                  Cancel this tool call",
    "  enter to submit | esc to cancel",
))


def test_live_claude_read_permission_dialog_is_waiting_input():
    state, input_required, _reason = classify_status(
        info("claude", activity=0), CLAUDE_READ_PERMISSION_DIALOG, now=900)
    assert state == "WAITING_INPUT"
    assert input_required is True


def test_live_codex_tool_approval_menu_is_waiting_input():
    state, input_required, _reason = classify_status(
        info("codex", activity=0), CODEX_TOOL_APPROVAL_MENU, now=30)
    assert state == "WAITING_INPUT"
    assert input_required is True


def test_ambiguous_agent_pane_stays_unknown():
    # No busy footer, no finished-turn marker, no prompt: the honest answer is
    # still UNKNOWN. This is what keeps the two markers above from becoming a
    # general-purpose guess.
    ambiguous = "\n".join(("  reading files...", "", "❯", RULE))
    assert detect_agent_ui_state(ambiguous)[0] is None
    state, _input_required, _reason = classify_status(info("claude", activity=0), ambiguous, now=999)
    assert state == "UNKNOWN"


def test_done_marker_far_above_the_footer_region_is_not_idle():
    # Bounded window: a "done" line scrolled well up into the transcript is
    # history, not the current state.
    buried = ("✻ Worked for 14m 33s · done 2:31 AM\n"
              + "\n".join(f"  transcript line {i}" for i in range(12)))
    assert detect_agent_ui_state(buried)[0] is None


def test_prose_mentioning_a_new_task_mid_sentence_is_not_idle():
    # "new task?" only counts as the CLI's own affordance line, never as words
    # inside a sentence -- the line-start anchor is what makes that true.
    prose = "\n".join((
        "  So should I pick up the new task? I can start on it now.",
        "❯",
    ))
    assert detect_agent_ui_state(prose)[0] is None


def test_real_yn_dialog_still_detected_as_waiting_input():
    # Removing the bare "approve"/"permission" words must not weaken
    # real detection -- the actual y/n dialog shape is unaffected.
    waiting, _reason = detect_waiting_input("Allow this command to run?\napprove or deny? [y/n]")
    assert waiting is True


# ---------------------------------------------------------------------------
# P0-7: structured completion marker
# ---------------------------------------------------------------------------

MARKER_OK = (
    "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
    "task_id=abc123 attempt=1 status=completion_candidate "
    "summary_sha256=deadbeef1234 checks=tests,lint nonce=n0nce###"
)


def test_parse_completion_marker_well_formed():
    fields = parse_completion_marker(f"some output\n{MARKER_OK}\nmore output")
    assert fields is not None
    assert fields["task_id"] == "abc123"
    assert fields["status"] == "completion_candidate"
    assert fields["summary_sha256"] == "deadbeef1234"
    assert fields["nonce"] == "n0nce"


def test_parse_completion_marker_returns_none_when_absent():
    assert parse_completion_marker("just plain output, task complete") is None


def test_parse_completion_marker_missing_required_field_is_absent():
    incomplete = "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id=abc status=completion_candidate###"
    assert parse_completion_marker(incomplete) is None  # missing summary_sha256


def test_parse_completion_marker_wrong_status_value_is_absent():
    wrong_status = MARKER_OK.replace("status=completion_candidate", "status=running")
    assert parse_completion_marker(wrong_status) is None


def test_parse_completion_marker_picks_the_last_of_several():
    two = f"{MARKER_OK}\nmore work happened\n" + MARKER_OK.replace("task_id=abc123", "task_id=xyz789")
    fields = parse_completion_marker(two)
    assert fields["task_id"] == "xyz789"


def test_parse_completion_marker_survives_real_terminal_line_wrapping():
    # Real, live-discovered bug (P0 QUEUE + SUPERVISOR LIVE TEST
    # checkpoint, 2026-09-07): a real disposable Claude session, in a
    # real ~80-column tmux pane, printed the marker exactly as
    # instructed -- but the pane's own row-wrapping (padding each
    # visual row to full width before a real '\n') split it across
    # several physical lines, and the OLD regex (`[^#\n]*?`, excluding
    # '\n') never matched at all -- the task sat in VERIFYING forever
    # even though the agent had genuinely completed it. This is a
    # faithful reproduction of the actual captured pane text from that
    # live session (trailing space padding before each wrap included).
    wrapped = (
        "  ###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1                \n"
        "  task_id=5455ccf75f314318886140bc48689d5d attempt=1                            \n"
        "  nonce=2dd1b21656084b6ca675cbc711c3f295 status=completion_candidate            \n"
        "  summary_sha256=e8899e7889d721c3###                                            \n"
    )
    fields = parse_completion_marker(f"some output\n{wrapped}\nmore output")
    assert fields is not None
    assert fields["task_id"] == "5455ccf75f314318886140bc48689d5d"
    assert fields["nonce"] == "2dd1b21656084b6ca675cbc711c3f295"
    assert fields["status"] == "completion_candidate"
    assert fields["summary_sha256"] == "e8899e7889d721c3"


def test_parse_completion_marker_wrapped_still_never_bridges_past_a_hash():
    # The relaxed `[^#]*?` (was `[^#\n]*?`) must still never match PAST
    # a real '#' -- confirms this isn't an unbounded/greedy regression:
    # two wrapped markers stay correctly separate, and a stray '#'
    # inside unrelated prose between them still terminates the match at
    # the FIRST closing ### it finds, never swallowing everything up to
    # a later one.
    stray_hash = "note: see issue #42 for context\n"
    two_wrapped = (
        "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1\n"
        "task_id=first attempt=1 status=completion_candidate\n"
        "summary_sha256=aaaa###\n"
        f"{stray_hash}"
        "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1\n"
        "task_id=second attempt=1 status=completion_candidate\n"
        "summary_sha256=bbbb###\n"
    )
    fields = parse_completion_marker(two_wrapped)
    assert fields["task_id"] == "second"  # last well-formed match, same as before this fix


def test_quoted_or_pasted_marker_text_is_still_just_a_marker_match():
    # This module has no concept of "quoting" -- a marker match is a
    # marker match regardless of surrounding context. That is fine: the
    # marker (like DONE_PATTERNS) only ever produces a COMPLETION_CANDIDATE
    # classification here -- never trusted as verified from this
    # classification alone (promotion to VERIFIED_DONE requires a later
    # poll to corroborate a quiet window; see supervisor.py's
    # _handle_completion_candidate).
    quoted = f'the user pasted this earlier: "{MARKER_OK}" -- not a real completion'
    assert parse_completion_marker(quoted) is not None
    state, reason = classify_supervisor_state("RUNNING", "r", quoted)
    assert state == "COMPLETION_CANDIDATE"
    assert "structured completion marker" in reason


def test_adversarial_marker_cannot_forge_a_different_status_value():
    forged = MARKER_OK.replace("status=completion_candidate", "status=verified_done")
    assert parse_completion_marker(forged) is None  # only completion_candidate is a recognized status


def test_classify_supervisor_state_marker_present_yields_candidate_not_verified():
    state, reason = classify_supervisor_state("RUNNING", "r", MARKER_OK)
    assert state == "COMPLETION_CANDIDATE"  # never direct proof of verification
    assert "structured completion marker" in reason


# ---------------------------------------------------------------------------
# P0-7 phase 2: nonce verification. MARKER_OK's fields: task_id=abc123,
# attempt=1, nonce=n0nce.
# ---------------------------------------------------------------------------

MARKER_OK_FIELDS = parse_completion_marker(MARKER_OK)


def test_verify_completion_marker_matches_current_unconsumed_attempt():
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="abc123", attempt=1, nonce="n0nce", nonce_consumed=False
    ) is True


def test_verify_completion_marker_none_marker_is_never_verified():
    assert verify_completion_marker(None, task_id="abc123", attempt=1, nonce="n0nce", nonce_consumed=False) is False


def test_verify_completion_marker_wrong_task_id_fails():
    # A marker echoing a different (or stale) watch's task_id must never
    # verify against this watch's token.
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="some-other-watch", attempt=1, nonce="n0nce", nonce_consumed=False
    ) is False


def test_verify_completion_marker_wrong_attempt_fails():
    # Same task_id/nonce but a stale attempt number (e.g. a marker copied
    # forward from a previous watch/rewatch cycle) must not verify.
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="abc123", attempt=2, nonce="n0nce", nonce_consumed=False
    ) is False


def test_verify_completion_marker_wrong_nonce_fails():
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="abc123", attempt=1, nonce="different-nonce", nonce_consumed=False
    ) is False


def test_verify_completion_marker_already_consumed_nonce_is_a_replay_and_fails():
    # Correct task_id/attempt/nonce, but the token was already spent --
    # this is exactly the replay case: an old, already-verified marker
    # (pasted back in, scrolled into view again, or reused deliberately)
    # must never verify a second time.
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="abc123", attempt=1, nonce="n0nce", nonce_consumed=True
    ) is False


def test_verify_completion_marker_none_nonce_on_watch_side_fails():
    # A watch with no nonce minted yet (shouldn't happen post-upsert_watch,
    # but defense in depth) never verifies, regardless of marker content.
    assert verify_completion_marker(
        MARKER_OK_FIELDS, task_id="abc123", attempt=1, nonce=None, nonce_consumed=False
    ) is False



# ---------------------------------------------------------------------------
# ERROR freshness (audited live on hp-linux, 2026-09-21). All five sessions
# the dashboard reported as ERROR were idle shells showing old text; one was
# matching against the operator's own typed command. Each case below is a
# verbatim line from that audit.
# ---------------------------------------------------------------------------

def test_stale_error_on_an_idle_shell_is_not_reported_as_error():
    # mesflow-ha-g3-hp: psql error still on screen 44 hours later, shell idle.
    pane = 'ERROR:  column "received_lsn" does not exist\nkimex@hp:~$'
    state, _ = classify_supervisor_state("IDLE", "shell pane has no tmux activity for 159483s", pane)
    assert state == "IDLE"


def test_fresh_error_on_an_active_pane_is_still_reported():
    # The freshness rule must not silence a real, current failure: a shell
    # with activity inside the last minute classifies RUNNING, not IDLE.
    pane = "Traceback (most recent call last):\n  File x\nValueError: boom"
    state, why = classify_supervisor_state("RUNNING", "r", pane)
    assert state == "ERROR" and "traceback" in why.lower()


def test_error_word_inside_the_typed_command_is_not_evidence_of_failure():
    # urbanflow-hp-relay2: the word EXCEPTION was in the command, not output.
    pane = 'kimex@hp:~$ adb logcat -t 600 2>/dev/null | grep -Ec "FATAL EXCEPTION|AndroidRuntime:"'
    state, _ = classify_supervisor_state("RUNNING", "r", pane)
    assert state != "ERROR"


def test_error_scrolled_out_of_the_bottom_window_is_not_reported():
    # repoport-public-smoke matched at offset 15 of the old 20-line window.
    pane = "Error locating origin cert: client didn't specify origincert path\n" + \
           "\n".join(f"line {i}" for i in range(12))
    state, _ = classify_supervisor_state("RUNNING", "r", pane)
    assert state != "ERROR"


# ---------------------------------------------------------------------------
# Shell readiness from the prompt instead of a timer. Measured 2026-09-21: a
# bash pane that had already finished its command reported RUNNING at every age
# from 0s to 60s and only flipped at 61s, so terminal_turn(send_wait) on a shell
# burned up to a minute after the work was done.
# ---------------------------------------------------------------------------

def _shell(age, cmd="bash"):
    return SessionInfo(name="s", activity_epoch=1_000_000 - age, pane_current_command=cmd,
                       pane_dead=False, created_epoch=0, windows=1, attached=False,
                       pane_pid=0, session_id="", pane_id="", pane_in_mode=False,
                       pane_current_path="")


def test_finished_shell_is_idle_immediately_not_after_sixty_seconds():
    pane = "$ echo hi\nhi\ndell@dell-Latitude-5511:~/workspace$ "
    for age in (0, 5, 30, 59):
        state, _, _ = classify_status(_shell(age), pane, now=1_000_000)
        assert state == "IDLE", f"age {age}s should already be IDLE"


def test_a_command_still_on_the_prompt_line_is_not_treated_as_finished():
    # The prompt is there, but something is typed after it -- the trailing
    # `\\s*$` must refuse this, or send_wait would return before the work ran.
    pane = "dell@dell-Latitude-5511:~/workspace$ sleep 30"
    state, _, _ = classify_status(_shell(0), pane, now=1_000_000)
    assert state == "RUNNING"


def test_bash_continuation_prompt_is_not_idle():
    # A bare ">" in bash means an unterminated quote: the shell is WAITING for
    # input, so calling it idle would be wrong in the dangerous direction.
    pane = "dell@host:~$ echo 'oops\n>"
    state, _, _ = classify_status(_shell(0), pane, now=1_000_000)
    assert state != "IDLE"


def test_powershell_prompt_counts_as_ready():
    pane = "PS C:\\Users\\tranv> dir\nDirectory listing\nPS C:\\Users\\tranv>"
    assert shell_prompt_is_back(pane) is True


def test_an_agent_pane_is_never_short_circuited_by_a_prompt_line():
    # claude/codex are not shells: a prompt-looking line in their output must
    # not make a thinking agent look finished.
    pane = "some output\ndell@host:~/ws$ "
    state, _, _ = classify_status(_shell(0, cmd="claude"), pane, now=1_000_000)
    assert state == "RUNNING"


def test_unrecognised_prompt_falls_back_to_the_timer():
    pane = "output\n\u276f "          # a customised prompt this regex does not know
    assert shell_prompt_is_back(pane) is False
    state, _, _ = classify_status(_shell(0), pane, now=1_000_000)
    assert state == "RUNNING"


# ---------------------------------------------------------------------------
# Agent panes: evidence, not a timer (audited live on this host, 2026-09-21).
# All ten nova-claude-* sessions sat idle at their composer; classify_status
# reported RUNNING for every one whose tmux activity age was under 60s, purely
# because "claude" is in ACTIVE_COMMANDS. Panes below are verbatim tails.
# ---------------------------------------------------------------------------

IDLE_CLAUDE_PANE = (
    "  Both peers have frozen and nothing is outstanding between the lanes.\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "\u276f \n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "  [Opus 5 (1M context)] \u2502 novaretail-chatgpt-prod git:(feature/x)\n"
    "  Context \u2588\u2591 74% \u2502 Usage \u2591 2% (resets in 4h 50m)\n"
    "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents"
)

BUSY_CLAUDE_PANE = (
    "  Reading tests/test_status.py\n"
    "\u273b Brewing\u2026 (12s \u00b7 esc to interrupt)\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents"
)


def test_idle_claude_composer_is_idle_immediately_not_after_sixty_seconds():
    # nova-claude-single-account: turn finished, composer back, activity 0s old.
    for age in (0, 5, 30, 59):
        state, waiting, why = classify_status(
            info("claude", 100 - age), IDLE_CLAUDE_PANE, now=100)
        assert state == "IDLE", f"age {age}s classified {state}"
        assert waiting is False
        assert "composer" in why


def test_claude_mid_turn_is_running_even_when_tmux_activity_is_stale():
    # The interrupt hint is live evidence; a quiet pane redraw must not make a
    # turn in flight look finished.
    state, _waiting, why = classify_status(info("claude", 1), BUSY_CLAUDE_PANE, now=10_000)
    assert state == "RUNNING"
    assert "esc to interrupt" in why


def test_claude_pane_without_composer_chrome_falls_back_to_the_old_rules():
    # No footer, no interrupt hint: the adapter has no opinion, so the existing
    # ACTIVE_COMMANDS age gate must still decide exactly as it did before.
    pane = "some half-drawn output with no composer at all"
    assert classify_status(info("claude", 95), pane, now=100)[0] == "RUNNING"
    # Unchanged from before this check existed: "claude" is not one of the
    # shells the age>60 rule turns IDLE, so an unreadable pane stays UNKNOWN.
    assert classify_status(info("claude", 1), pane, now=100)[0] == "UNKNOWN"


def test_a_permission_prompt_still_wins_over_the_composer_check():
    # detect_waiting_input runs first; an approval dialog must never read IDLE
    # just because the footer is on screen underneath it.
    pane = IDLE_CLAUDE_PANE + "\nDo you want to proceed? [y/N]"
    state, waiting, _why = classify_status(info("claude", 100), pane, now=100)
    assert state == "WAITING_INPUT"
    assert waiting is True
