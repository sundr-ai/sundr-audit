"""Core data types shared across Sundr."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

QuestionType = Literal["choice", "score", "noul"]


@dataclass(frozen=True)
class Question:
    """One typed question asked of a decision model about a record's text.

    `options` holds Choice options, or Score levels ordered low to high.
    Noul (yes/no) questions leave it empty.
    """

    name: str
    type: QuestionType
    instructions: str
    options: tuple[str, ...] = ()
    descriptions: tuple[str, ...] = ()
    criteria: str | None = None

    def option_names(self) -> tuple[str, ...]:
        return ("yes", "no") if self.type == "noul" else self.options


@dataclass
class Record:
    """One logged judgment call.

    `reference` is the verdict Sundr must reproduce: the customer's LLM
    verdict in agreement mode, or a human label in accuracy mode.
    """

    id: str
    input: str
    reference: str
    gold: str | None = None
    cost_usd: float | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Answer:
    """A decision model's answer: a probability per option name."""

    probs: dict[str, float]

    def top(self) -> tuple[str, float]:
        label = max(self.probs, key=self.probs.__getitem__)
        return label, self.probs[label]
