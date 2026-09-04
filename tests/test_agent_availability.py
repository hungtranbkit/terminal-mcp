"""agent_availability.py: real launcher-binary availability, applied
identically on every platform (fixes a real, pre-existing gap this
project had on Linux too -- a configured-but-not-installed launcher used
to be reported as available regardless)."""
from __future__ import annotations

from terminal_mcp.agent_availability import available_agent_types


def test_shell_always_included_even_with_no_launch_commands():
    assert available_agent_types(()) == ("shell",)


def test_real_installed_binary_included():
    # `true` is a real, universally-installed binary on this dev host.
    result = available_agent_types((("claude", "true"),))
    assert result == ("shell", "claude")


def test_missing_binary_excluded():
    result = available_agent_types((("claude", "this-binary-does-not-exist-anywhere-xyz"),))
    assert result == ("shell",)


def test_mixed_available_and_missing():
    result = available_agent_types((
        ("claude", "true"),
        ("codex", "this-binary-does-not-exist-anywhere-xyz"),
    ))
    assert result == ("shell", "claude")


def test_shell_entry_in_launch_commands_not_duplicated():
    result = available_agent_types((("shell", "true"),))
    assert result == ("shell",)
    assert result.count("shell") == 1
