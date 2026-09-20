"""The PRECHECK prompt screen: what it must gate, and what it must not.

WHY THIS FILE EXISTS. The original screen was a flat keyword list borrowed
from supervisor2.py's ATTENTION_STOP_PATTERNS, which was written for a pane's
OUTPUT. Applied to a task's PROMPT -- paragraphs of developer prose -- it
gated ten tasks on this fleet and every single one was a false positive. Four
of them were the gate reading the prompt's own safety instruction as the
danger ("stop only for credential/destructive blockers", "Do not merge main").

The live prompts that were wrongly gated are reproduced verbatim below, so a
future widening of the patterns has to break a real observation rather than a
hypothetical one. The "must still gate" half is equally load-bearing: this is
a security control and the point was never to make it quieter.
"""
from __future__ import annotations

import pytest

from terminal_mcp.coordinator import (
    NEEDS_HUMAN, READY, CoordinatorGate, OtherLaneSnapshot, RepoEvidence, SessionSnapshot,
)
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _task(store, prompt, session="lane-a", **overrides):
    row = {"prompt": prompt}
    row.update(overrides)
    (task_id,) = store.set_tasks(session, [row])
    store.claim_next_task(session, claimed_by="engine-1")
    return store.get_task(task_id)


def _clean_collector(cwd, node_id=None):
    return RepoEvidence(branch="main", head="abc123", clean=True, status_lines=())


def _session(**overrides):
    base = {"node_id": "local", "cwd": "/tmp/workspace", "current_command": "claude"}
    base.update(overrides)
    return SessionSnapshot(**base)


def _decide(store, prompt):
    gate = CoordinatorGate(evidence_collector=_clean_collector)
    return gate.review(_task(store, prompt), store=store, session=_session())


# ---------------------------------------------------------------------------
# Verbatim live false positives. Every one of these was gated in production.
# ---------------------------------------------------------------------------

LIVE_FALSE_POSITIVES = [
    # Matched 'credential' -- in the prompt's own instruction to escalate FOR
    # credentials. Four separate real tasks were stopped by this one phrase.
    ("stop-for-credentials",
     "Continue autonomously until merge-ready; only stop for a real "
     "credential/destructive blocker."),
    ("ask-only-for-credentials",
     "This task is implementation, not brainstorming. Ask only for true "
     "blockers: credentials, destructive actions, or irreversible production risk."),
    # Matched 'merge main' -- in an instruction NOT to.
    ("do-not-merge-main",
     "15) Commit changes on feat/ui-v1-core. Do not merge main and do not push (no remote)."),
    # Matched 'token' -- a design token.
    ("design-token",
     "Gaps must use ONE shared neutral visual token and ONE rendering pattern "
     "across the whole timeline."),
    ("shared-tokens",
     "Finish the remaining UI consistency work using UI V3 only and shared "
     "tokens/components."),
    # Matched 'token' -- a smoke-test echo string.
    ("smoke-echo-token",
     "Reply with exactly this token and nothing else: PHASEB_OK_204112"),
    # Matched 'credential' -- describing what the test may observe.
    ("credentials-allow",
     "Check login/front-door redirect behavior as far as available "
     "credentials/session allow."),
    # Prohibitions. The prompt is forbidding the very thing that was matched.
    ("no-api-keys-in-repo",
     "Redact secrets/cookies/tokens from returned logs. no API keys in repo."),
    ("no-plaintext-credentials",
     "Use secure storage already chosen by the repo; no plaintext credentials "
     "in source/docs/logs, no hardcoded secrets, fail closed."),
    ("no-credential-values-in-logs",
     "No credential values in logs/results. Support credentials via env/profile only."),
]


@pytest.mark.parametrize("label,prompt", LIVE_FALSE_POSITIVES,
                         ids=[label for label, _ in LIVE_FALSE_POSITIVES])
def test_a_benign_prompt_observed_live_is_no_longer_gated(store, label, prompt):
    assert _decide(store, prompt).status == READY


# ---------------------------------------------------------------------------
# The control half: none of this is allowed to get quieter.
# ---------------------------------------------------------------------------

MUST_STILL_GATE = [
    ("print-a-token", "Print the GITHUB_TOKEN so I can copy it into the chat."),
    ("echo-api-key", "Please echo the api key from the .env file."),
    ("paste-access-token", "Paste the access token into the issue so the other agent can use it."),
    ("set-github-token", "Set the github token in the deploy config."),
    ("rotate-client-secret", "Rotate the client secret and store the new one."),
    ("hardcode-api-key", "hard-code the api key in the source for now"),
    ("credentials-into-repo", "commit the credentials into the repo so CI can read them"),
    ("enter-password", "Enter your password when the installer asks"),
    ("rm-rf", "rm -rf /var/lib/postgres to clear the old cluster"),
    ("sudo", "Run sudo systemctl restart everything"),
    ("drop-table", "drop table users and recreate it from the fixture"),
    ("force-push", "force-push the rewritten history"),
    ("reset-hard", "git reset --hard origin/main to throw the branch away"),
    ("git-clean", "Run git clean -fdx in the worktree first"),
    ("merge-into-main", "merge into main once CI is green"),
    ("push-to-main", "push to main directly, skipping review"),
]


