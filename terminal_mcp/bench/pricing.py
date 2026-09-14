"""Versioned model price table, and the cost-weighted unit the
comparison is actually decided on.

WHY THIS FILE EXISTS. The first draft of this harness took the task
brief's `primary_cost_tokens = input + cache_write` literally as the
headline metric. The independent critic lane rejected it, correctly, on
three grounds, and the rejection is worth recording here because the
mistake is an easy one to make again:

1. It adds tokens of different unit cost at parity. A 5-minute cache
   write bills at 1.25x base input and a 1-hour cache write at 2x;
   summing either with a 1x input token is a unit error.
2. One `cache_write` field is not enough to price a cache write, because
   the multiplier depends on the TTL -- and the API already reports the
   split (`usage.cache_creation.ephemeral_5m_input_tokens` /
   `ephemeral_1h_input_tokens`). Collapsing them throws away the
   coefficient.
3. The serious one: EXCLUDING OUTPUT TOKENS SYSTEMATICALLY FAVOURS THE
   TREATMENT ARM. Output bills at 5x input across the entire current
   lineup. A pipeline whose whole thesis is "think harder up front"
   produces that thinking AS OUTPUT TOKENS, so a metric that ignores
   output charges the up-front arm nothing for its own primary cost
   while charging the baseline arm fully for the rework it does. That
   is not a conservative simplification; it manufactures the result.

So the decision metric is `cost_units` -- every token converted to
BASE-INPUT-EQUIVALENTS using the real ratios:

    cost_units = input
               + cache_write_5m * 1.25
               + cache_write_1h * 2.00
               + cache_read      * R_read
               + output          * R_out

`primary_cost_tokens` is still computed and still reported, because the
task brief asked for it by name and a reviewer comparing this report to
the brief should find it -- but it is labelled as a raw diagnostic, not
as the metric anything is decided on.

Prices are recorded per model with a `price_table_version`, so history
can be REPRICED rather than silently re-interpreted when rates change.
A model the table does not know yields `None` cost units -- missing,
never zero, never a guessed default.

Rates below are Anthropic first-party API list prices, $/MTok, as of
table version 2026-06-24. Bedrock/Vertex are partner-priced and are not
in this table; a deployment on those should add its own version rather
than edit this one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PRICE_TABLE_VERSION = "2026-06-24"

# Multipliers on base input price. These are properties of the API's
# billing model rather than of any one model.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00
DEFAULT_CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class ModelPrice:
    """$/MTok, plus the cache-read multiplier where it is not the usual
    0.1x (Claude Fable 5.1 reads at $0.25/MTok against a $10 base, i.e.
    0.025x)."""

    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_multiplier: float = DEFAULT_CACHE_READ_MULTIPLIER

    @property
    def output_multiplier(self) -> float:
        """Output cost in base-input-equivalents. 5.0 across the whole
        current lineup -- computed rather than hardcoded so a future
        model with a different ratio is priced correctly for free."""
        if self.input_per_mtok <= 0:
            return 0.0
        return self.output_per_mtok / self.input_per_mtok


PRICE_TABLE: dict[str, ModelPrice] = {
    price.model_id: price
    for price in (
        ModelPrice("claude-fable-5-1", 10.00, 50.00, cache_read_multiplier=0.025),
        ModelPrice("claude-mythos-5-1", 10.00, 50.00, cache_read_multiplier=0.025),
        ModelPrice("claude-fable-5", 10.00, 50.00),
        ModelPrice("claude-opus-5", 5.00, 25.00),
        ModelPrice("claude-opus-4-8", 5.00, 25.00),
        ModelPrice("claude-opus-4-7", 5.00, 25.00),
        ModelPrice("claude-opus-4-6", 5.00, 25.00),
        ModelPrice("claude-sonnet-5", 2.00, 10.00),
        ModelPrice("claude-sonnet-4-6", 3.00, 15.00),
        ModelPrice("claude-haiku-4-5", 1.00, 5.00),
    )
}


def normalise_model_id(model: str | None) -> str | None:
    """Strip a date suffix and the Bedrock `anthropic.` prefix, and drop
    a Vertex `@version`. Returns None for an unknown or absent model --
    pricing an unknown model by guessing is exactly the silent error
    this module exists to prevent."""
    if not model:
        return None
    candidate = str(model).strip().lower()
    if candidate.startswith("anthropic."):
        candidate = candidate[len("anthropic.") :]
    candidate = candidate.split("@", 1)[0]
    if candidate in PRICE_TABLE:
        return candidate
    # A dated snapshot such as claude-opus-5-20260401 prices as its base.
    parts = candidate.split("-")
    while len(parts) > 2:
        parts.pop()
        trimmed = "-".join(parts)
        if trimmed in PRICE_TABLE:
            return trimmed
    return None


def lookup(model: str | None) -> ModelPrice | None:
    key = normalise_model_id(model)
    return PRICE_TABLE.get(key) if key else None


def cost_units(
    *,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_5m_tokens: int | None,
    cache_write_1h_tokens: int | None,
) -> float | None:
    """Base-input-equivalent cost for one task.

    Returns None if the model is unknown, or if ANY component is
    missing -- a partial cost is a wrong cost, and a wrong cost that
    looks like a number is worse than no number. The caller reports it
    as missing and the task drops out of the cost comparison while
    still counting toward coverage."""
    price = lookup(model)
    if price is None:
        return None
    components = (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_5m_tokens,
        cache_write_1h_tokens,
    )
    if any(component is None for component in components):
        return None
    return (
        float(input_tokens)
        + float(cache_write_5m_tokens) * CACHE_WRITE_5M_MULTIPLIER
        + float(cache_write_1h_tokens) * CACHE_WRITE_1H_MULTIPLIER
        + float(cache_read_tokens) * price.cache_read_multiplier
        + float(output_tokens) * price.output_multiplier
    )


def dollars(units: float | None, model: str | None) -> float | None:
    """Convert base-input-equivalents back to dollars, for a report
    line a human can sanity-check against an invoice."""
    price = lookup(model)
    if price is None or units is None:
        return None
    return units * price.input_per_mtok / 1_000_000.0


def table_as_dict() -> dict[str, Any]:
    return {
        "price_table_version": PRICE_TABLE_VERSION,
        "cache_write_5m_multiplier": CACHE_WRITE_5M_MULTIPLIER,
        "cache_write_1h_multiplier": CACHE_WRITE_1H_MULTIPLIER,
        "models": {
            model_id: {
                "input_per_mtok": price.input_per_mtok,
                "output_per_mtok": price.output_per_mtok,
                "cache_read_multiplier": price.cache_read_multiplier,
                "output_multiplier": price.output_multiplier,
            }
            for model_id, price in sorted(PRICE_TABLE.items())
        },
    }
