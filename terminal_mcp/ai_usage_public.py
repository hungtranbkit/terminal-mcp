"""Restricted read-only usage projection for the Dell AI Usage Monitor.

Only provider quota fractions and non-sensitive timestamps leave this node.
Never forward account, plan, auth mode, provider errors, process data, prompt,
model, personal paths, token strings or raw monitor responses.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import threading
import time
from typing import Any, Callable

from .ai_usage_client import fetch_usage

_PROVIDERS = ("codex", "claude", "gemini", "antigravity")
_WINDOWS = ("five_hour", "weekly")
_LOCAL_MONITOR = "http://127.0.0.1:8787"
_MAX_TIMEOUT_SECONDS = 3.5


def _percentage(value: Any) -> float | None:
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    if not 0 <= value <= 100:
        return None
    return round(float(value), 1)


def _epoch(value: Any) -> int | None:
    if type(value) in (int, float) and math.isfinite(value) and 946684800 <= value <= 4102444800:
        return int(value)
    if isinstance(value, str) and len(value) <= 35:
        try:
            v = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            seconds = int(v.timestamp())
            return seconds if 946684800 <= seconds <= 4102444800 else None
        except (ValueError, OverflowError):
            return None
    return None


def _window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = _percentage(raw.get("used_percent"))
    remaining = _percentage(raw.get("remaining_percent"))
    # Normalize ONLY provided numeric usage evidence. No zero-filled quotas.
    if used is None and remaining is None:
        return None
    if used is None:
        used = round(100 - remaining, 1)
    if remaining is None:
        remaining = round(100 - used, 1)
    reset = _epoch(raw.get("resets_at"))
    if reset is None:
        reset = _epoch(raw.get("reset_at"))
    return {"used_percent": used, "remaining_percent": remaining, "reset_at": reset}


def _timestamp(raw: Any) -> str | None:
    if not isinstance(raw, str) or len(raw) > 50:
        return None
    try:
        v = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return None


def safe_public_usage(raw: Any, *, sampled_at: str) -> dict[str, Any]:
    """Finite, public-safe schema; no dynamic raw provider keys or values."""
    data = raw if isinstance(raw, dict) else {}
    providers = []
    for name in _PROVIDERS:
        item = data.get(name)
        item = item if isinstance(item, dict) else {}
        windows = {}
        if item.get("ok") is True:
            for kind in _WINDOWS:
                window = _window(item.get(kind))
                if window is not None:
                    windows[kind] = window
        providers.append({
            "provider": name,
            "state": ("available" if windows else
                      "unavailable" if item.get("ok") is False else "not_observed"),
            "observed_at": _timestamp(item.get("updated_at")),
            "windows": windows,
        })
    return {"schema_version": 1, "kind": "terminal-mcp-public-ai-usage",
            "source": "dell-local-ai-usage-monitor", "sampled_at": sampled_at,
            "providers": providers}


class PublicUsageService:
    """Cache provider usage for 60s; no private credentials or remote fetches."""

    def __init__(self, *, fetcher: Callable[..., dict[str, Any]] = fetch_usage,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ttl_seconds: float = 60.0) -> None:
        self._fetcher, self._clock, self._now = fetcher, clock, now
        self._ttl = min(120.0, max(5.0, ttl_seconds))
        self._lock = threading.Lock()
        self._last_at = float("-inf")
        self._cached: dict[str, Any] | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            tick = self._clock()
            if self._cached is not None and tick - self._last_at < self._ttl:
                return copy.deepcopy(self._cached)
            sampled_at = self._now().astimezone(timezone.utc).isoformat(timespec="seconds")
            try:
                raw = self._fetcher(_LOCAL_MONITOR, timeout=_MAX_TIMEOUT_SECONDS)
                value = safe_public_usage(raw, sampled_at=sampled_at)
                value["source_status"] = "ok" if isinstance(raw, dict) else "unavailable"
            except Exception:
                # No raw exception (may contain paths/account details) escapes.
                value = safe_public_usage(None, sampled_at=sampled_at)
                value["source_status"] = "unavailable"
            self._cached = value
            # A transient cold-start/provider timeout must not hide real
            # quota metrics for a full minute. Retry a failed read in 15s.
            self._last_at = tick if value["source_status"] == "ok" else tick - self._ttl + 15.0
            return copy.deepcopy(value)
