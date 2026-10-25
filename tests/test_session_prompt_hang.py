"""Regressions for sessions that silently hung on prompts (2026-10-25).

* Codex keys folder trust on the repository root, so a fresh managed repo or
  worktree opened on a modal nobody may answer (codex_trust.py fixes it
  before launch, for operator-approved roots only).
* The wrapped trust modal was not recognized as input, so the pane read
  RUNNING/IDLE instead of WAITING_INPUT.
* Sandbox command approval had no name of its own, so a waiting task gave no
  concrete reason or action.
* RemoteNodeClient refused every idle-guarded send (IDLE_GUARD_UNSUPPORTED)
  before any HTTP request, so a remote direct start could never reach the node
  (fixed with PR #79's /send-guarded route).
* `TMCP-DONE: <id>` (space after the colon) was not a marker, so a finished
  task failed at the continuation limit.

The two pane fixtures are real codex-cli 0.162.0 captures at 120 columns.
"""
from __future__ import annotations

import os
import subprocess
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp import adapters, codex_trust, status
from terminal_mcp.direct_task import completion_instruction, continuation_prompt, find_marker

FIXTURES = Path(__file__).parent / "fixtures"
TRUST = (FIXTURES / "codex_0162_folder_trust_worktree_120col.txt").read_text()
APPROVAL = (FIXTURES / "codex_0162_sandbox_approval_120col.txt").read_text()
UPDATE = (FIXTURES / "codex_0162_update_menu_80col.txt").read_text()
TASK_ID = "dt_9907a6a3c68f445b"


# -- (4) completion marker ----------------------------------------------------

@pytest.mark.parametrize("line", [
    f"• TMCP-DONE: {TASK_ID}",           # the exact line Codex printed live
    f"TMCP-DONE:{TASK_ID}",
    f"● TMCP-DONE:\t{TASK_ID}",
    f"  TMCP-DONE:   {TASK_ID}  ",
])
def test_done_marker_accepts_whitespace_after_colon(line):
    assert find_marker(f"work\n{line}\n", TASK_ID) == ("DONE", "")


def test_blocked_marker_with_space_keeps_reason():
    out = f"• TMCP-BLOCKED: {TASK_ID} authenticated Cloudflare Access session\n  required\n"
    assert find_marker(out, TASK_ID) == ("BLOCKED", "authenticated Cloudflare Access session required")


@pytest.mark.parametrize("line", [
    f"TMCP-DONE: {TASK_ID}0",            # a longer id is a different task
    f"TMCP-DONE:\n{TASK_ID}",            # the id on another row is prose
    f"> TMCP-DONE: {TASK_ID}",           # a composer echo is never a marker
])
def test_marker_still_rejects_non_markers(line):
    assert find_marker(line, TASK_ID) is None


def test_instructions_echoed_in_the_pane_never_complete_the_task():
    echoed = completion_instruction(TASK_ID) + "\n" + continuation_prompt(TASK_ID, 6, 6)
    assert find_marker(echoed, TASK_ID) is None


# -- (2) prompt classification -------------------------------------------------

def test_wrapped_codex_folder_trust_modal_is_waiting_input():
    lines = TRUST.splitlines()
    assert adapters.codex_folder_trust_prompt(lines)
    waiting, reason = status.detect_waiting_input(TRUST)
    assert waiting and reason.startswith("CODEX_FOLDER_TRUST")
    info = SimpleNamespace(pane_current_command="codex", pane_dead=False, activity_epoch=0)
    assert status.classify_status(info, TRUST, now=10)[0] == "WAITING_INPUT"
    assert adapters.classify_input_prompt(lines)["kind"] == "CODEX_FOLDER_TRUST"


def test_sandbox_approval_is_named_separately_from_folder_trust():
    lines = APPROVAL.splitlines()
    assert adapters.codex_sandbox_approval_prompt(lines) == "touch approval-probe.txt"
    assert not adapters.codex_folder_trust_prompt(lines)
    prompt = adapters.classify_input_prompt(lines)
    assert prompt["kind"] == "CODEX_SANDBOX_APPROVAL"
    assert "touch approval-probe.txt" in prompt["detail"]
    assert "never answers" in prompt["next_action"]
    waiting, reason = status.detect_waiting_input(APPROVAL)
    assert waiting and reason.startswith("CODEX_SANDBOX_APPROVAL")


