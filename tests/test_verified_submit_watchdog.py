from __future__ import annotations

from pathlib import Path

import pytest

from terminal_mcp.submit_watchdog import (
    ACK_ACCEPTED,
    ACK_BLOCKED_APPROVAL,
    ACK_NODE_UNAVAILABLE,
    ACK_RUNNING,
    ACK_STUCK,
    SubmissionStore,
    SubmissionSweeper,
    VerifiedSubmitWatchdog,
    WatchdogConfig,
)


@pytest.mark.parametrize("backend", ["linux-tmux", "windows-conpty"])
@pytest.mark.parametrize("prompt", ["short", "x" * 5_000, "x" * 20_000, "line 1\n```py\nprint(1)\n```"])
@pytest.mark.parametrize("accepted_after", [1, 2])
def test_verified_submit_sends_enter_only_until_ack(tmp_path: Path, backend: str,
                                                     prompt: str, accepted_after: int):
    store = SubmissionStore(tmp_path / f"{backend}.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=2,
                                                              max_enter_attempts=2))
    record, _ = store.create(idempotency_key=f"{backend}-{accepted_after}-{len(prompt)}",
                             session="codex-disposable", agent_type="codex", prompt=prompt)
    injected: list[str] = []
    enters: list[int] = []
    polls = {"n": 0}

    def capture() -> list[str]:
        polls["n"] += 1
        return (["composer: " + prompt + f" [tick {polls['n']}]" ]
                if polls["n"] <= accepted_after else ["Working"])

    def evidence(lines, current):
        if lines == ["Working"]:
            return ACK_RUNNING, "working_indicator"
        return "COMPOSER", "draft_still_present"

    result = watchdog.run(record.submission_id, capture=capture,
                          inject=lambda text: injected.append(text),
                          send_enter=lambda: enters.append(1), evidence=evidence)
    assert injected == [prompt]  # the prompt is never resent
    assert result["ack_state"] == ACK_RUNNING
    assert result["enter_count"] == accepted_after
    assert len(enters) == accepted_after
    assert result["recovery_enter_sent"] is (accepted_after == 2)
    assert result["composer_before"] == "present"
    assert result["submit_latency_ms"] >= 0


def test_vietnamese_ime_first_enter_commits_composition_then_one_recovery_enter(tmp_path: Path):
    store = SubmissionStore(tmp_path / "ime.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=2,
                                                              max_enter_attempts=2))
    prompt = "Kiểm tra tồn kho tiếng Việt\nDòng thứ hai"
    record, _ = store.create(idempotency_key="ime", session="codex", agent_type="codex", prompt=prompt)
    enters: list[int] = []
    polls = {"n": 0}

    def capture() -> list[str]:
        polls["n"] += 1
        # First Enter is consumed by the IME; the exact draft remains.
        if polls["n"] == 1:
            return ["> " + prompt]
        if polls["n"] == 2:
            return ["> " + prompt + " [composition committed]"]
        return ["esc to interrupt"]

    def evidence(lines, _record):
        return (ACK_RUNNING, "working_indicator") if lines == ["esc to interrupt"] else ("COMPOSER", "draft_still_in_composer")

    result = watchdog.run(record.submission_id, capture=capture,
                          inject=lambda text: None, send_enter=lambda: enters.append(1), evidence=evidence)
    assert len(enters) == 2
    assert result["ack_state"] == ACK_RUNNING
    assert result["recovery_enter_sent"] is True
    assert result["first_enter_effect"] == "composition_commit_or_submit"
    assert result["composer_after"] == "cleared_or_executing"


def test_stuck_submit_never_spams_more_than_two_enters(tmp_path: Path):
    store = SubmissionStore(tmp_path / "stuck.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=.8,
                                                              max_enter_attempts=2))
    record, _ = store.create(idempotency_key="stuck", session="codex", agent_type="codex", prompt="Xin chào")
    enters: list[int] = []
    result = watchdog.run(record.submission_id, capture=lambda: ["> Xin chào"],
                          inject=lambda text: None, send_enter=lambda: enters.append(1),
                          evidence=lambda _lines, _record: ("COMPOSER", "draft_still_in_composer"))
    assert result["ack_state"] == ACK_STUCK
    assert len(enters) == 2
    assert result["recovery_enter_sent"] is True


