"""Deterministic bounded context compiler for Work Runtime.

Callers resolve rules, task state, verified experience and code context first;
this module only assembles a small provider-neutral packet. Mandatory lanes
fail closed on overflow; lower-priority lanes may drop whole items with exact
accounting. Provider transcripts are activity, not the source of truth.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

DEFAULT_LANE_ORDER = ("mandatory", "task", "verification", "experience", "code")
DEFAULT_CHAR_BUDGETS = {
    "mandatory": 12000, "task": 8000, "verification": 5000,
    "experience": 6000, "code": 12000,
}

class ContextBudgetError(ValueError):
    """A hard lane cannot fit without losing required context."""

@dataclass(frozen=True)
class ContextPacket:
    text: str
    lanes: dict[str, tuple[str, ...]]
    stats: dict[str, dict[str, int]]
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "lanes": {k: list(v) for k, v in self.lanes.items()},
                "stats": self.stats, "fingerprint": self.fingerprint}

def _compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()

def _dedupe_key(value: str) -> str:
    return _compact(value).lower().rstrip(".;:")

def compile_context(lanes: Mapping[str, Sequence[object]], *,
                    budgets: Mapping[str, int] | None = None,
                    lane_order: Sequence[str] = DEFAULT_LANE_ORDER,
                    mandatory_lanes: Sequence[str] = ("mandatory",)) -> ContextPacket:
    effective = dict(DEFAULT_CHAR_BUDGETS)
    for key, value in (budgets or {}).items():
        if int(value) < 0:
            raise ValueError(f"budget for {key!r} must be >= 0")
        effective[str(key)] = int(value)
    names = list(dict.fromkeys([*lane_order, *lanes.keys()]))
    required, seen = set(mandatory_lanes), set()
    compiled: dict[str, tuple[str, ...]] = {}
    stats: dict[str, dict[str, int]] = {}
    for name in names:
        raw_items = list(lanes.get(name, ()))
        budget = effective.get(name, 4000)
        candidates, duplicates, input_chars = [], 0, 0
        for raw in raw_items:
            item = _compact(raw)
            if not item:
                continue
            input_chars += len(item)
            key = _dedupe_key(item)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key); candidates.append(item)
        if name in required:
            needed = sum(map(len, candidates))
            if needed > budget:
                raise ContextBudgetError(
                    f"mandatory lane {name!r} needs {needed} chars but budget is {budget}; hard context is never silently dropped")
            selected, dropped = candidates, 0
        else:
            selected, used, dropped = [], 0, 0
            for item in candidates:
                if used + len(item) <= budget:
                    selected.append(item); used += len(item)
                else:
                    dropped += 1
        compiled[name] = tuple(selected)
        stats[name] = {"input_items": len(raw_items), "candidate_items": len(candidates),
                       "used_items": len(selected), "duplicate_items": duplicates,
                       "dropped_items": dropped, "input_chars": input_chars,
                       "used_chars": sum(map(len, selected)), "budget_chars": budget}
    sections = []
    for name in names:
        if compiled[name]:
            sections.append(name.upper()); sections.extend(f"- {x}" for x in compiled[name])
    canonical = json.dumps({k: list(v) for k, v in compiled.items()}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return ContextPacket("\n".join(sections), compiled, stats,
                         hashlib.sha256(canonical.encode()).hexdigest())
