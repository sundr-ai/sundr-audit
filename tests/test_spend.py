import asyncio

import pytest

from sundr import demo, spend
from sundr.features import FeatureRunner


def test_ledger_and_monthly_cap():
    assert spend.month_to_date() == 0
    spend.record("jev", 20.0, "test")
    assert spend.month_to_date() == pytest.approx(20.0)
    spend.check(4.0)
    with pytest.raises(spend.BudgetExceeded, match="monthly budget"):
        spend.check(6.0)
    with pytest.raises(spend.BudgetExceeded, match="run's cap"):
        spend.check(1.5, run_cap=1.0)


def test_runner_stops_at_its_cap_and_cache_is_free(tmp_path):
    recs = demo.records(n=200)
    backends = demo.backends(recs)
    for b in backends:
        b.simulated = False  # treat as paid for this test
        b.usd_per_million_input_tokens = 1000.0
    cache = str(tmp_path / "c.sqlite")
    runner = FeatureRunner(backends[:1], cache_path=cache, max_usd=0.01)
    assert runner.uncached_usd(recs, demo.questions()) > 0.01
    with pytest.raises(spend.BudgetExceeded):
        asyncio.run(runner.run(recs, demo.questions()))
    assert spend.month_to_date() > 0  # partial spend still recorded

    full = FeatureRunner(backends[:1], cache_path=cache)
    asyncio.run(full.run(recs, demo.questions()))
    assert FeatureRunner(backends[:1], cache_path=cache).uncached_usd(recs, demo.questions()) == 0


def test_included_neurons_are_free_until_used_up(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_INCLUDED_NEURONS", "1000000")
    spend.record("clef-flash", 5.0, "first", neurons=800_000)
    assert spend.month_to_date() == 0
    spend.record("clef-flash", 5.0, "crosses the allowance", neurons=400_000)
    assert spend.month_to_date() == pytest.approx(200_000 * spend.NEURON_USD)
    assert spend.month_neurons() == 1_200_000