def test_codex_update_menu_is_waiting_input_not_unknown():
    info = SimpleNamespace(pane_current_command="codex", pane_dead=False, activity_epoch=0)
    state, waiting, reason = status.classify_status(info, UPDATE, now=3)
    assert (state, waiting) == ("WAITING_INPUT", True) and reason.startswith("CODEX_UPDATE_MENU")
    assert "never selects Update now" in adapters.classify_input_prompt(UPDATE.splitlines())["next_action"]


def test_answered_approval_left_in_scrollback_is_not_a_live_prompt():
    answered = APPROVAL + "\n".join(["• Ran touch approval-probe.txt"] + ["  ok"] * 24
                                    + ["› Ask Codex to do anything"])
    assert adapters.codex_sandbox_approval_prompt(answered.splitlines()) is None


def test_startup_readiness_blocks_on_sandbox_approval_not_trust():
    from terminal_mcp.lifecycle import _codex_readiness
    assert _codex_readiness(APPROVAL.splitlines()) == ("BLOCKED_PERMISSION", "CODEX_SANDBOX_APPROVAL")
    assert _codex_readiness(TRUST.splitlines()) == ("BLOCKED", "CODEX_TRUST_PROMPT")


def test_supervised_task_reports_named_prompt_then_resumes_after_explicit_approval(tmp_path):
    from tests.test_direct_task_lifecycle import Clock, FakePane, _run, _supervisor

    clock = Clock()
    pane = FakePane(agent="codex")
    prompt = adapters.classify_input_prompt(APPROVAL.splitlines())
    real_status = pane.status
    pane.status = lambda target: ({"state": "WAITING_INPUT", "input_required": True,
                                   "reason": "approval", "input_prompt": prompt}
                                  if pane.waiting_input else real_status(target))
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    _run(sup, clock, 1)
    pane.waiting_input = True
    _run(sup, clock, 10)
    task = sup.get(task_id, refresh=False)["task"]
    assert task["state"] == "WAITING_INPUT"
    assert task["wait_reason"] == "SESSION_INPUT"     # stable value kept
    assert task["input_prompt_kind"] == "CODEX_SANDBOX_APPROVAL"
    assert task["reason"].startswith("CODEX_SANDBOX_APPROVAL: ")
    assert "touch approval-probe.txt" in task["reason"]
    assert task["next_action"] == adapters.INPUT_PROMPT_ACTIONS["CODEX_SANDBOX_APPROVAL"]
    assert len(pane.sent) == 1, "nothing may be typed over an approval prompt"
    pane.waiting_input = False   # the owner answered in the session
    pane.busy = 2
    _run(sup, clock, 1)
    resumed = sup.get(task_id, refresh=False)["task"]
    assert resumed["state"] == "RUNNING" and resumed["input_prompt_kind"] is None


# -- (3) guarded remote submission ----------------------------------------------
# The route itself comes from PR #79 (tests in test_codex_direct_dispatch.py and
# test_node_agent.py); these pin the two properties the live failure needed.

