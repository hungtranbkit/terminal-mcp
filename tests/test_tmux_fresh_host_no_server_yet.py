"""TmuxClient.list_sessions on a host where tmux has NEVER had a server
run for this user -- found live onboarding a genuinely fresh node (M910):
the very first session-create attempt on that box (right after `apt
install tmux`, before anything had ever started a tmux server there)
consistently failed. Root cause: get_session()/list_sessions() is called
first (create()'s own duplicate-name check, lifecycle.py) and tmux
reports "no tmux server for this user" with TWO different wordings
depending on history --

  "no server running on /tmp/tmux-<uid>/default"
      -- a server existed and has since exited (e.g. its last session
      was killed); the socket FILE is still there, just unconnectable.

  "error connecting to /tmp/tmux-<uid>/default (No such file or
  directory)"
      -- no server has EVER run for this user; the socket path doesn't
      exist at all yet.

Only the first wording was treated as "no sessions" (empty list); the
second raised TmuxError, so a brand new node's first-ever session-create
call failed hard and permanently (100% reproducible, not a flaky race --
confirmed live), while every later call succeeded once *any* tmux
server had existed on the host, even briefly.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from terminal_mcp.tmux import TmuxClient

NEVER_HAD_A_SERVER = "error connecting to /tmp/tmux-1000/default (No such file or directory)\n"
HAD_ONE_BEFORE = "no server running on /tmp/tmux-1000/default\n"


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["tmux"], returncode=returncode, stdout="", stderr=stderr)


def test_list_sessions_is_empty_not_an_error_when_no_server_has_ever_run():
    client = TmuxClient()
    with patch("subprocess.run", return_value=_completed(1, NEVER_HAD_A_SERVER)):
        assert client.list_sessions() == []


def test_list_sessions_is_still_empty_for_the_previously_handled_stale_socket_wording():
    client = TmuxClient()
    with patch("subprocess.run", return_value=_completed(1, HAD_ONE_BEFORE)):
        assert client.list_sessions() == []


def test_list_sessions_still_raises_on_a_genuinely_different_error():
    from terminal_mcp.tmux import TmuxError
    import pytest
    client = TmuxClient()
    with patch("subprocess.run", return_value=_completed(1, "some other tmux failure\n")):
        with pytest.raises(TmuxError, match="some other tmux failure"):
            client.list_sessions()


def test_get_session_does_not_raise_on_a_never_had_a_server_host():
    client = TmuxClient()
    with patch("subprocess.run", return_value=_completed(1, NEVER_HAD_A_SERVER)):
        assert client.get_session("anything") is None
