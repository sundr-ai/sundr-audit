"""Certify an automation threshold with a finite-sample error guarantee.

Learn then Test (Angelopoulos et al., arXiv 2110.01052) with fixed-sequence
testing. For a threshold lambda, Sundr answers calls whose combiner score
is at least lambda. The selective risk R(lambda) is the error rate on those
answered calls. We test H0: R(lambda) > epsilon at each candidate threshold,
strict to loose, and stop at the first threshold we cannot reject.

Candidate thresholds come only from training-split scores and the calibration
set's size, never from calibration labels, so the guarantee holds:

    P(R(lambda_hat) <= epsilon) >= 1 - delta

assuming calibration data and future traffic are exchangeable.

Fixed-sequence testing stalls if its first threshold cannot pass. We pick the
starting coverage from the training split's out-of-fold errors: the coverage
most likely to pass given the calibration set's size. That choice never looks
at calibration labels, so one sequence runs at the full delta.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy.stats import beta, binom

def clopper_pearson_upper(k: int, n: int, delta: float) -> float:
    """One-sided (1 - delta) upper confidence bound on a binomial rate."""
    if n == 0 or k >= n:
        return 1.0
    return float(beta.ppf(1 - delta, k + 1, n - k))


def risk_p_value(k: int, n: int, epsilon: float) -> float:
    """p-value for H0: true error rate > epsilon, after k errors in n trials."""
    if n == 0:
        return 1.0
    return float(binom.cdf(k, n, epsilon))


def min_items_for(epsilon: float, delta: float) -> int:
    """Fewest accepted items that could certify epsilon, even with zero errors."""
    return math.ceil(math.log(1 / delta) / epsilon)


@dataclass
class Certificate:
    epsilon: float
    delta: float
    threshold: float
    n_calibration: int
    n_accepted: int
    n_errors: int
    upper_bound: float
    coverage: float

    def to_dict(self) -> dict:
        data = asdict(self)
        if not math.isfinite(data["threshold"]):
            data["threshold"] = None  # answers every call
        return data


def start_coverage(
    train_scores: np.ndarray,
    train_correct: np.ndarray,
    n_calibration: int,
    epsilon: float,
    delta: float,
    grid: int = 100,
) -> float | None:
    """Coverage at which the calibration test is most likely to pass.

    Uses only the training split's out-of-fold scores and errors plus the
    calibration set's size. Returns None when no coverage looks passable.
    """
    order = np.argsort(-np.asarray(train_scores))
    errors = ~np.asarray(train_correct, dtype=bool)[order]
    risk = np.cumsum(errors) / np.arange(1, len(errors) + 1)
    floor = min_items_for(epsilon, delta) / max(n_calibration, 1)
    best, best_p = None, 1.0
    for c in np.linspace(0.01, 1.0, grid):
        if c < floor:
            continue
        n = int(round(c * n_calibration))
        expected_k = int(round(risk[max(int(c * len(risk)) - 1, 0)] * n))
        p = risk_p_value(expected_k, n, epsilon)
        if p < best_p:
            best, best_p = float(c), p
    return best if best_p <= 0.5 else None


def candidate_thresholds(
    train_scores: np.ndarray, start: float, steps: int = 64
) -> np.ndarray:
    """Thresholds from strict to loose, spaced by training-split coverage."""
    coverages = np.linspace(min(start, 1.0), 1.0, steps)
    thresholds = np.quantile(train_scores, 1 - coverages, method="higher")
    thresholds[-1] = -np.inf  # the loosest candidate answers everything
    # Keep strictly decreasing values; ties add no new candidates.
    keep = np.concatenate([[True], np.diff(thresholds) < 0])
    return thresholds[keep]


def _fixed_sequence(
    cal_scores: np.ndarray,
    cal_correct: np.ndarray,
    thresholds: np.ndarray,
    epsilon: float,
    delta: float,
) -> tuple[float, int, int] | None:
    best = None
    for lam in thresholds:
        accepted = cal_scores >= lam
        n = int(accepted.sum())
        k = int((~cal_correct[accepted]).sum())
        if risk_p_value(k, n, epsilon) > delta:
            break
        best = (float(lam), n, k)
    return best


def certify(
    train_scores: np.ndarray,
    train_correct: np.ndarray,
    cal_scores: np.ndarray,
    cal_correct: np.ndarray,
    epsilon: float,
    delta: float = 0.05,
    steps: int = 64,
) -> Certificate | None:
    """Loosest threshold whose selective error is certified at most epsilon.

    `train_*` are out-of-fold scores and correctness from the training split.
    Returns None when no threshold can be certified; the task then stays in
    shadow mode.
    """
    cal_scores = np.asarray(cal_scores, dtype=float)
    cal_correct = np.asarray(cal_correct, dtype=bool)
    n_cal = len(cal_scores)
    start = start_coverage(train_scores, train_correct, n_cal, epsilon, delta)
    if start is None:
        return None

    thresholds = candidate_thresholds(np.asarray(train_scores), start, steps)
    found = _fixed_sequence(cal_scores, cal_correct, thresholds, epsilon, delta)
    if found is None:
        return None

    lam, n, k = found
    return Certificate(
        epsilon=epsilon,
        delta=delta,
        threshold=lam,
        n_calibration=n_cal,
        n_accepted=n,
        n_errors=k,
        upper_bound=clopper_pearson_upper(k, n, delta),
        coverage=n / n_cal,
    )


def coverage_curve(
    train_scores: np.ndarray,
    train_correct: np.ndarray,
    cal_scores: np.ndarray,
    cal_correct: np.ndarray,
    epsilons: tuple[float, ...] = (0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.1),
    delta: float = 0.05,
) -> list[tuple[float, Certificate | None]]:
    """Certified coverage at each error budget, for the Savings Audit report."""
    return [
        (eps, certify(train_scores, train_correct, cal_scores, cal_correct, eps, delta))
        for eps in epsilons
    ]
