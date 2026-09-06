from terminal_mcp.models import SessionInfo
from terminal_mcp.status import (
    classify_status,
    classify_supervisor_state,
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

