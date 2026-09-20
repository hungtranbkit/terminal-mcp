"""The Harness MCP surface. Five tools, and deliberately not more.

WHY IT IS THIS SMALL

Every tool registered here is paid for on every client connection, in the
tool list the model reads before it does anything. The engine has a large
internal API; almost all of it is machinery that the engine calls on its own
behalf and that nobody should be driving by hand. What an operator or an
orchestrator actually needs is: start a run, move it, look at it, see what is
waiting on a human, and answer that. Anything beyond those five is either a
read that `status` already returns or a write that would let a caller put the
run into a state the engine did not decide on.

`step` is the only tool that can invoke a model, and it invokes exactly one
stage's worth. There is deliberately no `run_to_completion` tool: a loop that
lives on the server side runs whether or not anyone is watching, and the
engine's whole posture is that something calls it when there is a reason to
believe work has moved.

A DEPLOYMENT WITHOUT AN AGENT RUNNER IS STILL USEFUL

Until an AgentRunner is bound, `step` can still take a run from INIT to
PLAN_READY whenever the Planner is skippable -- that path is a template
substitution and needs no model. In SHADOW that is a genuinely useful thing
to be able to do: it shows exactly what contract a task WOULD be built
against, with a content hash, before anything is allowed to write anywhere.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from . import harness_policy as policy
from . import harness_scheduler as sched
from . import harness_state as state
from .harness_context import ContextAssembler
from .harness_engine import HarnessEngine
from .harness_runner import TerminalAgentRunner
from .harness_store import HarnessStore
from .harness_trust import WorkspaceTrust

#: Where an agent's structured answer is written. Outside any repository on
#: purpose: an artifact inside the worktree would show up as an untracked file
#: in the diff the Evaluator is about to judge.
DEFAULT_ARTIFACTS_ROOT = "~/.local/state/terminal-mcp/harness/artifacts"


def build_engine(store: HarnessStore, *, ops: Any = None, router: Any = None,
                 repo_root: str | None = None,
                 artifacts_root: str | None = None,
                 worktree_roots: Sequence[str] = (),
                 claude_config_path: str | None = None) -> HarnessEngine:
    """The engine the server drives, with a REAL runner when one is possible.

    `ops` is the controller (or the local terminal service) -- anything with
    the SessionOps shape. Given one, the engine can actually reach agents:
    `terminal_harness_step` will pick or spawn a session, send the prompt
    through the same guarded transport the queue uses, and resolve the answer
    from the artifact on a later step.

    Without one, the engine is still useful and still honest: it plans,
    freezes contracts and runs declared checks, and any stage that genuinely
    needs a model says so rather than pretending. That is the difference
    between a degraded surface and a lying one.
    """
    runner = None
    if ops is not None:
        # WORKSPACE TRUST. Only constructed when an operator has named the
        # worktree roots: with none, WorkspaceTrust refuses everything (the
        # empty-set reading of an allowlist is the dangerous one), so wiring
        # it would add a call that can only ever say no. Absent, the spawned
        # session asks its question and the runner reports
        # PERMISSION_REQUIRED -- which is what happens today.
        trust = None
        if worktree_roots:
            trust = WorkspaceTrust(config_path=claude_config_path, store=store,
                                   worktree_roots=tuple(worktree_roots))
        runner = TerminalAgentRunner(
            ops, store, router=router, trust=trust,
            artifacts_root=Path(artifacts_root or DEFAULT_ARTIFACTS_ROOT).expanduser())
    return HarnessEngine(store, runner=runner, repo_root=repo_root,
                         assembler=ContextAssembler(store, repo_root=repo_root))


def register_harness_tools(server: Any, store: HarnessStore, *,
                           engine: HarnessEngine | None = None,
                           ops: Any = None, router: Any = None,
                           worktree_roots: Sequence[str] = (),
                           claude_config_path: str | None = None) -> dict[str, Any]:
    """Register the surface. Returns the handlers for the compact `turn` map."""

    harness = engine or build_engine(store, ops=ops, router=router,
                                     worktree_roots=worktree_roots,
                                     claude_config_path=claude_config_path)

    @server.tool()
    def terminal_harness_start(task_id: str, prompt: str, title: str = "",
                               project_id: str | None = None,
                               acceptance: list[str] | None = None,
                               checks: list[str] | None = None,
                               changed_paths: list[str] | None = None,
                               mode: str | None = None,
                               write_authority: str = policy.SHADOW,
                               node_id: str | None = None) -> dict:
        """Open a Harness run for one task. Idempotent on the task definition.

        `write_authority` defaults to `shadow`, which writes nothing outside
        the harness tables -- no queue status, no merge, no deploy. That is
        the default because it is the setting under which this engine can be
        compared against the existing pipeline on real work without being
        able to affect it.

        `mode` may only ESCALATE. Asking for `light` on a task that touches
        auth or a migration keeps the higher mode and records the refusal.
        """
        run, created = harness.start(
            task_id=task_id, prompt=prompt, title=title, project_id=project_id,
            acceptance=tuple(acceptance or ()), checks=tuple(checks or ()),
            changed_paths=tuple(changed_paths or ()), requested_mode=mode,
            write_authority=write_authority, node_id=node_id, actor="mcp")
        return {"created": created, "run": run.to_dict()}

    @server.tool()
    def terminal_harness_step(run_id: str) -> dict:
        """Advance one run by exactly ONE stage, then return.

        Never loops and never sleeps. Whether to call it again is the
        caller's decision, and `outcome.done` says whether there is any point.
        Stages that need a model (a Planner on an under-specified task, any
        Builder, a CRITICAL Evaluator) require a configured AgentRunner and
        will say so rather than pretending to have run.
        """
        outcome = harness.step(run_id)
        dispatch = store.open_dispatch_for(run_id)
        return {"outcome": outcome.to_dict(),
                "run": store.require_run(run_id).to_dict(),
                # What is in flight, if anything. A caller that sees
                # `pending` knows to come back rather than to step again --
                # stepping a pending stage only re-reads a pane.
                "awaiting": ({"dispatch_id": dispatch["id"], "role": dispatch["role"],
                              "state": dispatch["state"],
                              "session": dispatch["session_id"],
                              "iteration": dispatch["iteration"]}
                             if dispatch else None)}

    @server.tool()
    def terminal_harness_status(run_id: str | None = None,
                                task_id: str | None = None,
                                project_id: str | None = None,
                                limit: int = 25) -> dict:
        """Everything durably known about a run, or a list of runs.

        With `run_id` this is the single read the dashboard, this tool and the
        pilot report all use, so no two surfaces can describe the same run
        differently. `projected_stage` is the label a board should render;
        it is a pure function of the engine's stage, so a board cannot show a
        state the engine does not believe in.
        """
        if run_id:
            return store.run_report(run_id)
        if task_id:
            run = store.run_for_task(task_id, active_only=False)
            return {"run": run.to_dict() if run else None}
        runs = store.list_runs(project_id=project_id, limit=limit)
        return {"runs": [run.to_dict() for run in runs],
                "stages": sorted({run.stage for run in runs})}

    @server.tool()
    def terminal_harness_decisions(project_id: str | None = None,
                                   run_id: str | None = None,
                                   decision_id: str | None = None,
                                   resolution: str | None = None,
                                   resolved_by: str | None = None) -> dict:
        """The Human Decision Queue: read it, or answer one item of it.

        The list is closed and short, and a failing test is not on it. If
        something is here, deterministic code could not have handled it: a
        missing credential, an input only a person can supply, a permission, a
        destructive action, an ambiguous product call, a loop that is not
        converging, repeated infrastructure failure, or contradictory
        architecture.

        Passing `decision_id` with a `resolution` answers it; the answer is
        durable and appears in the run's event log.
        """
        if decision_id:
            if not resolution or not resolved_by:
                raise ValueError("answering a decision needs resolution and resolved_by")
            return {"resolved": store.resolve_decision(
                decision_id, resolution=resolution, resolved_by=resolved_by)}
        return {"open": store.list_decisions(status="open", project_id=project_id,
                                             run_id=run_id),
                "reasons": list(policy.HUMAN_DECISION_REASONS)}

    @server.tool()
    def terminal_harness_plan(target: str, tasks: list[dict],
                              scheduling: str = sched.COST_FIRST,
                              satisfied: list[str] | None = None) -> dict:
        """Order a dependency graph toward one milestone. No model, no writes.

        `tasks` is a list of {id, lane, priority, depends_on, autonomous,
        not_autonomous_reason}. The answer is a topological order ranked by
        longest remaining path to the target, with one Builder session per
        lane under `cost_first`.

        A task marked `autonomous: false` is DEFERRED and never marked
        satisfied, so everything depending on it stays unstartable. That is
        the point: a task needing an input only a person can supply must not
        be completed by iterating against a bar it cannot reach.
        """
        nodes = {}
        for row in tasks:
            task_id = str(row["id"])
            nodes[task_id] = sched.TaskNode(
                id=task_id, lane=str(row.get("lane") or ""),
                priority=str(row.get("priority") or "P2"),
                depends_on=tuple(row.get("depends_on") or ()),
                autonomous=bool(row.get("autonomous", True)),
                not_autonomous_reason=row.get("not_autonomous_reason"),
                requested_mode=row.get("mode"))
        result = sched.plan(nodes, target=target, mode=scheduling,
                            satisfied=tuple(satisfied or ()))
        return result.to_dict()

    return {
        "harness_start": terminal_harness_start,
        "harness_step": terminal_harness_step,
        "harness_status": terminal_harness_status,
        "harness_decisions": terminal_harness_decisions,
        "harness_plan": terminal_harness_plan,
    }
