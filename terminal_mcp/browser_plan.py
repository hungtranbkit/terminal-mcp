"""Declarative browser-verification plan: schema, bounds and URL policy.

This module is the SECURITY CORE of the browser gateway and deliberately
contains no I/O at all -- it is pure validation, so every rule below is
unit-testable without a browser, a node, or a network.

WHY DECLARATIVE: the gateway's whole premise is that ChatGPT (and Claude/
Codex through it) never gets raw Python, raw shell, or a pile of low-level
CDP verbs. It gets ONE verb -- "verify this plan" -- and a plan is data:
a URL, a viewport, a bounded list of steps drawn from a closed vocabulary.
A value in a plan can never become code, because nothing here is ever
interpolated into a script (see browser_exec.py, which reads the plan as
JSON and dispatches on the op name).

EVERYTHING IS BOUNDED. Step count, selector length, value length, wait
seconds, timeout, viewport size, screenshot name. An unbounded field is a
way to hang a node or fill a disk, and this surface is reachable from a
chat client.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Bounds. Every one of these is an upper limit on something a caller controls.
# ---------------------------------------------------------------------------

MAX_STEPS = 40
MAX_SELECTOR_CHARS = 256
MAX_VALUE_CHARS = 4096
MAX_EXPECT_CHARS = 1024
MAX_URL_CHARS = 2048
MAX_WAIT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 45.0

# The bounded window the CALLER's synchronous call may occupy. A plan may
# legitimately need longer (MAX_TIMEOUT_SECONDS); it then keeps running and
# the call returns PENDING with a resume handle. 45s is the contract ceiling.
MAX_SYNC_WAIT_SECONDS = 45.0
DEFAULT_SYNC_WAIT_SECONDS = 30.0

DEFAULT_VIEWPORT = (1348, 768)
MIN_VIEWPORT = (320, 240)
MAX_VIEWPORT = (3840, 2160)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Steps that CHANGE the page. They are separated from read-only steps
#: because the gateway requires them to be opted into explicitly
#: (`allow_mutations`) and audits each one -- "explicit and auditable
#: mutations" is a requirement, not a nicety: a chat client should not be
#: able to click a button on a logged-in page as a side effect of what
#: reads like a health check.
MUTATING_OPS = frozenset({"click", "fill", "press"})

READONLY_OPS = frozenset({
    "navigate", "wait",
    "assert_text", "assert_value", "assert_visible", "assert_url",
})

ALL_OPS = MUTATING_OPS | READONLY_OPS

ASSERT_OPS = frozenset({"assert_text", "assert_value", "assert_visible", "assert_url"})

#: Keys `press` accepts. A closed list, because press feeds a CDP key
#: dispatch and "whatever the caller typed" is not a key name.
ALLOWED_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Home", "End", "PageUp", "PageDown",
})

WAIT_STATES = frozenset({"load", "network_idle"})

SCREENSHOT_POLICIES = frozenset({"never", "on_failure", "always"})

#: Only these two schemes ever reach a browser tab. Everything else is
#: rejected by name below so the rejection is explicit in the error.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Rejected loudly rather than by omission -- these are the schemes that
#: turn "open a URL" into local file read, browser-internal control, or
#: script execution.
DANGEROUS_SCHEMES = frozenset({
    "file", "chrome", "chrome-extension", "chrome-search", "chrome-untrusted",
    "devtools", "javascript", "data", "blob", "about", "view-source",
    "ftp", "ws", "wss", "filesystem", "intent", "content", "resource",
})

_SCREENSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Cloud metadata endpoints. Blocked even when a private-range allowlist is
#: configured: "let me reach localhost dev" must never quietly become "let
#: me read this box's instance credentials".
METADATA_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "metadata"})


class BrowserGatewayError(Exception):
    """A typed, code-carrying failure -- the same shape the rest of the
    codebase returns ({"error": CODE, ...}), so a caller never needs a
    second error-handling path for the browser surface."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        out.update(self.detail)
        return out


def _invalid(message: str, **detail: Any) -> BrowserGatewayError:
    return BrowserGatewayError("BROWSER_INVALID_PLAN", message, **detail)


