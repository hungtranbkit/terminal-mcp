"""The STATIC executor that runs inside `browser-harness`.

This file is piped to the harness CLI verbatim, every time, unchanged. The
plan is NOT part of it: the plan arrives as JSON on disk, named by
TMCP_BROWSER_PLAN, and is read with json.load(). That is the entire reason
the gateway can accept selectors and values from a chat client -- there is
no template, no f-string, no shell word-splitting anywhere between the
caller's data and this dispatch loop, so caller data cannot become code.

Where data DOES have to cross into JavaScript (a CSS selector must reach
document.querySelector somehow), it crosses as a json.dumps()-encoded
string literal built by `js_selector_expression` below -- which is a pure
function, importable and unit-tested without a browser.

The harness pre-imports its helpers into this script's globals
(new_tab/goto_url/js/click_at_xy/fill_input/press_key/wait*/page_info/
capture_screenshot). They are resolved through `_helper()` so that
importing this module for tests -- where those globals do not exist --
never fails at import time.
"""
from __future__ import annotations

import json
import os
import time

RESULT_SENTINEL = "__TMCP_BROWSER_RESULT__"

#: Per-step ceiling for element waits inside the executor. The overall plan
#: timeout is enforced by the PARENT (which can kill the process group);
#: this one just keeps a single step from eating the whole budget.
STEP_ELEMENT_TIMEOUT = 10.0


def js_selector_expression(selector: str, body: str) -> str:
    """Build a JS expression that binds `el` to `selector`'s first match.

    `selector` is embedded as a JSON string literal, so quotes, backslashes,
    backticks, newlines, `</script>`, `${...}` and comment markers are all
    inert data to the JS parser. `body` is OURS -- one of the fixed snippets
    below -- and never comes from a caller.
    """
    literal = json.dumps(selector)
    return (
        "(() => { const el = document.querySelector(%s);"
        " if (!el) { return JSON.stringify({found: false}); }"
        " %s })()" % (literal, body)
    )


_TEXT_BODY = "return JSON.stringify({found: true, text: (el.innerText || el.textContent || '')});"
_VALUE_BODY = "return JSON.stringify({found: true, value: (el.value !== undefined ? String(el.value) : '')});"
_VISIBLE_BODY = (
    "const r = el.getBoundingClientRect();"
    " const s = window.getComputedStyle(el);"
    " const vis = r.width > 0 && r.height > 0 && s.visibility !== 'hidden'"
    " && s.display !== 'none' && Number(s.opacity) > 0;"
    " return JSON.stringify({found: true, visible: vis});"
)
_BOX_BODY = (
    "el.scrollIntoView({block: 'center', inline: 'center'});"
    " const r = el.getBoundingClientRect();"
    " return JSON.stringify({found: true, x: r.left + r.width / 2,"
    " y: r.top + r.height / 2, w: r.width, h: r.height});"
)


def _helper(name: str):
    fn = globals().get(name)
    if fn is None or not callable(fn):
        raise RuntimeError(f"browser-harness helper {name!r} is unavailable")
    return fn


def _js_object(expression: str) -> dict:
    raw = _helper("js")(expression)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"found": False, "raw": raw[:200]}
    return {"found": False}


def _matches(actual: str, step: dict) -> tuple[bool, str]:
    if "equals" in step:
        expected = step["equals"]
        return actual == expected, f"expected == {expected!r}"
    expected = step.get("contains", "")
    return expected in actual, f"expected contains {expected!r}"


