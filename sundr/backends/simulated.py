"""An offline stand-in for a decision model, for tests and demos without API keys.

It knows each text's true label and answers questions with noisy, partly
informative probabilities, so the pipeline can be exercised end to end.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from ..types import Answer, Question
from .base import BackendResult


def _rng(*parts: str) -> np.random.Generator:
    seed = int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "little")
    return np.random.default_rng(seed)


class SimulatedBackend:
    simulated = True  # never recorded as spend

    def __init__(
        self,
        truth: dict[str, str],
        labels: list[str],
        name: str = "simulated",
        verdict_strength: float = 1.2,
        signal_strength: float = 1.6,
        noise: float = 1.0,
        position_bias: float = 0.4,
        label_noise: float = 0.03,
    ):
        self.name = name
        self.model = f"{name}-1"
        self.usd_per_million_input_tokens = 0.042
        self.truth = truth
        self.labels = labels
        self.verdict_strength = verdict_strength
        self.signal_strength = signal_strength
        self.noise = noise
        self.position_bias = position_bias
        self.label_noise = label_noise

    def _apparent(self, state: str) -> tuple[str, float]:
        """What the text looks like to every model: shared clarity, and a few
        texts whose logged verdict disagrees with their content."""
        shared = _rng("text", state)
        clarity = float(shared.beta(2.0, 1.2))
        label = self.truth[state]
        # Judges err mostly on ambiguous texts.
        if shared.random() < self.label_noise * 3 * (1 - clarity) ** 2:
            label = next(x for x in self.labels if x != label)
        return label, clarity

    def _answer(self, state: str, q: Question) -> Answer:
        rng = _rng(self.name, state, q.name)
        label, clarity = self._apparent(state)
        options = q.option_names()
        if q.type == "noul":
            # Each yes/no signal fires for one label, chosen by its name's hash.
            target = self.labels[int(hashlib.md5(q.name.encode()).hexdigest(), 16) % len(self.labels)]
            strength = self.verdict_strength if q.name.startswith("verdict") else self.signal_strength
            z = strength * clarity * (1 if label == target else -1) + rng.normal(0, self.noise)
            p = 1 / (1 + math.exp(-z))
            return Answer({"yes": p, "no": 1 - p})
        # Noise belongs to each option (the model's belief), not its position,
        # plus a first-option bias that averaging both orders should cancel.
        logits = np.array([_rng(self.name, state, q.name, opt).normal(0, self.noise) for opt in options])
        logits[0] += self.position_bias
        if label in options:
            logits[options.index(label)] += self.verdict_strength * clarity
        else:
            logits[int(rng.integers(len(options)))] += self.signal_strength * 0.5
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        return Answer(dict(zip(options, probs.tolist())))

    async def ask(self, state: str, questions: list[Question]) -> BackendResult:
        answers = {q.name: self._answer(state, q) for q in questions}
        tokens = len(state) // 4 + sum(len(q.instructions) // 4 + 8 for q in questions)
        return BackendResult(answers=answers, input_tokens=tokens, latency_ms=1.0, model=self.model)

    async def aclose(self) -> None:
        return None
