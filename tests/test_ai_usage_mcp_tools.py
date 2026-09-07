"""AI Usage -- MCP tool surface (terminal_ai_usage_status). Exercises
the real MCP call path (server.call_tool), same pattern as every other
*_mcp_tools.py file, with an injected fake fetcher -- no real network."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.ai_usage_client import AiUsageClientError
from terminal_mcp.ai_usage_service import AiUsageService
from terminal_mcp.config import AiUsageConfig
from terminal_mcp.mcp_app import build_mcp


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _fetcher_returning(payload):
    def fetch(base_url, timeout):
        return payload
    return fetch


@pytest.mark.anyio
async def test_ai_usage_status_through_real_mcp_path():
    ai_usage = AiUsageService(AiUsageConfig(), fetcher=_fetcher_returning(
        {"app_version": "0.6.0", "codex": {"ok": True, "five_hour": {"used_percent": 50.0}}}))
    server = build_mcp(ai_usage=ai_usage)
    result = await _call(server, "terminal_ai_usage_status")
    assert result["available"] is True
    codex = next(p for p in result["providers"] if p["provider"] == "codex")
    assert codex["windows"][0]["used_percent"] == 50.0


@pytest.mark.anyio
async def test_ai_usage_status_degrades_cleanly_when_service_is_down():
    def broken_fetch(base_url, timeout):
        raise AiUsageClientError("connection refused")
    ai_usage = AiUsageService(AiUsageConfig(), fetcher=broken_fetch)
    server = build_mcp(ai_usage=ai_usage)
    result = await _call(server, "terminal_ai_usage_status")
    assert result["available"] is False
    assert "connection refused" in result["error"]


@pytest.mark.anyio
async def test_ai_usage_status_force_param_bypasses_cache():
    calls = []

    def fetch(base_url, timeout):
        calls.append(1)
        return {"codex": {"ok": True}}
    ai_usage = AiUsageService(AiUsageConfig(), fetcher=fetch)
    server = build_mcp(ai_usage=ai_usage)
    await _call(server, "terminal_ai_usage_status")
    await _call(server, "terminal_ai_usage_status", force=True)
    assert len(calls) == 2