def test_verified_submit_stops_without_enter_when_pager_or_buffer_incomplete(tmp_path: Path):
    store = SubmissionStore(tmp_path / "pager.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=1,
                                                              max_enter_attempts=2))
    record, _ = store.create(idempotency_key="pager", session="codex", agent_type="codex",
                             prompt="long prompt")
    enters: list[int] = []
    result = watchdog.run(record.submission_id, capture=lambda: ["-- MORE --"],
                          inject=lambda _: None, send_enter=lambda: enters.append(1),
                          evidence=lambda _lines, _record: ("PAGER", "pager_visible"))
    assert result["ack_state"] == "BLOCKED_APPROVAL"
    assert result["enter_count"] == 0
    assert enters == []


def test_submission_idempotency_survives_store_reopen(tmp_path: Path):
    path = tmp_path / "restart.db"
    first = SubmissionStore(path)
    record, created = first.create(idempotency_key="same", session="codex", agent_type="codex", prompt="once")
    assert created
    first.update(record.submission_id, ack_state=ACK_ACCEPTED, enter_count=1,
                 attempts=1, evidence="adapter_execution_evidence")
    reopened = SubmissionStore(path)
    same, created_again = reopened.create(idempotency_key="same", session="codex",
                                          agent_type="codex", prompt="once")
    assert not created_again
    assert same.submission_id == record.submission_id
    assert same.ack_state == ACK_ACCEPTED
    assert same.enter_count == 1


def test_different_prompt_cannot_reuse_submission_key(tmp_path: Path):
    store = SubmissionStore(tmp_path / "collision.db")
    store.create(idempotency_key="same", session="codex", agent_type="codex", prompt="one")
    with pytest.raises(ValueError, match="DIFFERENT_PROMPT"):
        store.create(idempotency_key="same", session="codex", agent_type="codex", prompt="two")


def test_claude_watchdog_is_single_submit_even_when_evidence_stays_pending(tmp_path: Path):
    """A non-Codex record can never inherit Codex's Enter retry budget."""
    store = SubmissionStore(tmp_path / "claude.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(
        poll_interval_seconds=.05, timeout_seconds=.25, max_enter_attempts=5,
    ))
    record, _ = store.create(idempotency_key="claude-single", session="claude-disposable",
                             agent_type="claude", prompt="one prompt only")
    enters: list[int] = []
    result = watchdog.run(
        record.submission_id,
        capture=lambda: ["> one prompt only", "Working"],
        inject=lambda _text: None,
        send_enter=lambda: enters.append(1),
        evidence=lambda _lines, _record: ("COMPOSER", "draft_still_in_composer"),
    )
    assert enters == [1]
    assert result["enter_count"] == 1
    assert result["ack_state"] == ACK_STUCK


@pytest.mark.parametrize("state,reason", [
    ("WAITING_APPROVAL", "pending"), ("COMPOSER", "permission confirmation required"),
    ("COMPOSER", "numbered-choice menu"),
])
def test_approval_looking_state_blocks_without_enter(tmp_path: Path, state: str, reason: str):
    store = SubmissionStore(tmp_path / ("approval-" + state + ".db"))
    record, _ = store.create(idempotency_key=state + reason, session="codex", agent_type="codex", prompt="safe")
    entered: list[int] = []
    result = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.01, timeout_seconds=.1)).run(
        record.submission_id, capture=lambda: ["menu"], inject=lambda _: None,
        send_enter=lambda: entered.append(1), evidence=lambda _lines, _record: (state, reason))
    assert result["ack_state"] == ACK_BLOCKED_APPROVAL
    assert entered == []


