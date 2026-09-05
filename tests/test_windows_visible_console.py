"""windows_visible_console.py's pure-Python helpers -- _parse_size/
_read_line_locked -- testable on any OS (no pywin32/real socket needed
for these two). The module's actual Win32/socket-server behavior
(DesktopBridgeServer, spawn_desktop_viewer) is exercised indirectly via
test_windows_backend.py's resize-ownership tests and this project's own
live verification against dell-5530 (see the P0 hotfix report) --
consistent with this whole file's own "fake only the one platform-
specific primitive" discipline.
"""
from __future__ import annotations

import socket
import threading

from terminal_mcp.windows_visible_console import _parse_size, _read_line_locked


def test_parse_size_valid_pair():
    assert _parse_size("80,24") == (80, 24)


def test_parse_size_empty_string():
    assert _parse_size("") == (None, None)


def test_parse_size_missing_values():
    assert _parse_size(",") == (None, None)


def test_parse_size_non_numeric():
    assert _parse_size("abc,def") == (None, None)


def test_parse_size_rejects_zero_or_negative():
    assert _parse_size("0,24") == (None, None)
    assert _parse_size("80,0") == (None, None)
    assert _parse_size("-1,24") == (None, None)


def test_parse_size_rejects_wrong_field_count():
    assert _parse_size("80,24,1") == (None, None)
    assert _parse_size("80") == (None, None)


def _socketpair():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    accepted, _ = server.accept()
    server.close()
    return client, accepted


def test_read_line_locked_reads_one_newline_terminated_line():
    client, accepted = _socketpair()
    try:
        client.sendall(b"hello-token\n")
        assert _read_line_locked(accepted) == "hello-token"
    finally:
        client.close(); accepted.close()


def test_read_line_locked_reads_two_sequential_lines_independently():
    """The real handshake shape: token line, then a separate size line --
    must not blend the two or read past the first newline."""
    client, accepted = _socketpair()
    try:
        client.sendall(b"my-token\n80,24\n")
        first = _read_line_locked(accepted)
        second = _read_line_locked(accepted)
        assert first == "my-token"
        assert second == "80,24"
    finally:
        client.close(); accepted.close()


def test_read_line_locked_returns_none_on_closed_connection():
    client, accepted = _socketpair()
    try:
        client.close()
        assert _read_line_locked(accepted) is None
    finally:
        accepted.close()


def test_read_line_locked_strips_whitespace():
    client, accepted = _socketpair()
    try:
        client.sendall(b"  padded-token  \n")
        assert _read_line_locked(accepted) == "padded-token"
    finally:
        client.close(); accepted.close()