def test_guarded_route_passes_the_idle_guard_to_the_owning_node(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.node_agent import build_node_agent
    from tests.test_node_agent import TOKEN, _config

    terminal = TerminalService(_config(tmp_path), grants=SessionGrantStore(tmp_path / "g.db"))
    calls = []
    monkeypatch.setattr(terminal, "terminal_send_text", lambda *a, **kw: calls.append(kw) or {
        "error": "DIRECT_TARGET_NOT_IDLE", "sent": False, "enter_sent": False})
    client = TestClient(build_node_agent(node_id="n", terminal=terminal, token=TOKEN,
                                         workspace_root=str(tmp_path)))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = {"text": "t", "press_enter": True, "idempotency_key": "k"}
    refused = client.post("/v1/sessions/agent-x/send-guarded", headers=auth, json=body).json()
    assert calls[-1]["require_idle"] is True and refused["error"] == "DIRECT_TARGET_NOT_IDLE"
    client.post("/v1/sessions/agent-x/send", headers=auth, json={**body, "require_idle": True})
    assert calls[-1]["require_idle"] is False, "the legacy route never claims to guard"


def test_remote_direct_start_is_no_longer_refused_before_reaching_the_node():
    from terminal_mcp.node_client import RemoteNodeClient

    class Node(RemoteNodeClient):
        paths: list[str] = []

        def _request(self, method, path, *, body=None, params=None, timeout_seconds=None):
            self.paths.append(path)
            return {"delivery_state": "SUBMIT_CONFIRMED", "enter_sent": True}

    node = Node("http://node.test:8790", "inert-test-token")
    result = node.send_text("codex", "inert", True, require_idle=True, idempotency_key="once")
    assert result.get("error") != "IDLE_GUARD_UNSUPPORTED"
    assert node.paths == ["/v1/sessions/codex/send-guarded"]


# -- (1) prelaunch managed-workspace trust ----------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir()
    original = 'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n\n[projects."/x"]\ntrust_level = "trusted"\n'
    config.write_text(original)
    return SimpleNamespace(root=root, config=config, original=original)


def _projects(config: Path) -> dict:
    return tomllib.loads(config.read_text()).get("projects", {})


def test_fresh_repo_is_registered_and_unrelated_settings_are_preserved(ws):
    repo = _repo(ws.root / "app")
    result = codex_trust.ensure_trust(repo / ".", [str(ws.root)], config_path=ws.config)
    assert result["status"] == codex_trust.REGISTERED and result["path"] == str(repo)
    text = ws.config.read_text()
    assert text.startswith(ws.original.rstrip("\n"))
    parsed = tomllib.loads(text)
    assert parsed["approval_policy"] == "on-request" and parsed["sandbox_mode"] == "workspace-write"
    assert parsed["projects"][str(repo)] == {"trust_level": "trusted"}
    again = codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)
    assert again["status"] == codex_trust.ALREADY_TRUSTED and ws.config.read_text() == text


def test_worktree_trusts_the_main_repository_root(ws):
    repo = _repo(ws.root / "app")
    _git("worktree", "add", "-q", "-b", "feature", str(ws.root / "app-wt"), cwd=repo)
    result = codex_trust.ensure_trust(ws.root / "app-wt", [str(ws.root)], config_path=ws.config)
    assert result["status"] == codex_trust.REGISTERED and result["path"] == str(repo)
    assert str(ws.root / "app-wt") not in _projects(ws.config)


@pytest.mark.parametrize("case", ["root_itself", "outside", "symlink_cwd", "worktree_of_outside"])
def test_never_trusts_the_root_an_outside_path_or_a_symlink_escape(ws, tmp_path, case):
    outside = _repo(tmp_path / "outside")
    if case == "root_itself":
        _git("init", "-q", cwd=ws.root)
        _git("commit", "-q", "--allow-empty", "-m", "i", cwd=ws.root)
        cwd = ws.root
    elif case == "outside":
        cwd = outside
    elif case == "symlink_cwd":
        (ws.root / "link").symlink_to(outside)
        cwd = ws.root / "link"
    else:
        _git("worktree", "add", "-q", "-b", "w", str(ws.root / "wt"), cwd=outside)
        cwd = ws.root / "wt"
    result = codex_trust.ensure_trust(cwd, [str(ws.root)], config_path=ws.config)
    assert result["status"] == codex_trust.REFUSED
    assert ws.config.read_text() == ws.original


def test_unsafe_roots_are_ignored(ws, tmp_path, monkeypatch):
    repo = _repo(ws.root / "app")
    (tmp_path / "rootlink").symlink_to(ws.root)
    for root in ("/", str(tmp_path / "rootlink")):
        assert codex_trust.ensure_trust(repo, [root], config_path=ws.config)["status"] == codex_trust.REFUSED
    monkeypatch.setattr(codex_trust, "service_home", lambda: ws.root)
    assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REFUSED
    monkeypatch.undo()
    monkeypatch.setattr(codex_trust, "_private_group", lambda gid: False)
    ws.root.chmod(0o775)
    try:
        assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REFUSED
    finally:
        ws.root.chmod(0o755)
    monkeypatch.undo()
    ws.root.chmod(0o777)
    try:
        assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REFUSED
    finally:
        ws.root.chmod(0o755)
    assert ws.config.read_text() == ws.original


