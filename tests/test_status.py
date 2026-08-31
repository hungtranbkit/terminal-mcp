from terminal_mcp.models import SessionInfo
from terminal_mcp.status import classify_status, classify_supervisor_state, detect_waiting_input, parse_completion_marker


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


def test_quoted_or_pasted_marker_text_is_still_just_a_marker_match():
    # This module has no concept of "quoting" -- a marker match is a
    # marker match regardless of surrounding context. That is fine: the
    # marker (like DONE_PATTERNS) only ever produces a v1 "DONE"
    # classification, itself only ever a COMPLETION_CANDIDATE at the v2
    # layer (see supervisor2.py _reconcile_observing_actions) -- never
    # trusted as verified from this classification alone.
    quoted = f'the user pasted this earlier: "{MARKER_OK}" -- not a real completion'
    assert parse_completion_marker(quoted) is not None
    state, reason = classify_supervisor_state("RUNNING", "r", quoted)
    assert state == "DONE"
    assert "structured completion marker" in reason


def test_adversarial_marker_cannot_forge_a_different_status_value():
    forged = MARKER_OK.replace("status=completion_candidate", "status=verified_done")
    assert parse_completion_marker(forged) is None  # only completion_candidate is a recognized status


def test_classify_supervisor_state_marker_present_yields_done_not_verified():
    state, reason = classify_supervisor_state("RUNNING", "r", MARKER_OK)
    assert state == "DONE"  # backward-compatible v1 label; NOT proof of verification
    assert "structured completion marker" in reason

