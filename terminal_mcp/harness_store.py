"""Durable persistence for the Harness engine, inside the canonical queue db.

WHY THERE IS NO `HarnessStore(path=...)` DEFAULT THAT POINTS SOMEWHERE ELSE

`default_queue_db_path()` is the only default. Passing a different path is
something tests do; production has one database, and the harness tables live
in it beside `queue_tasks`. See harness_schema's module docstring for why that
decision is the load-bearing one in this feature.

WHAT THIS MODULE REFUSES TO DO

* It has no `update_event`. `harness_events` is append-only, and the absence
  of the method is the enforcement -- a rule that exists only in a docstring
  is a rule that gets broken during an incident.
* It will not write a stage transition the state machine does not have an edge
  for. `advance()` calls `require_transition` INSIDE the write transaction, so
  a caller racing another caller cannot slip an illegal stage in between the
  check and the write.
* It will not open a human decision for a reason outside the closed list.
  `require_human_reason` raises, and it raises here rather than at the UI
  boundary, because the UI is not the only caller.
* It will not overwrite a frozen contract. A redefine writes a NEW version
  row; the old one stays, still joined to the iterations that ran against it.

CONCURRENCY POSTURE

Same as QueueStore: one SQLite file, WAL, short transactions, and every
read-modify-write done in a single `with self._connection()` block so the
read and the write are in the same transaction. Runs are additionally guarded
by a lease (`lease_owner`/`lease_expires_at`) so two engines on two nodes
cannot both drive the same run -- the lease is advisory for reads and
required for `advance()` when the caller passes an owner.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import harness_policy as policy
from . import harness_state as state
from .harness_contract import (EvaluationResult, ExecutionContract, iso_now,
                               new_id)
from .queue_store import QUEUE_MIGRATIONS, default_queue_db_path
from .schema import apply_migrations

#: The canonical ladder, which ALREADY ENDS with the harness migrations --
#: queue_store.py appends them there. Aliased rather than re-composed here so
#: there is one list; composing a second one is how the two stores would come
#: to disagree about what "migrated" means.
ALL_MIGRATIONS = QUEUE_MIGRATIONS


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unjson(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _tuple(raw: str | None) -> tuple[str, ...]:
    value = _unjson(raw, [])
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


class RunNotFound(LookupError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no harness run {run_id}")


class LeaseNotHeld(RuntimeError):
    """Someone else is driving this run.

    Raised instead of overwriting, because the alternative -- last writer
    wins -- is how two engines on two nodes both advance the same run and
    each records half the event log.
    """

    def __init__(self, run_id: str, owner: str | None, holder: str | None) -> None:
        self.run_id, self.owner, self.holder = run_id, owner, holder
        super().__init__(
            f"run {run_id} is held by {holder or '(nobody)'}, not {owner or '(nobody)'}")


@dataclass
class HarnessRun:
    """One attempt to satisfy one ExecutionContract."""

    id: str
    task_id: str
    project_id: str | None = None
    title: str = ""
    prompt: str = ""
    mode: str = policy.STANDARD
    stage: str = state.INIT
    status: str = "active"
    write_authority: str = policy.SHADOW
    policy: dict[str, Any] = field(default_factory=dict)
    planner_agent: str | None = None
    builder_agent: str | None = None
    evaluator_agent: str | None = None
    node_id: str | None = None
    builder_session_id: str | None = None
    evaluator_session_id: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    base_commit: str | None = None
    result_commit: str | None = None
    max_iterations: int = 3
    current_iteration: int = 0
    request_key: str | None = None
    definition_hash: str | None = None
    contract_request_key: str | None = None
    contract_id: str | None = None
    contract_hash: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    resume_stage: str | None = None
    infra_failure_count: int = 0
    last_checkpoint_id: str | None = None
    soft_token_budget: int | None = None
    tokens_spent_estimate: int = 0
    cost_policy_decision: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    blocked_reason: str | None = None
    shadow_of_task_status: str | None = None
    created_at: str = ""
    started_at: str | None = None
    updated_at: str = ""
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HarnessRun":
        return cls(
            id=row["id"], task_id=row["task_id"], project_id=row["project_id"],
            title=row["title"] or "", prompt=row["prompt"] or "",
            mode=row["mode"], stage=row["stage"], status=row["status"],
            write_authority=row["write_authority"],
            policy=_unjson(row["policy"], {}) or {},
            planner_agent=row["planner_agent"], builder_agent=row["builder_agent"],
            evaluator_agent=row["evaluator_agent"], node_id=row["node_id"],
            builder_session_id=row["builder_session_id"],
            evaluator_session_id=row["evaluator_session_id"],
            branch=row["branch"], worktree_path=row["worktree_path"],
            base_commit=row["base_commit"], result_commit=row["result_commit"],
            max_iterations=row["max_iterations"],
            current_iteration=row["current_iteration"],
            request_key=row["request_key"], definition_hash=row["definition_hash"],
            contract_request_key=row["contract_request_key"],
            contract_id=row["contract_id"], contract_hash=row["contract_hash"],
            lease_owner=row["lease_owner"], lease_expires_at=row["lease_expires_at"],
            resume_stage=row["resume_stage"],
            infra_failure_count=row["infra_failure_count"],
            last_checkpoint_id=row["last_checkpoint_id"],
            soft_token_budget=row["soft_token_budget"],
            tokens_spent_estimate=row["tokens_spent_estimate"],
            cost_policy_decision=_unjson(row["cost_policy_decision"], {}) or {},
            last_error=row["last_error"], blocked_reason=row["blocked_reason"],
            shadow_of_task_status=row["shadow_of_task_status"],
            created_at=row["created_at"], started_at=row["started_at"],
            updated_at=row["updated_at"], completed_at=row["completed_at"],
        )

    @property
    def projected_stage(self) -> str:
        return state.project_stage(self.stage)

    @property
    def is_shadow(self) -> bool:
        return self.write_authority == policy.SHADOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "task_id": self.task_id, "project_id": self.project_id,
            "title": self.title, "mode": self.mode, "stage": self.stage,
            "projected_stage": self.projected_stage, "status": self.status,
            "write_authority": self.write_authority, "policy": self.policy,
            "planner_agent": self.planner_agent, "builder_agent": self.builder_agent,
            "evaluator_agent": self.evaluator_agent, "node_id": self.node_id,
            "builder_session_id": self.builder_session_id,
            "evaluator_session_id": self.evaluator_session_id,
            "branch": self.branch, "worktree_path": self.worktree_path,
            "base_commit": self.base_commit, "result_commit": self.result_commit,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "contract_id": self.contract_id, "contract_hash": self.contract_hash,
            "resume_stage": self.resume_stage,
            "infra_failure_count": self.infra_failure_count,
            "last_checkpoint_id": self.last_checkpoint_id,
            "soft_token_budget": self.soft_token_budget,
            "tokens_spent_estimate": self.tokens_spent_estimate,
            "cost_policy_decision": self.cost_policy_decision,
            "last_error": self.last_error, "blocked_reason": self.blocked_reason,
            "created_at": self.created_at, "started_at": self.started_at,
            "updated_at": self.updated_at, "completed_at": self.completed_at,
        }


#: Columns `advance()` and `patch_run()` are allowed to write. An explicit
#: allowlist because the alternative -- interpolating whatever keys a caller
#: passed -- is how a typo silently becomes a no-op UPDATE.
_PATCHABLE: frozenset[str] = frozenset({
    "title", "prompt", "mode", "write_authority", "planner_agent",
    "builder_agent", "evaluator_agent", "node_id", "builder_session_id",
    "evaluator_session_id", "branch", "worktree_path", "base_commit",
    "result_commit", "max_iterations", "current_iteration", "contract_id",
    "contract_hash", "contract_request_key", "resume_stage",
    "infra_failure_count", "last_checkpoint_id", "soft_token_budget",
    "tokens_spent_estimate", "last_error", "blocked_reason",
    "shadow_of_task_status", "started_at", "project_id",
})
#: Patchable, but stored as JSON.
_PATCHABLE_JSON: frozenset[str] = frozenset({"policy", "cost_policy_decision"})

#: Efficiency counters that accumulate. `bump_efficiency` adds; nothing
#: overwrites them, because a counter that can be reset is a counter nobody
#: can reason about after the fact.
_EFFICIENCY_COUNTERS: tuple[str, ...] = (
    "llm_calls", "prompt_tokens_estimate", "context_bytes",
    "reused_context_hits", "context_cache_misses", "planner_skipped",
    "evaluator_skipped", "session_reused", "sessions_spawned",
    "rollover_count", "delta_prompts", "full_prompts", "skills_injected",
    "skill_bytes",
)


class HarnessStore:
    """The durable half of the engine. Deterministic, no LLM, no loop."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_queue_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, ALL_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    # -- plumbing ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- events (append only; there is deliberately no update path) ----------
    def _event_locked(self, connection: sqlite3.Connection, run_id: str, *,
                      event_type: str, from_stage: str | None = None,
                      to_stage: str | None = None, iteration: int | None = None,
                      actor: str | None = None, reason: str | None = None,
                      metadata: Any = None) -> None:
        connection.execute(
            "INSERT INTO harness_events (run_id, timestamp, event_type, from_stage, "
            "to_stage, iteration, actor, reason, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, iso_now(), event_type, from_stage, to_stage, iteration,
             actor, reason, _json(metadata)))

    def record_event(self, run_id: str, *, event_type: str, **fields: Any) -> None:
        with self._connection() as connection:
            self._event_locked(connection, run_id, event_type=event_type, **fields)

    def events(self, run_id: str, *, limit: int = 500,
               since_id: int = 0) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_events WHERE run_id = ? AND id > ? "
                "ORDER BY id LIMIT ?", (run_id, since_id, limit)).fetchall()
        return [{"id": r["id"], "timestamp": r["timestamp"],
                 "event_type": r["event_type"], "from_stage": r["from_stage"],
                 "to_stage": r["to_stage"], "iteration": r["iteration"],
                 "actor": r["actor"], "reason": r["reason"],
                 "metadata": _unjson(r["metadata"], {})} for r in rows]

    # -- runs ----------------------------------------------------------------
    def create_run(self, *, task_id: str, prompt: str, title: str = "",
                   project_id: str | None = None,
                   run_policy: policy.HarnessPolicy | None = None,
                   cost: policy.CostPolicy | None = None,
                   request_key: str | None = None,
                   definition_hash: str | None = None,
                   node_id: str | None = None,
                   actor: str | None = None) -> tuple[HarnessRun, bool]:
        """(run, created). A repeated call with the same `request_key` returns
        the SAME run and `created=False` -- exactly-once at START, keyed on the
        task definition because the contract does not exist yet."""
        effective = run_policy or policy.HarnessPolicy()
        cost_policy = cost or policy.cost_policy_for(effective.mode)
        now = iso_now()
        run_id = new_id("hrn")
        with self._connection() as connection:
            if request_key:
                existing = connection.execute(
                    "SELECT * FROM harness_runs WHERE request_key = ?",
                    (request_key,)).fetchone()
                if existing is not None:
                    return HarnessRun.from_row(existing), False
            connection.execute(
                "INSERT INTO harness_runs (id, project_id, task_id, title, prompt, "
                "mode, stage, status, write_authority, policy, node_id, "
                "max_iterations, current_iteration, request_key, definition_hash, "
                "soft_token_budget, cost_policy_decision, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project_id, task_id, title, prompt, effective.mode,
                 state.INIT, "active", effective.write_authority,
                 _json(effective.to_dict()), node_id, effective.max_iterations, 0,
                 request_key, definition_hash, cost_policy.soft_token_budget,
                 _json(cost_policy.to_dict()), now, now))
            connection.execute(
                "INSERT OR IGNORE INTO harness_efficiency (run_id, updated_at) VALUES (?,?)",
                (run_id, now))
            self._event_locked(connection, run_id, event_type="RUN_CREATED",
                               to_stage=state.INIT, actor=actor,
                               reason=f"mode={effective.mode} "
                                      f"authority={effective.write_authority}",
                               metadata={"policy": effective.to_dict(),
                                         "cost": cost_policy.to_dict()})
            row = connection.execute(
                "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
        return HarnessRun.from_row(row), True

    def get_run(self, run_id: str) -> HarnessRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
        return HarnessRun.from_row(row) if row else None

    def require_run(self, run_id: str) -> HarnessRun:
        run = self.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    def run_for_task(self, task_id: str, *, active_only: bool = True) -> HarnessRun | None:
        sql = "SELECT * FROM harness_runs WHERE task_id = ?"
        args: list[Any] = [task_id]
        if active_only:
            sql += " AND status = 'active'"
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        return HarnessRun.from_row(row) if row else None

    def runs_for_tasks(self, task_ids: Sequence[str]) -> dict[str, HarnessRun]:
        """The latest run per task, for a whole board, in ONE query.

        The Global Task board renders a harness projection on every card, and
        the obvious way to do that -- `run_for_task` in a loop -- is one
        SELECT per card on a view that refreshes on a timer. This exists so
        the board reads the runs the same way it reads the tasks: in bulk.

        Latest wins per task: a task that has been run more than once shows
        its current attempt, never an arbitrary earlier one. Tasks with no
        run are simply absent from the mapping rather than present with a
        null, so a caller cannot mistake "never harnessed" for "harnessed and
        in no stage".
        """
        ids = [str(task_id) for task_id in task_ids if task_id]
        if not ids:
            return {}
        latest: dict[str, HarnessRun] = {}
        with self._connection() as connection:
            # Chunked to stay under SQLite's variable limit on a large board.
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM harness_runs WHERE task_id IN ({placeholders}) "
                    "ORDER BY created_at ASC", chunk).fetchall()
                for row in rows:
                    run = HarnessRun.from_row(row)
                    latest[run.task_id] = run  # ascending: the last write wins
        return latest

    def list_runs(self, *, project_id: str | None = None, status: str | None = None,
                  stage: str | None = None, limit: int = 200) -> list[HarnessRun]:
        sql = "SELECT * FROM harness_runs WHERE 1=1"
        args: list[Any] = []
        if project_id:
            sql += " AND project_id = ?"
            args.append(project_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if stage:
            sql += " AND stage = ?"
            args.append(stage)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [HarnessRun.from_row(row) for row in rows]

    def patch_run(self, run_id: str, **fields: Any) -> HarnessRun:
        """Field updates that are NOT stage changes. A stage change goes
        through `advance()` so it cannot bypass the transition table."""
        if "stage" in fields or "status" in fields:
            raise ValueError("stage/status change must go through advance()")
        with self._connection() as connection:
            run = self._apply_patch_locked(connection, run_id, fields)
        return run

    def _apply_patch_locked(self, connection: sqlite3.Connection, run_id: str,
                            fields: dict[str, Any]) -> HarnessRun:
        row = connection.execute(
            "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        sets, args = [], []
        for key, value in fields.items():
            if key in _PATCHABLE_JSON:
                sets.append(f"{key} = ?")
                args.append(_json(value))
            elif key in _PATCHABLE:
                sets.append(f"{key} = ?")
                args.append(value)
            else:
                raise ValueError(f"{key!r} is not a patchable harness_runs column")
        sets.append("updated_at = ?")
        args.append(iso_now())
        args.append(run_id)
        connection.execute(f"UPDATE harness_runs SET {', '.join(sets)} WHERE id = ?", args)
        updated = connection.execute(
            "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
        return HarnessRun.from_row(updated)

    def advance(self, run_id: str, to_stage: str, *, actor: str | None = None,
                reason: str | None = None, owner: str | None = None,
                metadata: Any = None, **fields: Any) -> HarnessRun:
        """THE stage transition. Validates, writes and logs in one transaction.

        The validation happens inside the transaction rather than before it so
        two engines racing on the same run cannot both read stage X, both
        decide X->Y is legal, and both write.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            current = HarnessRun.from_row(row)
            self._require_lease_locked(current, owner)
            state.require_transition(current.stage, to_stage)
            status = "terminal" if state.is_terminal(to_stage) else "active"
            patch = dict(fields)
            if to_stage == state.BLOCKED and reason:
                patch.setdefault("blocked_reason", reason)
            if current.started_at is None and to_stage in state.ACTIVE_STAGES:
                patch.setdefault("started_at", iso_now())
            if to_stage in state.RESUMABLE_STAGES:
                # The stage a resume returns to is recorded as the run enters
                # it, not reconstructed afterwards from the event log.
                patch.setdefault("resume_stage", to_stage)
            if patch:
                self._apply_patch_locked(connection, run_id, patch)
            now = iso_now()
            connection.execute(
                "UPDATE harness_runs SET stage = ?, status = ?, updated_at = ?, "
                "completed_at = CASE WHEN ? = 'terminal' THEN ? ELSE completed_at END "
                "WHERE id = ?",
                (to_stage, status, now, status, now, run_id))
            self._event_locked(connection, run_id, event_type="STAGE",
                               from_stage=current.stage, to_stage=to_stage,
                               iteration=current.current_iteration, actor=actor,
                               reason=reason, metadata=metadata)
            updated = connection.execute(
                "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
        return HarnessRun.from_row(updated)

    # -- leases --------------------------------------------------------------
    @staticmethod
    def _lease_live(run: HarnessRun, *, now: datetime | None = None) -> bool:
        if not run.lease_owner or not run.lease_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(run.lease_expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        moment = now or datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > moment

    def _require_lease_locked(self, run: HarnessRun, owner: str | None) -> None:
        """A caller that names itself must actually hold the lease.

        A caller that passes no owner is trusted -- single-process callers and
        tests -- but one that DOES name itself is claiming exclusivity, and
        letting that claim through while someone else holds the lease is worse
        than not having leases at all.
        """
        if owner is None:
            return
        if not self._lease_live(run):
            return
        if run.lease_owner != owner:
            raise LeaseNotHeld(run.id, owner, run.lease_owner)

    def acquire_lease(self, run_id: str, owner: str, *,
                      ttl_seconds: float = 900.0) -> bool:
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            run = HarnessRun.from_row(row)
            if self._lease_live(run) and run.lease_owner != owner:
                return False
            connection.execute(
                "UPDATE harness_runs SET lease_owner = ?, lease_expires_at = ?, "
                "updated_at = ? WHERE id = ?", (owner, expires, iso_now(), run_id))
            self._event_locked(connection, run_id, event_type="LEASE_ACQUIRED",
                               actor=owner, reason=f"ttl={ttl_seconds}s")
        return True

    def release_lease(self, run_id: str, owner: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lease_owner FROM harness_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["lease_owner"] not in (None, owner):
                return False
            connection.execute(
                "UPDATE harness_runs SET lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE id = ?", (iso_now(), run_id))
            self._event_locked(connection, run_id, event_type="LEASE_RELEASED", actor=owner)
        return True

    # -- contracts -----------------------------------------------------------
    def save_contract(self, contract: ExecutionContract, *,
                      actor: str | None = None) -> ExecutionContract:
        """Insert a contract version. Never an UPDATE: a redefine is a new row."""
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT frozen FROM harness_contracts WHERE run_id = ? AND version = ?",
                (contract.run_id, contract.version)).fetchone()
            if existing is not None:
                raise ValueError(
                    f"contract version {contract.version} already exists for run "
                    f"{contract.run_id}; a change is a new version, not an edit")
            content = contract.content()
            connection.execute(
                "INSERT INTO harness_contracts (id, run_id, task_id, version, scope, "
                "out_of_scope, affected_areas, dependencies, functional_acceptance, "
                "visual_acceptance, performance_acceptance, security_acceptance, "
                "required_checks, manual_checks, dev_command, test_command, "
                "build_command, planner_agent, frozen, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract.id, contract.run_id, contract.task_id, contract.version,
                 contract.scope, _json(content["out_of_scope"]),
                 _json(content["affected_areas"]), _json(content["dependencies"]),
                 _json(content["functional_acceptance"]),
                 _json(content["visual_acceptance"]),
                 _json(content["performance_acceptance"]),
                 _json(content["security_acceptance"]),
                 _json(content["required_checks"]), _json(content["manual_checks"]),
                 contract.dev_command, contract.test_command, contract.build_command,
                 contract.planner_agent, int(contract.frozen),
                 contract.content_hash, contract.created_at))
            connection.execute(
                "UPDATE harness_runs SET contract_id = ?, contract_hash = ?, "
                "updated_at = ? WHERE id = ?",
                (contract.id, contract.content_hash, iso_now(), contract.run_id))
            self._event_locked(connection, contract.run_id,
                               event_type="CONTRACT_WRITTEN", actor=actor,
                               reason=f"v{contract.version}",
                               metadata={"contract_id": contract.id,
                                         "content_hash": contract.content_hash,
                                         "criteria": len(contract.criteria())})
        return contract

    def freeze_contract(self, contract_id: str, *, actor: str | None = None) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id, version FROM harness_contracts WHERE id = ?",
                (contract_id,)).fetchone()
            if row is None:
                raise LookupError(f"no contract {contract_id}")
            connection.execute(
                "UPDATE harness_contracts SET frozen = 1 WHERE id = ?", (contract_id,))
            self._event_locked(connection, row["run_id"],
                               event_type="CONTRACT_FROZEN", actor=actor,
                               reason=f"v{row['version']}",
                               metadata={"contract_id": contract_id})

    def get_contract(self, run_id: str, *, version: int | None = None
                     ) -> ExecutionContract | None:
        sql = "SELECT * FROM harness_contracts WHERE run_id = ?"
        args: list[Any] = [run_id]
        if version is not None:
            sql += " AND version = ?"
            args.append(version)
        sql += " ORDER BY version DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        if row is None:
            return None
        return ExecutionContract(
            id=row["id"], run_id=row["run_id"], task_id=row["task_id"],
            version=row["version"], scope=row["scope"] or "",
            out_of_scope=_tuple(row["out_of_scope"]),
            affected_areas=_tuple(row["affected_areas"]),
            dependencies=_tuple(row["dependencies"]),
            functional_acceptance=_tuple(row["functional_acceptance"]),
            visual_acceptance=_tuple(row["visual_acceptance"]),
            performance_acceptance=_tuple(row["performance_acceptance"]),
            security_acceptance=_tuple(row["security_acceptance"]),
            required_checks=_tuple(row["required_checks"]),
            manual_checks=_tuple(row["manual_checks"]),
            dev_command=row["dev_command"], test_command=row["test_command"],
            build_command=row["build_command"], created_at=row["created_at"],
            planner_agent=row["planner_agent"], frozen=bool(row["frozen"]))

    def contract_versions(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, version, content_hash, frozen, created_at "
                "FROM harness_contracts WHERE run_id = ? ORDER BY version",
                (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- iterations ----------------------------------------------------------
    def start_iteration(self, run_id: str, iteration: int, *,
                        builder_agent: str | None = None,
                        builder_session_id: str | None = None,
                        base_commit: str | None = None,
                        request_key: str | None = None) -> dict[str, Any]:
        """Exactly-once per (run, iteration). A repeat returns the same row --
        which is what makes a resumed run continue its iteration instead of
        starting a second one with the same number."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_iterations WHERE run_id = ? AND iteration = ?",
                (run_id, iteration)).fetchone()
            if row is not None:
                return dict(row)
            iteration_id = new_id("itr")
            connection.execute(
                "INSERT INTO harness_iterations (id, run_id, iteration, request_key, "
                "builder_agent, builder_session_id, base_commit, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (iteration_id, run_id, iteration, request_key, builder_agent,
                 builder_session_id, base_commit, iso_now()))
            connection.execute(
                "UPDATE harness_runs SET current_iteration = ?, updated_at = ? WHERE id = ?",
                (iteration, iso_now(), run_id))
            self._event_locked(connection, run_id, event_type="ITERATION_STARTED",
                               iteration=iteration, actor=builder_agent,
                               metadata={"base_commit": base_commit})
            row = connection.execute(
                "SELECT * FROM harness_iterations WHERE id = ?", (iteration_id,)).fetchone()
        return dict(row)

    def complete_iteration(self, run_id: str, iteration: int, *,
                           verdict: str | None = None,
                           failure_class: str | None = None,
                           result_commit: str | None = None,
                           evaluator_agent: str | None = None,
                           evaluator_session_id: str | None = None,
                           feedback_artifact: str | None = None,
                           verdict_artifact: str | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE harness_iterations SET completed_at = ?, verdict = ?, "
                "failure_class = ?, result_commit = COALESCE(?, result_commit), "
                "evaluator_agent = COALESCE(?, evaluator_agent), "
                "evaluator_session_id = COALESCE(?, evaluator_session_id), "
                "feedback_artifact = COALESCE(?, feedback_artifact), "
                "verdict_artifact = COALESCE(?, verdict_artifact) "
                "WHERE run_id = ? AND iteration = ?",
                (iso_now(), verdict, failure_class, result_commit, evaluator_agent,
                 evaluator_session_id, feedback_artifact, verdict_artifact,
                 run_id, iteration))
            self._event_locked(connection, run_id, event_type="ITERATION_COMPLETED",
                               iteration=iteration, reason=verdict,
                               metadata={"failure_class": failure_class,
                                         "result_commit": result_commit})
            row = connection.execute(
                "SELECT * FROM harness_iterations WHERE run_id = ? AND iteration = ?",
                (run_id, iteration)).fetchone()
        return dict(row) if row else None

    def iterations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_iterations WHERE run_id = ? ORDER BY iteration",
                (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- evaluations ---------------------------------------------------------
    def record_evaluation(self, evaluation: EvaluationResult) -> EvaluationResult:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_evaluations (id, run_id, iteration, contract_id, "
                "contract_hash, result, summary, evaluator_agent, evaluator_session_id, "
                "result_commit, failure_class, criteria, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation.id, evaluation.run_id, evaluation.iteration,
                 evaluation.contract_id, evaluation.contract_hash, evaluation.result,
                 evaluation.summary, evaluation.evaluator_agent,
                 evaluation.evaluator_session_id, evaluation.result_commit,
                 evaluation.failure_class,
                 _json([c.to_dict() for c in evaluation.criteria]),
                 evaluation.created_at))
            self._event_locked(connection, evaluation.run_id,
                               event_type="EVALUATION", iteration=evaluation.iteration,
                               actor=evaluation.evaluator_agent,
                               reason=evaluation.result,
                               metadata={"contract_hash": evaluation.contract_hash,
                                         "failed": [c.criterion_id
                                                    for c in evaluation.failed_criteria]})
        return evaluation

    def evaluations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_evaluations WHERE run_id = ? "
                "ORDER BY iteration, created_at", (run_id,)).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["criteria"] = _unjson(row["criteria"], [])
            out.append(record)
        return out

    def latest_evaluation(self, run_id: str, iteration: int | None = None
                          ) -> dict[str, Any] | None:
        sql = "SELECT * FROM harness_evaluations WHERE run_id = ?"
        args: list[Any] = [run_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            args.append(iteration)
        sql += " ORDER BY iteration DESC, created_at DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["criteria"] = _unjson(row["criteria"], [])
        return record

    # -- checkpoints ---------------------------------------------------------
    def save_checkpoint(self, run_id: str, *, task_id: str, iteration: int,
                        branch: str | None = None, commit_sha: str | None = None,
                        worktree_path: str | None = None,
                        session_id: str | None = None,
                        checks: Any = None, remaining: Any = None,
                        note: str = "") -> dict[str, Any]:
        """The record a REPLACEMENT builder reads.

        It deliberately carries the worktree path and the branch, not just a
        description of progress: the replacement continues in the same place
        rather than starting a second worktree from the same prompt, which is
        the specific failure the old retry path produced.
        """
        checkpoint_id = new_id("ckp")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_checkpoints (id, run_id, task_id, iteration, "
                "branch, commit_sha, worktree_path, session_id, checks, remaining, "
                "note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_id, run_id, task_id, iteration, branch, commit_sha,
                 worktree_path, session_id, _json(checks), _json(remaining),
                 note, iso_now()))
            connection.execute(
                "UPDATE harness_runs SET last_checkpoint_id = ?, updated_at = ? WHERE id = ?",
                (checkpoint_id, iso_now(), run_id))
            self._event_locked(connection, run_id, event_type="CHECKPOINT",
                               iteration=iteration, actor=session_id, reason=note,
                               metadata={"checkpoint_id": checkpoint_id,
                                         "commit": commit_sha,
                                         "worktree_path": worktree_path})
            row = connection.execute(
                "SELECT * FROM harness_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return self._checkpoint_dict(row)

    @staticmethod
    def _checkpoint_dict(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["checks"] = _unjson(row["checks"], [])
        record["remaining"] = _unjson(row["remaining"], [])
        return record

    def latest_checkpoint(self, run_id: str, *, iteration: int | None = None
                          ) -> dict[str, Any] | None:
        sql = "SELECT * FROM harness_checkpoints WHERE run_id = ?"
        args: list[Any] = [run_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            args.append(iteration)
        sql += " ORDER BY iteration DESC, created_at DESC, rowid DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        return self._checkpoint_dict(row) if row else None

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_checkpoints WHERE run_id = ? "
                "ORDER BY iteration, rowid", (run_id,)).fetchall()
        return [self._checkpoint_dict(row) for row in rows]

    # -- human decisions -----------------------------------------------------
    def open_decision(self, *, reason: str, question: str, run_id: str | None = None,
                      task_id: str | None = None, project_id: str | None = None,
                      detail: Any = None) -> dict[str, Any]:
        """Refuses anything outside the closed list. See harness_policy."""
        policy.require_human_reason(reason)
        decision_id = new_id("dec")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_decisions (id, run_id, task_id, project_id, "
                "reason, question, detail, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,'open',?)",
                (decision_id, run_id, task_id, project_id, reason, question,
                 _json(detail), iso_now()))
            if run_id:
                self._event_locked(connection, run_id, event_type="DECISION_OPENED",
                                   reason=reason,
                                   metadata={"decision_id": decision_id,
                                             "question": question})
            row = connection.execute(
                "SELECT * FROM harness_decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._decision_dict(row)

    @staticmethod
    def _decision_dict(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["detail"] = _unjson(row["detail"], {})
        return record

    def resolve_decision(self, decision_id: str, *, resolution: str,
                         resolved_by: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_decisions WHERE id = ?", (decision_id,)).fetchone()
            if row is None:
                raise LookupError(f"no decision {decision_id}")
            connection.execute(
                "UPDATE harness_decisions SET status = 'resolved', resolved_at = ?, "
                "resolved_by = ?, resolution = ? WHERE id = ?",
                (iso_now(), resolved_by, resolution, decision_id))
            if row["run_id"]:
                self._event_locked(connection, row["run_id"],
                                   event_type="DECISION_RESOLVED", actor=resolved_by,
                                   reason=resolution,
                                   metadata={"decision_id": decision_id})
            updated = connection.execute(
                "SELECT * FROM harness_decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._decision_dict(updated)

    def list_decisions(self, *, status: str = "open", run_id: str | None = None,
                       project_id: str | None = None, limit: int = 100
                       ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM harness_decisions WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if project_id:
            sql += " AND project_id = ?"
            args.append(project_id)
        sql += " ORDER BY created_at LIMIT ?"
        args.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._decision_dict(row) for row in rows]

    # -- durable policy overrides -------------------------------------------
    def set_policy_override(self, *, scope: str, scope_key: str,
                            mode: str | None = None,
                            write_authority: str | None = None,
                            max_iterations: int | None = None,
                            payload: Any = None,
                            updated_by: str | None = None) -> dict[str, Any]:
        if scope not in ("global", "project", "task"):
            raise ValueError(f"unknown policy scope {scope!r}")
        if mode is not None and mode not in policy.MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if write_authority is not None and write_authority not in policy.AUTHORITIES:
            raise ValueError(f"unknown write authority {write_authority!r}")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_policies (id, scope, scope_key, mode, "
                "write_authority, max_iterations, payload, updated_at, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(scope, scope_key) DO UPDATE SET "
                "mode = excluded.mode, write_authority = excluded.write_authority, "
                "max_iterations = excluded.max_iterations, payload = excluded.payload, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (new_id("hpl"), scope, scope_key, mode, write_authority,
                 max_iterations, _json(payload), iso_now(), updated_by))
            row = connection.execute(
                "SELECT * FROM harness_policies WHERE scope = ? AND scope_key = ?",
                (scope, scope_key)).fetchone()
        record = dict(row)
        record["payload"] = _unjson(row["payload"], {})
        return record

    def policy_override(self, *, scope: str, scope_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_policies WHERE scope = ? AND scope_key = ?",
                (scope, scope_key)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = _unjson(row["payload"], {})
        return record

    def resolve_policy_overrides(self, *, project_id: str | None,
                                 task_id: str | None) -> dict[str, Any]:
        """Most specific wins: task, then project, then global.

        Merged field by field rather than whole-row, so a project override of
        `write_authority` does not silently drop a global `max_iterations`.
        """
        merged: dict[str, Any] = {}
        for scope, key in (("global", "*"), ("project", project_id or ""),
                           ("task", task_id or "")):
            if not key:
                continue
            found = self.policy_override(scope=scope, scope_key=key)
            if not found:
                continue
            for name in ("mode", "write_authority", "max_iterations"):
                if found.get(name) is not None:
                    merged[name] = found[name]
            merged.setdefault("sources", []).append(f"{scope}:{key}")
        return merged

    # -- artifacts -----------------------------------------------------------
    def record_artifact(self, run_id: str, *, kind: str, path: str,
                        iteration: int | None = None, content_hash: str | None = None,
                        size_bytes: int | None = None,
                        metadata: Any = None) -> dict[str, Any]:
        artifact_id = new_id("art")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_artifacts (id, run_id, iteration, kind, path, "
                "content_hash, size_bytes, metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (artifact_id, run_id, iteration, kind, path, content_hash,
                 size_bytes, _json(metadata), iso_now()))
            row = connection.execute(
                "SELECT * FROM harness_artifacts WHERE id = ?", (artifact_id,)).fetchone()
        record = dict(row)
        record["metadata"] = _unjson(row["metadata"], {})
        return record

    def artifacts(self, run_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM harness_artifacts WHERE run_id = ?"
        args: list[Any] = [run_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY created_at, rowid"
        with self._connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["metadata"] = _unjson(row["metadata"], {})
            out.append(record)
        return out

    # -- efficiency ----------------------------------------------------------
    def bump_efficiency(self, run_id: str, *, decision: str | None = None,
                        **counters: int) -> dict[str, Any]:
        """Add to the counters. Never assigns -- see _EFFICIENCY_COUNTERS."""
        unknown = set(counters) - set(_EFFICIENCY_COUNTERS)
        if unknown:
            raise ValueError(f"unknown efficiency counters: {sorted(unknown)}")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO harness_efficiency (run_id, updated_at) VALUES (?,?)",
                (run_id, iso_now()))
            if counters:
                sets = ", ".join(f"{name} = {name} + ?" for name in counters)
                connection.execute(
                    f"UPDATE harness_efficiency SET {sets}, updated_at = ? WHERE run_id = ?",
                    [*counters.values(), iso_now(), run_id])
            if decision:
                row = connection.execute(
                    "SELECT cost_policy_decisions FROM harness_efficiency WHERE run_id = ?",
                    (run_id,)).fetchone()
                log = _unjson(row["cost_policy_decisions"] if row else None, []) or []
                log.append({"at": iso_now(), "decision": decision})
                connection.execute(
                    "UPDATE harness_efficiency SET cost_policy_decisions = ? WHERE run_id = ?",
                    (_json(log), run_id))
            if "prompt_tokens_estimate" in counters:
                # The run carries its own running total so the budget ladder
                # can be evaluated without joining.
                connection.execute(
                    "UPDATE harness_runs SET tokens_spent_estimate = "
                    "tokens_spent_estimate + ?, updated_at = ? WHERE id = ?",
                    (counters["prompt_tokens_estimate"], iso_now(), run_id))
            row = connection.execute(
                "SELECT * FROM harness_efficiency WHERE run_id = ?", (run_id,)).fetchone()
        record = dict(row)
        record["cost_policy_decisions"] = _unjson(row["cost_policy_decisions"], [])
        return record

    def efficiency(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_efficiency WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return {name: 0 for name in _EFFICIENCY_COUNTERS} | {
                "run_id": run_id, "cost_policy_decisions": []}
        record = dict(row)
        record["cost_policy_decisions"] = _unjson(row["cost_policy_decisions"], [])
        return record

    def efficiency_totals(self, run_ids: Sequence[str]) -> dict[str, Any]:
        """Summed counters across a set of runs -- what the pilot reports."""
        totals = {name: 0 for name in _EFFICIENCY_COUNTERS}
        for run_id in run_ids:
            record = self.efficiency(run_id)
            for name in _EFFICIENCY_COUNTERS:
                totals[name] += int(record.get(name) or 0)
        totals["runs"] = len(run_ids)
        return totals

    # -- content-addressed context cache ------------------------------------
    def cache_get(self, cache_key: str) -> dict[str, Any] | None:
        """A hit also RECORDS the hit. A cache whose hit rate is measured by a
        separate counter that callers must remember to increment reports the
        hit rate of callers' memories."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_context_cache WHERE cache_key = ?",
                (cache_key,)).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE harness_context_cache SET hits = hits + 1, last_used_at = ? "
                "WHERE cache_key = ?", (iso_now(), cache_key))
        record = dict(row)
        record["payload"] = _unjson(row["payload"], {})
        record["modules"] = _tuple(row["modules"])
        record["hits"] = int(row["hits"]) + 1
        return record

    def cache_put(self, cache_key: str, payload: Any, *,
                  project_id: str | None = None,
                  modules: Sequence[str] = ()) -> dict[str, Any]:
        blob = _json(payload) or "{}"
        now = iso_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO harness_context_cache (cache_key, project_id, modules, "
                "payload, bytes, hits, created_at, last_used_at) VALUES (?,?,?,?,?,0,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET last_used_at = excluded.last_used_at",
                (cache_key, project_id, _json(list(modules)), blob, len(blob), now, now))
            row = connection.execute(
                "SELECT * FROM harness_context_cache WHERE cache_key = ?",
                (cache_key,)).fetchone()
        record = dict(row)
        record["payload"] = _unjson(row["payload"], {})
        record["modules"] = _tuple(row["modules"])
        return record

    def cache_stats(self, *, project_id: str | None = None) -> dict[str, Any]:
        sql = ("SELECT COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits, "
               "COALESCE(SUM(bytes),0) AS bytes FROM harness_context_cache")
        args: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            args.append(project_id)
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        return {"entries": row["entries"], "hits": row["hits"], "bytes": row["bytes"]}

    # -- dispatches (the real-runtime adapter's durable record) -------------
    def open_dispatch(self, *, run_id: str, task_id: str, iteration: int, role: str,
                      attempt: int, idempotency_key: str, nonce: str,
                      session_id: str | None = None, node_id: str | None = None,
                      runtime: str | None = None, worktree_path: str | None = None,
                      branch: str | None = None, correlation_id: str | None = None,
                      artifact_path: str | None = None,
                      session_reused: bool = False,
                      prompt_bytes: int = 0,
                      prompt_tokens_estimate: int = 0) -> tuple[dict[str, Any], bool]:
        """(dispatch, created). Exactly-once on (run, iteration, role, attempt).

        A repeat returns the EXISTING row rather than a second one, which is
        what makes a re-stepped stage resolve the dispatch already in flight
        instead of sending the same prompt to a working agent again.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_dispatches WHERE run_id = ? AND iteration = ? "
                "AND role = ? AND attempt = ?",
                (run_id, iteration, role, attempt)).fetchone()
            if row is not None:
                return dict(row), False
            dispatch_id = new_id("dsp")
            connection.execute(
                "INSERT INTO harness_dispatches (id, run_id, task_id, iteration, role, "
                "attempt, state, session_id, node_id, runtime, worktree_path, branch, "
                "idempotency_key, correlation_id, nonce, artifact_path, session_reused, "
                "prompt_bytes, prompt_tokens_estimate, created_at) "
                "VALUES (?,?,?,?,?,?,'dispatching',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, run_id, task_id, iteration, role, attempt, session_id,
                 node_id, runtime, worktree_path, branch, idempotency_key,
                 correlation_id, nonce, artifact_path, int(session_reused),
                 prompt_bytes, prompt_tokens_estimate, iso_now()))
            self._event_locked(connection, run_id, event_type="DISPATCH_OPENED",
                               iteration=iteration, actor=session_id, reason=role,
                               metadata={"dispatch_id": dispatch_id,
                                         "idempotency_key": idempotency_key,
                                         "session": session_id, "node": node_id,
                                         "reused": bool(session_reused),
                                         "artifact_path": artifact_path})
            row = connection.execute(
                "SELECT * FROM harness_dispatches WHERE id = ?", (dispatch_id,)).fetchone()
        return dict(row), True

    #: States a dispatch may still be waiting in. `awaiting_session` means the
    #: session exists but nothing has been sent yet -- a freshly spawned CLI
    #: is still drawing its welcome screen and is not listening.
    OPEN_DISPATCH_STATES = ("awaiting_session", "dispatching", "accepted", "running")

    def open_dispatch_for(self, run_id: str, *, iteration: int | None = None,
                          role: str | None = None) -> dict[str, Any] | None:
        sql = ("SELECT * FROM harness_dispatches WHERE run_id = ? AND state IN "
               "('awaiting_session','dispatching','accepted','running')")
        args: list[Any] = [run_id]
        if iteration is not None:
            sql += " AND iteration = ?"
            args.append(iteration)
        if role:
            sql += " AND role = ?"
            args.append(role)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, args).fetchone()
        return dict(row) if row else None

    def latest_dispatch(self, run_id: str, *, iteration: int, role: str
                        ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM harness_dispatches WHERE run_id = ? AND iteration = ? "
                "AND role = ? ORDER BY attempt DESC LIMIT 1",
                (run_id, iteration, role)).fetchone()
        return dict(row) if row else None

    def update_dispatch(self, dispatch_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"state", "session_id", "node_id", "runtime", "worktree_path",
                   "branch", "artifact_path", "artifact_hash", "delivery_state",
                   "delivery_verdict", "context_percent", "error", "accepted_at",
                   "last_observed_at", "completed_at", "session_reused",
                   "prompt_bytes", "prompt_tokens_estimate", "correlation_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not updatable on harness_dispatches: {sorted(unknown)}")
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            args.append(_json(value) if key == "delivery_verdict"
                        and not isinstance(value, str) else value)
        args.append(dispatch_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id, iteration, role, state FROM harness_dispatches WHERE id = ?",
                (dispatch_id,)).fetchone()
            if row is None:
                raise LookupError(f"no dispatch {dispatch_id}")
            connection.execute(
                f"UPDATE harness_dispatches SET {', '.join(sets)} WHERE id = ?", args)
            new_state = fields.get("state")
            if new_state and new_state != row["state"]:
                self._event_locked(
                    connection, row["run_id"], event_type="DISPATCH_STATE",
                    iteration=row["iteration"], reason=f"{row['role']}: "
                    f"{row['state']} -> {new_state}",
                    metadata={"dispatch_id": dispatch_id,
                              "error": fields.get("error")})
            updated = connection.execute(
                "SELECT * FROM harness_dispatches WHERE id = ?", (dispatch_id,)).fetchone()
        return dict(updated)

    def dispatches(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM harness_dispatches WHERE run_id = ? "
                "ORDER BY created_at, rowid", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def sessions_in_use(self, *, exclude_run: str | None = None) -> set[str]:
        """Sessions currently holding an OPEN harness dispatch.

        Used to stop two runs handing work to the same pane at once -- the
        duplicate-spawn guard's other half, and the reason a fresh evaluator
        can be proven independent rather than merely requested.
        """
        sql = ("SELECT DISTINCT session_id FROM harness_dispatches WHERE state IN "
               "('awaiting_session','dispatching','accepted','running') "
               "AND session_id IS NOT NULL")
        args: list[Any] = []
        if exclude_run:
            sql += " AND run_id != ?"
            args.append(exclude_run)
        with self._connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        return {row["session_id"] for row in rows}

    # -- reporting -----------------------------------------------------------
    def run_report(self, run_id: str) -> dict[str, Any]:
        """Everything durably known about one run, in one read.

        The dashboard, the MCP status tool and the pilot report all call this
        rather than each assembling their own view, which is what keeps three
        surfaces from disagreeing about the same run.
        """
        run = self.require_run(run_id)
        contract = self.get_contract(run_id)
        return {
            "run": run.to_dict(),
            "contract": contract.to_dict() if contract else None,
            "contract_versions": self.contract_versions(run_id),
            "iterations": self.iterations(run_id),
            "evaluations": self.evaluations(run_id),
            "checkpoints": self.checkpoints(run_id),
            "decisions": self.list_decisions(status="", run_id=run_id),
            "artifacts": self.artifacts(run_id),
            "efficiency": self.efficiency(run_id),
            "dispatches": self.dispatches(run_id),
            "events": self.events(run_id),
        }
