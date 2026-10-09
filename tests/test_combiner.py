import numpy as np

from sundr.combiner import Combiner, select_features


def _data(rng, n=2000):
    y = rng.choice(np.array(["phish", "safe"], dtype=object), n)
    signal = (y == "phish").astype(float)
    X = np.column_stack([
        signal * 2 + rng.normal(0, 1, n),   # strong signal
        signal * 0.5 + rng.normal(0, 1, n),  # weak signal
        rng.normal(0, 1, n),                 # noise
    ])
    return X, y


def test_fit_predict_and_out_of_fold():
    rng = np.random.default_rng(0)
    X, y = _data(rng)
    comb = Combiner().fit(X, y)
    labels, scores = comb.predict(X)
    assert (labels == y).mean() > 0.8
    assert ((scores >= 0.5) & (scores <= 1)).all()
    oof_labels, oof_scores = comb.out_of_fold(X, y)
    assert len(oof_labels) == len(y) and (oof_labels == y).mean() > 0.8


def test_select_features_prefers_signal():
    rng = np.random.default_rng(1)
    X, y = _data(rng)
    kept = select_features(X, y, ["strong", "weak", "noise"], max_groups=1)
    assert kept == ["strong"]
