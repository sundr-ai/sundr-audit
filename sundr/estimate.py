"""Rough decision-model token estimates, for approving spend before a run."""

from __future__ import annotations

from .types import Question, Record

CHARS_PER_TOKEN = 4
# Servers wrap each question and option in template text. Measured on Jev and Clef-flash
# (Oct 9 runs: ~1,200 tokens per phishing call vs ~620 from text alone); set to err high.
QUESTION_OVERHEAD = 35
OPTION_OVERHEAD = 10


def question_tokens(q: Question) -> int:
    text = q.instructions + " ".join(q.options) + " ".join(d for d in q.descriptions if d)
    return len(text) // CHARS_PER_TOKEN + QUESTION_OVERHEAD + OPTION_OVERHEAD * len(q.option_names())


def estimate_tokens(records: list[Record], questions: list[Question], option_orders: int = 2) -> int:
    """Input tokens per backend: the state once per call, plus every question's text.

    Choice questions are asked a second time with options reversed, in a second call.
    """
    all_q = sum(question_tokens(q) for q in questions)
    choice_q = sum(question_tokens(q) for q in questions if q.type == "choice")
    total = 0
    for r in records:
        state = len(r.input) // CHARS_PER_TOKEN
        total += state + all_q
        if option_orders >= 2 and choice_q:
            total += state + choice_q
    return total


def estimate_audit_tokens(records: list[Record], questions: list[Question], max_questions: int = 20,
                          selection_rows: int = 1500, train_fraction: float = 0.6) -> int:
    """Upper estimate for run_audit: every question on the selection slice, then the kept ones on all rows."""
    if len(questions) <= max_questions + 1 or len(records) * train_fraction <= selection_rows:
        return estimate_tokens(records, questions)
    largest = sorted(questions, key=question_tokens, reverse=True)[: max_questions + 1]
    return estimate_tokens(records[:selection_rows], questions) + estimate_tokens(records, largest)
