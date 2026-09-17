import pytest

from terminal_mcp.server import mcp


@pytest.mark.anyio
async def test_server_registers_v1_and_binding_tools():
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "terminal_list_sessions",
        "terminal_tail",
        "terminal_capture",
        "terminal_status",
        "terminal_batch_inspect",
        "terminal_wait_for_state",
        "terminal_send_text",
        "terminal_send_task",
        "terminal_send_keys",
        "terminal_exit_copy_mode",
        "terminal_bind",
        "terminal_get_binding",
        "terminal_list_bindings",
        "terminal_unbind",
        "terminal_tail_bound",
        "terminal_status_bound",
        "terminal_send_bound",
        "terminal_list_input_audit",
        "terminal_input_context",
        "terminal_create_session",
        "terminal_detach_session",
        "terminal_delete_session",
        "terminal_kill_session",
        "terminal_rename_session",
        "terminal_reopen_session",
        "terminal_list_killed_sessions",
        "supervisor_watch",
        "supervisor_set_verifier_policy",
        "supervisor_unwatch",
        "supervisor_list_watches",
        "supervisor_get_completion_token",
        "supervisor_status",
        "supervisor_list_events",
        "supervisor_ack_event",
        "supervisor_run_once",
        "supervisor2_set_policy",
        "supervisor2_get_policy",
        "supervisor2_list_actionable_events",
        "supervisor2_claim_event",
        "supervisor2_submit_decision",
        "supervisor2_review_action",
        "supervisor2_execute_send",
        "supervisor2_list_actions",
        "terminal_list_nodes",
        "terminal_node_health",
        "terminal_connection_status",
        # Fleet environment audit: tools/services/auth readiness per node, so
        # "which node could take over?" is answerable without logging into
        # each machine. Status only -- never credential contents.
        "terminal_fleet_environment",
        # Fleet Metadata Registry: the replicated view, the SSH inventory, its
        # readiness and its per-peer sync state. All four are reads, and all
        # four answer from the LOCAL cache so they keep working when the
        # controller does not.
        "terminal_fleet_registry",
        "terminal_fleet_ssh_inventory",
        "terminal_fleet_readiness",
        "terminal_fleet_sync_status",
        # Deployment redundancy: read status, declare targets/paths, ask who
        # would deploy, and rehearse a failover. Nothing here runs a deploy --
        # dispatch takes a lease and is a separate, deliberate call.
        "terminal_deployment_status",
        "terminal_deployment_upsert_target",
        "terminal_deployment_upsert_path",
        "terminal_deployment_choose_node",
        "terminal_deployment_dry_run_failover",
        # Permission/audit policy pass: the audit log, auth status and the
        # policy table, all readable over MCP with exactly the same rows and
        # redaction the dashboard shows -- one implementation, so neither
        # surface can see something the other refuses.
        "terminal_audit_search",
        "terminal_auth_status",
        "terminal_access_policy",
        # Work Runtime V1. Kept deliberately small -- the template's own
        # "MCP SURFACE — GIỮ NHỎ": create, read, enqueue more, gate, decide,
        # control. Everything else is the existing queue/task surface.
        "work_create",
        "work_status",
        "work_list",
        "work_continue",
        "work_request_approval",
        "work_approve",
        "work_control",
        # Project Knowledge, runbook registry, Work Policy and telemetry.
        # Reads, plus two deliberate writes: recording ONE verified module,
        # and a worker reporting what its own task cost. Running a procedure
        # above preview risk still needs an explicit approval.
        "work_knowledge",
        "work_knowledge_record",
        "work_procedures",
        "work_policy",
        "work_telemetry",
        "work_telemetry_report",
        # Rapid Capture Inbox + planner pool. Capture is deliberately shallow
        # (split, title, rough type, duplicate flag) so a developer can dump a
        # batch faster than any planner can analyse one; the pool then claims
        # them under a concurrency cap with leases, so a dead planner does not
        # hold an issue forever.
        "work_inbox_capture",
        "work_inbox_list",
        "work_inbox_claim",
        "work_inbox_transition",
        "work_inbox_request_hint",
        "work_inbox_answer_hint",
        "work_inbox_history",
        # Work v1: a generic spec/plan surface -- decompose a requirement,
        # gate the spec, select the tests it needs. Two more defs land in
        # mcp_app alongside these (_gate_report, _spec_store) but are private
        # helpers, not registered tools, which is why this list is 8 not 10.
        "work_spec_create",
        "work_spec_get",
        "work_spec_list",
        "work_spec_update",
        "work_spec_gate",
        "work_plan",
        "work_plan_redefine",
        "work_test_selection",
        # Session permission management: ChatGPT could rename a session but
        # had no way to grant or revoke access to one -- that took an SSH
        # session and a config edit.
        "session_get_permissions",
        "session_set_permissions",
        "session_grant",
        "session_revoke",
        "session_repair_stale_pin",
        "session_bulk_set_permissions",
        "terminal_node_status",
        # P0.3: probed tool/runtime capability axis, used for routing.
        "terminal_node_capabilities",
        # P0.4: task lease verbs (renew/release/handoff + holder read).
        "terminal_task_renew_lease",
        "terminal_task_release_claim",
        "terminal_task_handoff",
        "terminal_task_lease_holder",
        # P0.5 Verify Queue -- verification as claimable work routed by capability.
        "terminal_verify_request",
        "terminal_verify_list",
        "terminal_verify_claim",
        "terminal_verify_start",
        "terminal_verify_renew",
        "terminal_verify_release",
        "terminal_verify_handoff",
        "terminal_verify_complete",
        "terminal_verify_fail",
        "terminal_verify_requeue",
        "terminal_verify_trace",
        "terminal_verify_reconcile",
        # P0.6 named-resource ownership lock (advisory, project-scoped).
        "terminal_resource_lock",
        "terminal_resource_lock_many",
        "terminal_resource_renew",
        "terminal_resource_unlock",
        "terminal_resource_unlock_all",
        "terminal_resource_holder",
        "terminal_resource_locks",
        "terminal_resource_force_unlock",
        # P0.7 project-level APIs (pure composition, no new state).
        "terminal_project_status",
        "terminal_project_submit_goal",
        "terminal_project_events",
        "terminal_project_report",
        "terminal_project_pause",
        "terminal_project_resume",
        "terminal_project_assign",
        # Chat reset recovery tools.
        "terminal_chat_checkpoint",
        "terminal_chat_recover",
        "terminal_chat_checkpoint_list",
        # Previously HTTP-only: build_mcp() left `backlog`/`events` at None
        # while server.py calls it bare, so these 19 were absent from the
        # stdio surface AND from this contract test. Both surfaces now match.
        "terminal_project_list",
        "terminal_backlog_get",
        "terminal_backlog_add",
        "terminal_backlog_update",
        "terminal_backlog_bulk_update",
        "terminal_backlog_claim",
        "terminal_backlog_block",
        "terminal_backlog_complete",
        "terminal_backlog_dispatch",
        "terminal_backlog_validate",
        "terminal_backlog_export",
        "terminal_backlog_import",
        "terminal_event_publish",
        "terminal_event_list",
        "terminal_event_claim",
        "terminal_event_ack",
        "terminal_event_release",
        "terminal_event_retry",
        "terminal_event_stats",
        # Orchestration V1: the OUTCOME layer (backlog -> outcome -> N tasks).
        "terminal_outcome_create",
        "terminal_outcome_attach_task",
        "terminal_outcome_status",
        "terminal_outcome_list",
        "terminal_outcome_complete",
        "terminal_outcome_block",
        "terminal_outcome_unblock",
        "terminal_outcome_trace",
        # Orchestration V1: the worker view (roles/capabilities/liveness).
        "terminal_worker_declare",
        "terminal_worker_list",
        "terminal_worker_status",
        "terminal_worker_roles",
        "terminal_node_sessions",
        "terminal_registry_list",
        "terminal_registry_get",
        "terminal_registry_search",
        "terminal_registry_reopen",
        "terminal_registry_purge",
        "terminal_knowledge_search",
        "terminal_knowledge_timeline",
        "terminal_knowledge_recover",
        "terminal_knowledge_checkpoint",
        "terminal_watchdog_session_events",
        "terminal_watchdog_acknowledge_session_event",
        "terminal_watchdog_node_events",
        "terminal_watchdog_acknowledge_node_event",
        "terminal_queue_set",
        "terminal_queue_append",
        "terminal_queue_status",
        "terminal_queue_list_all",
        "terminal_queue_pause",
        "terminal_queue_resume",
        "terminal_queue_retry",
        "terminal_queue_skip",
        "terminal_queue_cancel",
        "terminal_queue_reorder",
        "terminal_queue_clear",
        "terminal_queue_events",
        "terminal_queue_run_once",
        "terminal_queue_verify",
        "terminal_queue_set_auto_dispatch",
        "terminal_integration_configure",
        "terminal_integration_status",
        "terminal_integration_list_handoffs",
        "terminal_integration_run_once",
        "terminal_integration_loop_status",
        "terminal_integration_loop_run_once",
        "terminal_integration_pause",
        "terminal_integration_resume",
        "terminal_integration_retry_handoff",
        "terminal_integration_force_regression",
        "terminal_integration_promote",
        "terminal_integration_events",
        "terminal_enqueue_task",
        "terminal_task_status",
        "terminal_queue_metrics",
        "terminal_task_set_project",
        "terminal_task_reassign",
        "terminal_task_assignment_history",
        "terminal_task_rebalance_plan",
        "terminal_task_rebalance",
        "terminal_session_tasks",
        "terminal_fleet_task_summary",
        "terminal_queue_loop_status",
        "terminal_queue_loop_run_once",
        "terminal_queue_global_inbox",
        "terminal_queue_recent_events",
        "terminal_integration_fleet_overview",
        # Unified Task System: Global Tasks Kanban (queue.board()).
        "terminal_task_create",
        "terminal_task_assign",
        "terminal_task_board",
        # PM/Orchestrator Agent: skill-based routing (§20.2).
        "terminal_pm_set_capability",
        "terminal_pm_list_capabilities",
        "terminal_pm_delete_capability",
        "terminal_pm_eligible_workers",
        "terminal_pm_route_task",
        "terminal_pm_approve_routing",
        "terminal_pm_route_all_unassigned",
        "terminal_pm_explain",
        # Planner: task-breaking (§20.3).
        "terminal_task_split",
        "terminal_task_approve_plan",
        "terminal_task_children",
        "terminal_task_complete_parent",
        # Git isolation policy (§20.4).
        "terminal_task_create_isolated",
        "terminal_worktree_status",
        "terminal_worktree_cleanup",
        # Worktree Janitor P0 -- AUDIT-ONLY classification. Its presence in
        # this pinned set alongside the ABSENCE of any
        # terminal_worktree_janitor_run/remove/prune name is part of the P0
        # contract: the classifier ships with no executor.
        "terminal_worktree_janitor_scan",
        # P3 periodic sweep. run_once is exposed so the manual path works with
        # the background loop disabled.
        "terminal_worktree_sweep_run_once",
        "terminal_worktree_sweep_status",
        # P5 operator surface. READ-ONLY -- it reports, and the review
        # decision route lives on the dashboard (auth+CSRF), not here.
        "terminal_worktree_janitor_report",
        # Delivery discipline: Definition of Ready (§20.6 Phase A).
        "terminal_task_check_dor",
        # Incident lane (§20.6 Phase B).
        "terminal_task_create_incident",
        "terminal_list_active_incidents",
        # Release lifecycle (§20.6 Phase C).
        "terminal_release_create",
        "terminal_release_advance",
        "terminal_release_rollback",
        "terminal_release_status",
        "terminal_release_list",
        # PM summary + backlog hygiene (§20.6 Phase D).
        "terminal_pm_summary",
        "terminal_pm_detect_stale_backlog",
        "terminal_pm_detect_duplicate_tasks",
        "terminal_pm_close_task_with_confirmation",
        # Emergency Stop / Agent failure policy (§20.6 Phase E).
        "terminal_emergency_stop",
        "terminal_emergency_resume",
        # AI Usage: read-only integration with the local AI Usage Monitor.
        "terminal_ai_usage_status",
        # Auto Recovery: session recovery after reboot/crash/node-agent restart.
        "terminal_recovery_set_policy",
        "terminal_recovery_status",
        "terminal_recovery_list",
        "terminal_recover_session",
        "terminal_recovery_reconcile_node",
        "terminal_checkpoint_session",
        "terminal_recovery_loop_status",
        "terminal_recovery_loop_run_once",
        # Read-only repository access (repo_read.py / repo_service.py) --
        # the surface an external agent uses to read Git and source
        # directly. These ten are READS ONLY: the absence of any
        # repo_write/repo_checkout/repo_commit/repo_push name from this
        # set is itself part of the V1 contract, and this assertion is
        # what keeps a write primitive from being added without anyone
        # noticing.
        "repo_status",
        "repo_head",
        "repo_branches",
        "repo_remotes",
        "repo_tree",
        "repo_read",
        "repo_search",
        "repo_diff",
        "repo_log",
        "repo_show_commit",
        # Notes / Ideas: the cross-project kho ghi chú (notes_store.py).
        # Deliberately NOT terminal_*-prefixed -- these touch no terminal,
        # session or node, and the name a model reads in a tool list is the
        # main thing steering it to the right tool. Present on the STDIO
        # surface too (this test builds it), which is the whole point of
        # build_mcp's default_optional_services defaulting.
        "note_create",
        "note_get",
        "note_search",
        "note_list",
        "note_update",
        "note_delete",
        "note_restore",
        "note_add_attachment",
        "note_remove_attachment",
        "note_link_to_project",
        "note_mark_applied",
        "note_facets",
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"
