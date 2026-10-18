"""Optional Camofox browser provider (TMCP-CAMOFOX-001).

WHAT THIS IS
------------
A thin, fail-closed client for a shared Camofox REST browser worker
(github.com/jo-inc/camofox-browser, pinned in deploy/camofox/). It gives an
agent a *stateful* tab -- create, navigate, snapshot, click, type, scroll,
screenshot, list, close -- where the Phase-1 gateway (browser_gateway.py)
gives one-shot verdicts in a fresh Chromium.

It does NOT replace the gateway. `browser_verify`/`browser_screenshot` stay
the default browser; this provider is OFF unless
TERMINAL_MCP_CAMOFOX_ENABLED=1, and when off the `camofox_browser` tool is
not even registered, so the default tool catalog is unchanged.

SAFETY, IN THE ORDER IT IS APPLIED
----------------------------------
1. The worker endpoint must be loopback or a private/Tailscale address, and
   the access key comes from a chmod-600 file. The key is never returned,
   logged, or echoed in an error.
2. Every URL passes `browser_safety.validate_url` (schemes, metadata hard
   block, DNS-resolved loopback/private refusal) AND a strict hostname
   allowlist (`allowed_domains`: exact host or a subdomain of it). The
   gateway's allow patterns are substrings; a pilot needs a real boundary.
3. The URL a tab ends up on is re-checked after navigation and every
   snapshot. A tab that redirected or was clicked off the allowlist is
   closed and the call fails.
4. click/type write to the page and need allow_mutations=true, matching
   browser_run_task. No evaluate, cookie import, upload or download route
   is exposed.
5. Per-agent isolation: each agent_id maps to its own Camofox userId
   (separate browser context, server-side tab ownership), tab counts are
   capped per agent, and close_all drops the whole context.
6. Output passes `browser_safety.scrub` and hard length caps.

Retries are for idempotent reads (GET/DELETE) on connection errors and
502/503/504 only. A POST that may have reached the browser is never
replayed.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .browser_safety import UrlPolicy, UrlRejected, scrub, validate_url

ENGINE = "camofox"
DEFAULT_BROWSER = "browser_verify"
ACTIONS = ("status", "create_tab", "navigate", "snapshot", "click", "type",
           "scroll", "screenshot", "list_tabs", "close_tab", "close_all")
MUTATING_ACTIONS = frozenset({"click", "type"})

_AGENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TAB_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_REF_RE = re.compile(r"^e\d{1,6}$")
_MAX_SELECTOR = 300
_MAX_TYPE_TEXT = 2_000
_RETRY_STATUSES = frozenset({502, 503, 504})
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class CamofoxError(Exception):
    """A classified failure. `code` is stable and machine-readable."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False,
                 status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.status = status