def test_root_writable_only_by_the_users_private_group_is_accepted(ws, monkeypatch):
    repo = _repo(ws.root / "app")
    monkeypatch.setattr(codex_trust, "_private_group", lambda gid: True)
    ws.root.chmod(0o775)
    try:
        assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REGISTERED
    finally:
        ws.root.chmod(0o755)


def test_owner_decisions_and_broken_configs_are_never_overwritten(ws, tmp_path):
    repo = _repo(ws.root / "app")
    explicit = ws.original + f'\n[projects."{repo}"]\ntrust_level = "untrusted"\n'
    ws.config.write_text(explicit)
    assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REFUSED
    assert ws.config.read_text() == explicit
    ws.config.write_text("not = [valid")
    assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.REFUSED
    assert ws.config.read_text() == "not = [valid"
    real = tmp_path / "real.toml"
    real.write_text(ws.original)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    assert codex_trust.ensure_trust(repo, [str(ws.root)], config_path=link)["status"] == codex_trust.REFUSED
    assert link.is_symlink() and real.read_text() == ws.original


def test_feature_off_and_non_git_paths_write_nothing(ws):
    plain = ws.root / "plain"
    plain.mkdir()
    assert codex_trust.ensure_trust(plain, [], config_path=ws.config)["status"] == codex_trust.NOT_CONFIGURED
    assert codex_trust.ensure_trust(plain, [str(ws.root)], config_path=ws.config)["status"] == codex_trust.NOT_A_REPO
    assert ws.config.read_text() == ws.original


def test_concurrent_registrations_are_serialized_and_all_kept(ws):
    repos = [_repo(ws.root / f"r{i}") for i in range(8)]
    results = []
    threads = [threading.Thread(target=lambda r=r: results.append(
        codex_trust.ensure_trust(r, [str(ws.root)], config_path=ws.config))) for r in repos]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {r["status"] for r in results} == {codex_trust.REGISTERED}
    projects = _projects(ws.config)
    assert all(str(r) in projects for r in repos) and "/x" in projects


def test_config_path_follows_codex_home_of_the_service_process(tmp_path):
    assert codex_trust.codex_config_path({"CODEX_HOME": str(tmp_path)}) == tmp_path / "config.toml"
    assert codex_trust.codex_config_path({}) == codex_trust.service_home() / ".codex" / "config.toml"


def test_lifecycle_registers_trust_before_launch_on_create(tmp_path, monkeypatch):
    from terminal_mcp.core import TerminalService
    from tests.test_session_lifecycle import _lifecycle_config
    import dataclasses

    monkeypatch.setattr("terminal_mcp.lifecycle.resolve_launcher", lambda command: command)
    monkeypatch.setattr("terminal_mcp.lifecycle._codex_supports_no_daemon", lambda command: False)
    config = _lifecycle_config(tmp_path, timeout=0.3)
    config = dataclasses.replace(config, session_lifecycle=dataclasses.replace(
        config.session_lifecycle, codex_trust_roots=(str(tmp_path),)))
    service = TerminalService(config)
    order = []
    monkeypatch.setattr("terminal_mcp.lifecycle.ensure_trust",
                        lambda cwd, roots: order.append(("trust", str(cwd), roots))
                        or {"status": "REGISTERED", "path": str(cwd)})
    info = SimpleNamespace(pane_dead=False, pane_current_command="codex", session_id="$1",
                           pane_id="%1", created_epoch=1)
    state = {"exists": False}
    monkeypatch.setattr(service.tmux, "get_session", lambda _n: info if state["exists"] else None)
    monkeypatch.setattr(service.tmux, "new_session",
                        lambda *a, **kw: order.append(("launch",)) or state.update(exists=True) or (False, None))
    monkeypatch.setattr(service.tmux, "capture_lines", lambda *a, **kw: ["› Ask Codex to do anything"])
    result = service.lifecycle.create("codex-lc-trust", "codex", str(tmp_path))
    assert [step[0] for step in order] == ["trust", "launch"]
    assert order[0][2] == (str(tmp_path),)
    assert result["codex_trust"]["status"] == "REGISTERED"
    # A plain shell and a node without roots never touch Codex config.
    order.clear()
    state["exists"] = False
    plain = TerminalService(_lifecycle_config(tmp_path, timeout=0.3))
    monkeypatch.setattr(plain.tmux, "get_session", lambda _n: info if state["exists"] else None)
    monkeypatch.setattr(plain.tmux, "new_session",
                        lambda *a, **kw: order.append(("launch",)) or state.update(exists=True) or (False, None))
    monkeypatch.setattr(plain.tmux, "capture_lines", lambda *a, **kw: ["› Ask Codex to do anything"])
    assert "codex_trust" not in plain.lifecycle.create("codex-lc-trust2", "codex", str(tmp_path))
    assert order == [("launch",)]


