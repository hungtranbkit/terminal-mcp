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
        "terminal_bind",
        "terminal_get_binding",
        "terminal_list_bindings",
        "terminal_unbind",
        "terminal_tail_bound",
        "terminal_status_bound",
        "terminal_send_bound",
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"
