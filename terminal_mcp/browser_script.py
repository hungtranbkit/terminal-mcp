"""The two small grammars the browser gateway understands.

WHY A GRAMMAR AND NOT AN LLM
----------------------------
Phase 1 drives a real browser with Playwright and NO model in the loop.
That is a deliberate scope choice, not a missing piece: a verification
tool whose verdict depends on a second model's judgement cannot be used
as evidence that a UI works, because a PASS is then only as trustworthy
as the summariser. Everything here is deterministic -- the same page and
the same assertion produce the same verdict on every run, and a FAIL
names the exact expectation that failed.

So `browser_verify`'s assertions and `browser_run_task`'s steps are
parsed into explicit specs by this module. Plain prose that does not fit
the grammar is NOT guessed at. An assertion with no directive prefix is
the one exception, and it has an unambiguous reading -- "the page should
say this" -- so it is treated as `text contains`. A task STEP that cannot
be parsed is an error naming the line, because guessing which element to
click is how a "verification" tool silently clicks Delete.

PARSING IS SEPARATE FROM THE BROWSER ON PURPOSE
-----------------------------------------------
Nothing in this module imports Playwright or touches a page. `evaluate`
judges checks against an `Observation` the worker already collected --
including selector probe counts -- so the entire verdict path is unit
testable without launching anything. The browser's only job is to report
what it saw; deciding whether that is a PASS happens here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .browser_safety import MAX_CHECKS, scrub

#: A task may not expand into an unbounded script. 25 is far more than any
#: single chat turn needs and keeps a runaway task bounded in wall-clock.
MAX_STEPS = 25

#: Longest selector/value accepted. A CSS selector longer than this is not
#: a selector, it is a payload.
MAX_TARGET_CHARS = 300
MAX_VALUE_CHARS = 1_000


@dataclass(frozen=True)
class Check:
    """One parsed assertion. `raw` is echoed back in the result so a chat
    can see exactly which of its own words passed or failed."""

    raw: str
    kind: str
    target: str = ""
    value: str = ""


@dataclass(frozen=True)
class Step:
    """One parsed task step."""

    raw: str
    action: str
    target: str = ""
    value: str = ""
    check: Check | None = None


@dataclass
class Observation:
    """What the worker saw. Populated by `browser_worker`, judged here.

    Deliberately a plain container with defaults: a partial observation
    (navigation failed halfway) must still be judgeable, so every field
    has a safe empty value rather than being required.
    """

    final_url: str = ""
    status: int | None = None
    title: str = ""
    text: str = ""
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_errors: list[dict[str, Any]] = field(default_factory=list)
    #: css selector -> {"count": int, "text": str}
    selector_probes: dict[str, dict[str, Any]] = field(default_factory=dict)
    screenshot: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Observation":
        payload = payload or {}
        return cls(
            final_url=str(payload.get("final_url") or ""),
            status=payload.get("status") if isinstance(payload.get("status"), int) else None,
            title=str(payload.get("title") or ""),
            text=str(payload.get("text") or ""),
            console_errors=list(payload.get("console_errors") or []),
            page_errors=list(payload.get("page_errors") or []),
            network_errors=list(payload.get("network_errors") or []),
            selector_probes=dict(payload.get("selector_probes") or {}),
            screenshot=payload.get("screenshot") or None,
        )


class ParseError(ValueError):
    """A line the grammar refuses. `line` is echoed verbatim to the caller
    so the fix is obvious without reading this file."""

    def __init__(self, line: str, detail: str) -> None:
        super().__init__(f"{detail}: {line!r}")
        self.line = line
        self.detail = detail


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
#
# Directive -> (kind, takes_target). Ordered longest-prefix-first at match
# time so `text not contains` is never shadowed by `text contains`.
_ASSERTION_RULES: tuple[tuple[str, str, bool], ...] = (
    ("selector text contains", "selector_text_contains", True),
    ("selector text is", "selector_text_is", True),
    ("text not contains", "text_not_contains", False),
    ("text contains", "text_contains", False),
    ("selector exists", "selector_exists", False),
    ("selector missing", "selector_missing", False),
    ("title contains", "title_contains", False),
    ("title is", "title_is", False),
    ("url contains", "url_contains", False),
    ("status is", "status_is", False),
)

#: Zero-argument assertions.
_ASSERTION_FLAGS: dict[str, str] = {
    "no console errors": "no_console_errors",
    "no network errors": "no_network_errors",
    "no page errors": "no_page_errors",
    "no errors": "no_errors",
}

#: Splits `selector text contains: #banner :: Welcome` into target and value.
_TARGET_SEP = "::"


def parse_assertion(raw: str) -> Check:
    """Parse one assertion string, or raise ParseError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ParseError(str(raw), "empty assertion")
    line = raw.strip()
    if len(line) > MAX_TARGET_CHARS + MAX_VALUE_CHARS:
        raise ParseError(line[:80], "assertion is too long")

    flag = _ASSERTION_FLAGS.get(line.lower().rstrip("."))
    if flag:
        return Check(raw=line, kind=flag)

    lowered = line.lower()
    for directive, kind, takes_target in _ASSERTION_RULES:
        if not lowered.startswith(directive):
            continue
        rest = line[len(directive):].lstrip()
        if rest.startswith(":"):
            rest = rest[1:].strip()
        elif rest and not takes_target and not rest[0].isspace():
            # `text containsfoo` -- a near-miss of a directive, not a
            # directive. Fall through so it is read as plain text instead
            # of silently asserting something the caller did not write.
            continue
        if not rest:
            raise ParseError(line, f"{directive!r} needs a value")
        if takes_target:
            if _TARGET_SEP not in rest:
                raise ParseError(
                    line, f"{directive!r} needs '<selector> {_TARGET_SEP} <expected>'")
            target, _, value = rest.partition(_TARGET_SEP)
            return Check(raw=line, kind=kind, target=target.strip()[:MAX_TARGET_CHARS],
                         value=value.strip()[:MAX_VALUE_CHARS])
        if kind in ("selector_exists", "selector_missing"):
            return Check(raw=line, kind=kind, target=rest[:MAX_TARGET_CHARS])
        if kind == "status_is":
            if not rest.isdigit():
                raise ParseError(line, "'status is' needs an HTTP status number")
            return Check(raw=line, kind=kind, value=rest)
        return Check(raw=line, kind=kind, value=rest[:MAX_VALUE_CHARS])

    # No directive. The unambiguous reading of a bare phrase in a list of
    # assertions is "the page should say this".
    return Check(raw=line, kind="text_contains", value=line[:MAX_VALUE_CHARS])


