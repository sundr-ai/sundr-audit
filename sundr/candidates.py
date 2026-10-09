"""Cheaper LLMs as takeover candidates: ask one the same judgment, then certify where it agrees.

A candidate (Claude Haiku 5.5, GPT-6 Luna, ...) answers the task with structured output: a
JSON object holding one label. Two ways to certify it:

- **alone**: the candidate answers every call. Certified all-or-nothing on its overall
  agreement with the reference ("just switch").
- **gated**: a small model learns, from the decision models' answers and the candidate's
  verdict, how likely the candidate is to agree with the reference on each call. Learn then
  Test certifies a threshold on that probability, so the candidate answers only the calls
  it's certified on and the rest stay with the current model.

In an audit the candidate sees a generic prompt built from the task description; in the
proxy's shadow mode it sees the customer's own request with only the model swapped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from . import spend
from .certify import Certificate, certify
from .combiner import Combiner
from .compiler import TaskSpec
from .types import Record

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# $ per million input / output tokens (OpenRouter list prices, Oct 2026).
CANDIDATE_PRICES = {
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "openai/gpt-6-luna": (0.10, 0.50),
    "google/gemini-3.8-flash": (0.75, 3.75),
    "deepseek/deepseek-v4.1-flash": (0.30, 1.20),
}
DEFAULT_CANDIDATES = ("anthropic/claude-haiku-5.5", "openai/gpt-6-luna")
PROMPT_OVERHEAD_TOKENS = 120  # instructions and schema around the logged text
CHARS_PER_TOKEN = 4


class CandidateError(RuntimeError):
    pass


@dataclass
class CandidateAnswer:
    verdict: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


def short_name(model: str) -> str:
    """'anthropic/claude-haiku-5.5' -> 'claude-haiku-5.5'."""
    return model.rsplit("/", 1)[-1]


def price(model: str) -> tuple[float, float]:
    if model in CANDIDATE_PRICES:
        return CANDIDATE_PRICES[model]
    from .connectors.prices import price_for  # noqa: PLC0415

    found = price_for(model)
    if not found:
        raise CandidateError(f"No price known for candidate {model}; add it to CANDIDATE_PRICES")
    return found


def call_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = price(model)
    return (input_tokens * p_in + output_tokens * p_out) / 1e6


def system_prompt(task: TaskSpec) -> str:
    lines = [task.description.strip(), "", "Answer with exactly one of these labels:"]
    for label in task.labels:
        note = task.label_descriptions.get(label)
        lines.append(f"- {label}" + (f": {note}" if note else ""))
    lines += ["", 'Reply with JSON only: {"verdict": "<label>"}.']
    return "\n".join(lines)


def verdict_schema(task: TaskSpec) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["verdict"],
            "properties": {"verdict": {"type": "string", "enum": list(task.labels)}}}


def estimate_usd(model: str, task: TaskSpec, records: list[Record]) -> float:
    prompt = len(system_prompt(task)) // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS
    tokens_in = sum(len(r.input) // CHARS_PER_TOKEN + prompt for r in records)
    return call_usd(model, tokens_in, 12 * len(records))


class CandidateClient:
    """Asks one candidate model for verdicts through OpenRouter, with a SQLite cache and a spend cap."""

    def __init__(self, model: str, api_key: str | None = None, cache_path: str | Path | None = None,
                 concurrency: int = 8, transport: httpx.AsyncBaseTransport | None = None,
                 max_usd: float | None = None, system: str | None = None):
        """`system` replaces the generic prompt built from the task, e.g. with the team's own prompt."""
        self.model, self.system = model, system
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            raise CandidateError("Set OPENROUTER_API_KEY to evaluate candidate models")
        self.client = httpx.AsyncClient(timeout=60, transport=transport)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_usd = max_usd
        self.spent_usd = 0.0
        self.db = sqlite3.connect(str(cache_path) if cache_path else ":memory:", timeout=60)
        if cache_path:
            self.db.execute("pragma journal_mode=wal")
            self.db.execute("pragma busy_timeout=60000")
        self.db.execute("create table if not exists candidate_answers (key text primary key, value text)")

    def _key(self, system: str, text: str) -> str:
        return hashlib.sha256(json.dumps([self.model, system, text]).encode()).hexdigest()

    async def answer(self, task: TaskSpec, text: str) -> CandidateAnswer:
        system = self.system or system_prompt(task)
        key = self._key(system, text)
        row = self.db.execute("select value from candidate_answers where key = ?", (key,)).fetchone()
        if row:
            return CandidateAnswer(**json.loads(row[0]))
        if self.max_usd is not None and self.spent_usd > self.max_usd:
            raise spend.BudgetExceeded(f"Stopped: candidate {self.model} reached its ${self.max_usd:.2f} cap")
        body = {
            # Room for a reasoning model's thinking; the answer itself is a few tokens. (Current models
            # reject temperature, so none is sent.)
            "model": self.model, "max_tokens": 1024,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "verdict", "strict": True, "schema": verdict_schema(task)}},
            "provider": {"require_parameters": True},
        }
        if self.model.startswith("openai/"):
            body["reasoning"] = {"effort": "minimal"}  # a label needs little thought, and thinking is billed
        async with self.semaphore:
            started = time.perf_counter()
            data = await self._post(body)
            latency = (time.perf_counter() - started) * 1000
        usage = data.get("usage") or {}
        answer = CandidateAnswer(_verdict(data, task.labels), int(usage.get("prompt_tokens") or 0),
                                 int(usage.get("completion_tokens") or 0), latency)
        self.spent_usd += call_usd(self.model, answer.input_tokens, answer.output_tokens)
        # Cache real answers only: a failed or cut-off reply is retried next run, not remembered as "no answer".
        finish = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
        if data and finish != "length":
            self.db.execute("insert or replace into candidate_answers values (?, ?)", (key, json.dumps(answer.__dict__)))
            self.db.commit()
        return answer

    async def _post(self, body: dict) -> dict:
        """POST with retries: rate limits (429) and server errors back off, honouring Retry-After."""
        for attempt in range(7):
            try:
                response = await self.client.post(OPENROUTER_URL, json=body,
                                                   headers={"Authorization": f"Bearer {self.key}",
                                                            "X-Title": "Sundr audit"})
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code < 400:
                data = response.json()
                if not data.get("error"):  # OpenRouter can report upstream failures inside a 200
                    return data
            elif response is not None and response.status_code in (400, 401, 402, 403, 404):
                raise CandidateError(f"{self.model}: OpenRouter returned {response.status_code}: "
                                     f"{response.text[:200]}")
            wait = min(2 ** attempt, 30) + random.random()
            if response is not None and response.headers.get("retry-after", "").isdigit():
                wait = min(float(response.headers["retry-after"]), 60)
            await asyncio.sleep(wait)
        return {}

    async def answer_all(self, task: TaskSpec, records: list[Record]) -> list[CandidateAnswer]:
        try:
            return list(await asyncio.gather(*(self.answer(task, r.input) for r in records)))
        finally:
            if self.spent_usd:
                spend.record(f"openrouter:{short_name(self.model)}", self.spent_usd, f"candidate verdicts")

    async def aclose(self) -> None:
        await self.client.aclose()
        self.db.close()


