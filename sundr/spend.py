"""A local ledger of paid API spend, with a monthly cap that stops runs before they start.

Costs are recorded at list price (Cloudflare's included neurons are ignored),
so the cap errs on the side of stopping early.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


def ledger_path() -> Path:
    return Path(os.environ.get("SUNDR_SPEND_LEDGER", ".sundr/spend.jsonl"))


def monthly_cap() -> float:
    return float(os.environ.get("SUNDR_MONTHLY_BUDGET_USD", "25"))


NEURON_USD = 0.011 / 1000  # Workers AI overage price per neuron


def included_neurons() -> float:
    """Workers AI neurons included each month (10M on Workers Paid)."""
    return float(os.environ.get("CLOUDFLARE_INCLUDED_NEURONS", "10000000"))


def record(service: str, usd: float, detail: str = "", neurons: float = 0.0) -> None:
    """Log spend. With `neurons`, only usage beyond the monthly allowance is billable;
    `usd` is then kept as the list price for reference."""
    if usd <= 0 and neurons <= 0:
        return
    row = {"ts": time.time(), "service": service, "usd": round(usd, 6), "detail": detail}
    if neurons:
        over = max(0.0, month_neurons() + neurons - included_neurons()) - max(0.0, month_neurons() - included_neurons())
        row.update(usd=round(over * NEURON_USD, 6), list_usd=round(usd, 6), neurons=round(neurons))
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _this_month() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["ts"] >= start]


def month_to_date() -> float:
    """Billable spend this month."""
    return sum(r["usd"] for r in _this_month())


def month_neurons() -> float:
    return sum(r.get("neurons", 0) for r in _this_month())


def check(estimated_usd: float, run_cap: float | None = None) -> None:
    """Refuse a run whose estimate breaks the per-run cap or this month's budget."""
    if run_cap is not None and estimated_usd > run_cap:
        raise BudgetExceeded(f"Estimated ${estimated_usd:.2f} exceeds this run's cap of ${run_cap:.2f}")
    spent, cap = month_to_date(), monthly_cap()
    if spent + estimated_usd > cap:
        raise BudgetExceeded(
            f"Estimated ${estimated_usd:.2f} on top of ${spent:.2f} spent this month exceeds the "
            f"${cap:.2f} monthly budget (SUNDR_MONTHLY_BUDGET_USD)"
        )


# $ per million input / output tokens (Anthropic list prices, Oct 2026).
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def claude_usd(model: str, usage) -> float:
    price_in, price_out = CLAUDE_PRICES.get(model, (5.0, 25.0))
    tokens_in = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    return (tokens_in * price_in + (getattr(usage, "output_tokens", 0) or 0) * price_out) / 1e6
