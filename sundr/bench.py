"""Gate A benchmarks: does the guarantee hold on held-out data, and how much coverage does it buy?

Features are computed once per dataset. Each of many random splits then
trains a combiner, certifies thresholds on a calibration slice, and measures
realised coverage and error on a disjoint test slice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.stats import binom

from .audit import _baseline_scores
from .certify import certify
from .combiner import Combiner, select_features
from .features import FeatureTable


@dataclass
class SplitResult:
    epsilon: float
    certified: bool
    coverage: float
    error: float | None
    baseline_coverage: float
    baseline_error: float | None
    clearly_over: bool = False


@dataclass
class BenchSummary:
    dataset: str
    n_records: int
    splits: int
    agreement: float
    baseline_agreement: float | None
    per_epsilon: dict[str, dict] = field(default_factory=dict)
    usd_per_1k_decisions: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _realised(scores, correct, threshold) -> tuple[float, float | None, int, int]:
    accepted = scores >= threshold
    coverage = float(accepted.mean())
    n, k = int(accepted.sum()), int((~correct[accepted]).sum())
    return coverage, (k / n if n else None), n, k


def clearly_over(n: int, k: int, epsilon: float, alpha: float = 0.05) -> bool:
    """Test errors significantly above budget: P(Binomial(n, epsilon) >= k) < alpha."""
    return n > 0 and float(binom.sf(k - 1, n, epsilon)) < alpha


def run_bench(
    name: str,
    table: FeatureTable,
    y: np.ndarray,
    labels: list[str],
    baseline_backend: str,
    epsilons: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
    splits: int = 100,
    fractions: tuple[float, float] = (0.4, 0.3),
    max_questions: int = 20,
    delta: float = 0.05,
    seed: int = 0,
) -> BenchSummary:
    rng = np.random.default_rng(seed)
    n = len(y)
    n_train, n_cal = int(fractions[0] * n), int(fractions[1] * n)
    baseline = _baseline_scores(table, baseline_backend, labels)

    results: dict[float, list[SplitResult]] = {eps: [] for eps in epsilons}
    agreements, baseline_agreements = [], []
    for _ in range(splits):
        order = rng.permutation(n)
        tr, ca, te = order[:n_train], order[n_train : n_train + n_cal], order[n_train + n_cal :]

        kept = table.select(select_features(table.X[tr], y[tr], table.groups, max_groups=max_questions))
        combiner = Combiner()
        oof_labels, oof_scores = combiner.out_of_fold(kept.X[tr], y[tr], seed=int(rng.integers(1 << 30)))
        combiner.fit(kept.X[tr], y[tr])
        cal_labels, cal_scores = combiner.predict(kept.X[ca])
        test_labels, test_scores = combiner.predict(kept.X[te])
        cal_ok, test_ok, train_ok = cal_labels == y[ca], test_labels == y[te], oof_labels == y[tr]
        agreements.append(float(test_ok.mean()))

        if baseline is not None:
            b_labels, b_scores = baseline
            b_ok = b_labels == y
            baseline_agreements.append(float(b_ok[te].mean()))

        for eps in epsilons:
            cert = certify(oof_scores, train_ok, cal_scores, cal_ok, eps, delta)
            coverage, error, n_acc, k_err = _realised(test_scores, test_ok, cert.threshold) if cert else (0.0, None, 0, 0)
            b_cov, b_err = 0.0, None
            if baseline is not None:
                b_cert = certify(b_scores[tr], b_ok[tr], b_scores[ca], b_ok[ca], eps, delta)
                if b_cert:
                    b_cov, b_err, _, _ = _realised(b_scores[te], b_ok[te], b_cert.threshold)
            results[eps].append(SplitResult(eps, cert is not None, coverage, error, b_cov, b_err,
                                            clearly_over(n_acc, k_err, eps)))

    summary = BenchSummary(
        dataset=name,
        n_records=n,
        splits=splits,
        agreement=float(np.mean(agreements)),
        baseline_agreement=float(np.mean(baseline_agreements)) if baseline_agreements else None,
        usd_per_1k_decisions=1000 * sum(s.usd for s in table.stats.values()) / max(n, 1),
    )
    for eps, rows in results.items():
        certified = [r for r in rows if r.certified]
        errors = [r.error for r in certified if r.error is not None]
        summary.per_epsilon[f"{eps:g}"] = {
            "certified_share": len(certified) / len(rows),
            "mean_coverage": float(np.mean([r.coverage for r in rows])),
            "baseline_mean_coverage": float(np.mean([r.baseline_coverage for r in rows])),
            # Share of certified splits whose realised test error stayed within budget.
            # Test slices are finite, so this sits a little under the true validity.
            "within_budget_share": float(np.mean([e <= eps for e in errors])) if errors else None,
            # Share of certified splits whose test error is significantly above budget (binomial, 5%).
            # Under the guarantee this should stay near or below 5%.
            "clearly_over_share": float(np.mean([r.clearly_over for r in certified])) if certified else None,
            "mean_realised_error": float(np.mean(errors)) if errors else None,
        }
    return summary


def format_summary(s: BenchSummary) -> str:
    lines = [
        f"## {s.dataset}",
        "",
        f"{s.n_records:,} items, {s.splits} splits. Agreement {s.agreement:.1%}"
        + (f" (one question {s.baseline_agreement:.1%})" if s.baseline_agreement is not None else "")
        + f". Decision-model cost ${s.usd_per_1k_decisions:.4f} per 1,000 decisions.",
        "",
        "| Budget | Certified in | Mean coverage | One question | Mean realised error | Significantly over budget |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for eps, row in s.per_epsilon.items():
        within = "—" if row["clearly_over_share"] is None else f"{row['clearly_over_share']:.0%} of splits"
        err = "—" if row["mean_realised_error"] is None else f"{row['mean_realised_error']:.2%}"
        lines.append(
            f"| {float(eps):.1%} | {row['certified_share']:.0%} | {row['mean_coverage']:.0%} | "
            f"{row['baseline_mean_coverage']:.0%} | {err} | {within} |"
        )
    return "\n".join(lines)
