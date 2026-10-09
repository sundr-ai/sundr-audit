"""Turn a judgment task into a bank of narrow decision-model questions.

Claude drafts candidate questions once per task. A linter then removes the
patterns Jev 1.13 is documented to handle badly: negation, "unsure" options,
option names that fight their definitions, and counting, maths or dates.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import claude, spend
from .types import Question, Record

DRAFT_MODEL = "claude-opus-5-5"
DRAFT_MAX_USD = 0.5  # 20k input + 16k output tokens on Opus 5.5 is ~$0.40
MAX_QUESTION_CHARS = 300
MAX_OPTIONS = 255
MAX_LABELS_FOR_ALT_WORDING = 20
VERDICT_NAMES = ("verdict", "verdict_alt", "verdict_mirror")

ABSTAIN_OPTIONS = re.compile(
    r"^(unsure|not sure|unknown|other|n/?a|none of (the|these)|can'?t tell|unclear|maybe)$", re.I
)
NEGATION = re.compile(r"\b(not|never|no|none|neither|nor|without)\b|n't\b", re.I)
NUMERIC = re.compile(
    r"\b(how many|count|number of|more than \d|fewer than \d|at least \d|sum|total|percent|"
    r"average|before|after|older than|newer than|days? (ago|since)|date)\b",
    re.I,
)


@dataclass
class TaskSpec:
    """The judgment the customer's LLM makes today."""

    name: str
    description: str
    labels: list[str]
    label_descriptions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TaskSpec":
        data = yaml.safe_load(Path(path).read_text())
        return cls(
            name=data["name"],
            description=data["description"],
            labels=[str(x) for x in data["labels"]],
            label_descriptions={str(k): v for k, v in (data.get("label_descriptions") or {}).items()},
        )


@dataclass
class LintIssue:
    question: str
    rule: str
    detail: str
    fixed: bool


def verdict_questions(task: TaskSpec) -> list[Question]:
    """The direct verdict in two wordings, plus a yes/no mirror for binary tasks."""
    descriptions = tuple(task.label_descriptions.get(label, "") for label in task.labels)
    questions = [
        Question(
            name="verdict",
            type="choice",
            instructions=task.description,
            options=tuple(task.labels),
            descriptions=descriptions,
        ),
    ]
    # A second wording is cheap for a few labels; with many labels it doubles the
    # cost of the largest question for little gain.
    if len(task.labels) <= MAX_LABELS_FOR_ALT_WORDING:
        questions.append(
            Question(
                name="verdict_alt",
                type="choice",
                instructions=f"Which label best describes this text? Task: {task.description}",
                options=tuple(task.labels),
                descriptions=descriptions,
            )
        )
    if len(task.labels) == 2:
        positive = task.labels[0]
        questions.append(
            Question(
                name="verdict_mirror",
                type="noul",
                instructions=f"The correct label for this text is '{positive}'. Task: {task.description}",
            )
        )
    return questions


def lint_question(q: Question) -> tuple[Question | None, list[LintIssue]]:
    """Fix what can be fixed automatically; drop what cannot."""
    issues: list[LintIssue] = []
    options, descriptions = list(q.options), list(q.descriptions)

    if q.type in ("choice", "score"):
        keep = [i for i, opt in enumerate(options) if not ABSTAIN_OPTIONS.match(opt.strip())]
        if len(keep) < len(options):
            dropped = [options[i] for i in range(len(options)) if i not in keep]
            issues.append(LintIssue(q.name, "abstain-option", f"removed {dropped}", True))
            options = [options[i] for i in keep]
            descriptions = [descriptions[i] for i in keep if i < len(descriptions)]
        if len(set(o.lower() for o in options)) < len(options):
            issues.append(LintIssue(q.name, "duplicate-options", "options repeat", False))
            return None, issues
        if len(options) < 2:
            issues.append(LintIssue(q.name, "too-few-options", "fewer than 2 options", False))
            return None, issues
        if len(options) > MAX_OPTIONS:
            issues.append(LintIssue(q.name, "too-many-options", f"{len(options)} > {MAX_OPTIONS}", False))
            return None, issues
        # The verdict questions carry the customer's own labels ("Yes"/"No" included); never rename or drop them.
        short = [o for o in options if len(o.strip()) < 2] if q.name not in VERDICT_NAMES else []
        if short:
            issues.append(LintIssue(q.name, "opaque-option-name", f"names too short: {short}", False))
            return None, issues

    if len(q.instructions) > MAX_QUESTION_CHARS:
        issues.append(LintIssue(q.name, "too-long", f"{len(q.instructions)} chars", False))
        return None, issues
    if NEGATION.search(q.instructions) and q.name not in VERDICT_NAMES:
        issues.append(LintIssue(q.name, "negation", "instructions contain a negation", False))
        return None, issues
    if NUMERIC.search(q.instructions):
        issues.append(
            LintIssue(q.name, "numeric", "counting, maths or dates belong in code features", False)
        )
        return None, issues

    fixed = Question(
        name=q.name,
        type=q.type,
        instructions=q.instructions.strip(),
        options=tuple(options),
        descriptions=tuple(descriptions),
        criteria=q.criteria,
    )
    return fixed, issues


