"""The Savings Audit: how much of a judging bill could be taken over, at what certified error."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sklearn.model_selection import train_test_split

from . import spend
from .backends.base import Backend
from .certify import Certificate, certify
from .candidates import CandidateError
from .candidates import estimate_usd as estimate_candidate_usd
from .candidates import evaluate as evaluate_candidate
from .combiner import Combiner, select_features
from .compiler import TaskSpec
from .features import FeatureRunner, FeatureTable
from .types import Question, Record

DEFAULT_EPSILONS = (0.005, 0.01, 0.02, 0.05)


@dataclass
class AuditConfig:
    epsilons: tuple[float, ...] = DEFAULT_EPSILONS
    delta: float = 0.05
    train_fraction: float = 0.6
    seed: int = 0
    max_questions: int = 20
    # Training rows that see every drafted question; the rest are scored on the kept ones only.
    selection_rows: int = 1500
    monthly_calls: int | None = None
    usd_per_call: float | None = None
    audit_share: float = 0.03
    fee_share: float = 0.20  # the Takeover plan's share of savings


@dataclass
class BudgetRow:
    epsilon: float
    certificate: Certificate | None
    baseline: Certificate | None
    monthly_savings_usd: float | None = None
    monthly_fee_usd: float | None = None
    monthly_net_usd: float | None = None

    @property
    def coverage(self) -> float:
        return self.certificate.coverage if self.certificate else 0.0

    @property
    def baseline_coverage(self) -> float:
        return self.baseline.coverage if self.baseline else 0.0


@dataclass
class GoldCheck:
    """How the LLM and Sundr compare with human labels on held-out calls."""

    n: int
    llm_error: float
    sundr_error: float  # all held-out calls with a label, Sundr's own prediction
    handled_n: int  # labelled calls above the headline threshold
    handled_error: float | None  # Sundr's error on just those


@dataclass
class AuditResult:
    task: TaskSpec
    n_records: int
    n_train: int
    n_calibration: int
    label_counts: dict[str, int]
    questions_drafted: int
    questions_kept: list[Question]
    agreement_all: float
    baseline_agreement_all: float | None
    rows: list[BudgetRow]
    decision_usd_per_call: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    backends: list[str]
    monthly_calls: int | None
    usd_per_call: float | None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # The trained combiner its certificates apply to; the proxy serves exactly this model.
    combiner: Combiner | None = field(default=None, repr=False)
    # Against human labels, when the logs carry them (held-out calls only).
    gold: GoldCheck | None = None
    # Cheaper LLMs evaluated as takeover candidates, and any that were skipped (model, reason).
    candidates: list = field(default_factory=list)
    candidate_notes: list[tuple[str, str]] = field(default_factory=list)
    fee_share: float = 0.20

    def best_row(self, preferred: float = 0.01) -> BudgetRow | None:
        """The 1% budget when it certifies, else the tightest budget that does."""
        certified = [r for r in self.rows if r.certificate and r.coverage > 0]
        exact = [r for r in certified if abs(r.epsilon - preferred) < 1e-9]
        return (exact or certified or [None])[0]


def _baseline_scores(table: FeatureTable, backend: str, labels: list[str]) -> tuple[np.ndarray, np.ndarray] | None:
    """The single direct-verdict question, read straight from the first backend."""
    cols = [table.columns.index(f"{backend}:verdict={label}") for label in labels if f"{backend}:verdict={label}" in table.columns]
    if len(cols) != len(labels):
        return None
    probs = np.exp(table.X[:, cols])
    probs /= probs.sum(axis=1, keepdims=True)
    idx = probs.argmax(axis=1)
    return np.array(labels, dtype=object)[idx], probs[np.arange(len(idx)), idx]


def _savings(row: BudgetRow, config: AuditConfig, decision_usd: float) -> None:
    if not (config.monthly_calls and config.usd_per_call) or row.certificate is None:
        return
    handled = row.coverage * config.monthly_calls
    counterfactual = handled * config.usd_per_call
    savings = counterfactual * (1 - config.audit_share)
    row.monthly_savings_usd = savings
    row.monthly_fee_usd = savings * config.fee_share
    row.monthly_net_usd = savings - row.monthly_fee_usd


MIN_GOLD = 30


def _gold_check(records, idx_cal, y, cal_labels, cal_scores, rows) -> GoldCheck | None:
    gold = np.array([records[i].gold for i in idx_cal], dtype=object)
    has = np.array([g is not None for g in gold])
    if has.sum() < MIN_GOLD:
        return None
    certified = [r for r in rows if r.certificate and r.coverage > 0]
    exact = [r for r in certified if abs(r.epsilon - 0.01) < 1e-9]
    best = (exact or certified or [None])[0]
    handled = has & (cal_scores >= best.certificate.threshold) if best else np.zeros_like(has)
    return GoldCheck(
        n=int(has.sum()),
        llm_error=float((y[idx_cal][has] != gold[has]).mean()),
        sundr_error=float((cal_labels[has] != gold[has]).mean()),
        handled_n=int(handled.sum()),
        handled_error=float((cal_labels[handled] != gold[handled]).mean()) if handled.sum() else None,
    )


def _candidate_savings(row, config: AuditConfig, cost_per_call: float) -> None:
    """A candidate saves the difference between the current call and its own (plus any gate) cost."""
    if not (config.monthly_calls and config.usd_per_call) or row.certificate is None:
        return
    handled = row.coverage * config.monthly_calls
    savings = max(handled * (config.usd_per_call - cost_per_call) * (1 - config.audit_share), 0.0)
    row.monthly_savings_usd = savings
    row.monthly_fee_usd = savings * config.fee_share
    row.monthly_net_usd = savings - row.monthly_fee_usd


def _percentile(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


async def run_audit(
    task: TaskSpec,
    records: list[Record],
    questions: list[Question],
    backends: list[Backend],
    config: AuditConfig | None = None,
    cache_path: str | None = None,
    max_usd: float | None = None,
    candidates: list | None = None,
) -> AuditResult:
    """`candidates` are CandidateClients (sundr.candidates) for cheaper LLMs to certify alongside."""
    config = config or AuditConfig()
    runner = FeatureRunner(backends, cache_path=cache_path, max_usd=max_usd)
    y = np.array([r.reference for r in records], dtype=object)

    counts = Counter(y.tolist())
    stratify = y if min(counts.values()) >= 2 else None
    idx_train, idx_cal = train_test_split(
        np.arange(len(records)), train_size=config.train_fraction, random_state=config.seed, stratify=stratify
    )

    two_stage = len(questions) > config.max_questions + 1 and len(idx_train) > config.selection_rows
    if two_stage:
        # Choose questions on a slice of the training split, then score everything on the kept ones.
        idx_sel = idx_train[: config.selection_rows]
        sel_records = [records[i] for i in idx_sel]
        spend.check(runner.uncached_usd(sel_records, questions), max_usd)
        sel_table = await runner.run(sel_records, questions)
        kept_names = select_features(sel_table.X, y[idx_sel], sel_table.groups, max_groups=config.max_questions)
        # The direct verdict stays in so the report can show the one-question baseline.
        scored = [q for q in questions if q.name in kept_names or q.name == "verdict"]
    else:
        scored = questions
    remaining = None if max_usd is None else max_usd - runner.spent_usd()
    spend.check(runner.uncached_usd(records, scored), remaining)
    table = await runner.run(records, scored)
    if not two_stage:
        kept_names = select_features(table.X[idx_train], y[idx_train], table.groups, max_groups=config.max_questions)
    kept = table.select(kept_names)
    X_train, X_cal = kept.X[idx_train], kept.X[idx_cal]

    combiner = Combiner()
    oof_labels, oof_scores = combiner.out_of_fold(X_train, y[idx_train], seed=config.seed)
    combiner.fit(X_train, y[idx_train])
    cal_labels, cal_scores = combiner.predict(X_cal)
    cal_correct = cal_labels == y[idx_cal]
    train_correct = oof_labels == y[idx_train]

    baseline = _baseline_scores(table, backends[0].name, task.labels)
    baseline_agreement = None
    if baseline is not None:
        b_labels, b_scores = baseline
        b_train_correct = b_labels[idx_train] == y[idx_train]
        b_cal_correct = b_labels[idx_cal] == y[idx_cal]
        baseline_agreement = float(b_cal_correct.mean())

    decision_usd = sum(s.usd for s in runner.stats.values()) / max(len(records), 1)
    rows = []
    for eps in config.epsilons:
        cert = certify(oof_scores, train_correct, cal_scores, cal_correct, eps, config.delta)
        base = None
        if baseline is not None:
            base = certify(b_scores[idx_train], b_train_correct, b_scores[idx_cal], b_cal_correct, eps, config.delta)
        row = BudgetRow(epsilon=eps, certificate=cert, baseline=base)
        _savings(row, config, decision_usd)
        rows.append(row)

    gold = _gold_check(records, idx_cal, y, cal_labels, cal_scores, rows)
    candidate_results, candidate_notes = [], []
    runnable = []
    for client in candidates or []:
        try:
            if not getattr(client, "prepaid", False):
                planned = sum(estimate_candidate_usd(c.model, task, records) for c in runnable
                              if not getattr(c, "prepaid", False))
                remaining = None if max_usd is None else max_usd - runner.spent_usd() - planned
                spend.check(estimate_candidate_usd(client.model, task, records), remaining)
            runnable.append(client)
        except (spend.BudgetExceeded, CandidateError) as exc:
            candidate_notes.append((client.model, str(exc)))
    # Candidates answer concurrently; one failing doesn't stop the others.
    answered = await asyncio.gather(*(c.answer_all(task, records) for c in runnable), return_exceptions=True)
    for client, answers in zip(runnable, answered):
        if isinstance(answers, BaseException):
            candidate_notes.append((client.model, str(answers)[:200]))
            continue
        candidate_results.append(evaluate_candidate(
            client.model, answers, y, task.labels, idx_train, idx_cal, kept.X, config.epsilons, config.delta,
            decision_usd, lambda row, cost: _candidate_savings(row, config, cost), seed=config.seed))
    latencies = [ms for s in runner.stats.values() for ms in s.latencies_ms]
    question_by_name = {q.name: q for q in questions}
    return AuditResult(
        task=task,
        n_records=len(records),
        n_train=len(idx_train),
        n_calibration=len(idx_cal),
        label_counts=dict(counts.most_common()),
        questions_drafted=len(questions),
        questions_kept=[question_by_name[n] for n in kept_names],
        agreement_all=float(cal_correct.mean()),
        baseline_agreement_all=baseline_agreement,
        rows=rows,
        decision_usd_per_call=decision_usd,
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        backends=[f"{b.name} ({b.model})" for b in backends],
        monthly_calls=config.monthly_calls,
        usd_per_call=config.usd_per_call,
        combiner=combiner,
        gold=gold,
        candidates=candidate_results,
        candidate_notes=candidate_notes,
        fee_share=config.fee_share,
    )

