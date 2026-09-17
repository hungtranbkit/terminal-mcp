"""The tmux_session_factory guard itself.

It has to hold two properties at once, and they pull in opposite
directions:

  * never touch a session it did not create -- the suite runs in the same
    tmux server as this project's real, attended sessions, so killing a
    name-collision would be a production incident, not a test failure;
  * still be runnable twice on the same host -- an interrupted run leaves
    its sessions behind, and under a name-only guard that leftover made
    the test refuse its own name forever after.

Ownership tagging is what reconciles them, so it is worth a test.
"""

import pytest

from tests.conftest import OWNER_OPTION, _is_test_owned, tmux


def _kill(name: str) -> None:
    tmux("kill-session", "-t", name, check=False)


def test_a_session_the_factory_created_is_tagged_as_test_owned(tmux_session_factory):
    name = tmux_session_factory("test-guard-tagged")
    assert _is_test_owned(name)


def test_an_untagged_pre_existing_session_is_refused_not_killed(tmux_session_factory):
    # Stands in for a real, attended session that happens to share the name.
    name = "test-guard-not-ours"
    tmux("new-session", "-d", "-s", name, "sleep 30")
    try:
        with pytest.raises(RuntimeError, match="refuses to touch"):
            tmux_session_factory(name)
        # The point of refusing: it is still there, unharmed.
        assert tmux("has-session", "-t", name, check=False).returncode == 0
    finally:
        _kill(name)


def test_a_tagged_leftover_from_an_earlier_run_is_reaped_and_reused(tmux_session_factory):
    # Exactly what an interrupted run leaves behind: our session, our tag,
    # but no live fixture that remembers creating it.
    name = "test-guard-leftover"
    tmux("new-session", "-d", "-s", name, "sleep 30")
    tmux("set-option", "-t", name, OWNER_OPTION, "1", check=False)
    try:
        assert tmux_session_factory(name) == name
        assert _is_test_owned(name)
    finally:
        _kill(name)