def parse_assertions(raws: Any) -> tuple[list[Check], list[str]]:
    """Parse a list of assertions. Returns (checks, errors); a bad entry
    never discards the good ones -- a chat gets its 4 valid checks judged
    and is told precisely which 5th one was malformed."""
    if raws is None:
        return [], []
    if isinstance(raws, str):
        raws = [line for line in raws.splitlines() if line.strip()]
    if not isinstance(raws, (list, tuple)):
        return [], ["assertions must be a list of strings"]
    checks: list[Check] = []
    errors: list[str] = []
    for raw in list(raws)[:MAX_CHECKS]:
        try:
            checks.append(parse_assertion(raw))
        except ParseError as exc:
            errors.append(str(exc))
    if len(list(raws)) > MAX_CHECKS:
        errors.append(f"only the first {MAX_CHECKS} assertions were parsed")
    return checks, errors


def selectors_for(checks: list[Check], steps: list[Step] | None = None) -> list[str]:
    """Every CSS selector the browser must probe to judge `checks`.

    Collected up front so the worker does one pass over the page instead
    of a round trip per assertion.
    """
    out: list[str] = []
    for check in checks:
        if check.target and check.kind.startswith("selector"):
            if check.target not in out:
                out.append(check.target)
    for step in steps or []:
        if step.check is not None and step.check.target and \
                step.check.kind.startswith("selector") and step.check.target not in out:
            out.append(step.check.target)
    return out


def _probe(observation: Observation, selector: str) -> dict[str, Any]:
    return observation.selector_probes.get(selector) or {}


