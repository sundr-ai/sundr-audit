"""USD list prices per million tokens, for calls whose export carries no cost."""

from __future__ import annotations

import re

# (input, output) USD per million tokens. All input tokens are billed at the base
# input rate (cache discounts are ignored), so computed costs are an upper bound.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gemini-3.8-flash": (0.75, 3.75),
    # Older OpenAI entries are approximate; check current OpenAI pricing before relying on them.
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
}

_VENDOR_PREFIX = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")


def price_for(model: str | None) -> tuple[float, float] | None:
    """The entry whose key is the longest prefix of the model id, ignoring vendor prefixes."""
    if not model:
        return None
    name = _VENDOR_PREFIX.sub("", model.strip().lower().rsplit("/", 1)[-1])
    matches = [key for key in PRICES if name.startswith(key)]
    return PRICES[max(matches, key=len)] if matches else None


def compute_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Cost in USD from the price table, or None when the model or token counts are unknown."""
    price = price_for(model)
    if price is None or (input_tokens is None and output_tokens is None):
        return None
    return ((input_tokens or 0) * price[0] + (output_tokens or 0) * price[1]) / 1_000_000
