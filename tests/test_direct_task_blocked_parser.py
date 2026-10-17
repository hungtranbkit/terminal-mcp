"""Supervisor: wrapped BLOCKED reasons and agent usage limits.

A long ``TMCP-BLOCKED`` reason is hard-wrapped by the pane and used to be cut
at the first row, losing who must act. An agent CLI that hit its provider
usage limit answered every continuation with the same notice until the task
ended as a misleading ``CONTINUATION_LIMIT_REACHED``; it is now BLOCKED with
the notice as the reason and nothing more is sent.
"""
from __future__ import annotations

from terminal_mcp.direct_task import find_marker, usage_limit_notice

from tests.test_direct_task_lifecycle import Clock, FakePane, _run, _supervisor

TASK_ID = "dt_0123456789abcdef"


def test_wrapped_blocked_reason_is_folded_back():
    pane = (f"⏺ Summary of work.\n\n"
            f"  TMCP-BLOCKED:{TASK_ID} no SSH access from m910 to HP; the owner\n"
            f"  must authorize the controller key on kimex@hp-linux\n\n"
            f"╭──────╮\n│ ❯    │\n╰──────╯")
    assert find_marker(pane, TASK_ID) == (
        "BLOCKED", "no SSH access from m910 to HP; the owner must authorize the "
        "controller key on kimex@hp-linux")


def test_reason_folding_stops_at_next_output_and_is_bounded():
    pane = f"• TMCP-BLOCKED:{TASK_ID} need creds\n• Next bullet\n"
    assert find_marker(pane, TASK_ID) == ("BLOCKED", "need creds")
    long = f"TMCP-BLOCKED:{TASK_ID} a\n" + "\n".join(f"  w{i}" for i in range(20))
    assert find_marker(long, TASK_ID) == ("BLOCKED", "a w0 w1 w2 w3 w4 w5")


def test_done_marker_never_absorbs_following_rows():
    assert find_marker(f"⏺ TMCP-DONE:{TASK_ID}\n  trailing text", TASK_ID) == ("DONE", "")


def test_markdown_wrapped_markers():
    assert find_marker(f"  `TMCP-DONE:{TASK_ID}`", TASK_ID) == ("DONE", "")
    assert find_marker(f"  **TMCP-BLOCKED:{TASK_ID}** need approval", TASK_ID) == (
        "BLOCKED", "need approval")
    # The composer echo prefix stays excluded.
    assert find_marker(f"> TMCP-DONE:{TASK_ID}", TASK_ID) is None


def test_usage_limit_notice_only_after_latest_dispatch():
    needle = f"{TASK_ID}, continuation 2/"
    codex = ("> old prompt\n■ You've hit your usage limit. Try again at 3:05 PM.\n"
             f"> [Terminal MCP supervised task {needle}6] keep going\n• Working on it\n❯ ")
    assert usage_limit_notice(codex, needle) is None, "an older notice must not count"
    codex += ("\n■ You've hit your usage limit. Upgrade to Pro or try again at 9:00 PM.\n❯ ")
    codex = codex.replace("❯ \n", "", 1)
    assert usage_limit_notice(codex, needle) == (
        "You've hit your usage limit. Upgrade to Pro or try again at 9:00 PM.")
    claude = f"> {needle}6]\n  ⎿  Claude usage limit reached. Your limit will reset at 5pm\n❯ "
    assert usage_limit_notice(claude, needle).startswith("Claude usage limit reached")
    prose = f"> {needle}6]\n⏺ The HP Codex session said it hit its usage limit earlier.\n❯ "
    assert usage_limit_notice(prose, needle) is None


def test_usage_limit_blocks_instead_of_burning_continuations(tmp_path):
    clock = Clock()
    pane = FakePane(agent="codex", script=lambda n, text: (
        "■ You've hit your usage limit. Try again at 9:00 PM." if n >= 2 else "• Did part of it"))
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("hp-codex", text="release", max_continuations=6)["task"]["task_id"]
    _run(sup, clock, 80)
    task = sup.get(task_id, refresh=False)["task"]
    assert task["state"] == "BLOCKED"
    assert task["reason"].startswith("AGENT_USAGE_LIMIT: You've hit your usage limit")
    assert len(pane.sent) == 2, "nothing is sent after the limit notice"
    _run(sup, clock, 20)
    assert len(pane.sent) == 2