def _env_bool(raw: str, name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class CamofoxConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:9377"
    access_key_file: str = "~/.config/camofox/access.key"
    #: Strict hostname allowlist. Empty refuses every URL: the pilot must
    #: name its domains.
    allowed_domains: tuple[str, ...] = ("example.com",)
    allow_loopback_targets: bool = False
    allow_private_targets: bool = False
    request_timeout_seconds: float = 30.0
    retries: int = 1
    max_tabs_per_agent: int = 2
    max_snapshot_chars: int = 8_000
    screenshots_enabled: bool = False
    artifact_dir: str = ""
    user_prefix: str = "tmcp"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CamofoxConfig":
        env = dict(os.environ if env is None else env)

        def get(key: str) -> str | None:
            return env.get("TERMINAL_MCP_CAMOFOX_" + key)

        values: dict[str, Any] = {}
        for key in ("enabled", "allow_loopback_targets", "allow_private_targets",
                    "screenshots_enabled"):
            raw = get(key.upper())
            if raw is not None:
                values[key] = _env_bool(raw, "TERMINAL_MCP_CAMOFOX_" + key.upper())
        for key in ("base_url", "access_key_file", "artifact_dir", "user_prefix"):
            raw = get(key.upper())
            if raw is not None:
                values[key] = raw.strip()
        raw = get("ALLOWED_DOMAINS")
        if raw is not None:
            values["allowed_domains"] = tuple(
                d.strip().lower().rstrip(".") for d in raw.split(",") if d.strip())
        bounds = {"request_timeout_seconds": (float, 2.0, 120.0),
                  "retries": (int, 0, 3),
                  "max_tabs_per_agent": (int, 1, 4),
                  "max_snapshot_chars": (int, 500, 50_000)}
        for key, (kind, low, high) in bounds.items():
            raw = get(key.upper())
            if raw is None:
                continue
            value = kind(raw)
            if not low <= value <= high:
                raise ValueError(f"TERMINAL_MCP_CAMOFOX_{key.upper()} must be between {low} and {high}")
            values[key] = value
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("TERMINAL_MCP_CAMOFOX_BASE_URL must be an http(s) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("TERMINAL_MCP_CAMOFOX_BASE_URL must not carry credentials or a query")
        host = parts.hostname.lower()
        if host != "localhost":
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError("TERMINAL_MCP_CAMOFOX_BASE_URL host must be localhost or an IP "
                                 "(loopback, SSH forward, or private network)") from None
            # The access key goes to this endpoint: never to the public internet.
            if ip.is_global or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ValueError("TERMINAL_MCP_CAMOFOX_BASE_URL must be loopback or private")
        if not re.fullmatch(r"[a-z0-9-]{1,16}", self.user_prefix):
            raise ValueError("TERMINAL_MCP_CAMOFOX_USER_PREFIX must be [a-z0-9-]{1,16}")
        for domain in self.allowed_domains:
            if not re.fullmatch(r"[a-z0-9.-]{1,253}", domain) or domain.startswith("."):
                raise ValueError(f"invalid allowed domain {domain!r}")

    def public_view(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "base_url": self.base_url,
                "allowed_domains": list(self.allowed_domains),
                "allow_loopback_targets": self.allow_loopback_targets,
                "allow_private_targets": self.allow_private_targets,
                "request_timeout_seconds": self.request_timeout_seconds,
                "retries": self.retries, "max_tabs_per_agent": self.max_tabs_per_agent,
                "max_snapshot_chars": self.max_snapshot_chars,
                "screenshots_enabled": self.screenshots_enabled}


def read_access_key(path: str) -> str:
    """Read the key, refusing a file other users can read."""
    key_path = Path(path).expanduser()
    try:
        info = key_path.stat()
    except OSError:
        raise CamofoxError("CAMOFOX_KEY_UNAVAILABLE", "access key file is missing") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CamofoxError("CAMOFOX_KEY_UNAVAILABLE",
                           "access key file must not be group/world accessible (chmod 600)")
    key = key_path.read_text(encoding="utf-8").strip()
    if len(key) < 24:
        raise CamofoxError("CAMOFOX_KEY_UNAVAILABLE", "access key is too short")
    return key


Transport = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, bytes, str]]


def _urllib_transport(method: str, url: str, body: bytes | None, headers: dict[str, str],
                      timeout: float) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    # No proxies: the worker is loopback/private and the key must not detour.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(4 * 1024 * 1024), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(64 * 1024), exc.headers.get("Content-Type", "") if exc.headers else ""


