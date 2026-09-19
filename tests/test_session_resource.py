"""TMCP-SESSION-HEALTH-001: session resource health.

The footer samples below are REAL observed output, not invented shapes. The
malformed cases are the ones that matter most: this parser feeds an
autonomous rollover decision, so "no number" must always beat "a plausible
number", and every one of these asserts a null rather than a value.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from terminal_mcp.bindings import BindingStore
from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import (AppConfig, PermissionsConfig, SessionHealthConfig,
                                 load_config)
from terminal_mcp.core import TerminalService
from terminal_mcp.models import SessionInfo
from terminal_mcp.session_resource import (
    ACTION_CHECKPOINT_BEFORE_ROLLOVER, ACTION_CHECKPOINT_ONLY, ACTION_CONTINUE,
    ACTION_CONTINUE_WATCH, ACTION_FINISH_AND_ROLLOVER, ACTION_PREPARE_ROLLOVER,
    QUOTA_CRITICAL, QUOTA_NORMAL, QUOTA_UNKNOWN, QUOTA_WARNING, TIER_CHECKPOINT_ONLY,
    TIER_FINISH_AND_ROLLOVER, TIER_NORMAL, TIER_PREPARE_ROLLOVER, TIER_UNKNOWN,
    TIER_WATCH, ContextPolicy, build_resource_block, classify_context, classify_quota,
    parse_session_resources, probe_git_state, reset_git_cache)


# The real footer this task was specified against (observed 2026-09-19).
REAL_FOOTER = (
    "[Opus 5 (1M context)] | Context ██████████ 96% (in: 2, cache: 955k) | "
    "Usage ███░░░░░░░ 30% (resets in 49m)"
)


def _pane(footer: str) -> str:
    """A footer where it really lives: at the bottom of ordinary pane text."""
    return "\n".join([
        "  Searched for 2 patterns, ran 12 shell commands",
        "",
        "✽ Brewed for 9m 49s · done 10:27 AM",
        "─" * 80,
        "❯ ",
        "─" * 80,
        footer,
    ])


# ---------------------------------------------------------------------------
# Parser: the normal case
# ---------------------------------------------------------------------------

def test_parses_real_claude_footer():
    parsed = parse_session_resources(_pane(REAL_FOOTER))
    assert parsed.context_percent == 96
    assert parsed.usage_percent == 30
    assert parsed.usage_reset_in_minutes == 49
    assert parsed.context_max_tokens == 1_000_000
    # in: 2 + cache: 955k -- the footer's own stated prompt tokens, summed,
    # never back-computed from 96%.
    assert parsed.context_used_tokens == 955_002
    assert parsed.model == "Opus 5"
    assert parsed.observed is True


def test_parses_footer_wrapped_across_pane_rows():
    """A ~110-char footer in an 80-column pane genuinely wraps; the same
    real tmux row-wrapping status.py's completion marker had to be fixed
    for. All three numbers must still be read."""
    wrapped = (
        "[Opus 5 (1M context)] | Context ██████████ 96% (in: 2, cache:\n"
        "955k) | Usage ███░░░░░░░ 30% (resets in\n49m)"
    )
    parsed = parse_session_resources(_pane(wrapped))
    assert (parsed.context_percent, parsed.usage_percent, parsed.usage_reset_in_minutes) == (96, 30, 49)


def test_parses_percent_remaining_form():
    parsed = parse_session_resources("Context low (5% remaining) · Run /compact to continue")
    assert parsed.context_percent == 95
    assert parsed.usage_percent is None


@pytest.mark.parametrize("footer,minutes", [
    ("Usage 30% (resets in 49m)", 49),
    ("Usage 30% (resets in 1h 5m)", 65),
    ("Usage 30% (resets in 2h)", 120),
    ("Usage 30% (reset in 90 min)", 90),
    # Observed live on a weekly quota window, 2026-09-19 -- a null here would
    # have lost a number the footer stated outright.
    ("Usage Weekly █░░░░░░░░░ 8% (resets in 6d 18h)", 6 * 24 * 60 + 18 * 60),
    ("Usage 30% (resets in 1d)", 24 * 60),
])
def test_parses_reset_window_forms(footer, minutes):
    assert parse_session_resources(footer).usage_reset_in_minutes == minutes


def test_latest_footer_wins_over_older_redraw():
    """A pane holds scrollback: an earlier footer is stale by definition."""
    output = "\n".join([
        "Context 40% | Usage 10% (resets in 5h)",
        "...work...",
        "Context 96% | Usage 30% (resets in 49m)",
    ])
    parsed = parse_session_resources(output)
    assert (parsed.context_percent, parsed.usage_percent) == (96, 30)


# ---------------------------------------------------------------------------
# Parser: missing and malformed -- every assertion here is a null
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("output", [
    "",
    "   \n\n  ",
    "$ ls -la\ntotal 4\ndrwxr-xr-x 2 kimex kimex 4096 Sep 19 10:48 .",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents",
    "✽ Pouncing… (15s · ↓ 590 tokens)",
])
def test_absent_footer_yields_nulls_not_errors(output):
    parsed = parse_session_resources(output)
    assert parsed.context_percent is None
    assert parsed.usage_percent is None
    assert parsed.usage_reset_in_minutes is None
    assert parsed.context_used_tokens is None
    assert parsed.model is None
    assert parsed.observed is False


@pytest.mark.parametrize("footer", [
    # Mid-redraw: label drawn, number not yet.
    "[Opus 5 (1M context)] | Context ██████████ % | Usage",
    # Out of range -- 150% means the pattern matched the wrong thing, not
    # that the context is full.
    "Context 150% | Usage 300%",
    # Negative reads as a bare number with no label attached to it.
    "Context -5%",
    # A percentage with no owning label must never be adopted.
    "96% of the test suite passes | 30% coverage",
    # 'context' in ordinary prose, with the percentage far past the bounded gap.
    "the context of this change is broad and touches many modules, roughly 96% of them",
])
def test_malformed_footer_never_invents_a_number(footer):
    parsed = parse_session_resources(_pane(footer))
    assert parsed.context_percent is None, footer
    assert parsed.usage_percent is None or footer.startswith("Context 150"), footer


def test_partial_footer_reports_only_what_it_stated():
    """Context present, quota absent. The missing half must be null, and
    must not borrow the half that is present."""
    parsed = parse_session_resources("[Sonnet 5] | Context ████ 42%")
    assert parsed.context_percent == 42
    assert parsed.usage_percent is None
    assert parsed.usage_reset_in_minutes is None
    assert parsed.context_max_tokens is None  # no window stated -> unknown
    assert parsed.model == "Sonnet 5"


def test_weekly_quota_footer_is_read_in_full():
    """The exact live remote footer (dell-linux/nova-claude-long): a weekly
    Usage window, and a context bar at 0%."""
    parsed = parse_session_resources(
        "  [Opus 5 (1M context)] │ novaretail-web git:(feature/nwr-biz-audit-001*)\n"
        "  Context ░░░░░░░░░░ 0% │ Usage Weekly █░░░░░░░░░ 8% (resets in 6d 18h)")
    assert parsed.context_percent == 0
    assert parsed.usage_percent == 8
    assert parsed.usage_reset_in_minutes == 6 * 24 * 60 + 18 * 60
    assert parsed.model == "Opus 5"
    assert parsed.context_max_tokens == 1_000_000
    # 0% is a real observation, not a missing one.
    assert parsed.observed is True
    block = build_resource_block(parsed=parsed)
    assert block["context"]["status"] == "NORMAL"
    assert block["usage"]["reset_at"] is not None


def test_zero_reset_window_is_not_reported():
    assert parse_session_resources("Usage 30% (resets in 0m)").usage_reset_in_minutes is None


def test_output_tokens_are_not_counted_as_context():
    parsed = parse_session_resources("Context 50% (in: 10k, out: 5k, cache: 90k)")
    assert parsed.context_used_tokens == 100_000


# ---------------------------------------------------------------------------
# Threshold boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("percent,tier", [
    (0, TIER_NORMAL), (69.9, TIER_NORMAL),
    (70, TIER_WATCH), (84.9, TIER_WATCH),
    (85, TIER_PREPARE_ROLLOVER), (92, TIER_PREPARE_ROLLOVER),
    (92.1, TIER_FINISH_AND_ROLLOVER), (96, TIER_FINISH_AND_ROLLOVER),
    (97, TIER_FINISH_AND_ROLLOVER),
    (97.1, TIER_CHECKPOINT_ONLY), (100, TIER_CHECKPOINT_ONLY),
])
def test_context_threshold_boundaries(percent, tier):
    assert classify_context(percent)[0] == tier


def test_unknown_context_is_unknown_not_normal():
    tier, status = classify_context(None)
    assert tier == TIER_UNKNOWN and status == "UNKNOWN"
    assert build_resource_block()["recommended_action"] == "UNKNOWN"


def test_thresholds_are_configurable():
    strict = ContextPolicy(watch_percent=50, prepare_rollover_percent=60,
                           finish_rollover_percent=70, checkpoint_only_percent=80)
    assert classify_context(55, strict)[0] == TIER_WATCH
    assert classify_context(75, strict)[0] == TIER_FINISH_AND_ROLLOVER
    assert classify_context(85, strict)[0] == TIER_CHECKPOINT_ONLY


@pytest.mark.parametrize("percent,expected", [
    (None, QUOTA_UNKNOWN), (30, QUOTA_NORMAL), (70, QUOTA_WARNING),
    (89.9, QUOTA_WARNING), (90, QUOTA_CRITICAL), (100, QUOTA_CRITICAL),
])
def test_quota_severity_has_its_own_scale(percent, expected):
    assert classify_quota(percent) == expected


def test_quota_never_influences_the_context_recommendation():
    """The whole point of the split: a spent quota window says nothing
    about whether this session can hold another task."""
    block = build_resource_block(
        parsed=parse_session_resources("Context 10% | Usage 99% (resets in 3h)"),
        git={"repo": "/r", "branch": "main", "dirty": False})
    assert block["recommended_action"] == ACTION_CONTINUE
    assert block["usage"]["status"] == QUOTA_CRITICAL
    assert block["context"]["status"] == "NORMAL"
    # And the reverse: a full context with no quota pressure still rolls over.
    block = build_resource_block(
        parsed=parse_session_resources("Context 96% | Usage 2% (resets in 3h)"),
        git={"repo": "/r", "branch": "main", "dirty": False})
    assert block["recommended_action"] == ACTION_FINISH_AND_ROLLOVER
    assert block["usage"]["status"] == QUOTA_NORMAL


@pytest.mark.parametrize("percent,action", [
    (10, ACTION_CONTINUE), (75, ACTION_CONTINUE_WATCH), (88, ACTION_PREPARE_ROLLOVER),
    (96, ACTION_FINISH_AND_ROLLOVER), (99, ACTION_CHECKPOINT_ONLY),
])
def test_recommended_action_follows_the_tier_on_a_clean_tree(percent, action):
    block = build_resource_block(
        parsed=parse_session_resources(f"Context {percent}%"),
        git={"repo": "/r", "branch": "main", "dirty": False})
    assert block["recommended_action"] == action
    assert block["rollover"]["blocked_reason"] is None


# ---------------------------------------------------------------------------
# Dirty-git safety
# ---------------------------------------------------------------------------

def test_dirty_tree_blocks_rollover_and_demands_a_checkpoint():
    block = build_resource_block(
        parsed=parse_session_resources(REAL_FOOTER),
        git={"repo": "/home/kimex/workspace/terminal-mcp", "branch": "main", "dirty": True},
        checkpoint={"branch": "main", "last_commit": "1e06752"})
    assert block["context"]["status"] == "CRITICAL"
    assert block["recommended_action"] == ACTION_CHECKPOINT_BEFORE_ROLLOVER
    assert block["rollover"]["recommended"] is True
    assert block["rollover"]["allowed"] is False
    assert block["rollover"]["requires_checkpoint"] is True
    assert block["rollover"]["blocked_reason"] == "GIT_DIRTY_CHECKPOINT_REQUIRED"
    # The metadata a rollover has to carry forward is preserved verbatim.
    assert block["rollover"]["checkpoint"]["last_commit"] == "1e06752"


def test_unknown_git_state_is_not_treated_as_clean():
    block = build_resource_block(parsed=parse_session_resources("Context 96%"),
                                 git={"repo": None, "branch": None, "dirty": None})
    assert block["rollover"]["allowed"] is False
    assert block["rollover"]["blocked_reason"] == "GIT_STATE_UNKNOWN"
    assert block["rollover"]["requires_checkpoint"] is True
    # Unknown git must not rewrite an honest context recommendation.
    assert block["recommended_action"] == ACTION_FINISH_AND_ROLLOVER


def test_checkpoint_only_stays_checkpoint_only_when_dirty():
    block = build_resource_block(parsed=parse_session_resources("Context 99%"),
                                 git={"repo": "/r", "branch": "main", "dirty": True})
    assert block["recommended_action"] == ACTION_CHECKPOINT_ONLY
    assert block["rollover"]["allowed"] is False


def test_clean_tree_below_threshold_has_no_rollover_verdict():
    block = build_resource_block(parsed=parse_session_resources("Context 10%"),
                                 git={"repo": "/r", "branch": "main", "dirty": False})
    assert block["rollover"]["recommended"] is False
    assert block["rollover"]["allowed"] is None
    assert block["rollover"]["requires_checkpoint"] is False


def test_probe_git_state_on_a_real_repo_and_a_non_repo(tmp_path):
    reset_git_cache()
    state = probe_git_state(str(tmp_path), cache_seconds=0)
    assert state == {"repo": None, "branch": None, "dirty": None}
    assert probe_git_state(None) == {"repo": None, "branch": None, "dirty": None}


# ---------------------------------------------------------------------------
# Acceptance example, end to end through the policy
# ---------------------------------------------------------------------------

def test_acceptance_example_from_the_task():
    block = build_resource_block(
        agent="claude", parsed=parse_session_resources(_pane(REAL_FOOTER)),
        git={"repo": "/home/kimex/workspace/terminal-mcp", "branch": "main", "dirty": False},
        now=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))
    assert block["agent"] == "claude"
    assert block["model"] == "Opus 5"
    assert block["context"]["percent"] == 96
    assert block["context"]["status"] == "CRITICAL"
    assert block["context"]["tier"] == TIER_FINISH_AND_ROLLOVER
    assert block["context"]["max_tokens"] == 1_000_000
    assert block["usage"]["percent"] == 30
    assert block["usage"]["reset_in_minutes"] == 49
    assert block["usage"]["reset_at"] == "2026-09-19T12:49:00+00:00"
    assert block["recommended_action"] == ACTION_FINISH_AND_ROLLOVER
    # Usage must never be read as context capacity.
    assert block["context"]["percent"] != block["usage"]["percent"]
    assert block["usage"]["percent"] != block["context"]["max_tokens"]


def test_block_shape_is_stable_when_nothing_is_observable():
    block = build_resource_block()
    assert set(block) == {"agent", "model", "observed", "context", "usage", "git",
                          "recommended_action", "rollover", "policy"}
    assert block["observed"] is False
    assert block["context"] == {"used_tokens": None, "max_tokens": None, "percent": None,
                                "status": "UNKNOWN", "tier": TIER_UNKNOWN}
    assert block["usage"] == {"percent": None, "reset_in_minutes": None,
                              "reset_at": None, "status": QUOTA_UNKNOWN}
    assert block["git"] == {"repo": None, "branch": None, "dirty": None}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_session_health_config_defaults_match_the_policy_table():
    defaults = SessionHealthConfig()
    assert (defaults.watch_percent, defaults.prepare_rollover_percent,
            defaults.finish_rollover_percent, defaults.checkpoint_only_percent) == (70.0, 85.0, 92.0, 97.0)
    assert defaults.enabled is True


def test_session_health_config_loads_and_validates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("session_health:\n  watch_percent: 60\n  git_probe_enabled: false\n")
    loaded = load_config(path)
    assert loaded.session_health.watch_percent == 60.0
    assert loaded.session_health.git_probe_enabled is False

    path.write_text("session_health:\n  watch_percent: 90\n")
    with pytest.raises(ValueError, match="strictly increasing"):
        load_config(path)

    path.write_text("session_health:\n  watch_percent: 140\n")
    with pytest.raises(ValueError, match="0-100"):
        load_config(path)


def test_repo_config_files_still_load():
    """The shipped configs must keep loading with the new section absent."""
    for name in ("config.yaml", "config.example.yaml"):
        assert load_config(name).session_health.enabled is True


# ---------------------------------------------------------------------------
# Integration: terminal_status and the compact/batch surface
# ---------------------------------------------------------------------------

class FakeTmux:
    def __init__(self, sessions: dict[str, str]) -> None:
        self.sessions = sessions

    def get_session(self, name: str):
        if name not in self.sessions:
            return None
        return SessionInfo(name, False, 1, 1, 1, 123, "claude", False)

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]:
        if session not in self.sessions:
            from terminal_mcp.tmux import TmuxError
            raise TmuxError("missing")
        return self.sessions[session].splitlines()[-lines:]


def _service(tmp_path, *, health=None):
    config = AppConfig(PermissionsConfig(True, False), ("test-*",), 200, 20,
                       session_health=health or SessionHealthConfig(git_probe_enabled=False))
    tmux = FakeTmux({"test-health": _pane(REAL_FOOTER), "test-plain": "$ ls\ntotal 0"})
    return TerminalService(config, tmux, BindingStore(tmp_path / "bindings.db"))


def test_terminal_status_exposes_the_resource_block(tmp_path):
    result = _service(tmp_path).terminal_status("test-health")
    resource = result["resource"]
    assert resource["agent"] == "claude"
    assert resource["context"]["percent"] == 96
    assert resource["context"]["status"] == "CRITICAL"
    assert resource["usage"]["percent"] == 30
    assert resource["usage"]["reset_in_minutes"] == 49
    assert resource["recommended_action"] in (ACTION_FINISH_AND_ROLLOVER,
                                              ACTION_CHECKPOINT_BEFORE_ROLLOVER)
    assert "checkpoint" in resource["rollover"]


def test_terminal_status_keeps_every_pre_existing_field(tmp_path):
    """Backward compatibility: the new key is purely additive."""
    result = _service(tmp_path).terminal_status("test-health")
    for field in ("session", "exists", "allowed", "state", "input_required", "reason",
                  "last_output", "untrusted_output", "untrusted_fields", "content_source", "cwd"):
        assert field in result, field
    assert result["state"] in ("RUNNING", "IDLE", "WAITING_INPUT", "UNKNOWN")
    assert result["untrusted_fields"] == ["last_output"]


def test_plain_pane_reports_unknown_rather_than_zero(tmp_path):
    resource = _service(tmp_path).terminal_status("test-plain")["resource"]
    assert resource["observed"] is False
    assert resource["context"]["percent"] is None
    assert resource["context"]["status"] == "UNKNOWN"
    assert resource["recommended_action"] == "UNKNOWN"


def test_resource_block_absent_when_disabled(tmp_path):
    service = _service(tmp_path, health=SessionHealthConfig(enabled=False))
    result = service.terminal_status("test-health")
    assert "resource" not in result
    assert result["state"] and result["session"] == "test-health"


def test_batch_inspect_passes_the_resource_block_through(tmp_path):
    terminal = _service(tmp_path)

    class Controller:
        def terminal_status(self, session):
            return terminal.terminal_status(session)

        def terminal_tail(self, session, lines):
            return terminal.terminal_tail(session, lines)

    compact = CompactTerminalTools(terminal, Controller())
    result = compact.batch_inspect(["test-health", "test-plain"], tail_lines=5)
    rows = {row["target"]: row for row in result["targets"]}
    assert rows["test-health"]["resource"]["context"]["percent"] == 96
    assert rows["test-health"]["resource"]["recommended_action"] in (
        ACTION_FINISH_AND_ROLLOVER, ACTION_CHECKPOINT_BEFORE_ROLLOVER)
    assert rows["test-plain"]["resource"]["context"]["percent"] is None
    # Pre-existing row fields are untouched.
    for field in ("target", "target_type", "session", "state", "input_required",
                  "reason", "tail", "tail_truncated"):
        assert field in rows["test-health"], field


def test_batch_inspect_unchanged_when_status_has_no_resource_block(tmp_path):
    terminal = _service(tmp_path, health=SessionHealthConfig(enabled=False))

    class Controller:
        def terminal_status(self, session):
            return terminal.terminal_status(session)

        def terminal_tail(self, session, lines):
            return terminal.terminal_tail(session, lines)

    result = CompactTerminalTools(terminal, Controller()).batch_inspect(["test-health"])
    assert "resource" not in result["targets"][0]


# ---------------------------------------------------------------------------
# Live false positive, 2026-09-19: a pane displaying this project's own source
# reported model "Mcp-Session-Id". Brackets are the most common punctuation in
# a terminal; "any bracketed word" was never a model field.
# ---------------------------------------------------------------------------

def test_bracketed_source_code_is_never_read_as_a_model():
    pane = "\n".join([
        '    h = {"Content-Type": "application/json"}',
        '    if sid: h["Mcp-Session-Id"] = sid',
        '    r = urllib.request.Request(URL, json.dumps(payload).encode(), h)',
        "    print(rows[0], data['result'])",
    ])
    parsed = parse_session_resources(pane)
    assert parsed.model is None
    assert parsed.context_percent is None
    assert parsed.usage_percent is None
    assert parsed.usage_reset_in_minutes is None
    assert parsed.context_max_tokens is None
    assert parsed.observed is False
    block = build_resource_block(parsed=parsed)
    assert block["model"] is None
    assert block["recommended_action"] == "UNKNOWN"


@pytest.mark.parametrize("bracketed", [
    "Mcp-Session-Id", "INFO", "0", "ERROR", "warn", "x-api-key", "2026-09-19",
    "tool_use", "terminal-mcp-claude-fleet",
])
def test_only_known_model_families_are_accepted(bracketed):
    """Even beside a real footer, an unrecognized bracketed field is reported
    as unknown rather than echoed back as an identified model."""
    parsed = parse_session_resources(f"[{bracketed}] | Context 42% | Usage 10% (resets in 5m)")
    assert parsed.model is None
    # The numbers are still read -- a strict model gate must not cost the
    # fields that were genuinely observed.
    assert (parsed.context_percent, parsed.usage_percent) == (42, 10)


@pytest.mark.parametrize("bracketed,expected", [
    ("Opus 5 (1M context)", "Opus 5"),
    ("Sonnet 5", "Sonnet 5"),
    ("Haiku 4.5", "Haiku 4.5"),
    ("claude-opus-5", "claude-opus-5"),
    ("GPT-5", "GPT-5"),
    ("gpt-5-codex", "gpt-5-codex"),
    ("Codex", "Codex"),
])
def test_known_model_families_are_still_read(bracketed, expected):
    parsed = parse_session_resources(f"[{bracketed}] | Context 42%")
    assert parsed.model == expected


def test_model_is_only_read_from_something_that_is_a_footer():
    """A real model name with no labelled percentage beside it is text on a
    screen, not a status footer."""
    assert parse_session_resources("Running with [Opus 5 (1M context)] today").model is None
    assert parse_session_resources("Running with [Opus 5 (1M context)] today").context_max_tokens is None


# ---------------------------------------------------------------------------
# Remote nodes: the CENTRAL controller enriches, so a node running an older
# build needs no upgrade to report resource health.
# ---------------------------------------------------------------------------

REMOTE_FOOTER = (
    "[Opus 5 (1M context)] | Context ██ 7% (in: 5, cache: 70k) | "
    "Usage ███ 34% (resets in 20m)"
)


def _remote_controller(status_payload, *, health=None):
    """A ControllerService with one REAL registered remote node whose client
    returns an OLD-build status payload: a real footer in `last_output`, and
    no `resource` key -- exactly what a node that has not been upgraded
    sends today."""
    import tempfile
    from terminal_mcp.controller import ControllerService
    from terminal_mcp.host_metrics import NodeMetrics
    from terminal_mcp.node_registry import NodeRegistry

    session_name = status_payload.get("session", "remote-session")

    class OldBuildNodeClient:
        def status(self, session, *, timeout_seconds=None):
            return dict(status_payload)

        def list_sessions(self, *, timeout_seconds=None):
            return {"sessions": [{"name": session_name}]}

    registry = NodeRegistry(Path(tempfile.mkdtemp()) / "nodes.db")
    controller = ControllerService(registry, session_health=health or SessionHealthConfig())
    registry.register("dell-linux", display_name="Dell", hostname="dell-host",
                      endpoint="http://dell")
    controller._clients["dell-linux"] = OldBuildNodeClient()
    registry.heartbeat(
        "dell-linux",
        metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                            ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000,
                            ram_percent=12.5, swap_total_bytes=0, swap_used_bytes=0,
                            swap_percent=0.0, disk_total_bytes=100_000_000_000,
                            disk_used_bytes=1_000_000_000, disk_free_bytes=99_000_000_000,
                            disk_percent=1.0),
        tmux_session_count=1, agent_counts={}, agent_types=("claude",),
        agent_version="0.13.0", labels=(),
    )
    return controller


def test_controller_enriches_a_remote_status_payload():
    payload = {"session": "nova-claude-long", "exists": True, "allowed": True,
               "state": "RUNNING", "input_required": False, "reason": "claude is running",
               "last_output": _pane(REMOTE_FOOTER), "cwd": "/home/mesflow/work/nova",
               "untrusted_output": True, "untrusted_fields": ["last_output"]}
    result = _remote_controller(payload).terminal_status("nova-claude-long")
    resource = result["resource"]
    assert resource["context"]["percent"] == 7
    assert resource["usage"]["percent"] == 34
    assert resource["usage"]["reset_in_minutes"] == 20
    assert resource["model"] == "Opus 5"
    assert resource["context"]["status"] == "NORMAL"
    assert resource["recommended_action"] == ACTION_CONTINUE
    # 7% context with 34% quota spent: the two must not be conflated.
    assert resource["usage"]["status"] == QUOTA_NORMAL
    # Remote git is never probed from the controller -- that cwd is on
    # another machine.
    assert resource["git"] == {"repo": None, "branch": None, "dirty": None}
    assert resource["rollover"]["checkpoint"]["cwd"] == "/home/mesflow/work/nova"
    # Pre-existing remote fields survive the enrichment untouched.
    assert result["state"] == "RUNNING" and result["node_id"] == "dell-linux"
    assert result["untrusted_fields"] == ["last_output"]


def test_remote_enrichment_also_applies_to_the_bounded_status_path():
    """wait/resume use terminal_status_bounded; it must not be the one
    surface that silently lacks the field."""
    payload = {"session": "nova-claude-long", "state": "RUNNING",
               "last_output": _pane(REMOTE_FOOTER)}
    result = _remote_controller(payload).terminal_status_bounded("nova-claude-long", 10)
    assert result["resource"]["context"]["percent"] == 7


def test_controller_never_overwrites_a_block_the_node_already_sent():
    own = {"agent": "claude", "model": "from-the-node", "observed": True}
    payload = {"session": "nova-claude-long", "state": "RUNNING",
               "last_output": _pane(REMOTE_FOOTER), "resource": own}
    result = _remote_controller(payload).terminal_status("nova-claude-long")
    assert result["resource"] == own


def test_controller_enrichment_reports_unknown_for_a_footerless_remote_pane():
    payload = {"session": "nova-shell", "state": "IDLE", "last_output": "$ ls\ntotal 0",
               "cwd": "/home/mesflow"}
    resource = _remote_controller(payload).terminal_status("nova-shell")["resource"]
    assert resource["observed"] is False
    assert resource["context"]["percent"] is None
    assert resource["recommended_action"] == "UNKNOWN"


def test_controller_enrichment_leaves_errors_alone():
    payload = {"session": "nova-claude-long", "state": "RUNNING",
               "last_output": _pane(REMOTE_FOOTER)}
    controller = _remote_controller(payload)
    assert "resource" not in controller.terminal_status("nope-not-here")


def test_controller_enrichment_honours_disabled_config():
    payload = {"session": "nova-claude-long", "state": "RUNNING",
               "last_output": _pane(REMOTE_FOOTER)}
    controller = _remote_controller(payload, health=SessionHealthConfig(enabled=False))
    assert "resource" not in controller.terminal_status("nova-claude-long")


def test_batch_inspect_includes_resource_for_a_remote_target():
    payload = {"session": "nova-claude-long", "state": "RUNNING",
               "last_output": _pane(REMOTE_FOOTER), "cwd": "/home/mesflow/work/nova"}
    controller = _remote_controller(payload)

    class Terminal:
        def terminal_get_binding(self, binding):
            return {"error": "BINDING_NOT_FOUND", "binding": binding}

    class RoutingController:
        def terminal_status(self, session):
            return controller.terminal_status(session)

        def terminal_tail(self, session, lines):
            return {"session": session, "output": "tail", "truncated": False}

    rows = CompactTerminalTools(Terminal(), RoutingController()).batch_inspect(
        ["nova-claude-long"])["targets"]
    assert rows[0]["resource"]["context"]["percent"] == 7
    assert rows[0]["resource"]["usage"]["reset_in_minutes"] == 20


def test_controller_picks_up_session_health_from_the_local_process_config(tmp_path):
    """No new argument at any existing call site: the controller finds the
    policy through the in-process TerminalService it already wraps."""
    from terminal_mcp.controller import ControllerService
    from terminal_mcp.node_client import LocalNodeClient
    from terminal_mcp.node_registry import NodeRegistry
    terminal = _service(tmp_path, health=SessionHealthConfig(watch_percent=2,
                                                            prepare_rollover_percent=4,
                                                            finish_rollover_percent=6,
                                                            checkpoint_only_percent=8))
    controller = ControllerService(NodeRegistry(tmp_path / "nodes.db"),
                                   local_client=LocalNodeClient(terminal))
    assert controller.session_health.watch_percent == 2
    # And that policy is what the remote path applies: 7% is CRITICAL here.
    enriched = controller._with_resource_health(
        {"session": "s", "state": "RUNNING", "last_output": _pane(REMOTE_FOOTER)})
    assert enriched["resource"]["context"]["status"] == "CRITICAL"
