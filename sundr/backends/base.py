"""The interface every decision-model backend implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..types import Answer, Question


@dataclass
class BackendResult:
    answers: dict[str, Answer]
    input_tokens: int
    latency_ms: float
    model: str


class Backend(Protocol):
    """Answers a batch of typed questions about one text in a single call."""

    name: str
    model: str
    usd_per_million_input_tokens: float

    async def ask(self, state: str, questions: list[Question]) -> BackendResult: ...

    async def aclose(self) -> None: ...


class BackendError(RuntimeError):
    """A backend call failed after its retries."""
