import asyncio

import numpy as np

from sundr import demo
from sundr.bench import format_summary, run_bench
from sundr.features import FeatureRunner


def test_bench_on_simulated_backends():
    recs = demo.records(n=6000)
    table = asyncio.run(FeatureRunner(demo.backends(recs)).run(recs, demo.questions()))
    y = np.array([r.reference for r in recs], dtype=object)
    summary = run_bench("demo", table, y, demo.TASK.labels, "jev-sim", epsilons=(0.02, 0.05), splits=5)
    row = summary.per_epsilon["0.05"]
    assert row["certified_share"] == 1.0 and row["mean_coverage"] > row["baseline_mean_coverage"]
    assert row["within_budget_share"] >= 0.8
    assert "| 5.0% |" in format_summary(summary)
