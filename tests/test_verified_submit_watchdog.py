from __future__ import annotations

from pathlib import Path

import pytest

from terminal_mcp.submit_watchdog import (
    ACK_ACCEPTED,
    ACK_RUNNING,
    ACK_STUCK,
    SubmissionStore,
    VerifiedSubmitWatchdog,
    WatchdogConfig,
)


@pytest.mark.parametrize("backend", ["linux-tmux", "windows-conpty"])
@pytest.mark.parametrize("prompt", ["short", "x" * 5_000, "x" * 20_000, "line 1\n```py\nprint(1)\n```"])
@pytest.mark.parametrize("accepted_after", [1, 2, 3])
def test_verified_submit_sends_enter_only_until_ack(tmp_path: Path, backend: str,
                                                     prompt: str, accepted_after: int):
    store = SubmissionStore(tmp_path / f"{backend}.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=2,
                                                              max_enter_attempts=3))
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


def test_verified_submit_stops_without_enter_when_pager_or_buffer_incomplete(tmp_path: Path):
    store = SubmissionStore(tmp_path / "pager.db")
    watchdog = VerifiedSubmitWatchdog(store, WatchdogConfig(poll_interval_seconds=.3,
                                                              timeout_seconds=1,
                                                              max_enter_attempts=5))
    record, _ = store.create(idempotency_key="pager", session="codex", agent_type="codex",
                             prompt="long prompt")
    enters: list[int] = []
    result = watchdog.run(record.submission_id, capture=lambda: ["-- MORE --"],
                          inject=lambda _: None, send_enter=lambda: enters.append(1),
                          evidence=lambda _lines, _record: ("PAGER", "pager_visible"))
    assert result["ack_state"] == ACK_STUCK
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