def _truncate(value: str, limit: int = 160) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def run_plan(plan: dict) -> dict:
    """Execute one validated plan and return a compact, JSON-safe result."""
    started = time.monotonic()
    checks: list[dict] = []
    errors: list[str] = []
    artifact = ""
    status = "PASS"

    viewport = plan.get("viewport") or {}
    width = int(viewport.get("width") or 1348)
    height = int(viewport.get("height") or 768)

    # Viewport BEFORE the first navigation, so the page never lays out at
    # the wrong size and then reflows -- a 1348x768 assertion has to be
    # about a page that was always 1348x768.
    try:
        _helper("cdp")(
            "Emulation.setDeviceMetricsOverride",
            width=width, height=height, deviceScaleFactor=1, mobile=False,
        )
    except Exception as exc:  # noqa: BLE001 -- viewport override is best-effort
        errors.append(f"viewport: {_truncate(exc)}")

    opened_tab = None
    try:
        _helper("new_tab")(plan["url"])
        _helper("wait_for_load")(timeout=STEP_ELEMENT_TIMEOUT)
        try:
            opened_tab = _helper("current_tab")()
        except Exception:  # noqa: BLE001 -- cleanup is best-effort, never fatal
            opened_tab = None
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "ERROR", "checks": [], "errors": [f"navigate: {_truncate(exc)}"],
            "artifact": "", "elapsed_ms": int((time.monotonic() - started) * 1000),
            "page": {},
        }

    for index, step in enumerate(plan.get("steps") or []):
        op = step.get("op")
        entry: dict = {"i": index, "op": op, "ok": True}
        try:
            if op == "navigate":
                _helper("goto_url")(step["url"])
                _helper("wait_for_load")(timeout=STEP_ELEMENT_TIMEOUT)
            elif op == "wait":
                if "seconds" in step:
                    _helper("wait")(float(step["seconds"]))
                elif "state" in step:
                    if step["state"] == "network_idle":
                        _helper("wait_for_network_idle")()
                    else:
                        _helper("wait_for_load")(timeout=STEP_ELEMENT_TIMEOUT)
                else:
                    _helper("wait_for_element")(step["selector"], timeout=STEP_ELEMENT_TIMEOUT)
            elif op == "fill":
                _helper("fill_input")(step["selector"], step.get("value", ""))
            elif op == "press":
                _helper("press_key")(step["key"])
            elif op == "click":
                box = _js_object(js_selector_expression(step["selector"], _BOX_BODY))
                if not box.get("found"):
                    raise RuntimeError(f"no element matches {step['selector']!r}")
                _helper("click_at_xy")(box["x"], box["y"])
            elif op == "assert_text":
                found = _js_object(js_selector_expression(step["selector"], _TEXT_BODY))
                if not found.get("found"):
                    entry.update(ok=False, detail=f"no element matches {step['selector']!r}")
                else:
                    ok, expectation = _matches(found.get("text", ""), step)
                    entry.update(ok=ok, detail=expectation if ok
                                 else f"{expectation}, got {_truncate(found.get('text', ''))!r}")
            elif op == "assert_value":
                found = _js_object(js_selector_expression(step["selector"], _VALUE_BODY))
                if not found.get("found"):
                    entry.update(ok=False, detail=f"no element matches {step['selector']!r}")
                else:
                    ok, expectation = _matches(found.get("value", ""), step)
                    entry.update(ok=ok, detail=expectation if ok
                                 else f"{expectation}, got {_truncate(found.get('value', ''))!r}")
            elif op == "assert_visible":
                found = _js_object(js_selector_expression(step["selector"], _VISIBLE_BODY))
                actual = bool(found.get("visible")) if found.get("found") else False
                expected = bool(step.get("visible", True))
                entry.update(ok=actual == expected,
                             detail=f"visible={actual}, expected {expected}")
            elif op == "assert_url":
                info = _helper("page_info")() or {}
                actual = str(info.get("url", ""))
                ok, expectation = _matches(actual, step)
                entry.update(ok=ok, detail=expectation if ok
                             else f"{expectation}, got {_truncate(actual)!r}")
            else:
                entry.update(ok=False, detail=f"unsupported op {op!r}")
        except Exception as exc:  # noqa: BLE001 -- one bad step must not lose the rest
            entry.update(ok=False, detail=_truncate(exc))
            errors.append(f"step {index} ({op}): {_truncate(exc)}")
        checks.append(entry)
        if not entry["ok"]:
            status = "FAIL"

    policy = plan.get("screenshot", "on_failure")
    want_shot = policy == "always" or (policy == "on_failure" and status != "PASS")
    target = plan.get("screenshot_path") or ""
    if want_shot and target:
        try:
            _helper("capture_screenshot")(path=target)
            artifact = target
        except Exception as exc:  # noqa: BLE001 -- evidence is best-effort
            errors.append(f"screenshot: {_truncate(exc)}")

    page: dict = {}
    try:
        info = _helper("page_info")() or {}
        page = {"url": str(info.get("url", ""))[:512], "title": _truncate(info.get("title", ""), 120)}
    except Exception:  # noqa: BLE001
        page = {}

    # Close the tab THIS run opened. The harness daemon outlives a single
    # CLI invocation, so without this every verification leaves a tab
    # behind and a long-lived gateway slowly fills the browser. Never
    # close the last one: a headless Chrome with no tabs exits, and the
    # next run would pay a full browser start.
    if opened_tab is not None:
        try:
            if len(_helper("list_tabs")() or []) > 1:
                _helper("close_tab")(opened_tab)
        except Exception:  # noqa: BLE001 -- cleanup must never fail a result
            pass

    return {
        "status": status,
        "checks": checks,
        "errors": errors,
        "artifact": artifact,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "page": page,
    }


def main() -> None:
    plan_path = os.environ.get("TMCP_BROWSER_PLAN", "")
    with open(plan_path, "r", encoding="utf-8") as handle:
        plan = json.load(handle)
    try:
        result = run_plan(plan)
    except Exception as exc:  # noqa: BLE001 -- always emit one parseable line
        result = {"status": "ERROR", "checks": [], "errors": [_truncate(exc)],
                  "artifact": "", "elapsed_ms": 0, "page": {}}
    print(RESULT_SENTINEL + json.dumps(result))


# Runs ONLY under the harness (which sets TMCP_BROWSER_PLAN). Importing this
# module in a test therefore executes nothing.
if os.environ.get("TMCP_BROWSER_PLAN"):
    main()