def _verdict(data: dict, labels: list[str]) -> str | None:
    try:
        content = data["choices"][0]["message"]["content"]
        verdict = json.loads(content).get("verdict") if isinstance(content, str) else content.get("verdict")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    return verdict if verdict in labels else None


@dataclass
class CandidateRow:
    epsilon: float
    certificate: Certificate | None
    strategy: str  # gated | alone
    monthly_savings_usd: float | None = None
    monthly_fee_usd: float | None = None
    monthly_net_usd: float | None = None

    @property
    def coverage(self) -> float:
        return self.certificate.coverage if self.certificate else 0.0


@dataclass
class CandidateResult:
    """How much of the task one cheaper model can take over, at each budget."""

    model: str
    agreement: float  # candidate verdict == reference, held-out calls
    answered_share: float  # calls where the candidate returned a valid label
    usd_per_call: float  # the candidate's own cost
    gate_usd_per_call: float  # decision-model cost per call when gated (0 when alone)
    rows: list[CandidateRow] = field(default_factory=list)
    switch_all_bound: float | None = None  # 95% upper bound on disagreement if it answered everything
    latency_p50_ms: float | None = None
    gate: Combiner | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return short_name(self.model)

    def best_row(self, preferred: float = 0.01) -> CandidateRow | None:
        certified = [r for r in self.rows if r.certificate and r.coverage > 0]
        exact = [r for r in certified if abs(r.epsilon - preferred) < 1e-9]
        return (exact or certified or [None])[0]


