"""Read logged judgment calls into Records."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .types import Record


@dataclass
class FieldMap:
    """Where each Record field lives in the customer's export. Dotted paths reach into nested JSON."""

    id: str = "id"
    input: str = "input"
    reference: str = "verdict"
    gold: str | None = "gold"
    cost_usd: str | None = "cost_usd"


def _get(row: dict, path: str | None):
    if not path:
        return None
    value = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_records(path: str | Path, fields: FieldMap | None = None, limit: int | None = None) -> list[Record]:
    """Read JSONL or CSV. Rows missing an input or reference verdict are skipped."""
    fields = fields or FieldMap()
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        with path.open() as f:
            rows = [json.loads(line) for line in f if line.strip()]

    records = []
    for i, row in enumerate(rows):
        text, reference = _get(row, fields.input), _get(row, fields.reference)
        if text is None or reference is None:
            continue
        cost = _get(row, fields.cost_usd)
        gold = _get(row, fields.gold)
        records.append(
            Record(
                id=str(_get(row, fields.id) or i),
                input=_text(text),
                reference=str(reference),
                gold=None if gold is None else str(gold),
                cost_usd=float(cost) if cost not in (None, "") else None,
            )
        )
        if limit and len(records) >= limit:
            break
    return records
