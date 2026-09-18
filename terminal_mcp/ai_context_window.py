"""How full a session's context window is -- when that can be known, and not otherwise.

THREE DIFFERENT PERCENTAGES, ONLY ONE OF WHICH IS COMPUTABLE HERE

The page says "usage %", and three unrelated numbers could mean that:

  1. request/task token usage -- how many tokens a turn or a task spent.
     MEASURED: the transcripts report it per request, and the index already
     sums it. It is a count, not a percentage: there is no denominator.
  2. provider/account quota % -- how much of a subscription window is spent.
     UNAVAILABLE for Claude Code and Codex on this machine: neither writes
     rate-limit or reset metadata to any local artefact. `quota_windows`
     already records that honestly as `source: "unavailable"` with
     `used_percent: null`, and nothing here changes it.
  3. session context-window % -- how full the model's context is right now.
     COMPUTABLE, and this module is the part that was missing: the prompt
     size of the most recent request is reported by the provider, and the
     window is a property of the model.

Conflating them is how a screen ends up showing a confident number that
answers a question nobody asked.

WHY A PERCENTAGE NEEDS A VERIFIED DENOMINATOR

A window that is merely assumed produces a figure that is wrong in the one
direction that matters: reassuring. On this machine every transcript reports
`message.model` as `claude-opus-5`, while the cost block reports
`claude-opus-5[1m]` -- the same session, two ids, and only the second says
which window is in force. Sessions here were measured holding 795k tokens of
context: against an assumed 200k window that is 397%, and against the real 1M
window it is 80%. A default would not have been slightly off, it would have
been nonsense.

So a window is only ever taken from a structured provider-reported source: an
explicit variant suffix, or a table of models whose window is unambiguous.
Anything else answers "unknown", and the caller shows N/A.

THE CONTRADICTION GUARD

If the measured context already exceeds the window we resolved, the window is
disproven -- it cannot be the right one. That is reported as a contradiction
rather than as a percentage over 100, because a number above 100% is a bug
wearing the costume of a measurement.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Sources, as stable strings: they reach the API and the UI, and a caller may
# branch on them to decide whether to show a bar or an N/A.
VARIANT_SUFFIX = "model_variant_suffix"
MODEL_TABLE = "model_table"
UNKNOWN_MODEL = "unknown_context_window"
CONTRADICTED = "window_contradicted"
NO_ACTIVITY = "no_activity"

# Models whose context window is unambiguous from the id alone. Deliberately
# short: an entry here is a claim that this id can only mean this window, and
# a wrong entry produces a confident wrong percentage. A model missing from
# this table is reported as unknown, which is the safe direction.
CONTEXT_WINDOWS: dict[str, int] = {
    "claude-haiku-4-5-20251001": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-opus-5": 200_000,
}

# `claude-opus-5[1m]`, `some-model[200k]` -- the provider stating the window
# in the id itself. This is the most reliable signal available locally.
_VARIANT = re.compile(r"\[(\d+)\s*([km])\]\s*$", re.IGNORECASE)

_MULTIPLIER = {"k": 1_000, "m": 1_000_000}


def parse_variant_window(model_id: str | None) -> int | None:
    """The window a model id states outright, e.g. `claude-opus-5[1m]` -> 1000000."""
    if not model_id:
        return None
    match = _VARIANT.search(str(model_id))
    if not match:
        return None
    return int(match.group(1)) * _MULTIPLIER[match.group(2).lower()]


def _base(model_id: str | None) -> str:
    return _VARIANT.sub("", str(model_id or "")).strip()


def resolve_window(model: str | None, *,
                   variant_ids: Iterable[str] = ()) -> tuple[int | None, str, str]:
    """(window, source, detail) for this session's model.

    `variant_ids` are other ids the provider used for the SAME session -- in
    practice the keys of the cost block's per-model breakdown, which carry the
    `[1m]` suffix that `message.model` drops. They are consulted first because
    an explicit statement beats a lookup table.
    """
    candidates = [model, *variant_ids]
    base = _base(model)
    for candidate in candidates:
        if base and _base(candidate) != base:
            # A different model entirely (a haiku sub-agent inside an opus
            # session, say). Its window says nothing about this one.
            continue
        window = parse_variant_window(candidate)
        if window:
            return window, VARIANT_SUFFIX, f"{candidate} states its window"

    if base in CONTEXT_WINDOWS:
        return CONTEXT_WINDOWS[base], MODEL_TABLE, f"{base} has a known window"

    return None, UNKNOWN_MODEL, (
        f"no verified context window for {model or 'an unnamed model'}; "
        f"reported as unavailable rather than assumed")


def context_usage(used_tokens: int | None, *, model: str | None,
                  variant_ids: Iterable[str] = ()) -> dict[str, Any]:
    """How full the context is, or why that cannot be said.

    `used_tokens` is the prompt size of the MOST RECENT request -- input plus
    cache-read plus cache-write -- which is what the model actually had in
    front of it. Summing a whole session would answer a different question
    (total spend) and would climb past any window within a few turns.
    """
    if not used_tokens:
        return {"used": used_tokens or 0, "window": None, "used_percent": None,
                "source": NO_ACTIVITY,
                "detail": "no request recorded for this session yet"}

    window, source, detail = resolve_window(model, variant_ids=variant_ids)
    if window is None:
        return {"used": used_tokens, "window": None, "used_percent": None,
                "source": source, "detail": detail}

    if used_tokens > window:
        # The measurement disproves the window. Reporting 397% would dress a
        # resolution failure up as a reading.
        return {"used": used_tokens, "window": None, "used_percent": None,
                "source": CONTRADICTED,
                "detail": (f"measured context {used_tokens:,} exceeds the {window:,} "
                           f"window resolved from {source}; the model variant in use "
                           f"is not the one reported")}

    return {"used": used_tokens, "window": window,
            "used_percent": round(100.0 * used_tokens / window, 1),
            "source": source, "detail": detail}