def gate_features(X: np.ndarray | None, verdicts: list[str | None], labels: list[str]) -> np.ndarray:
    """Decision-model features (if any) plus a one-hot of the candidate's verdict."""
    onehot = np.array([[1.0 if v == label else 0.0 for label in labels] + [1.0 if v is None else 0.0]
                       for v in verdicts])
    return onehot if X is None else np.hstack([X, onehot])


def evaluate(
    model: str,
    answers: list[CandidateAnswer],
    y: np.ndarray,
    labels: list[str],
    idx_train: np.ndarray,
    idx_cal: np.ndarray,
    X: np.ndarray | None,
    epsilons: tuple[float, ...],
    delta: float,
    gate_usd_per_call: float,
    savings,
    seed: int = 0,
) -> CandidateResult:
    """Certify a candidate on the audit's own train/calibration split.

    `X` holds the decision models' kept features for every record (None without decision
    models); `savings(row, per_call_cost)` fills a row's monthly figures.
    """
    verdicts = [a.verdict for a in answers]
    agree = np.array([v == ref for v, ref in zip(verdicts, y)])
    usd = float(np.mean([call_usd(model, a.input_tokens, a.output_tokens) for a in answers])) if answers else 0.0
    result = CandidateResult(model=model, agreement=float(agree[idx_cal].mean()),
                             answered_share=float(np.mean([v is not None for v in verdicts])),
                             usd_per_call=usd, gate_usd_per_call=gate_usd_per_call if X is not None else 0.0,
                             latency_p50_ms=_p50([a.latency_ms for a in answers if a.latency_ms]))

    # Everything to the candidate: certified only if its overall disagreement bound fits the budget.
    ones_train, ones_cal = np.ones(len(idx_train)), np.ones(len(idx_cal))
    alone = {eps: certify(ones_train, agree[idx_train], ones_cal, agree[idx_cal], eps, delta) for eps in epsilons}
    from .certify import clopper_pearson_upper  # noqa: PLC0415
    result.switch_all_bound = float(clopper_pearson_upper(int((~agree[idx_cal]).sum()), len(idx_cal), delta))

    gated: dict[float, Certificate | None] = {}
    if X is not None and agree[idx_train].any() and (~agree[idx_train]).any():
        F = gate_features(X, verdicts, labels)
        target = np.where(agree, "agree", "disagree").astype(object)
        gate = Combiner()
        oof_labels, oof_scores = gate.out_of_fold(F[idx_train], target[idx_train], seed=seed)
        gate.fit(F[idx_train], target[idx_train])
        cal_labels, cal_scores = gate.predict(F[idx_cal])
        p_train = np.where(oof_labels == "agree", oof_scores, 1 - oof_scores)
        p_cal = np.where(cal_labels == "agree", cal_scores, 1 - cal_scores)
        gated = {eps: certify(p_train, agree[idx_train], p_cal, agree[idx_cal], eps, delta) for eps in epsilons}
        result.gate = gate

    for eps in epsilons:
        a, g = alone.get(eps), gated.get(eps)
        use_gate = g is not None and (a is None or g.coverage > a.coverage)
        row = CandidateRow(eps, g if use_gate else a, "gated" if use_gate else "alone")
        savings(row, usd + (result.gate_usd_per_call if use_gate else 0.0))
        result.rows.append(row)
    return result


def _p50(values: list[float]) -> float | None:
    return float(np.percentile(values, 50)) if values else None