def evaluate(checks: list[Check], observation: Observation) -> list[dict[str, Any]]:
    """Judge each check. Every entry is PASS, FAIL or ERROR -- never absent.

    ERROR is distinct from FAIL on purpose: "the selector was never
    probed" and "the element is not on the page" are different bugs, and
    collapsing them would let a broken worker read as a clean FAIL.
    """
    results: list[dict[str, Any]] = []
    for check in checks:
        results.append(_evaluate_one(check, observation))
    return results


def _evaluate_one(check: Check, observation: Observation) -> dict[str, Any]:
    def ok(detail: str = "") -> dict[str, Any]:
        return {"assertion": scrub(check.raw), "status": "PASS",
                **({"detail": scrub(detail)} if detail else {})}

    def fail(detail: str) -> dict[str, Any]:
        return {"assertion": scrub(check.raw), "status": "FAIL", "detail": scrub(detail)}

    def err(detail: str) -> dict[str, Any]:
        return {"assertion": scrub(check.raw), "status": "ERROR", "detail": scrub(detail)}

    kind = check.kind
    haystack = observation.text or ""
    if kind == "text_contains":
        return ok() if check.value.lower() in haystack.lower() \
            else fail("page text does not contain it")
    if kind == "text_not_contains":
        return ok() if check.value.lower() not in haystack.lower() \
            else fail("page text contains it")
    if kind == "title_contains":
        return ok() if check.value.lower() in (observation.title or "").lower() \
            else fail(f"title is {observation.title!r}")
    if kind == "title_is":
        return ok() if check.value.strip() == (observation.title or "").strip() \
            else fail(f"title is {observation.title!r}")
    if kind == "url_contains":
        return ok() if check.value.lower() in (observation.final_url or "").lower() \
            else fail(f"final url is {observation.final_url!r}")
    if kind == "status_is":
        if observation.status is None:
            return err("no HTTP status was captured for this navigation")
        return ok() if str(observation.status) == check.value \
            else fail(f"status is {observation.status}")
    if kind in ("selector_exists", "selector_missing",
                "selector_text_contains", "selector_text_is"):
        probe = _probe(observation, check.target)
        if not probe:
            return err(f"selector {check.target!r} was not probed")
        count = probe.get("count")
        if not isinstance(count, int):
            return err(f"selector {check.target!r} returned no usable probe")
        if kind == "selector_exists":
            return ok(f"{count} match(es)") if count > 0 else fail("no element matched")
        if kind == "selector_missing":
            return ok() if count == 0 else fail(f"{count} element(s) matched")
        if count == 0:
            return fail("no element matched, so its text cannot match")
        found = str(probe.get("text") or "")
        if kind == "selector_text_is":
            return ok() if found.strip() == check.value.strip() \
                else fail(f"element text is {found.strip()[:120]!r}")
        return ok() if check.value.lower() in found.lower() \
            else fail(f"element text is {found.strip()[:120]!r}")
    if kind in ("no_console_errors", "no_errors"):
        if observation.console_errors:
            return fail(f"{len(observation.console_errors)} console error(s)")
        if kind == "no_console_errors":
            return ok()
    if kind in ("no_page_errors", "no_errors"):
        if observation.page_errors:
            return fail(f"{len(observation.page_errors)} uncaught page error(s)")
        if kind == "no_page_errors":
            return ok()
    if kind in ("no_network_errors", "no_errors"):
        if observation.network_errors:
            return fail(f"{len(observation.network_errors)} failed request(s)")
        return ok()
    return err(f"unsupported assertion kind {kind!r}")


# ---------------------------------------------------------------------------
# Task steps
# ---------------------------------------------------------------------------

_WAIT_MS = re.compile(r"^wait\s+(\d{1,6})\s*(ms|milliseconds?)$", re.I)
_WAIT_S = re.compile(r"^wait\s+(\d{1,4})\s*(s|sec|secs|seconds?)$", re.I)
_FILL_WITH = re.compile(r"^(?:fill|enter|set)\s+(.+?)\s+with\s+(.+)$", re.I)
_TYPE_INTO = re.compile(r"^(?:type|input)\s+(.+?)\s+(?:into|in)\s+(.+)$", re.I)