def lint_bank(questions: list[Question]) -> tuple[list[Question], list[LintIssue]]:
    kept, issues, seen = [], [], set()
    for q in questions:
        if q.name in seen:
            issues.append(LintIssue(q.name, "duplicate-name", "name already used", False))
            continue
        fixed, found = lint_question(q)
        issues.extend(found)
        if fixed is not None:
            kept.append(fixed)
            seen.add(q.name)
    return kept, issues


DRAFT_SYSTEM = """You design question banks for a decision model. The decision model reads one \
text and answers typed questions about it, each independently, returning calibrated \
probabilities. It cannot write text.

Question types:
- "noul": a single yes/no claim about the text. Leave options and descriptions empty.
- "choice": pick exactly one option from a fixed list.
- "score": pick one level from an ordered list, lowest first (3 to 5 levels).

Your questions become features for a small classifier that reproduces an existing judgment. \
Good questions are narrow, observable signals that separate the labels, the way a careful \
reviewer would break the decision down.

Rules the decision model needs:
- One claim per question. No compound "and/or" claims.
- No negations in the wording (avoid not, never, no, without, n't). Phrase the positive claim.
- Option names must mean what their descriptions say; use plain descriptive names.
- Never offer "unsure", "other" or "unknown" options.
- No counting, arithmetic, date comparison or length checks; those are computed in code.
- At most 300 characters of instructions per question.
- Names are short snake_case identifiers, unique across the bank."""


QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["choice", "score", "noul"]},
                    "instructions": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "descriptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "type", "instructions", "options", "descriptions"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def example_records(records: list[Record], per_label: int = 8, total: int = 60, seed: int = 0) -> list[Record]:
    """A seeded sample with every label represented, for the drafter to read."""
    rng = random.Random(seed)
    by_label: dict[str, list[Record]] = {}
    for r in records:
        by_label.setdefault(r.reference, []).append(r)
    shown = []
    for rows in by_label.values():
        shown += rng.sample(rows, min(per_label, len(rows)))
    rng.shuffle(shown)
    return shown[:total]


def _draft_prompt(task: TaskSpec, examples: list[Record], n_questions: int) -> str:
    labels = "\n".join(
        f"- {label}: {task.label_descriptions.get(label, '')}".rstrip(": ") for label in task.labels
    )
    shown = "\n\n".join(
        f"<example verdict={json.dumps(r.reference)}>\n{r.input[:2000]}\n</example>" for r in examples
    )
    return (
        f"Task name: {task.name}\n\nThe judgment made today:\n{task.description}\n\n"
        f"Labels:\n{labels}\n\nExamples with the verdict the current judge gave:\n\n{shown}\n\n"
        f"Draft {n_questions} candidate questions that together let a classifier reproduce these "
        "verdicts. Cover every label, include signals that separate commonly confused labels, and "
        "skip the direct verdict itself (it is added separately)."
    )


def draft_questions(
    task: TaskSpec,
    examples: list[Record],
    n_questions: int = 40,
    model: str = DRAFT_MODEL,
    client=None,
    fallbacks: bool = True,
) -> list[Question]:
    """Ask Claude for candidate questions. Returns them unlinted."""
    spend.check(DRAFT_MAX_USD)
    client = client or claude.client()
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": DRAFT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": QUESTION_SCHEMA}},
        messages=[{"role": "user", "content": _draft_prompt(task, examples, n_questions)}],
    )
    if fallbacks:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    else:
        response = client.messages.create(**kwargs)

    spend.record("anthropic", spend.claude_usd(model, response.usage), f"draft {task.name}")
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Question drafting was declined: {response.stop_details}")
    text = next(b.text for b in response.content if b.type == "text")
    drafted = json.loads(text)["questions"]
    return [
        Question(
            name=q["name"],
            type=q["type"],
            instructions=q["instructions"],
            options=tuple(q["options"]),
            descriptions=tuple(q["descriptions"]),
        )
        for q in drafted
    ]


def save_bank(questions: list[Question], path: str | Path) -> None:
    rows = []
    for q in questions:
        row = {"name": q.name, "type": q.type, "instructions": q.instructions}
        if q.options:
            row["options"] = list(q.options)
        if any(q.descriptions):
            row["descriptions"] = list(q.descriptions)
        if q.criteria:
            row["criteria"] = q.criteria
        rows.append(row)
    Path(path).write_text(yaml.safe_dump({"questions": rows}, sort_keys=False, allow_unicode=True))


def load_bank(path: str | Path) -> list[Question]:
    data = yaml.safe_load(Path(path).read_text())
    return [
        Question(
            name=row["name"],
            type=row["type"],
            instructions=row["instructions"],
            options=tuple(row.get("options", ())),
            descriptions=tuple(row.get("descriptions", ())),
            criteria=row.get("criteria"),
        )
        for row in data["questions"]
    ]
