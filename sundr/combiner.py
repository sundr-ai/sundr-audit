"""Combine many question probabilities into one calibrated verdict score."""

from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def _model(C: float):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000))


class Combiner:
    """Multinomial logistic regression over encoded question answers.

    The score of a call is the probability of its predicted label, which the
    certifier thresholds.
    """

    def __init__(self, C: float = 1.0):
        self.C = C
        self.model = None
        self.classes_: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Combiner":
        self.model = _model(self.C).fit(X, y)
        self.classes_ = self.model.classes_
        return self

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        proba = self.model.predict_proba(X)
        idx = proba.argmax(axis=1)
        return self.classes_[idx], proba[np.arange(len(idx)), idx]

    def out_of_fold(
        self, X: np.ndarray, y: np.ndarray, folds: int = 5, seed: int = 0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Predictions and scores for the training split without refitting leakage."""
        labels = np.empty(len(y), dtype=object)
        scores = np.empty(len(y))
        _, counts = np.unique(y, return_counts=True)
        k = max(2, min(folds, int(counts.min()))) if len(counts) > 1 else 2
        splitter = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
        for train_idx, held_idx in splitter.split(X, y):
            if len(np.unique(y[train_idx])) < 2:
                labels[held_idx], scores[held_idx] = y[train_idx][0], 1.0
                continue
            fold = Combiner(self.C).fit(X[train_idx], y[train_idx])
            labels[held_idx], scores[held_idx] = fold.predict(X[held_idx])
        return labels, scores


def select_features(
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    max_groups: int = 20,
    C: float = 0.1,
) -> list[str]:
    """Rank question groups by L1 logistic-regression weight; keep the strongest.

    `groups[j]` names the question that produced column j.
    """
    unique = list(dict.fromkeys(groups))
    if len(unique) <= max_groups:
        return unique  # nothing to drop; skip the (slow, many-class) L1 fit
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=C, l1_ratio=1.0, solver="saga", max_iter=5000),
    ).fit(X, y)
    weights = np.abs(model[-1].coef_).sum(axis=0)
    totals: dict[str, float] = {}
    for g, w in zip(groups, weights):
        totals[g] = totals.get(g, 0.0) + float(w)
    ranked = sorted(totals, key=totals.__getitem__, reverse=True)
    kept = [g for g in ranked if totals[g] > 0][:max_groups]
    return kept or ranked[:1]