#: `then` is the natural connective a chat writes ("open X then click Y"),
#: and `;`/newline are the explicit ones. All three split steps.
_STEP_SPLIT = re.compile(r"(?:\r?\n|;|\bthen\b)", re.I)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_step(raw: str) -> Step:
    """Parse one task step, or raise ParseError.

    Note what is NOT here: there is no fuzzy "find the button that looks
    like X". A step names a selector or a Playwright `text=`/`role=`
    locator, and anything else is refused rather than guessed -- clicking
    the wrong element is not a recoverable mistake.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ParseError(str(raw), "empty step")
    line = raw.strip()
    if len(line) > MAX_TARGET_CHARS + MAX_VALUE_CHARS:
        raise ParseError(line[:80], "step is too long")
    lowered = line.lower()

    if lowered in ("screenshot", "take a screenshot", "capture screenshot"):
        return Step(raw=line, action="screenshot")
    if lowered in ("scroll to bottom", "scroll down"):
        return Step(raw=line, action="scroll_bottom")

    match = _WAIT_MS.match(line)
    if match:
        return Step(raw=line, action="wait_ms", value=match.group(1))
    match = _WAIT_S.match(line)
    if match:
        return Step(raw=line, action="wait_ms", value=str(int(match.group(1)) * 1000))

    for prefix in ("goto", "go to", "open", "navigate to", "visit"):
        if lowered.startswith(prefix + " "):
            return Step(raw=line, action="goto",
                        target=_unquote(line[len(prefix):])[:MAX_TARGET_CHARS])
    for prefix in ("wait for", "await"):
        if lowered.startswith(prefix + " "):
            return Step(raw=line, action="wait_for",
                        target=_unquote(line[len(prefix):])[:MAX_TARGET_CHARS])
    for prefix in ("assert", "check", "expect", "verify"):
        if lowered.startswith(prefix + " "):
            body = line[len(prefix):].strip()
            if body.startswith("that "):
                body = body[5:].strip()
            return Step(raw=line, action="assert", check=parse_assertion(body))
    if lowered.startswith("click "):
        return Step(raw=line, action="click",
                    target=_unquote(line[6:])[:MAX_TARGET_CHARS])
    if lowered.startswith("press "):
        return Step(raw=line, action="press",
                    value=_unquote(line[6:])[:MAX_TARGET_CHARS])

    match = _FILL_WITH.match(line)
    if match:
        return Step(raw=line, action="fill", target=_unquote(match.group(1))[:MAX_TARGET_CHARS],
                    value=_unquote(match.group(2))[:MAX_VALUE_CHARS])
    match = _TYPE_INTO.match(line)
    if match:
        return Step(raw=line, action="fill", target=_unquote(match.group(2))[:MAX_TARGET_CHARS],
                    value=_unquote(match.group(1))[:MAX_VALUE_CHARS])

    raise ParseError(line[:120], "no supported action in this step")


def parse_task(task: Any) -> tuple[list[Step], list[str]]:
    """Split a natural-language task into steps and parse each one.

    Returns (steps, errors). A task with ANY unparsed step is reported as
    an error by the caller rather than half-executed: running the first
    three steps of a five-step instruction and stopping leaves the UI in a
    state nobody asked for.
    """
    if not isinstance(task, str) or not task.strip():
        return [], ["task must be a non-empty string"]
    if len(task) > 16000:
        return [], ["task exceeds 16000 characters"]
    pieces = [piece.strip() for piece in _STEP_SPLIT.split(task)]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return [], ["task contained no steps"]
    steps: list[Step] = []
    errors: list[str] = []
    for piece in pieces[:MAX_STEPS]:
        try:
            steps.append(parse_step(piece))
        except ParseError as exc:
            errors.append(str(exc))
    if len(pieces) > MAX_STEPS:
        errors.append(f"a task may contain at most {MAX_STEPS} steps, got {len(pieces)}")
    return steps, errors
