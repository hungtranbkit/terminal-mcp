# Module Map

## Work runtime
`work_store.py` (runs, tasks, approvals, artifacts, events), `work_service.py`
(the service every surface shares), `work_loop.py` (coordinator tick),
`work_eligibility.py` (the `-work` opt-in rule, in ONE place).
Tables: `work_runs`, `work_tasks`, `work_approvals`, `work_artifacts`,
`work_events`. `work_tasks` POINTS at a queue task; the queue owns its state.

## Queue
`queue_engine.py`, `request_governor.py`, `queue_store.py`. Task rows key on `id`; enqueue returns
`task_id`. `auto_dispatch_enabled` defaults False per lane.

## Planner
`bug_spec.py` (spec, levels, completeness gate, triage, assist, budgets),
`task_classifier.py` (FAST_FIX / NORMAL / SAFE, exclusions first).

## Knowledge and procedures
`project_knowledge.py` (map, confidence, locking), `context_pack.py` (module
packs, similar-bug retrieval), `procedures.py` (runbook registry),
`scripts/agent/*.sh` (the actual scripts).

## Policy and telemetry
`work_policy.py` (canonical policy, subset loading, bindings),
`work_telemetry.py` (token provenance, counters).

## UI
`dashboard.py` -- one module holding several page templates as separate HTML
constants: `TERMINAL_WALL_HTML`, `FLEET_HTML`, `AUDIT_HTML`, `WORK_HTML`.

## Fleet and security
`fleet_registry.py`, `terminal_wall.py`, `access_policy.py`, `redaction.py`.
