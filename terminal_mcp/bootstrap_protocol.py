"""`terminalmcp://` — what the Bootstrap helper is allowed to be asked to do.

This module exists because the helper's input is a URL that **any web page
open on the machine can trigger**. Windows will happily hand
`terminalmcp://anything` to a registered handler; the handler is the only
thing standing between a hostile page and a process running as
Administrator.

So the grammar is deliberately tiny and the parser is deliberately
suspicious. There is no field that carries a command, a script, a path, an
argument list, a package name or a URL to fetch code from. The only inputs
are:

    terminalmcp://enroll?handle=<32 hex>&controller=<https origin>

`action` is a closed set. `handle` is a fixed-width hex id that is useless
without a matching server-side record. `controller` must appear in the
allowlist the helper was installed with, which is what prevents a page
from pointing the helper at an attacker's controller and having it install
that instead.

Everything the helper actually needs -- the enrollment code, the profile,
the controller candidates -- it fetches ITSELF, over HTTPS, by redeeming
the handle. It never accepts any of that from the page.

Parsing lives here, in Python, rather than in the PowerShell helper for
one reason: this is the part that must be exhaustively tested, and it can
be. tests/test_bootstrap_helper.py is mostly a list of URLs that must be
refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

SCHEME = "terminalmcp"

# Closed set. An unknown action is refused, never ignored and never
# passed through to something that might interpret it.
ACTION_ENROLL = "enroll"
ACTION_REPAIR = "repair"
ACTION_STATUS = "status"
ACTIONS = (ACTION_ENROLL, ACTION_REPAIR, ACTION_STATUS)

HANDLE_BYTES = 16  # 128 bits
_HANDLE_RE = re.compile(r"^[0-9a-f]{32}$")

# Rejection reasons. Distinct so the helper can log WHICH rule refused a
# request -- without logging the request, which may be hostile input.
ERR_SCHEME = "bad_scheme"
ERR_ACTION = "unknown_action"
ERR_HANDLE = "bad_handle"
ERR_CONTROLLER = "controller_not_allowed"
ERR_EXTRA = "unexpected_parameter"
ERR_MALFORMED = "malformed_url"


class ProtocolError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class BootstrapRequest:
    action: str
    handle: str | None
    controller: str

    def to_dict(self) -> dict[str, Any]:
        # `handle` is deliberately absent: this dict exists for logging and
        # for the helper's own status output, and a handle is a credential
        # for its 120 seconds of life.
        return {"action": self.action, "controller": self.controller,
                "has_handle": bool(self.handle)}


def normalize_origin(value: str) -> str:
    """scheme://host[:port], lower-cased, no path, no trailing slash.

    Comparing origins as raw strings is how allowlists get bypassed --
    `https://ctl.example/`, `https://CTL.example` and
    `https://ctl.example/x` are the same origin and must compare equal,
    while `https://ctl.example.evil.com` must not.
    """
    parts = urlsplit(str(value or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProtocolError(ERR_CONTROLLER, "controller must be an http(s) origin")
    return f"{parts.scheme}://{parts.netloc}".lower()


def parse(url: str, *, allowed_controllers: "list[str] | tuple[str, ...]") -> BootstrapRequest:
    """Parse and VALIDATE a terminalmcp:// URL. Raises ProtocolError on
    anything that is not exactly the documented grammar.

    `allowed_controllers` is the helper's own install-time allowlist. An
    empty allowlist refuses everything: a helper that does not know which
    controller owns it must do nothing, not trust whoever asked first.
    """
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ProtocolError(ERR_MALFORMED, str(exc)) from exc
    if parts.scheme.lower() != SCHEME:
        raise ProtocolError(ERR_SCHEME, f"expected {SCHEME}://")

    # Windows hands the handler the whole URL; the action may land in
    # netloc ("terminalmcp://enroll?x") or in path ("terminalmcp:enroll").
    action = (parts.netloc or parts.path.lstrip("/")).strip().lower()
    # A path AFTER the action is not part of the grammar -- refuse rather
    # than silently ignore, since ignoring is how traversal payloads ride
    # along unnoticed.
    if parts.netloc and parts.path.strip("/"):
        raise ProtocolError(ERR_EXTRA, "no path segments are accepted")
    if action not in ACTIONS:
        raise ProtocolError(ERR_ACTION, f"action must be one of {', '.join(ACTIONS)}")
    if parts.fragment:
        raise ProtocolError(ERR_EXTRA, "fragments are not accepted")

    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=False)
    unexpected = set(query) - {"handle", "controller"}
    if unexpected:
        # The important case: a future field like `cmd=` or `script=` must
        # be a hard refusal, not something a permissive parser drops.
        raise ProtocolError(ERR_EXTRA, f"unexpected parameter(s): {', '.join(sorted(unexpected))}")
    # Repeated parameters are ambiguous, and ambiguity is how one layer
    # reads `handle` #1 while another reads #2.
    for key, values in query.items():
        if len(values) != 1:
            raise ProtocolError(ERR_EXTRA, f"{key} given more than once")

    controller = normalize_origin(query.get("controller", [""])[0])
    allowed = {normalize_origin(entry) for entry in (allowed_controllers or ()) if str(entry).strip()}
    if not allowed:
        raise ProtocolError(ERR_CONTROLLER, "this helper has no controller allowlist configured")
    if controller not in allowed:
        raise ProtocolError(ERR_CONTROLLER, "controller is not in this helper's allowlist")

    handle = query.get("handle", [""])[0].strip().lower()
    if action == ACTION_ENROLL:
        if not _HANDLE_RE.match(handle):
            raise ProtocolError(ERR_HANDLE, "handle must be 32 lowercase hex characters")
    elif handle:
        raise ProtocolError(ERR_EXTRA, f"{action} takes no handle")

    return BootstrapRequest(action=action, handle=handle or None, controller=controller)


def build_enroll_url(*, controller: str, handle: str) -> str:
    """The URL the Dashboard hands to the browser. Built here so the page
    cannot assemble a shape the helper would refuse -- and so the two can
    never drift apart."""
    if not _HANDLE_RE.match(str(handle or "")):
        raise ValueError("handle must be 32 lowercase hex characters")
    origin = normalize_origin(controller)
    from urllib.parse import quote
    return f"{SCHEME}://{ACTION_ENROLL}?handle={handle}&controller={quote(origin, safe='')}"
