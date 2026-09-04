import pytest

from terminal_mcp.tmux import TmuxError, parse_session_line


def test_parse_tmux_session_line():
    item = parse_session_line("test-one|0|2|100|120|999|bash|0|$3|%7|0|/home/dell/workspace")
    assert item.name == "test-one"
    assert item.windows == 2
    assert not item.attached
    assert item.pane_current_command == "bash"
    assert item.session_id == "$3"
    assert item.pane_id == "%7"
    assert item.pane_in_mode is False
    assert item.pane_current_path == "/home/dell/workspace"


def test_parse_tmux_session_line_pane_in_mode():
    # P0 audit finding #14: copy-mode/other tmux client mode is parsed too.
    item = parse_session_line("test-one|0|2|100|120|999|bash|0|$3|%7|1|/tmp")
    assert item.pane_in_mode is True


def test_parser_rejects_bad_shape():
    with pytest.raises(TmuxError):
        parse_session_line("bad|shape")
