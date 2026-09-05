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
        "terminal_send_text",
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
        "terminal_node_status",
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
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"
