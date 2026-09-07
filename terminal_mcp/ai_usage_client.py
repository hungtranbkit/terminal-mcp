"""Read-only HTTP client for the separate 'AI Usage Monitor' local
service (see docs/REQUIREMENTS.md's own "AI Usage" section for the full
audit: a SEPARATE project -- source at ~/workspace/ai-usage-monitor,
installed to ~/.local/share/ai-usage-monitor, its own `ai-usage-monitor`
systemd --user service, serving a plain stdlib `http.server` JSON API on
127.0.0.1:8787 by default). This project makes ZERO changes to that
service and duplicates NONE of its usage-collection logic (it never
reads a provider's OAuth credential file or calls a provider's own
usage API directly) -- it only reads that service's own already-
computed `/api/usage` response.

GET-only, short timeout, no retry (matches node_client.py's own
RemoteNodeClient posture -- one clean request, retry semantics are a
caller-layer decision). NEVER raises past AiUsageClientError -- a
caller (ai_usage_service.py) always gets either real data or a clear,
typed failure, never an uncaught exception propagating into a dashboard
route or MCP tool."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 2.0


class AiUsageClientError(Exception):
    """The AI Usage Monitor service is unreachable, timed out, or
    returned something this client can't parse as JSON."""


def fetch_usage(base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """One GET /api/usage round trip. Deliberately never sends
    `?force=1` -- this project is a passive reader of whatever cache
    state the AI Usage Monitor itself already maintains (its own real
    per-provider 60s/180s TTLs against the actual provider APIs), never
    forcing a fresh upstream provider call on its behalf; ai_usage_
    service.py's own cache_ttl_seconds is a SEPARATE, additional layer
    on top of that, not a replacement for it."""
    url = f"{base_url.rstrip('/')}/api/usage"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise AiUsageClientError(f"GET /api/usage -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AiUsageClientError(f"GET /api/usage -> {type(exc).__name__}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AiUsageClientError(f"GET /api/usage -> timed out after {timeout}s") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AiUsageClientError(f"GET /api/usage -> invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AiUsageClientError("GET /api/usage -> response was not a JSON object")
    return data
