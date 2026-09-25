"""One-call Fast Agent Mode E2E through a real disposable local tmux session.

The small compiled fake Codex process implements only the visible composer,
busy footer, multiline paste boundary, and nonce-bound completion response.
It uses the real TerminalService, guarded Codex adapter, durable QueueStore,
CoordinatorGate, QueueEngine, CompactTerminalTools.start action, and follower.
No model API or production session is involved.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.queue_smoke


@pytest.mark.skipif(not shutil.which("cc"), reason="C compiler required for disposable TUI worker")
def test_one_turn_call_advances_real_durable_task_without_client_polling(
        tmp_path, tmux_session_factory):
    from terminal_mcp.audit import AuditStore
    from terminal_mcp.bindings import BindingStore
    from terminal_mcp.compact_tools import CompactTerminalTools
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.controller import build_default_controller
    from terminal_mcp.coordinator import CoordinatorGate
    from terminal_mcp.core import TerminalService
    from terminal_mcp.queue_engine import QueueEngine
    from terminal_mcp.queue_service import QueueService
    from terminal_mcp.queue_store import QueueStore
    from terminal_mcp.queue_task_follower import StartedTaskFollower

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fast-agent@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fast Agent E2E"], cwd=repo, check=True)
    (repo / "README.md").write_text("disposable\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    source = r'''#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
int main(void) {
  char line[8192], marker[8192] = "";
  puts("gpt-5.6 · disposable repo");
  puts("› Ask Codex to do anything");
  fflush(stdout);
  while (fgets(line, sizeof(line), stdin)) {
    if (strstr(line, "###TERMINAL_MCP_COMPLETION")) {
      snprintf(marker, sizeof(marker), "%s", line);
      char *end = strstr(marker + 3, "###");
      if (end) end[3] = '\0';
    }
    if (line[0] == '\n' && marker[0]) {
      FILE *proof = fopen("fast-agent-e2e.txt", "w");
      if (proof) { fputs("created by the disposable worker\n", proof); fclose(proof); }
      puts("Working");
      fflush(stdout);
      struct timespec pause = {0, 100000000};
      nanosleep(&pause, NULL);
      printf("\n%s\n", marker);
      puts("› Ask Codex to do anything");
      fflush(stdout);
      marker[0] = '\0';
    }
  }
  return 0;
}
'''
    c_path = tmp_path / "fake_codex.c"
    binary = tmp_path / "codex"
    c_path.write_text(source)
    subprocess.run(["cc", "-O0", "-o", str(binary), str(c_path)], check=True)
    session = "queue-smoke-fast-agent"
    tmux_session_factory(session, f"cd {repo} && exec -a codex {binary}")

    config = AppConfig(
        PermissionsConfig(True, True), ("queue-smoke-fast-agent",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("queue-smoke-fast-agent",), max_text_length=8000),
    )
    terminal = TerminalService(config,
        bindings=BindingStore(tmp_path / "bindings.db"),
        audit=AuditStore(tmp_path / "audit.db"))
    controller = build_default_controller(terminal)
    controller.refresh_local_heartbeat(tmux_session_count=1, agent_counts={"codex": 1},
                                       agent_types=("codex",), agent_version="test-double")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    engine = QueueEngine(queue.store, controller, coordinator=CoordinatorGate())
    queue.engine = engine
    follower = StartedTaskFollower(lambda lane: engine.tick(lane), queue.task_status,
                                   poll_interval_seconds=0.05, ttl_seconds=20)

    def enqueue(lane, prompt, *, title=None, priority=0, metadata=None, request_key=None):
        return queue.enqueue(lane, prompt, title=title, priority=priority,
                             metadata=metadata, request_key=request_key)

    tools = CompactTerminalTools(terminal, controller, handlers={
        "enqueue_task": enqueue, "task_status": queue.task_status,
        "dispatch_tick": lambda lane: engine.tick(lane).to_dict(),
        "follow_task": follower.follow,
    })
    receipt = tools.turn(action="start", target=session, title="Disposable E2E",
                         text="Create fast-agent-e2e.txt with the proof string.",
                         request_key="fast-agent-e2e-once")

    assert receipt["task_id"]
    assert receipt["server_side_progress"]["following"] is True
    task_id = receipt["task_id"]
    deadline = time.monotonic() + 20
    while queue.store.get_task(task_id).status not in {"COMPLETED", "BLOCKED", "FAILED"} \
            and time.monotonic() < deadline:
        time.sleep(0.05)

    task = queue.store.get_task(task_id)
    assert task.status == "COMPLETED", task.last_error
    assert (repo / "fast-agent-e2e.txt").read_text() == "created by the disposable worker\n"
    outcome = queue.task_status(task_id)["outcome"]
    assert outcome["status"] == "COMPLETE"
    assert task.attempt_count == 1
