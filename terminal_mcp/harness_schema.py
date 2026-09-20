"""Harness durable schema -- IN the canonical queue database, not beside it.

THE SINGLE MOST IMPORTANT DECISION IN THIS FEATURE

These tables live in the SAME SQLite file as queue_tasks, and they are added
through the SAME schema.py Migration mechanism the queue already uses. Not a
harness.db, not a second store with its own lifecycle, not a JSON directory
that has to be reconciled with the database afterwards.

A separate store would have been easier to write and would have been a second
runtime source of truth within a week: two files that can disagree about
whether a task is running, two backup schedules, two recovery paths, and a
reconciliation loop whose job is to guess which one was right. Terminal MCP
already has exactly one durable queue, and the whole point of Harness is to
stop adding parallel authorities. So a HarnessRun and the queue task it drives
commit in the same database, and `.harness/` on disk holds only human-readable
evidence that can be deleted without losing a single fact.

APPEND-ONLY MEANS NO UPDATE STATEMENT EXISTS

`harness_events` has an INSERT path and no other. The store exposes no update
or delete for it, and the recovery path reads it forward rather than
correcting it. An event log that can be edited is a narrative, not a record,
and the one time it matters is exactly the time someone will have "fixed" it.

BACKWARDS COMPATIBILITY

Every statement here is CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT
EXISTS and adds nothing to any existing table. A database that has never seen
Harness gets the tables and behaves identically; a database that has them
already is untouched. Running the migration twice is a no-op, and
schema.py's PRAGMA user_version means it will not even be attempted twice.
"""
from __future__ import annotations

import sqlite3

from .schema import Migration

HARNESS_SCHEMA_VERSION = 15


