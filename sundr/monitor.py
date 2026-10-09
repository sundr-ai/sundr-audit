"""Live estimate of the error rate on calls Sundr answers.

Active statistical inference (Zrnic and Candes, arXiv 2403.03208). Each
answered call is also sent to the reference with probability pi(x), which is
higher for calls near the threshold. With predicted error f(x), audit
indicator xi and observed disagreement e on audited calls:

    theta_hat = mean( f(x) + xi / pi(x) * (e - f(x)) )

This is unbiased however good f is; a better f only narrows the interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import beta, norm

Status = Literal["ok", "warn", "breach", "warming_up"]
MIN_AUDITS = 100


def audit_probability(
    score: np.ndarray | float,
    threshold: float,
    pi_min: float = 0.02,
    pi_max: float = 0.10,
) -> np.ndarray:
    """Audit rate: pi_min for the most confident calls, pi_max at the threshold."""
    score = np.asarray(score, dtype=float)
    span = max(1.0 - threshold, 1e-9)
    u = np.clip((1.0 - score) / span, 0.0, 1.0)
    return pi_min + (pi_max - pi_min) * u


@dataclass
class ErrorEstimate:
    theta: float
    se: float
    upper: float
    n_handled: int
    n_audited: int
    lower: float = 0.0
    audit_errors: int = 0

    def status(self, epsilon: float, warn_fraction: float = 0.8, min_audits: int = MIN_AUDITS) -> Status:
        """breach: the audits alone show the budget is broken, or (with enough audits) it is no longer provably met.

        The early check uses only audited calls, never the model's own predicted error, so a
        pessimistic prior can't end a takeover before any evidence arrives. Audits lean toward
        calls near the threshold, so their raw error rate overstates the true one: the safe direction.
        """
        if self.n_audited and audit_lower_bound(self.audit_errors, self.n_audited) > epsilon:
            return "breach"
        if self.n_audited < min_audits:
            return "warming_up"
        if self.upper > epsilon:
            return "breach"
        if self.upper > warn_fraction * epsilon:
            return "warn"
        return "ok"


def audit_lower_bound(errors: int, audits: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson lower bound on the error rate among audited calls."""
    return float(beta.ppf(alpha, errors, audits - errors + 1)) if errors else 0.0


def estimate_error(
    predicted_error: np.ndarray,
    audited: np.ndarray,
    audit_prob: np.ndarray,
    observed_error: np.ndarray,
    alpha: float = 0.05,
) -> ErrorEstimate:
    """Estimate and one-sided (1 - alpha) upper bound of the handled-call error rate.

    `observed_error` is read only where `audited` is true.
    """
    f = np.asarray(predicted_error, dtype=float)
    xi = np.asarray(audited, dtype=bool)
    pi = np.asarray(audit_prob, dtype=float)
    e = np.where(xi, np.asarray(observed_error, dtype=float), 0.0)

    terms = f + xi / pi * (e - f)
    n = len(terms)
    theta = float(terms.mean())
    se = float(terms.std(ddof=1) / np.sqrt(n)) if n > 1 else float("inf")
    z = float(norm.ppf(1 - alpha))
    return ErrorEstimate(theta=theta, se=se, upper=theta + z * se, n_handled=n, n_audited=int(xi.sum()),
                         lower=theta - z * se, audit_errors=int(round(e[xi].sum())))