# ---------------------------------------------------------------------------
# URL policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UrlPolicy:
    """What the gateway is allowed to point a browser at.

    `allow_hosts` exists for the real, stated case: verifying a local dev
    target (`127.0.0.1:3200`, a LAN staging box). It is an explicit
    operator allowlist, never a default -- without it every private,
    loopback, link-local and unique-local destination is refused, which is
    the SSRF rule. An entry may be `host` or `host:port`; a bare host
    allows any port on it.
    """

    allow_private: bool = False
    allow_hosts: frozenset[str] = frozenset()
    resolve: Callable[[str], Iterable[str]] | None = None

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "UrlPolicy":
        import os

        src = env if env is not None else dict(os.environ)
        raw = (src.get("TERMINAL_MCP_BROWSER_ALLOW_HOSTS") or "").strip()
        hosts = frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
        allow_private = (src.get("TERMINAL_MCP_BROWSER_ALLOW_PRIVATE") or "").strip() in {"1", "true", "yes"}
        return UrlPolicy(allow_private=allow_private, allow_hosts=hosts)


def _rejected(message: str, **detail: Any) -> BrowserGatewayError:
    return BrowserGatewayError("BROWSER_URL_REJECTED", message, **detail)


def _is_private_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _host_allowlisted(host: str, port: int | None, policy: UrlPolicy) -> bool:
    host = host.lower()
    if host in policy.allow_hosts:
        return True
    return port is not None and f"{host}:{port}" in policy.allow_hosts


def validate_url(url: Any, policy: UrlPolicy | None = None) -> str:
    """Return `url` unchanged if a browser may open it, else raise.

    Order matters: scheme first (so `javascript:` is reported as a scheme
    rejection, not a parse failure), then the metadata blocklist (which no
    allowlist overrides), then the allowlist, then the SSRF range check.
    """
    policy = policy or UrlPolicy()
    if not isinstance(url, str) or not url.strip():
        raise _rejected("url must be a non-empty string")
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        raise _rejected(f"url exceeds {MAX_URL_CHARS} characters", length=len(url))
    if any(ch in url for ch in ("\n", "\r", "\t", "\x00")):
        raise _rejected("url must not contain control characters")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise _rejected("url must be absolute and start with http:// or https://", url=url)
    if scheme in DANGEROUS_SCHEMES or scheme not in ALLOWED_SCHEMES:
        raise _rejected(f"scheme {scheme!r} is not allowed; only http and https are",
                        scheme=scheme)

    host = (parsed.hostname or "").lower()
    if not host:
        raise _rejected("url has no host", url=url)
    try:
        port = parsed.port
    except ValueError:
        raise _rejected("url has an invalid port") from None

    if host in METADATA_HOSTS or host.endswith(".internal"):
        raise _rejected("cloud metadata endpoints are never reachable from the browser gateway",
                        host=host)

    if _host_allowlisted(host, port, policy):
        return url
    if policy.allow_private:
        return url

    if _is_private_address(host):
        raise _rejected(
            "private/loopback addresses are blocked; add the host to "
            "TERMINAL_MCP_BROWSER_ALLOW_HOSTS to verify a local dev target",
            host=host,
        )
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise _rejected(
            "localhost is blocked; add it to TERMINAL_MCP_BROWSER_ALLOW_HOSTS "
            "to verify a local dev target",
            host=host,
        )

    # A public NAME that resolves into a private range is the DNS-rebinding
    # shape of the same attack, so the names are checked too when a resolver
    # is available. Resolution failure is not fatal: the browser's own
    # request will fail anyway, and a hard failure here would make the
    # gateway unusable behind a split-horizon resolver.
    resolver = policy.resolve or _default_resolve
    try:
        addresses = list(resolver(host))
    except Exception:  # noqa: BLE001 -- see above; unresolvable is the browser's problem
        addresses = []
    for addr in addresses:
        if addr in METADATA_HOSTS or _is_private_address(addr):
            raise _rejected(
                f"host {host!r} resolves to blocked address {addr}",
                host=host, address=addr,
            )
    return url


def _default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


# ---------------------------------------------------------------------------
# Screenshot artifact naming
# ---------------------------------------------------------------------------