def create_harness_tables(connection: sqlite3.Connection) -> None:
    """Additive, idempotent. Safe to call on any queue database."""

    # -- the run: one attempt to satisfy one ExecutionContract --------------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'standard',
            stage TEXT NOT NULL DEFAULT 'INIT',
            -- 'active' | 'terminal'. Derived from stage and stored so the
            -- common "what is still running" query never has to know the
            -- stage vocabulary.
            status TEXT NOT NULL DEFAULT 'active',
            write_authority TEXT NOT NULL DEFAULT 'shadow',
            policy TEXT,
            planner_agent TEXT,
            builder_agent TEXT,
            evaluator_agent TEXT,
            node_id TEXT,
            builder_session_id TEXT,
            evaluator_session_id TEXT,
            branch TEXT,
            worktree_path TEXT,
            base_commit TEXT,
            result_commit TEXT,
            max_iterations INTEGER NOT NULL DEFAULT 3,
            current_iteration INTEGER NOT NULL DEFAULT 0,
            -- exactly-once at START, keyed on the task DEFINITION because the
            -- contract does not exist yet when a run is created.
            request_key TEXT,
            definition_hash TEXT,
            -- exactly-once once the contract EXISTS: run:<project>:<task>:<hash>
            contract_request_key TEXT,
            contract_id TEXT,
            contract_hash TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            -- the stage a resume returns to. Never PLANNING unless the run
            -- was actually interrupted during planning.
            resume_stage TEXT,
            infra_failure_count INTEGER NOT NULL DEFAULT 0,
            last_checkpoint_id TEXT,
            -- TOKEN/COST MINIMISATION. A run is allowed a soft budget, not a
            -- hard one: hitting it checkpoints and narrows what is sent next,
            -- it never kills work in flight. A hard cap would turn an
            -- expensive run into a wasted one, which is strictly worse.
            soft_token_budget INTEGER,
            tokens_spent_estimate INTEGER NOT NULL DEFAULT 0,
            cost_policy_decision TEXT,
            last_error TEXT,
            blocked_reason TEXT,
            shadow_of_task_status TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    # Partial UNIQUE: a repeated harness_start with the same definition
    # resolves to the SAME run instead of creating a second one. Partial
    # because a run created without a request key must not collide with
    # every other keyless run.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_harness_runs_request_key "
        "ON harness_runs(request_key) WHERE request_key IS NOT NULL")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_runs_task ON harness_runs(task_id, status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_runs_project ON harness_runs(project_id, status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_runs_stage ON harness_runs(stage)")

    # -- the iteration: one Builder pass and the Evaluator answer to it -----
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_iterations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            request_key TEXT,
            builder_agent TEXT,
            evaluator_agent TEXT,
            builder_session_id TEXT,
            evaluator_session_id TEXT,
            base_commit TEXT,
            result_commit TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            verdict TEXT,
            failure_class TEXT,
            feedback_artifact TEXT,
            verdict_artifact TEXT,
            UNIQUE(run_id, iteration)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_iterations_run ON harness_iterations(run_id, iteration)")

    # -- the contract: content-addressed, versioned, never edited -----------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_contracts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            scope TEXT NOT NULL DEFAULT '',
            out_of_scope TEXT,
            affected_areas TEXT,
            dependencies TEXT,
            functional_acceptance TEXT,
            visual_acceptance TEXT,
            performance_acceptance TEXT,
            security_acceptance TEXT,
            required_checks TEXT,
            manual_checks TEXT,
            dev_command TEXT,
            test_command TEXT,
            build_command TEXT,
            planner_agent TEXT,
            frozen INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, version)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_contracts_hash ON harness_contracts(content_hash)")

    # -- the verdict: per-criterion, with evidence --------------------------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_evaluations (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            contract_id TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            result TEXT NOT NULL,
            summary TEXT,
            evaluator_agent TEXT,
            evaluator_session_id TEXT,
            result_commit TEXT,
            failure_class TEXT,
            criteria TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, iteration, id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_evaluations_run ON harness_evaluations(run_id, iteration)")

    # -- append-only event log ----------------------------------------------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_stage TEXT,
            to_stage TEXT,
            iteration INTEGER,
            actor TEXT,
            reason TEXT,
            metadata TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_events_run ON harness_events(run_id, id)")

    # -- Builder checkpoints: what a replacement Builder reads --------------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_checkpoints (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            branch TEXT,
            commit_sha TEXT,
            worktree_path TEXT,
            session_id TEXT,
            checks TEXT,
            remaining TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_run "
        "ON harness_checkpoints(run_id, iteration, id)")

    # -- Human Decision Queue: the closed list of things humans decide ------
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_decisions (
            id TEXT PRIMARY KEY,
            run_id TEXT,
            task_id TEXT,
            project_id TEXT,
            reason TEXT NOT NULL,
            question TEXT NOT NULL,
            detail TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT,
            resolution TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_decisions_open "
        "ON harness_decisions(status, created_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_decisions_run ON harness_decisions(run_id)")

    # -- durable policy overrides -------------------------------------------
    # `mode_for()` is the default answer; a row here is a deliberate, durable
    # override for one project or one task. Scope is explicit so a project
    # override can never silently become a global one.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_policies (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            mode TEXT,
            write_authority TEXT,
            max_iterations INTEGER,
            payload TEXT,
            updated_at TEXT NOT NULL,
            updated_by TEXT,
            UNIQUE(scope, scope_key)
        )
        """
    )

    # -- artifact metadata ---------------------------------------------------
    # The bytes live under .harness/ as human-readable evidence. THIS is the
    # durable record that they exist, what they are of, and whether what is on
    # disk still matches -- so a deleted artifact is a known gap rather than a
    # silently missing fact.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            iteration INTEGER,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            content_hash TEXT,
            size_bytes INTEGER,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_artifacts_run ON harness_artifacts(run_id, iteration)")

    # -- efficiency counters -------------------------------------------------
    # One row per run. These are the numbers that answer "did the cost policy
    # actually do anything", and they are counters rather than a derived
    # report because the thing worth knowing -- an LLM call that did NOT
    # happen -- leaves no trace anywhere else. `planner_skipped` and
    # `evaluator_skipped` are the two biggest savings in the system and are
    # invisible unless something increments them deliberately.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_efficiency (
            run_id TEXT PRIMARY KEY,
            llm_calls INTEGER NOT NULL DEFAULT 0,
            prompt_tokens_estimate INTEGER NOT NULL DEFAULT 0,
            context_bytes INTEGER NOT NULL DEFAULT 0,
            reused_context_hits INTEGER NOT NULL DEFAULT 0,
            context_cache_misses INTEGER NOT NULL DEFAULT 0,
            planner_skipped INTEGER NOT NULL DEFAULT 0,
            evaluator_skipped INTEGER NOT NULL DEFAULT 0,
            session_reused INTEGER NOT NULL DEFAULT 0,
            sessions_spawned INTEGER NOT NULL DEFAULT 0,
            rollover_count INTEGER NOT NULL DEFAULT 0,
            delta_prompts INTEGER NOT NULL DEFAULT 0,
            full_prompts INTEGER NOT NULL DEFAULT 0,
            skills_injected INTEGER NOT NULL DEFAULT 0,
            skill_bytes INTEGER NOT NULL DEFAULT 0,
            cost_policy_decisions TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    # -- content-addressed context cache ------------------------------------
    # A Module Context Pack is expensive to assemble and identical for every
    # task touching the same modules at the same content hash. Keyed by the
    # hash of what went INTO it, so invalidation is automatic: a changed file,
    # rule or skill version produces a different key and therefore a miss,
    # and nothing has to remember to expire anything.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS harness_context_cache (
            cache_key TEXT PRIMARY KEY,
            project_id TEXT,
            modules TEXT,
            payload TEXT NOT NULL,
            bytes INTEGER NOT NULL DEFAULT 0,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_context_cache_project "
        "ON harness_context_cache(project_id, last_used_at)")


HARNESS_MIGRATIONS: list[Migration] = [
    Migration(HARNESS_SCHEMA_VERSION,
              "TMCP-HARNESS-001: HarnessRun/Iteration/ExecutionContract/"
              "EvaluationResult/checkpoints/decisions/policies/artifacts + the "
              "append-only harness event log, in the canonical queue database",
              create_harness_tables),
]

#: Every table this feature owns. Used by the deprecation report and by the
#: migration-idempotency test, so "what did Harness add" has one answer.
HARNESS_TABLES: tuple[str, ...] = (
    "harness_runs", "harness_iterations", "harness_contracts",
    "harness_evaluations", "harness_events", "harness_checkpoints",
    "harness_decisions", "harness_policies", "harness_artifacts",
    "harness_efficiency", "harness_context_cache",
)
