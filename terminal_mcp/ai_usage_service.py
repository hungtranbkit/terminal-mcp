"""AI Usage integration -- the normalization/caching/degraded-state
layer over ai_usage_client.py's raw HTTP read (task follow-up:
"tích hợp dự án AI Usage đang chạy tại http://127.0.0.1:8787/ vào
Terminal MCP"). This is the ONE place the AI Usage Monitor's own
provider-specific response shapes (codex/claude/gemini/antigravity,
each subtly different -- see the real /api/usage response this was
built against) get normalized into ONE common `providers: [...]` shape
a dashboard/MCP caller can render generically, plus this project's own
warning/critical threshold classification (config.ai_usage.warning_
threshold_percent/critical_threshold_percent -- the AI Usage Monitor
itself has no such concept, its own UI just picks a bar color inline).

DEGRADED STATE (task requirement: "tuyệt đối không làm dashboard/session
controller treo theo" -- never let a down AI Usage Monitor hang the
dashboard): every call is bounded by config.ai_usage.timeout_seconds
(default 2.0s) and NEVER raises -- a failure returns `available: True,
stale: True` with the last-known-good snapshot (if one exists) or
`available: False` (if none does) plus a real `error`/`last_error`
string, never a fake/zero-filled usage number standing in for real
data.

SESSION CORRELATION (item 4): best-effort only, and only for LOCAL
sessions -- the AI Usage Monitor reflects THIS machine's own single set
of locally logged-in CLI credential files (~/.codex/auth.json, ~/.claude
/.credentials.json), not a fleet-wide concept; a session on a remote
node (dell-5530, m910, macbook) has no relationship to this host's own
quota numbers at all, so correlation is deliberately never attempted for
non-local sessions -- see `correlate_local_sessions`'s own docstring."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .ai_usage_client import AiUsageClientError, fetch_usage
from .config import AiUsageConfig

# (raw /api/usage field, display label) -- codex/claude both use this
# shape; antigravity uses its own quota_windows list instead (handled
# separately in _provider_windows), gemini never has usage windows at
# all (usage_status stays USAGE_UNAVAILABLE by the AI Usage Monitor's
# own design -- no stable local/public quota endpoint for every Gemini
# auth mode, per that project's own SOURCES.md).
_WINDOW_FIELDS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"), ("weekly", "Weekly"),
    ("weekly_sonnet", "Weekly (Sonnet)"), ("weekly_opus", "Weekly (Opus)"),
)

_PROVIDER_KEYS: tuple[str, ...] = ("codex", "claude", "gemini", "antigravity")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _severity(used_percent: float | None, config: AiUsageConfig) -> str | None:
    if used_percent is None:
        return None
    if used_percent >= config.critical_threshold_percent:
        return "critical"
    if used_percent >= config.warning_threshold_percent:
        return "warning"
    return "ok"


def _provider_windows(entry: dict[str, Any], config: AiUsageConfig) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for key, label in _WINDOW_FIELDS:
        w = entry.get(key)
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        windows.append({
            "label": label, "used_percent": used, "remaining_percent": w.get("remaining_percent"),
            "resets_at": w.get("resets_at"), "severity": _severity(used, config),
        })
    # Antigravity's own shape: quota_windows: [{id, remaining_percent, ...}]
    for w in entry.get("quota_windows") or []:
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        if used is None and w.get("remaining_percent") is not None:
            try:
                used = 100.0 - float(w["remaining_percent"])
            except (TypeError, ValueError):
                used = None
        windows.append({
            "label": str(w.get("id") or "quota"), "used_percent": used,
            "remaining_percent": w.get("remaining_percent"), "resets_at": w.get("resets_at"),
            "severity": _severity(used, config),
        })
    return windows


def _normalize(raw: dict[str, Any], config: AiUsageConfig) -> dict[str, Any]:
    providers = []
    for key in _PROVIDER_KEYS:
        entry = raw.get(key)
        entry = entry if isinstance(entry, dict) else {}
        ok = bool(entry.get("ok"))
        windows = _provider_windows(entry, config) if ok else []
        severities = {w["severity"] for w in windows if w["severity"]}
        providers.append({
            "provider": key, "ok": ok, "account": entry.get("account"), "plan": entry.get("plan"),
            "windows": windows, "usage_available": ok and bool(windows),
            "usage_message": entry.get("usage_message") if not windows else None,
            "error": entry.get("error") if not ok else None,
            "updated_at": entry.get("updated_at"),
            "warning": "warning" in severities, "critical": "critical" in severities,
        })
    return {
        "available": True, "source": config.base_url, "app_version": raw.get("app_version"),
        "fetched_at": raw.get("timestamp") or _iso_now(), "providers": providers, "sessions": [],
    }


def correlate_local_sessions(normalized: dict[str, Any], session_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort session<->provider correlation for LOCAL sessions
    only (see this module's own docstring for why non-local sessions are
    never attempted). `session_rows` is a plain list of
    {"name", "agent_type"} dicts -- the caller (mcp_app.py/dashboard.py)
    supplies these via a real, already-existing session listing (e.g.
    TmuxClient.list_sessions), never re-derived here; this function is
    pure and independently testable without a real tmux session."""
    ok_providers = {p["provider"] for p in normalized.get("providers", []) if p.get("ok")}
    correlated = []
    for row in session_rows:
        agent_type = (row.get("agent_type") or "").casefold() or None
        provider = agent_type if agent_type in ("claude", "codex") else None
        correlated.append({
            "session": row.get("name"), "node_id": "local", "agent_type": agent_type, "provider": provider,
            "quota_available": provider in ok_providers if provider else False,
        })
    return correlated


