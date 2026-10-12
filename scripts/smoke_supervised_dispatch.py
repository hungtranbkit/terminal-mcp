"""Isolated live smoke for supervised dispatch (issue #52).

Runs N real create_session -> supervise -> duplicate supervise ->
supervise_status (from a second, "new client" supervisor) -> DONE cycles
against REAL Claude/Codex CLIs in disposable tmux sessions on this host.

Isolation: refuses to run unless XDG_STATE_HOME points somewhere other than
the default (~/.local/state), so it never touches the production controller's
databases; it talks to tmux and the agent CLIs directly through
TerminalService (the same core send path the controller's local node uses),
not through the live controller/sidecar. Sessions are named
`tmcp52-smoke-<run>-<n>` and always killed afterwards.

    XDG_STATE_HOME=/tmp/x python scripts/smoke_supervised_dispatch.py \
        --cycles 20 --agent claude --cwd /path/to/trusted/repo --out evidence.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionLifecycleConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor

PREFIX = "tmcp52-smoke-"


class _Controller:
    """The three calls CompactTerminalTools needs, on the local service."""

    def __init__(self, service: TerminalService) -> None:
        self.service = service

    def terminal_status(self, session):
        return self.service.terminal_status(session)

    def terminal_tail(self, session, lines):
        return self.service.terminal_tail(session, lines)

    def terminal_send_text(self, session, text, press_enter, dry_run, **kwargs):
        return self.service.terminal_send_text(session, text, press_enter, dry_run,
                                               idempotency_key=kwargs.get("idempotency_key"))


class _Terminal:
    def terminal_get_binding(self, binding):
        return {"error": "BINDING_NOT_FOUND"}


def _supervisor(tools: CompactTerminalTools, db: Path) -> DirectTaskSupervisor:
    return DirectTaskSupervisor(
        DirectTaskStore(db),
        send=lambda t, text, key: tools.send_task(t, text, idempotency_key=key),
        status=lambda t: tools._status(t), tail=lambda t, n: tools._tail(t, n),
        activate=lambda t, key: tools.send_task(t, "", idempotency_key=key),
        accept_wait_seconds=6)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--agent", default="claude", choices=["claude", "codex"])
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--out", required=True)
    parser.add_argument("--keep", action="store_true", help="leave sessions for inspection")
    opts = parser.parse_args()

    state = os.environ.get("XDG_STATE_HOME", "")
    default = str(Path.home() / ".local" / "state")
    if not state or os.path.realpath(state) == os.path.realpath(default):
        print("refusing: set XDG_STATE_HOME to an isolated directory", file=sys.stderr)
        return 2

    config = AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=(PREFIX + "*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=(PREFIX + "*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(opts.cwd,), protected_sessions=(),
            launch_commands=(("claude", "claude"), ("codex", "codex")),
            create_ready_timeout_seconds=15.0),
    )
    service = TerminalService(config)
    tools = CompactTerminalTools(_Terminal(), _Controller(service))
    db = Path(state) / "terminal-mcp" / "smoke_direct_tasks.db"
    sup = _supervisor(tools, db)
    run = uuid.uuid4().hex[:6]
    results = []
    for n in range(1, opts.cycles + 1):
        name = f"{PREFIX}{run}-{n:02d}"
        row: dict = {"cycle": n, "session": name, "agent": opts.agent}
        t0 = time.time()
        try:
            created = service.terminal_create_session(name, opts.agent, opts.cwd,
                                                      grant_mode="read_send")
            row["create_state"] = created.get("state")
            boot = service._await_agent_composer_ready(name)
            row["boot"] = boot and {k: boot.get(k) for k in ("error", "state", "reason")}
            key = f"smoke52-{run}-{n:02d}"
            text = (f"Smoke cycle {n} of an automated Terminal MCP dispatch test. Do not run "
                    "any tools or edit files; just reply with one short sentence confirming "
                    "you received cycle " + str(n) + ".")
            first = sup.start(name, text=text, idempotency_key=key)
            task = first.get("task") or {}
            task_id = task.get("task_id")
            row.update(task_id=task_id, start_status=first.get("status"),
                       start_error=first.get("error"), start_state=task.get("state"),
                       start_receipt=task.get("receipt"), start_reason=task.get("reason"),
                       start_running_confirmed=task.get("running_confirmed"))
            dup = sup.start(name, text=text, idempotency_key=key)
            row["duplicate_same_task"] = (dup.get("deduplicated") is True
                                          and (dup.get("task") or {}).get("task_id") == task_id)
            trace = [task.get("state")]
            final = task
            deadline = time.time() + opts.timeout
            while task_id and time.time() < deadline:
                time.sleep(3)
                sup.tick()
                # Every check is made by a NEW supervisor ("new client") on the
                # same durable store, never by the one that dispatched.
                final = _supervisor(tools, db).get(task_id)["task"]
                if final["state"] != trace[-1]:
                    trace.append(final["state"])
                if final["state"] == "WAITING_INPUT":
                    # Playing the HUMAN: only Claude's onboarding upsell is
                    # dismissed (Esc = "cancel"); the supervisor itself never
                    # answers a dialog, and any other dialog is left alone.
                    pane = "\n".join(service.tmux.capture_lines(name, 40))
                    if "Teach auto mode about your environment" in pane:
                        service.tmux.send_keys(name, ["Escape"])
                        row.setdefault("human_actions", []).append("Esc on auto-mode upsell")
                if final["completed_at"] is not None:
                    break
            row.update(final_state=final.get("state"), final_receipt=final.get("receipt"),
                       trace=trace, dispatches=final.get("dispatches"),
                       continuations=final.get("continuations"),
                       recovery_attempts=final.get("recovery_attempts"),
                       reason=final.get("reason"))
            pane = "\n".join(service.tmux.capture_lines(name, 400)) if task_id else ""
            needle = f"supervised task {task_id}]" if task_id else "-"
            row["prompt_occurrences_in_pane"] = pane.count(needle)
        except Exception as exc:  # noqa: BLE001 -- record and continue
            row["exception"] = f"{type(exc).__name__}: {exc}"
        finally:
            row["seconds"] = round(time.time() - t0, 1)
            row["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0))
            try:
                if not opts.keep:
                    service.tmux.kill_session(name)
            except Exception:  # noqa: BLE001
                pass
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {
        "run": run, "cycles": len(results),
        "done": sum(1 for r in results if r.get("final_state") == "DONE"),
        "lost_tasks": sum(1 for r in results if not r.get("task_id")),
        "duplicate_ok": sum(1 for r in results if r.get("duplicate_same_task")),
        "duplicate_sends": sum(1 for r in results if (r.get("dispatches") or 0) > 1
                               and r.get("continuations", 0) == 0),
        "false_running_at_start": sum(1 for r in results if r.get("start_state") == "RUNNING"
                                      and not r.get("start_running_confirmed")),
    }
    Path(opts.out).write_text(json.dumps({"summary": summary, "results": results}, indent=2,
                                         ensure_ascii=False))
    print(json.dumps(summary))
    return 0 if summary["done"] == summary["cycles"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
