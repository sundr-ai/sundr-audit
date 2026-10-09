"""Ask every question of every backend, and encode the answers as features."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import spend
from .backends.base import Backend, BackendResult
from .estimate import question_tokens, CHARS_PER_TOKEN
from .types import Answer, Question, Record

EPS = 1e-6


def reversed_options(q: Question) -> Question:
    """The same Choice question with its options in reverse order (first-option bias check)."""
    return Question(
        name=q.name,
        type=q.type,
        instructions=q.instructions,
        options=tuple(reversed(q.options)),
        descriptions=tuple(reversed(q.descriptions)),
        criteria=q.criteria,
    )


def average_answers(a: Answer, b: Answer) -> Answer:
    """Average two answers in log space (geometric mean, renormalised)."""
    logs = {k: (math.log(max(a.probs.get(k, EPS), EPS)) + math.log(max(b.probs.get(k, EPS), EPS))) / 2 for k in a.probs}
    total = sum(math.exp(v) for v in logs.values())
    return Answer({k: math.exp(v) / total for k, v in logs.items()})


class Cache:
    """Answers keyed by backend model, text and question set, in SQLite."""

    def __init__(self, path: str | Path | None):
        self.db = sqlite3.connect(str(path) if path else ":memory:", timeout=60)
        if path:
            # Several benchmark processes may share one cache file.
            self.db.execute("pragma journal_mode=wal")
            self.db.execute("pragma busy_timeout=60000")
        self.db.execute("create table if not exists answers (key text primary key, value text)")

    @staticmethod
    def key(backend: Backend, state: str, questions: list[Question]) -> str:
        payload = json.dumps([backend.model, state, [asdict(q) for q in questions]], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> BackendResult | None:
        row = self.db.execute("select value from answers where key = ?", (key,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        answers = {k: Answer(v) for k, v in data["answers"].items()}
        return BackendResult(answers, data["input_tokens"], data["latency_ms"], data["model"])

    def put(self, key: str, result: BackendResult) -> None:
        value = json.dumps({
            "answers": {k: a.probs for k, a in result.answers.items()},
            "input_tokens": result.input_tokens,
            "latency_ms": result.latency_ms,
            "model": result.model,
        })
        self.db.execute("insert or replace into answers values (?, ?)", (key, value))
        self.db.commit()


@dataclass
class CallStats:
    """Per-backend usage. Token, cost and latency figures include cached answers,
    so estimates stay right on a re-run; `spent_usd` counts only fresh calls."""

    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    usd: float = 0.0
    spent_usd: float = 0.0
    spent_tokens: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def usd_per_call(self) -> float:
        total = self.calls + self.cached
        return self.usd / total if total else 0.0


@dataclass
class FeatureTable:
    X: np.ndarray
    groups: list[str]
    columns: list[str]
    record_ids: list[str]
    stats: dict[str, CallStats]

    def select(self, keep: list[str]) -> "FeatureTable":
        mask = [g in keep for g in self.groups]
        idx = [i for i, m in enumerate(mask) if m]
        return FeatureTable(
            self.X[:, idx],
            [self.groups[i] for i in idx],
            [self.columns[i] for i in idx],
            self.record_ids,
            self.stats,
        )


def _encode_answer(q: Question, a: Answer) -> list[float]:
    if q.type == "noul":
        p = min(max(a.probs.get("yes", 0.5), EPS), 1 - EPS)
        return [math.log(p / (1 - p))]
    probs = [max(a.probs.get(opt, EPS), EPS) for opt in q.options]
    logs = [math.log(p) for p in probs]
    if q.type == "score":
        expected = sum(i * p for i, p in enumerate(probs)) / max(len(probs) - 1, 1)
        return [expected] + logs
    return logs


def _column_names(prefix: str, q: Question) -> list[str]:
    if q.type == "noul":
        return [f"{prefix}:{q.name}"]
    names = [f"{prefix}:{q.name}={opt}" for opt in q.options]
    return ([f"{prefix}:{q.name}#expected"] + names) if q.type == "score" else names


def _total_variation(q: Question, a: Answer, b: Answer) -> float:
    return 0.5 * sum(abs(a.probs.get(o, 0) - b.probs.get(o, 0)) for o in q.option_names())


class FeatureRunner:
    def __init__(
        self,
        backends: list[Backend],
        cache_path: str | Path | None = None,
        concurrency: int = 16,
        option_orders: int = 2,
        max_usd: float | None = None,
        record_spend: bool = True,
    ):
        self.backends = backends
        self.max_usd = max_usd
        self.record_spend = record_spend
        self.cache = Cache(cache_path)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.option_orders = option_orders
        self.stats = {b.name: CallStats() for b in backends}

    async def _call(self, backend: Backend, state: str, questions: list[Question]) -> BackendResult:
        key = Cache.key(backend, state, questions)
        stats = self.stats[backend.name]
        result = self.cache.get(key)
        fresh = result is None
        if fresh:
            async with self.semaphore:
                result = await backend.ask(state, questions)
            self.cache.put(key, result)
            stats.calls += 1
        else:
            stats.cached += 1
        usd = result.input_tokens * backend.usd_per_million_input_tokens / 1e6
        if fresh:
            stats.spent_usd += usd
            stats.spent_tokens += result.input_tokens
            if self.max_usd is not None and self.spent_usd() > self.max_usd:
                raise spend.BudgetExceeded(f"Stopped: this run reached its ${self.max_usd:.2f} cap")
        stats.input_tokens += result.input_tokens
        stats.usd += usd
        stats.latencies_ms.append(result.latency_ms)
        return result

    def spent_usd(self) -> float:
        return sum(s.spent_usd for s in self.stats.values())

    def uncached_usd(self, records: list[Record], questions: list[Question]) -> float:
        """List-price cost of the calls a run would actually make (cached answers are free)."""
        choices = [reversed_options(q) for q in questions if q.type == "choice"]
        calls = [(questions, sum(question_tokens(q) for q in questions))]
        if self.option_orders >= 2 and choices:
            calls.append((choices, sum(question_tokens(q) for q in choices)))
        usd = 0.0
        for backend in self.backends:
            for r in records:
                for qs, q_tokens in calls:
                    if self.cache.get(Cache.key(backend, r.input, qs)) is None:
                        usd += (len(r.input) // CHARS_PER_TOKEN + q_tokens) * backend.usd_per_million_input_tokens / 1e6
        return usd

    def _record_spend(self) -> None:
        for b in self.backends:
            if getattr(b, "simulated", False):
                continue
            stats = self.stats[b.name]
            neurons = stats.spent_tokens * getattr(b, "neurons_per_million_tokens", 0) / 1e6
            spend.record(b.name, stats.spent_usd, f"{b.model}: {stats.calls} calls", neurons=neurons)

    async def _answers(self, backend: Backend, state: str, questions: list[Question]) -> dict[str, Answer]:
        choices = [q for q in questions if q.type == "choice"]
        if self.option_orders < 2 or not choices:
            return (await self._call(backend, state, questions)).answers
        # Both orders at once: latency is one round trip, not two.
        forward, backward = await asyncio.gather(
            self._call(backend, state, questions),
            self._call(backend, state, [reversed_options(q) for q in choices]),
        )
        merged = dict(forward.answers)
        for q in choices:
            merged[q.name] = average_answers(forward.answers[q.name], backward.answers[q.name])
        return merged

    async def run(self, records: list[Record], questions: list[Question]) -> FeatureTable:
        async def one(record: Record) -> dict[str, dict[str, Answer]]:
            per_backend = await asyncio.gather(*(self._answers(b, record.input, questions) for b in self.backends))
            return {b.name: ans for b, ans in zip(self.backends, per_backend)}

        try:
            answered = await asyncio.gather(*(one(r) for r in records))
        finally:
            if self.record_spend:
                self._record_spend()

        groups, columns = [], []
        for b in self.backends:
            for q in questions:
                names = _column_names(b.name, q)
                columns += names
                groups += [q.name] * len(names)
        if len(self.backends) >= 2:
            for q in questions:
                columns.append(f"disagree:{q.name}")
                groups.append(q.name)

        rows = []
        for per_backend in answered:
            row: list[float] = []
            for b in self.backends:
                for q in questions:
                    row += _encode_answer(q, per_backend[b.name][q.name])
            if len(self.backends) >= 2:
                first, second = self.backends[0].name, self.backends[1].name
                for q in questions:
                    row.append(_total_variation(q, per_backend[first][q.name], per_backend[second][q.name]))
            rows.append(row)

        return FeatureTable(np.array(rows, dtype=float), groups, columns, [r.id for r in records], self.stats)
