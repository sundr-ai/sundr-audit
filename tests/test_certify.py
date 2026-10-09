import numpy as np
import pytest

from sundr.certify import certify, clopper_pearson_upper, coverage_curve, min_items_for


def test_clopper_pearson_rule_of_three():
    # Zero errors in 300 trials bounds the rate near 1% at 95% confidence.
    assert 0.0098 < clopper_pearson_upper(0, 300, 0.05) < 0.0101
    assert clopper_pearson_upper(0, 0, 0.05) == 1.0
    assert clopper_pearson_upper(5, 5, 0.05) == 1.0


def test_min_items_for_one_percent():
    assert min_items_for(0.01, 0.05) == 300


def _population(rng, n):
    """Scores from an overconfident model: true error is 1.3x what the score implies."""
    confident = rng.random(n) < 0.7
    scores = np.where(confident, rng.beta(60, 1, n), rng.uniform(0.5, 1.0, n))
    error_prob = np.clip(1.3 * (1 - scores), 0, 1)
    wrong = rng.random(n) < error_prob
    return scores, ~wrong, error_prob


def _true_risk(threshold, pop_scores, pop_error_prob):
    accepted = pop_scores >= threshold
    return pop_error_prob[accepted].mean() if accepted.any() else 0.0


# Certifying 1% reliably needs ~12k calibration items; 2% needs ~3k.
@pytest.mark.parametrize("epsilon,n_cal", [(0.01, 12_000), (0.02, 4_000)])
def test_guarantee_holds_across_trials(epsilon, n_cal):
    rng = np.random.default_rng(7)
    pop_scores, _, pop_error_prob = _population(rng, 1_000_000)

    trials, violations, found = 200, 0, 0
    for _ in range(trials):
        train_scores, train_correct, _ = _population(rng, 4000)
        cal_scores, cal_correct, _ = _population(rng, n_cal)
        cert = certify(train_scores, train_correct, cal_scores, cal_correct, epsilon, delta=0.05)
        if cert is None:
            continue
        found += 1
        if _true_risk(cert.threshold, pop_scores, pop_error_prob) > epsilon:
            violations += 1

    assert found / trials > 0.9, "certifier should usually find a threshold"
    # Nominal violation rate is at most 5%; allow Monte Carlo slack.
    assert violations / trials <= 0.08


def test_no_certificate_when_calibration_set_too_small():
    rng = np.random.default_rng(1)
    scores, correct, _ = _population(rng, 120)
    # 120 items cannot certify 1% even with zero errors (best bound ~2.5%).
    assert certify(scores, correct, scores, correct, 0.01) is None


def test_coverage_grows_with_budget():
    rng = np.random.default_rng(3)
    train, train_correct, _ = _population(rng, 4000)
    cal, correct, _ = _population(rng, 6000)
    curve = coverage_curve(train, train_correct, cal, correct, epsilons=(0.005, 0.01, 0.05))
    coverages = [c.coverage if c else 0.0 for _, c in curve]
    assert coverages == sorted(coverages)
    assert coverages[-1] > coverages[0]