def test_config_rejects_unsafe_trust_roots(tmp_path):
    from terminal_mcp.config import load_config
    base = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text()
    for bad in ('["relative/path"]', '["/"]', '"/home/x"'):
        path = tmp_path / "c.yaml"
        path.write_text(base + f"\nsession_lifecycle:\n  enabled: true\n  allowed_cwd_roots: [{tmp_path}]\n"
                        f"  codex_trust_roots: {bad}\n")
        with pytest.raises(ValueError, match="codex_trust_roots"):
            load_config(path)


# -- (3b) a slow remote create is never reported as an unreachable node -----------

class _SlowCreateClient:
    """Forwards to the real local client, but the create answer arrives late."""

    def __init__(self, inner, *, created: bool):
        self._inner, self._created = inner, created

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def create_session(self, *args, **kwargs):
        from terminal_mcp.node_client import NodeClientError
        if self._created:
            self._inner.create_session(*args, **kwargs)
        raise NodeClientError("POST /v1/sessions -> TimeoutError: timed out")


@pytest.fixture
def owned_session_name():
    import uuid
    from tests.conftest import tmux_cmd
    name = f"ctrl-slow-create-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run([*tmux_cmd(), "kill-session", "-t", name], check=False, capture_output=True)


@pytest.mark.parametrize("created", [True, False])
def test_timed_out_create_is_reconciled_with_the_node(tmp_path, created, owned_session_name):
    from tests.test_controller import _controller, _heartbeat_local
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    node_id = controller.local_node_id
    controller._clients[node_id] = _SlowCreateClient(controller._clients[node_id], created=created)
    result = controller.terminal_create_session(owned_session_name, "shell", str(tmp_path), node=node_id)
    assert result.get("error") != "NODE_UNREACHABLE"
    if created:
        assert result["state"] == "CREATED_UNCONFIRMED" and result["session_created"] is True
        assert "never recreate" in result["next_action"]
        assert controller.resolve_session(owned_session_name)["node_id"] == node_id
    else:
        assert result["error"] == "CREATE_OUTCOME_UNKNOWN"
        assert "Never recreate blindly" in result["next_action"]


def test_connect_failure_is_still_unreachable(tmp_path):
    from terminal_mcp.node_client import NodeClientError
    from tests.test_controller import _controller, _heartbeat_local
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    node_id = controller.local_node_id

    class Refused(_SlowCreateClient):
        def create_session(self, *a, **kw):
            raise NodeClientError("POST /v1/sessions -> URLError: [Errno 111] Connection refused")

    controller._clients[node_id] = Refused(controller._clients[node_id], created=False)
    assert controller.terminal_create_session("ctrl-refused", "shell", str(tmp_path),
                                              node=node_id)["error"] == "NODE_UNREACHABLE"


def test_no_daemon_probe_timeout_is_not_cached(monkeypatch):
    from terminal_mcp import lifecycle
    calls = []

    def run(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("codex", 3)
        return SimpleNamespace(returncode=0, stdout="      --no-daemon\n", stderr="")

    lifecycle._codex_supports_no_daemon.cache_clear()
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle._codex_supports_no_daemon("codex-x") is False   # loaded node: timed out
    assert lifecycle._codex_supports_no_daemon("codex-x") is True    # retried, then cached
    assert lifecycle._codex_supports_no_daemon("codex-x") is True and len(calls) == 2
    lifecycle._codex_supports_no_daemon.cache_clear()
