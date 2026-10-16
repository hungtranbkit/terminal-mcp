"""Strict read-only adaptation for cached ChatGPT browser arguments."""
from __future__ import annotations
from typing import Any

def normalize_cached_browser_args(action: str, args: dict | None, *, steps: list | None = None, viewport: dict | None = None, screenshot: str | None = None, allow_mutations: bool = False) -> dict | None:
    action = action.strip().lower()
    # Cached clients may serialize their screenshot default for every action.
    if action not in {"browser_verify", "verify"} and screenshot == "on_failure":
        screenshot = None
    supplied = steps is not None or viewport is not None or screenshot is not None or allow_mutations
    if not supplied:
        return args
    action = action.strip().lower()
    if action not in {"browser_verify", "verify", "browser_screenshot", "screenshot"}:
        raise ValueError("cached browser arguments require a browser action")
    if allow_mutations:
        raise ValueError("browser verification is read-only; mutations are unsupported")
    out = dict(args or {})
    def put(key: str, value: Any) -> None:
        if key in out and out[key] != value:
            raise ValueError("conflicting canonical and cached browser arguments")
        out[key] = value
    if steps is not None:
        if action not in {"browser_verify", "verify"}:
            raise ValueError("assertion steps require browser_verify")
        if not isinstance(steps, list) or len(steps) > 30:
            raise ValueError("steps must be a bounded list")
        assertions = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("invalid browser assertion step")
            kind = step.get("action")
            if kind in {"assert_text", "assert_title"}:
                value = step.get("text")
                if set(step) != {"action", "text"} or not isinstance(value, str) or not value.strip() or len(value) > 4000:
                    raise ValueError("invalid text assertion")
                assertions.append(("text contains: " if kind == "assert_text" else "title contains: ") + value)
            elif kind == "assert_status":
                value = step.get("status")
                if set(step) != {"action", "status"} or type(value) is not int or not 100 <= value <= 599:
                    raise ValueError("invalid HTTP status assertion")
                assertions.append("status is: " + str(value))
            else:
                raise ValueError("unsupported browser step; no step was executed")
        put("assertions", assertions)
    if viewport is not None:
        if not isinstance(viewport, dict) or set(viewport) != {"width", "height"}:
            raise ValueError("viewport must contain only width and height")
        for key in ("width", "height"):
            value = viewport[key]
            if type(value) is not int or not 1 <= value <= 8192:
                raise ValueError("invalid viewport dimension")
            put("viewport_" + key, value)
    if screenshot is not None:
        if action not in {"browser_verify", "verify"} or screenshot not in {"never", "always", "on_failure"}:
            raise ValueError("unsupported screenshot policy")
        put("screenshot", screenshot)
    return out