def validate_screenshot_name(name: Any) -> str:
    """A caller may name an artifact; it may never place one.

    No separators, no traversal, no leading dot, bounded length. The
    gateway joins this to its OWN artifact directory and re-checks
    containment (browser_gateway.artifact_path), so this is the first of
    two independent guards rather than the only one.
    """
    if name is None:
        return ""
    if not isinstance(name, str):
        raise _invalid("screenshot name must be a string")
    name = name.strip()
    if not name:
        return ""
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise _invalid("screenshot name must not contain path separators or traversal",
                       name=name)
    if not _SCREENSHOT_NAME_RE.match(name):
        raise _invalid("screenshot name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                       name=name)
    return name


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    url: str
    steps: tuple[dict[str, Any], ...]
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    screenshot: str = "on_failure"
    screenshot_name: str = ""
    allow_mutations: bool = False
    node: str | None = None
    session: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def mutating(self) -> tuple[str, ...]:
        return tuple(s["op"] for s in self.steps if s["op"] in MUTATING_OPS)

    def to_payload(self) -> dict[str, Any]:
        """The exact JSON handed to the executor. No caller string is ever
        formatted into code -- this is read back with json.load()."""
        return {
            "url": self.url,
            "steps": [dict(s) for s in self.steps],
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "timeout_seconds": self.timeout_seconds,
            "screenshot": self.screenshot,
        }


def _as_str(value: Any, field_name: str, *, limit: int, required: bool = True) -> str:
    if value is None:
        if required:
            raise _invalid(f"{field_name} is required")
        return ""
    if not isinstance(value, str):
        raise _invalid(f"{field_name} must be a string")
    if required and not value.strip():
        raise _invalid(f"{field_name} must not be empty")
    if len(value) > limit:
        raise _invalid(f"{field_name} exceeds {limit} characters", length=len(value))
    if "\x00" in value:
        raise _invalid(f"{field_name} must not contain null bytes")
    return value


def _validate_selector(step: dict[str, Any], index: int) -> str:
    selector = step.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise _invalid(f"step {index}: selector is required", step=index)
    if len(selector) > MAX_SELECTOR_CHARS:
        raise _invalid(f"step {index}: selector exceeds {MAX_SELECTOR_CHARS} characters",
                       step=index)
    if "\x00" in selector or "\n" in selector:
        raise _invalid(f"step {index}: selector must not contain control characters",
                       step=index)
    return selector


def _expectation(step: dict[str, Any], index: int) -> dict[str, Any]:
    """Assertions take exactly one of equals/contains (or `visible` for
    assert_visible). Requiring exactly one keeps a silently-unchecked
    assertion -- a test that always passes -- impossible to express."""
    present = [k for k in ("equals", "contains") if k in step and step[k] is not None]
    if len(present) != 1:
        raise _invalid(f"step {index}: exactly one of equals/contains is required",
                       step=index)
    key = present[0]
    value = _as_str(step[key], f"step {index}: {key}", limit=MAX_EXPECT_CHARS)
    return {key: value}


def validate_plan(payload: Any, *, policy: UrlPolicy | None = None) -> Plan:
    """Normalize and bounds-check one verification plan, or raise
    BrowserGatewayError('BROWSER_INVALID_PLAN' | 'BROWSER_URL_REJECTED')."""
    if not isinstance(payload, dict):
        raise _invalid("plan must be an object")

    url = validate_url(payload.get("url"), policy)

    raw_steps = payload.get("steps") or []
    if not isinstance(raw_steps, (list, tuple)):
        raise _invalid("steps must be a list")
    if len(raw_steps) > MAX_STEPS:
        raise _invalid(f"plan has {len(raw_steps)} steps; the maximum is {MAX_STEPS}",
                       steps=len(raw_steps))

    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise _invalid(f"step {index}: must be an object", step=index)
        op = raw.get("op")
        if not isinstance(op, str) or op not in ALL_OPS:
            raise _invalid(
                f"step {index}: unknown op {op!r}; allowed: {sorted(ALL_OPS)}",
                step=index, op=op if isinstance(op, str) else None,
            )
        step: dict[str, Any] = {"op": op}

        if op == "navigate":
            step["url"] = validate_url(raw.get("url"), policy)
        elif op == "click":
            step["selector"] = _validate_selector(raw, index)
        elif op == "fill":
            step["selector"] = _validate_selector(raw, index)
            step["value"] = _as_str(raw.get("value"), f"step {index}: value",
                                    limit=MAX_VALUE_CHARS, required=False)
            # A filled value may be a password or token. It is carried to the
            # executor but NEVER echoed back in a result (see summarize()).
            step["secret"] = bool(raw.get("secret", False))
        elif op == "press":
            key = raw.get("key")
            if key not in ALLOWED_KEYS:
                raise _invalid(
                    f"step {index}: key {key!r} is not allowed; allowed: {sorted(ALLOWED_KEYS)}",
                    step=index,
                )
            step["key"] = key
        elif op == "wait":
            has_selector = bool(raw.get("selector"))
            has_seconds = raw.get("seconds") is not None
            has_state = raw.get("state") is not None
            if sum((has_selector, has_seconds, has_state)) != 1:
                raise _invalid(
                    f"step {index}: wait takes exactly one of selector/seconds/state",
                    step=index,
                )
            if has_selector:
                step["selector"] = _validate_selector(raw, index)
            elif has_seconds:
                try:
                    seconds = float(raw["seconds"])
                except (TypeError, ValueError):
                    raise _invalid(f"step {index}: seconds must be a number", step=index) from None
                if not 0 < seconds <= MAX_WAIT_SECONDS:
                    raise _invalid(
                        f"step {index}: seconds must be in (0, {MAX_WAIT_SECONDS}]", step=index)
                step["seconds"] = seconds
            else:
                state = raw.get("state")
                if state not in WAIT_STATES:
                    raise _invalid(
                        f"step {index}: state must be one of {sorted(WAIT_STATES)}", step=index)
                step["state"] = state
        elif op == "assert_url":
            step.update(_expectation(raw, index))
        elif op == "assert_visible":
            step["selector"] = _validate_selector(raw, index)
            visible = raw.get("visible", True)
            if not isinstance(visible, bool):
                raise _invalid(f"step {index}: visible must be a boolean", step=index)
            step["visible"] = visible
        else:  # assert_text / assert_value
            step["selector"] = _validate_selector(raw, index)
            step.update(_expectation(raw, index))

        steps.append(step)

    if not any(s["op"] in ASSERT_OPS for s in steps):
        # A "verification" with nothing asserted is a page visit dressed up
        # as a check, and it would always report PASS.
        raise _invalid("a verification plan must contain at least one assert_* step")

    viewport = _validate_viewport(payload.get("viewport"))

    timeout = payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise _invalid("timeout_seconds must be a number") from None
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise _invalid(f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS}]")

    screenshot = payload.get("screenshot", "on_failure")
    if screenshot not in SCREENSHOT_POLICIES:
        raise _invalid(f"screenshot must be one of {sorted(SCREENSHOT_POLICIES)}")

    allow_mutations = bool(payload.get("allow_mutations", False))
    plan = Plan(
        url=url,
        steps=tuple(steps),
        viewport=viewport,
        timeout_seconds=timeout,
        screenshot=screenshot,
        screenshot_name=validate_screenshot_name(payload.get("screenshot_name")),
        allow_mutations=allow_mutations,
        node=_as_str(payload.get("node"), "node", limit=128, required=False) or None,
        session=_as_str(payload.get("session"), "session", limit=128, required=False) or None,
    )
    if plan.mutating and not allow_mutations:
        raise BrowserGatewayError(
            "BROWSER_MUTATION_NOT_ALLOWED",
            "plan contains page-mutating steps; pass allow_mutations=true to authorize them",
            mutating_steps=list(plan.mutating),
        )
    return plan


def _validate_viewport(raw: Any) -> tuple[int, int]:
    if raw is None:
        return DEFAULT_VIEWPORT
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        width, height = raw
    elif isinstance(raw, dict):
        width, height = raw.get("width"), raw.get("height")
    else:
        raise _invalid("viewport must be {width, height}")
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        raise _invalid("viewport width/height must be integers") from None
    if not (MIN_VIEWPORT[0] <= width <= MAX_VIEWPORT[0]):
        raise _invalid(f"viewport width must be in [{MIN_VIEWPORT[0]}, {MAX_VIEWPORT[0]}]")
    if not (MIN_VIEWPORT[1] <= height <= MAX_VIEWPORT[1]):
        raise _invalid(f"viewport height must be in [{MIN_VIEWPORT[1]}, {MAX_VIEWPORT[1]}]")
    return (width, height)
