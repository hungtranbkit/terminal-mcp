from terminal_mcp.models import SessionInfo
from terminal_mcp.status import (
    classify_status,
    classify_supervisor_state,
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
    assert "in flight" in why


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