def test_persisted_cap_is_shared_by_watcher_passes_and_restart(tmp_path: Path):
    path = tmp_path / "cap.db"
    store = SubmissionStore(path)
    record, _ = store.create(idempotency_key="cap", session="codex", agent_type="codex", prompt="once")
    config = WatchdogConfig(poll_interval_seconds=.01, timeout_seconds=.1, max_enter_attempts=6, max_total_enters=6)
    entered: list[int] = []
    for _ in range(6):
        VerifiedSubmitWatchdog(store, config).run(record.submission_id, capture=lambda: ["> once"],
                                                  inject=lambda _: None, send_enter=lambda: entered.append(1),
                                                  evidence=lambda _lines, _record: ("COMPOSER", "draft"), max_new_enters=1)
    result = VerifiedSubmitWatchdog(SubmissionStore(path), config).run(
        record.submission_id, capture=lambda: ["> once"], inject=lambda _: None,
        send_enter=lambda: entered.append(1), evidence=lambda _lines, _record: ("COMPOSER", "draft"), max_new_enters=1)
    assert entered == [1] * 6
    assert result["enter_count"] == 6 and result["ack_state"] == ACK_STUCK


@pytest.mark.parametrize("required_enters", [1, 2, 3, 6])
def test_execution_starts_after_each_bounded_enter_count(tmp_path: Path, required_enters: int):
    store = SubmissionStore(tmp_path / f"required-{required_enters}.db")
    record, _ = store.create(idempotency_key=f"required-{required_enters}", session="codex",
                             agent_type="codex", prompt="long prompt")
    enters: list[int] = []
    injected: list[str] = []

    def evidence(_lines, current):
        if current.enter_count >= required_enters:
            return ACK_RUNNING, "Working/tool execution"
        return "COMPOSER", "draft_still_in_composer"

    result = VerifiedSubmitWatchdog(
        store, WatchdogConfig(poll_interval_seconds=.01, timeout_seconds=.2,
                              max_enter_attempts=6, max_total_enters=6),
    ).run(record.submission_id, capture=lambda: ["> long prompt"],
          inject=lambda text: injected.append(text), send_enter=lambda: enters.append(1),
          evidence=evidence)
    assert len(injected) == 1
    assert len(enters) == required_enters
    assert result["execution_started"] is True
    assert result["enter_count"] == required_enters


def test_execution_evidence_stops_and_sessions_are_isolated(tmp_path: Path):
    store = SubmissionStore(tmp_path / "isolation.db")
    first, _ = store.create(idempotency_key="first", session="codex-a", agent_type="codex", prompt="a")
    second, _ = store.create(idempotency_key="second", session="codex-b", agent_type="codex", prompt="b")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.01, timeout_seconds=.1))
    started = watchdog.run(first.submission_id, capture=lambda: ["Working"], inject=lambda _: None,
                           send_enter=lambda: pytest.fail("no Enter after execution evidence"),
                           evidence=lambda _lines, _record: (ACK_RUNNING, "tool execution"))
    other = watchdog.run(second.submission_id, capture=lambda: ["> b"], inject=lambda _: None,
                         send_enter=lambda: None, evidence=lambda _lines, _record: ("COMPOSER", "draft"),
                         max_new_enters=1)
    assert started["execution_started"] is True and started["enter_count"] == 0
    assert other["enter_count"] == 1 and store.get(first.submission_id).enter_count == 0  # type: ignore[union-attr]


def test_sweeper_ttl_and_remote_unavailable_fail_closed(tmp_path: Path):
    import time
    from types import SimpleNamespace
    from terminal_mcp.core import TerminalService
    store = SubmissionStore(tmp_path / "terminal.db")
    stale, _ = store.create(idempotency_key="stale", session="codex", agent_type="codex", prompt="x")
    with store._connection() as db:
        db.execute("UPDATE prompt_submissions SET created_at=? WHERE submission_id=?", (time.time() - 601, stale.submission_id))
    SubmissionSweeper(store, lambda _: pytest.fail("stale recovery"), ttl_seconds=600).run_once()
    assert store.get(stale.submission_id).ack_state == ACK_STUCK  # type: ignore[union-attr]
    remote, _ = store.create(idempotency_key="remote", session="gone", agent_type="codex", prompt="x")
    service = object.__new__(TerminalService)
    service.submissions, service.tmux = store, SimpleNamespace(get_session=lambda _: None)
    service.recover_submission(remote)
    assert store.get(remote.submission_id).ack_state == ACK_NODE_UNAVAILABLE  # type: ignore[union-attr]