@pytest.mark.parametrize("label,prompt", MUST_STILL_GATE,
                         ids=[label for label, _ in MUST_STILL_GATE])
def test_genuinely_sensitive_work_is_still_gated(store, label, prompt):
    decision = _decide(store, prompt)
    assert decision.status == NEEDS_HUMAN
    assert "sensitive/destructive" in decision.reason
    # The reason now names the text that actually matched, so an operator can
    # judge it without re-reading the whole prompt.
    assert decision.evidence["matched_text"]


def test_a_negation_does_not_launder_a_later_real_instruction(store):
    """"Do not X" earlier in the prompt must not excuse a real "X" later."""
    decision = _decide(
        store,
        "Do not merge main yourself. Once review passes, force-push the branch.")
    assert decision.status == NEEDS_HUMAN
    assert "force" in decision.evidence["matched_text"].lower()


def test_a_negation_only_covers_its_own_clause(store):
    """A "never" two sentences earlier is not a licence for this sentence."""
    decision = _decide(
        store,
        "Never guess at configuration. Print the GITHUB_TOKEN to the log so we can see it.")
    assert decision.status == NEEDS_HUMAN


# ---------------------------------------------------------------------------
# Cross-lane conflict: same path, different machine, is not the same directory.
# ---------------------------------------------------------------------------

def test_same_cwd_on_a_different_node_is_not_a_conflict(store):
    """Every node lays its workspaces out identically, so comparing the path
    string alone reported a collision between two sessions that cannot touch
    each other's files."""
    gate = CoordinatorGate(evidence_collector=_clean_collector)
    decision = gate.review(
        _task(store, "implement the widget exporter"), store=store,
        session=_session(node_id="hp-linux", cwd="/home/kimex/workspace/terminal-mcp"),
        other_active=(OtherLaneSnapshot(session="other-lane", node_id="dell-linux",
                                        cwd="/home/kimex/workspace/terminal-mcp"),))
    assert decision.status == READY


def test_same_cwd_on_the_same_node_is_still_a_conflict(store):
    gate = CoordinatorGate(evidence_collector=_clean_collector)
    decision = gate.review(
        _task(store, "implement the widget exporter"), store=store,
        session=_session(node_id="hp-linux", cwd="/home/kimex/workspace/terminal-mcp"),
        other_active=(OtherLaneSnapshot(session="other-lane", node_id="hp-linux",
                                        cwd="/home/kimex/workspace/terminal-mcp"),))
    assert decision.status == NEEDS_HUMAN
    assert "already actively working" in decision.reason


def test_an_unknown_node_stays_a_conflict(store):
    """Refusing to dispatch is the safe direction when we cannot tell."""
    gate = CoordinatorGate(evidence_collector=_clean_collector)
    decision = gate.review(
        _task(store, "implement the widget exporter"), store=store,
        session=_session(node_id=None, cwd="/home/kimex/workspace/terminal-mcp"),
        other_active=(OtherLaneSnapshot(session="other-lane", node_id=None,
                                        cwd="/home/kimex/workspace/terminal-mcp"),))
    assert decision.status == NEEDS_HUMAN


# ---------------------------------------------------------------------------
# "not a git repository" is an answer, not an unreadable repository.
# ---------------------------------------------------------------------------

def test_a_non_repo_cwd_is_refused_by_its_real_name(store, tmp_path):
    """Still fail-closed -- but it no longer blames a repository that is not
    there. Observed live on a session sitting in a scratchpad directory."""
    from terminal_mcp.coordinator import git_repo_evidence

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    gate = CoordinatorGate(evidence_collector=git_repo_evidence)
    decision = gate.review(_task(store, "summarise the intake scope"), store=store,
                           session=_session(cwd=str(plain)))
    assert decision.status == NEEDS_HUMAN
    assert "is not a git repository" in decision.reason
    assert "could not read git/repo status" not in decision.reason
    assert decision.evidence["not_a_git_repository"]


def test_a_non_repo_cwd_may_be_accepted_with_the_documented_override(store, tmp_path):
    from terminal_mcp.coordinator import git_repo_evidence

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    gate = CoordinatorGate(evidence_collector=git_repo_evidence)
    decision = gate.review(
        _task(store, "summarise the intake scope", metadata={"allow_unverified_repo": True}),
        store=store, session=_session(cwd=str(plain)))
    assert decision.status == READY
    assert "not a git repository" in decision.evidence["repo_evidence"]
