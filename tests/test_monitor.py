import numpy as np

from sundr.monitor import audit_probability, estimate_error


def test_audit_probability_range():
    p = audit_probability(np.array([1.0, 0.95, 0.9, 0.5]), threshold=0.9)
    assert np.allclose(p[[0, 2, 3]], [0.02, 0.10, 0.10])
    assert 0.02 < p[1] < 0.10


def _simulate(rng, n, threshold=0.9, miscalibration=1.4):
    scores = rng.uniform(threshold, 1.0, n)
    predicted = 1 - scores
    true_prob = np.clip(miscalibration * predicted, 0, 1)
    errors = rng.random(n) < true_prob
    pi = audit_probability(scores, threshold)
    audited = rng.random(n) < pi
    return predicted, audited, pi, errors, true_prob.mean()


def test_estimate_is_unbiased_and_bound_covers():
    rng = np.random.default_rng(11)
    covered, thetas, truths = 0, [], []
    trials = 400
    for _ in range(trials):
        predicted, audited, pi, errors, truth = _simulate(rng, 20_000)
        est = estimate_error(predicted, audited, pi, errors)
        thetas.append(est.theta)
        truths.append(truth)
        covered += est.upper >= truth
    assert abs(np.mean(thetas) - np.mean(truths)) < 0.001
    assert covered / trials >= 0.93  # nominal 95%


def test_status_levels():
    rng = np.random.default_rng(5)
    predicted, audited, pi, errors, _ = _simulate(rng, 50_000)
    est = estimate_error(predicted, audited, pi, errors)
    assert est.status(epsilon=1.0) == "ok"
    assert est.status(epsilon=est.upper / 0.9) == "warn"
    assert est.status(epsilon=est.upper / 2) == "breach"


def test_few_audits_warm_up_but_clear_failures_breach():
    rng = np.random.default_rng(2)
    n = 400
    scores = rng.uniform(0.95, 1.0, n)
    audited = rng.random(n) < 0.05
    pi = np.full(n, 0.05)
    healthy = estimate_error(1 - scores, audited, pi, np.zeros(n))
    assert healthy.n_audited < 100 and healthy.status(0.05) == "warming_up"
    broken = estimate_error(1 - scores, audited, pi, np.ones(n))
    assert broken.status(0.05) == "breach"


def test_pessimistic_prior_without_audits_does_not_breach():
    n = 6000
    scores = np.full(n, 0.95)  # predicted error 5%, over a 2% budget
    none_audited = estimate_error(1 - scores, np.zeros(n, bool), np.full(n, 0.05), np.zeros(n))
    assert none_audited.n_audited == 0 and none_audited.status(0.02) == "warming_up"
    clean = np.zeros(n, bool)
    clean[:60] = True
    few_clean = estimate_error(1 - scores, clean, np.full(n, 0.05), np.zeros(n))
    assert few_clean.status(0.02) == "warming_up"