class CamofoxClient:
    """HTTP to the worker with auth, retries and error classification."""

    def __init__(self, config: CamofoxConfig, *, transport: Transport | None = None,
                 key_loader: Callable[[str], str] = read_access_key,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self._transport = transport or _urllib_transport
        self._key_loader = key_loader
        self._sleep = sleep

    def request(self, method: str, path: str, *, body: dict | None = None,
                query: dict[str, Any] | None = None, auth: bool = True,
                raw: bool = False, timeout: float | None = None) -> Any:
        url = self.config.base_url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        key = ""
        if auth:
            key = self._key_loader(self.config.access_key_file)
            headers["Authorization"] = "Bearer " + key
        idempotent = method in ("GET", "DELETE")
        attempts = 1 + (self.config.retries if idempotent else 0)
        deadline = timeout or self.config.request_timeout_seconds
        last: CamofoxError | None = None
        for attempt in range(attempts):
            try:
                status, payload, ctype = self._transport(method, url, data, headers, deadline)
            except (socket.timeout, TimeoutError):
                last = CamofoxError("CAMOFOX_TIMEOUT", "worker did not answer in time", retryable=True)
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    last = CamofoxError("CAMOFOX_TIMEOUT", "worker did not answer in time", retryable=True)
                else:
                    last = CamofoxError("CAMOFOX_UNAVAILABLE", "worker is not reachable", retryable=True)
            else:
                if status < 400:
                    if raw:
                        return payload, ctype
                    try:
                        return json.loads(payload or b"{}")
                    except ValueError:
                        raise CamofoxError("CAMOFOX_BAD_RESPONSE", "worker returned non-JSON",
                                           status=status) from None
                last = self._classify(status, payload, key)
            if not (last.retryable and attempt + 1 < attempts):
                break
            self._sleep(0.5 * (attempt + 1))
        assert last is not None
        raise last

    @staticmethod
    def _classify(status: int, payload: bytes, key: str) -> CamofoxError:
        try:
            message = str(json.loads(payload or b"{}").get("error", ""))
        except (ValueError, AttributeError):
            message = ""
        if key:
            message = message.replace(key, "<REDACTED>")
        message = scrub(message, 200)
        if status in (401, 403):
            return CamofoxError("CAMOFOX_AUTH_FAILED", "worker rejected the access key", status=status)
        if status in (404, 410):
            # 410: the worker restarted its browser and the tab is gone.
            return CamofoxError("CAMOFOX_TAB_NOT_FOUND", message or "tab not found", status=status)
        if status == 400:
            return CamofoxError("CAMOFOX_BAD_REQUEST", message, status=status)
        if status == 429 or (status == 503 and "limit" in message.lower()):
            return CamofoxError("CAMOFOX_LIMIT", message or "worker capacity reached", status=status)
        if status == 504:
            return CamofoxError("CAMOFOX_TIMEOUT", message or "worker timed out", status=status,
                                retryable=True)
        return CamofoxError("CAMOFOX_UPSTREAM_ERROR", message or f"HTTP {status}", status=status,
                            retryable=status in _RETRY_STATUSES)


def _state_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp"


@dataclass
class CamofoxBrowser:
    """The provider behind the `camofox_browser` tool."""

    config: CamofoxConfig
    client: CamofoxClient | None = None
    resolve_dns: bool = True
    _history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = CamofoxClient(self.config)

    # -- policy -------------------------------------------------------------

    def _policy(self) -> UrlPolicy:
        return UrlPolicy(allow_loopback=self.config.allow_loopback_targets,
                         allow_private_networks=self.config.allow_private_targets,
                         resolve_dns=self.resolve_dns)

    def check_url(self, url: str) -> str:
        candidate = validate_url(url, self._policy())
        host = (urllib.parse.urlsplit(candidate).hostname or "").lower().rstrip(".")
        if not any(host == d or host.endswith("." + d) for d in self.config.allowed_domains):
            raise CamofoxError("CAMOFOX_DOMAIN_NOT_ALLOWED",
                               f"{host} is not in the camofox allowed_domains pilot list")
        return candidate

    def _user(self, agent_id: str | None) -> str:
        if not isinstance(agent_id, str) or not _AGENT_RE.fullmatch(agent_id):
            raise CamofoxError("CAMOFOX_BAD_REQUEST", "agent_id must match [A-Za-z0-9._-]{1,64}")
        return f"{self.config.user_prefix}-{agent_id}"

    @staticmethod
    def _tab(tab_id: str | None) -> str:
        if not isinstance(tab_id, str) or not _TAB_RE.fullmatch(tab_id):
            raise CamofoxError("CAMOFOX_BAD_REQUEST", "tab_id is required and must be a tab id")
        return tab_id

    @staticmethod
    def _target(ref: str | None, selector: str | None) -> dict[str, str]:
        if ref:
            if not _REF_RE.fullmatch(ref):
                raise CamofoxError("CAMOFOX_BAD_REQUEST", "ref must look like e12 (from a snapshot)")
            return {"ref": ref}
        if selector:
            if len(selector) > _MAX_SELECTOR or any(ord(c) < 32 for c in selector):
                raise CamofoxError("CAMOFOX_BAD_REQUEST", "selector is too long or has control characters")
            return {"selector": selector}
        raise CamofoxError("CAMOFOX_BAD_REQUEST", "ref or selector is required")

    def _enforce_landing(self, user: str, tab_id: str, url: Any) -> str:
        """Close a tab that ended up off-policy (redirect, click, script)."""
        if not url:
            return ""
        try:
            self.check_url(str(url))
        except (UrlRejected, CamofoxError) as exc:
            try:
                self.client.request("DELETE", f"/tabs/{tab_id}", query={"userId": user})
            except CamofoxError:
                pass
            raise CamofoxError("CAMOFOX_LANDED_OFF_POLICY",
                               f"tab left the allowed targets ({exc.code}); tab closed") from None
        return scrub(url, 2_048)

    # -- actions ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"engine": ENGINE, "default_browser": DEFAULT_BROWSER,
                               "config": self.config.public_view()}
        try:
            health = self.client.request("GET", "/health", auth=False, timeout=5.0)
        except CamofoxError as exc:
            out.update(status="UNAVAILABLE", error=exc.code,
                       fallback=f"use {DEFAULT_BROWSER}/browser_screenshot")
            return out
        try:
            self.client.request("GET", "/tabs", query={"userId": f"{self.config.user_prefix}-status"},
                                timeout=5.0)
            auth = "OK"
        except CamofoxError as exc:
            auth = exc.code
        out.update(status="OK" if health.get("ok") and auth == "OK" else "DEGRADED",
                   auth=auth, browser_running=bool(health.get("browserRunning")),
                   active_tabs=health.get("activeTabs"), active_sessions=health.get("activeSessions"),
                   memory=health.get("memory"))
        return out

    def list_tabs(self, agent_id: str) -> dict[str, Any]:
        user = self._user(agent_id)
        data = self.client.request("GET", "/tabs", query={"userId": user})
        tabs = [{"tab_id": t.get("tabId") or t.get("targetId"), "url": scrub(t.get("url"), 2_048),
                 "title": scrub(t.get("title"), 300)} for t in (data.get("tabs") or [])]
        return {"status": "OK", "tabs": tabs, "count": len(tabs)}

    def create_tab(self, agent_id: str, url: str) -> dict[str, Any]:
        user = self._user(agent_id)
        target = self.check_url(url)
        if self.list_tabs(agent_id)["count"] >= self.config.max_tabs_per_agent:
            raise CamofoxError("CAMOFOX_TAB_LIMIT",
                               f"agent already has {self.config.max_tabs_per_agent} tabs; close one first")
        data = self.client.request("POST", "/tabs", body={
            "userId": user, "sessionKey": "tmcp-" + uuid.uuid4().hex[:8], "url": target})
        tab_id = data.get("tabId")
        if not tab_id:
            raise CamofoxError("CAMOFOX_BAD_RESPONSE", "worker returned no tabId")
        landed = self._enforce_landing(user, tab_id, data.get("url"))
        return {"status": "OK", "tab_id": tab_id, "url": landed,
                "http_status": data.get("httpStatus"), "navigation_ok": data.get("navigationOk")}

    def navigate(self, agent_id: str, tab_id: str, url: str) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        target = self.check_url(url)
        data = self.client.request("POST", f"/tabs/{tab}/navigate", body={"userId": user, "url": target})
        landed = self._enforce_landing(user, tab, data.get("url"))
        return {"status": "OK", "tab_id": tab, "url": landed, "http_status": data.get("httpStatus")}

    def snapshot(self, agent_id: str, tab_id: str, offset: int = 0) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        offset = max(0, int(offset or 0))
        data = self.client.request("GET", f"/tabs/{tab}/snapshot",
                                   query={"userId": user, "offset": offset or None})
        landed = self._enforce_landing(user, tab, data.get("url"))
        text = scrub(data.get("snapshot"), self.config.max_snapshot_chars)
        return {"status": "OK", "tab_id": tab, "url": landed, "snapshot": text,
                "refs_count": data.get("refsCount"), "truncated": bool(data.get("truncated")),
                "total_chars": data.get("totalChars"), "next_offset": data.get("nextOffset")}

    def click(self, agent_id: str, tab_id: str, ref: str | None = None,
              selector: str | None = None) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        body = {"userId": user, **self._target(ref, selector)}
        data = self.client.request("POST", f"/tabs/{tab}/click", body=body)
        landed = self._enforce_landing(user, tab, data.get("url"))
        return {"status": "OK", "tab_id": tab, "url": landed, "mutations": ["click"]}

    def type_text(self, agent_id: str, tab_id: str, text: str, ref: str | None = None,
                  selector: str | None = None, submit: bool = False) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        if not isinstance(text, str) or len(text) > _MAX_TYPE_TEXT:
            raise CamofoxError("CAMOFOX_BAD_REQUEST", f"text is required, at most {_MAX_TYPE_TEXT} chars")
        body = {"userId": user, "text": text, "submit": bool(submit), **self._target(ref, selector)}
        data = self.client.request("POST", f"/tabs/{tab}/type", body=body)
        landed = self._enforce_landing(user, tab, data.get("url"))
        # The typed text itself is not echoed: it may be a credential.
        return {"status": "OK", "tab_id": tab, "url": landed, "typed_chars": len(text),
                "submitted": bool(submit), "mutations": ["type"]}

    def scroll(self, agent_id: str, tab_id: str, direction: str = "down",
               amount: int | None = None) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        if direction not in ("up", "down", "left", "right"):
            raise CamofoxError("CAMOFOX_BAD_REQUEST", "direction must be up/down/left/right")
        body: dict[str, Any] = {"userId": user, "direction": direction}
        if amount is not None:
            body["amount"] = max(1, min(int(amount), 5_000))
        # A healthy scroll takes well under a second; do not make the agent
        # wait out upstream's 30 s wheel hang (see the hint in run()).
        self.client.request("POST", f"/tabs/{tab}/scroll", body=body,
                            timeout=min(10.0, self.config.request_timeout_seconds))
        return {"status": "OK", "tab_id": tab, "direction": direction}

    def screenshot(self, agent_id: str, tab_id: str) -> dict[str, Any]:
        if not self.config.screenshots_enabled:
            raise CamofoxError("SCREENSHOTS_DISABLED",
                               "set TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED=1 (raw pixels are not redacted)")
        user, tab = self._user(agent_id), self._tab(tab_id)
        payload, _ctype = self.client.request("GET", f"/tabs/{tab}/screenshot",
                                              query={"userId": user}, raw=True)
        if not payload.startswith(_PNG_MAGIC):
            raise CamofoxError("CAMOFOX_BAD_RESPONSE", "worker did not return a PNG")
        out_dir = Path(self.config.artifact_dir).expanduser() if self.config.artifact_dir \
            else _state_dir() / "browser-artifacts"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = out_dir / f"camofox-{int(time.time())}-{uuid.uuid4().hex[:8]}.png"
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        return {"status": "OK", "tab_id": tab, "screenshot": str(path), "bytes": len(payload)}

    def close_tab(self, agent_id: str, tab_id: str) -> dict[str, Any]:
        user, tab = self._user(agent_id), self._tab(tab_id)
        self.client.request("DELETE", f"/tabs/{tab}", query={"userId": user})
        return {"status": "OK", "tab_id": tab, "closed": True}

    def close_all(self, agent_id: str) -> dict[str, Any]:
        user = self._user(agent_id)
        try:
            self.client.request("DELETE", f"/sessions/{user}")
        except CamofoxError as exc:
            if exc.code != "CAMOFOX_TAB_NOT_FOUND":
                raise
        return {"status": "OK", "closed_all": True}

    # -- dispatch -----------------------------------------------------------

    def run(self, action: str, *, agent_id: str | None = None, url: str | None = None,
            tab_id: str | None = None, ref: str | None = None, selector: str | None = None,
            text: str | None = None, submit: bool = False, direction: str = "down",
            amount: int | None = None, offset: int = 0,
            allow_mutations: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        try:
            if action not in ACTIONS:
                raise CamofoxError("CAMOFOX_BAD_REQUEST", f"action must be one of {list(ACTIONS)}")
            if not self.config.enabled:
                raise CamofoxError("CAMOFOX_DISABLED", "set TERMINAL_MCP_CAMOFOX_ENABLED=1")
            if action in MUTATING_ACTIONS and not allow_mutations:
                raise CamofoxError("CAMOFOX_MUTATION_NOT_ALLOWED",
                                   f"{action} changes the page; pass allow_mutations=true")
            if action == "status":
                result = self.status()
            elif action == "create_tab":
                result = self.create_tab(agent_id, url)
            elif action == "navigate":
                result = self.navigate(agent_id, tab_id, url)
            elif action == "snapshot":
                result = self.snapshot(agent_id, tab_id, offset)
            elif action == "click":
                result = self.click(agent_id, tab_id, ref, selector)
            elif action == "type":
                result = self.type_text(agent_id, tab_id, text, ref, selector, submit)
            elif action == "scroll":
                result = self.scroll(agent_id, tab_id, direction, amount)
            elif action == "screenshot":
                result = self.screenshot(agent_id, tab_id)
            elif action == "list_tabs":
                result = self.list_tabs(agent_id)
            elif action == "close_tab":
                result = self.close_tab(agent_id, tab_id)
            else:
                result = self.close_all(agent_id)
        except UrlRejected as exc:
            result = {"status": "ERROR", "error": exc.code, "detail": scrub(exc.detail, 300)}
        except CamofoxError as exc:
            result = {"status": "ERROR", "error": exc.code, "detail": scrub(exc.detail, 300)}
            if exc.status is not None:
                result["http_status"] = exc.status
            if action == "scroll" and exc.code in ("CAMOFOX_UPSTREAM_ERROR", "CAMOFOX_TIMEOUT"):
                # Upstream v1.18.1 holds the wheel until its 30 s handler
                # timeout when the page cannot scroll or an input has focus.
                result["hint"] = ("upstream wheel timeout: page not scrollable or an input "
                                  "has focus; scroll before typing")
        except (TypeError, ValueError) as exc:
            result = {"status": "ERROR", "error": "CAMOFOX_BAD_REQUEST", "detail": scrub(exc, 300)}
        if result.get("status") == "ERROR":
            result["fallback"] = f"{DEFAULT_BROWSER}/browser_screenshot remain available"
        result.setdefault("engine", ENGINE)
        result["action"] = action
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        self._history.append({"action": action, "status": result.get("status"),
                              "error": result.get("error"), "duration_ms": result["duration_ms"]})
        del self._history[:-20]
        return result


def register_camofox_tools(server: Any, provider: CamofoxBrowser) -> dict[str, Any]:
    """Register `camofox_browser`. Callers only do this when the flag is on."""

    @server.tool()
    def camofox_browser(action: str, agent_id: str | None = None, url: str | None = None,
                        tab_id: str | None = None, ref: str | None = None,
                        selector: str | None = None, text: str | None = None,
                        submit: bool = False, direction: str = "down",
                        amount: int | None = None, offset: int = 0,
                        allow_mutations: bool = False) -> dict:
        """Optional stateful Camofox tab for one agent (pilot; allowlisted domains only).

        action: status | create_tab | navigate | snapshot | click | type | scroll |
        screenshot | list_tabs | close_tab | close_all. Every action but status needs
        agent_id; tab actions need tab_id. click/type require allow_mutations=true.
        Snapshot refs (e12) feed click/type. Close tabs when done (close_all drops
        the agent's whole browser context). browser_verify stays the default browser."""
        return provider.run(action, agent_id=agent_id, url=url, tab_id=tab_id, ref=ref,
                            selector=selector, text=text, submit=submit, direction=direction,
                            amount=amount, offset=offset, allow_mutations=allow_mutations)

    return {"camofox_browser": camofox_browser}


def build_from_env(env: dict[str, str] | None = None) -> CamofoxBrowser | None:
    """The provider when TERMINAL_MCP_CAMOFOX_ENABLED is on, else None.

    A malformed configuration disables the provider rather than taking the
    MCP server down: the default browser does not depend on it."""
    try:
        config = CamofoxConfig.from_env(env)
    except (TypeError, ValueError):
        import logging
        logging.getLogger(__name__).warning("camofox provider disabled: invalid configuration")
        return None
    return CamofoxBrowser(config) if config.enabled else None