class AiUsageService:
    def __init__(self, config: AiUsageConfig | None = None, *,
                fetcher: Callable[[str, float], dict[str, Any]] = fetch_usage,
                session_lister: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.config = config or AiUsageConfig()
        self._fetcher = fetcher
        self._session_lister = session_lister
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def _disabled_response(self) -> dict[str, Any]:
        return {"available": False, "error": "AI_USAGE_DISABLED", "providers": [], "sessions": [],
                "source": self.config.base_url, "app_version": None, "fetched_at": None,
                "cached": False, "cache_age_seconds": 0.0, "stale": False}

    def _view(self, normalized: dict[str, Any], *, cached_at: float, now: float, cached: bool, stale: bool,
             last_error: str | None = None) -> dict[str, Any]:
        result = dict(normalized)
        # `cached` reflects whether THIS call served the snapshot without
        # a real, fresh fetch (an explicit flag from the call site, never
        # inferred from elapsed wall-clock time -- two calls made back-
        # to-back in the same test/tick can have ~0s between them and
        # still be a genuine cache hit). `stale` (a fetch attempt failed
        # and this is a past-TTL snapshot) implies `cached` too.
        result["cached"] = cached or stale
        result["cache_age_seconds"] = round(max(0.0, now - cached_at), 1)
        result["stale"] = stale
        if last_error is not None:
            result["last_error"] = last_error
        return result

    def get_usage(self, *, force: bool = False) -> dict[str, Any]:
        """Returns the normalized, cached-or-fresh usage snapshot.
        `force=True` bypasses this service's own cache_ttl_seconds (but
        never sends its own force upstream -- see fetch_usage's own
        docstring) -- used by the dashboard's manual Refresh button."""
        if not self.config.enabled:
            return self._disabled_response()
        now = time.monotonic()
        with self._lock:
            cached = self._cache
        if not force and cached is not None and (now - cached[0]) < self.config.cache_ttl_seconds:
            return self._view(cached[1], cached_at=cached[0], now=now, cached=True, stale=False)
        try:
            raw = self._fetcher(self.config.base_url, self.config.timeout_seconds)
        except AiUsageClientError as exc:
            if cached is not None:
                return self._view(cached[1], cached_at=cached[0], now=now, cached=True, stale=True,
                                  last_error=str(exc))
            return {"available": False, "error": str(exc), "providers": [], "sessions": [],
                    "source": self.config.base_url, "app_version": None, "fetched_at": None,
                    "cached": False, "cache_age_seconds": 0.0, "stale": False}
        normalized = _normalize(raw, self.config)
        if self._session_lister is not None:
            try:
                normalized["sessions"] = correlate_local_sessions(normalized, self._session_lister())
            except Exception:  # noqa: BLE001 -- correlation is a best-effort extra, never breaks the real usage data
                normalized["sessions"] = []
        with self._lock:
            self._cache = (now, normalized)
        return self._view(normalized, cached_at=now, now=now, cached=False, stale=False)
