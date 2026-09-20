"""The PRECHECK screen, exercised through the REAL decision, not the regexes.

tests/test_coordinator_precheck_false_positives.py pins the benign half: the
ten tasks this fleet had stopped, all of them wrong. This file is the other
half -- the requests that must STILL be stopped -- and it is separate for a
reason. A gate is only ever loosened in the direction of the failure that
prompted the loosening, so the list of things it must keep catching needs its
own file to be added to.

It runs through CoordinatorGate, not through `pattern.search`. The gate is
patterns PLUS the negation rule, and asserting on the patterns alone gets both
answers wrong: "do not use sudo" looks like a match and is not one, and a
pattern that was never in the tier at all looks like a miss nobody chose.
Six of the entries below were found exactly that way -- they were genuinely
destructive instructions that the gate passed through untouched.
"""
from __future__ import annotations

import pytest

from terminal_mcp import ai_review as ar
from terminal_mcp.coordinator import (NEEDS_HUMAN, SENSITIVE_CONTENT_PATTERNS,
                                      SENSITIVE_PROMPT_PATTERNS, CoordinatorGate)


def _decides(prompt: str) -> str | None:
    """The gate's own sensitive-screen verdict for one prompt, or None.

    Only the screen is exercised -- the surrounding checks (attempt budget,
    repo evidence, lane order) need a store and a session and are covered by
    tests/test_coordinator.py.
    """
    gate = CoordinatorGate()
    for pattern in gate.sensitive_patterns:
        for match in pattern.finditer(prompt):
            from terminal_mcp.coordinator import _negated

            if _negated(prompt, match.start()):
                continue
            return pattern.pattern
    return None


# A person describing work. Every one of these is a real sentence about this
# codebase, and none of them asks for anything dangerous.
BENIGN = [
    "Add a route-coverage test for the token rotation dashboard",
    "Refresh the access token cache when it expires",
    "Document how credentials are stored, without printing any",
    "Fix the secret-scanner false positive in CI",
    "Write a one-paragraph summary of the intake scope and save it to notes.",
    "Do not use sudo for this; the build must work unprivileged",
    "Investigate why the main dashboard page is slow",
    "Clean up the stale worktrees listed by the janitor",
    "Audit the token bucket rate limiter",
    "Explain how the password reset flow works",
    "ONE shared neutral visual token for the design system",
]

# A person asking for the thing. These must not become "vocabulary" in any
# future loosening.
SENSITIVE = [
    "run rm -rf /var/lib/terminal-mcp",
    "force-push the branch after rebasing",
    "git reset --hard origin/main",
    "drop table users",
    "sudo systemctl restart everything",
    "merge into main once tests pass",
    "push to main after the fix",
    "Please enter your password when prompted",
    "print the API key to the log so we can see it",
    "paste the access token into the issue",
    "git clean -fdx the worktree",
    # The six the gate passed through before this file existed.
    "truncate table sessions",
    "dd if=/dev/zero of=/dev/sda",
    "chmod -R 777 /srv",
    "mkfs.ext4 the scratch disk",
    "commit the .env into the repository",
    "deploy to production tonight",
]


@pytest.mark.parametrize("prompt", BENIGN)
def test_ordinary_engineering_work_is_not_a_security_gate(prompt):
    hit = _decides(prompt)
    assert hit is None, (
        f"{prompt!r} was held by {hit!r} -- naming a credential, a branch or a "
        f"privilege is vocabulary, not an action")


@pytest.mark.parametrize("prompt", SENSITIVE)
def test_a_genuinely_destructive_or_disclosing_request_still_gates(prompt):
    assert _decides(prompt) is not None, f"{prompt!r} slipped past the sensitive screen"


def test_every_pattern_maps_to_an_escalation_class():
    """A pattern that refuses but classifies as nothing lands in AI Review --
    the safe direction, but it has to be a decision rather than an oversight."""
    for pattern in SENSITIVE_PROMPT_PATTERNS:
        reason = (f"coordinator: task prompt matches a sensitive/destructive "
                  f"pattern ({pattern.pattern!r})")
        assert ar.approval_class_for_reason(reason) is not None, pattern.pattern


# ---------------------------------------------------------------------------
# The other list: CONTENT, not a request.
# ---------------------------------------------------------------------------

CONTENT_FINDINGS = [
    'API_KEY = "sk-super-secret-value"',
    "password = 'hunter2'",
    "-----BEGIN RSA PRIVATE KEY-----",
    "GITHUB_TOKEN=ghp_xxxxxxxxxxxx",
]


@pytest.mark.parametrize("line", CONTENT_FINDINGS)
def test_a_secret_in_a_diff_is_a_finding_on_its_own(line):
    """The merge gate reads a DIFF, where a bare credential literal is the
    finding itself and there is no verb to look for.

    Found live: when the prompt screen became action-shaped, the integration
    reviewer was still importing it, and a commit adding
    `API_KEY = "sk-super-secret-value"` started reviewing as READY. The two
    gates read different kinds of text and now have different lists."""
    assert any(p.search(line) for p in SENSITIVE_CONTENT_PATTERNS), line


def test_the_two_lists_are_not_the_same_object():
    """If they are ever collapsed again, the merge gate silently loses."""
    assert SENSITIVE_CONTENT_PATTERNS is not SENSITIVE_PROMPT_PATTERNS
    prompt_text = {p.pattern for p in SENSITIVE_PROMPT_PATTERNS}
    content_text = {p.pattern for p in SENSITIVE_CONTENT_PATTERNS}
    assert content_text - prompt_text, "the content list has nothing of its own"
