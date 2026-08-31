from terminal_mcp.models import SessionInfo
from terminal_mcp.status import classify_status, detect_waiting_input


def info(command="bash", activity=100, dead=False):
    return SessionInfo("test-x", False, 1, 1, activity, 123, command, dead)


def test_waiting_prompt_near_bottom():
    waiting, reason = detect_waiting_input("work\nDo you want to continue? [y/N]")
    assert waiting
    assert "matched" in reason


def test_old_prompt_not_false_positive():
    output = "Do you want to continue? [y/N]\n" + "\n".join(f"line {i}" for i in range(20))
    assert not detect_waiting_input(output)[0]


def test_running_and_idle_classification():
    assert classify_status(info("python", 95), "working", now=100)[0] == "RUNNING"
    assert classify_status(info("bash", 1), "$", now=100)[0] == "IDLE"

