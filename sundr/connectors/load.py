"""Load observability-tool exports into JudgmentCalls, then into Sundr Records and a TaskSpec."""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from ..compiler import TaskSpec
from ..types import Record
from . import extract as ex
from .sources import JudgmentCall, apply_verdict, build_context, dedupe_braintrust, map_row

ENVELOPES = ("data", "items", "events", "runs")
ROW_KEYS = {"id", "request_id", "request", "response"}
DESCRIPTION_CHARS = 1500


def read_rows(path: str | Path) -> list[dict]:
    """Rows from a JSON array, a {data|items|events|runs: [...]} envelope, or JSONL (optionally gzipped)."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except ValueError:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [row for item in items for row in _unwrap(item)]


def _unwrap(item: Any) -> list[dict]:
    if not isinstance(item, dict):
        return []
    if not ROW_KEYS & item.keys() and any(key in item for key in ENVELOPES):
        rows = next((item[key] for key in ENVELOPES if isinstance(item.get(key), list)), [])
        return [row for row in rows if isinstance(row, dict)]
    return [item]


def map_rows(
    rows: list[dict], source: str, *, task_key: str | None = "task", verdict_field: str | None = None
) -> list[JudgmentCall]:
    """Map exported rows to calls, skipping non-LLM spans and cache hits, and deduplicating by id."""
    if source == "braintrust":
        rows = dedupe_braintrust(rows)
    ctx = build_context(rows, source, task_key, verdict_field)
    calls, seen = [], set()
    for row in rows:
        call = map_row(row, source, ctx) if isinstance(row, dict) else None
        if call is not None and call.id not in seen:
            seen.add(call.id)
            calls.append(call)
    return calls


def load_export(
    path: str | Path, source: str, *, task_key: str | None = "task", verdict_field: str | None = None
) -> list[JudgmentCall]:
    """Read an export from `source` (langfuse, helicone, langsmith, braintrust or raw) into calls.

    `task_key` names the metadata/property key that holds an explicit task name;
    `verdict_field` is a JSON Pointer that overrides verdict-field selection.
    """
    return map_rows(read_rows(path), source, task_key=task_key, verdict_field=verdict_field)


def group_tasks(
    calls: list[JudgmentCall], verdict_field: str | dict[str, str] | None = None
) -> dict[str, list[JudgmentCall]]:
    """Group calls by task and settle each task's verdict field: config, then schema, then the data (§6.5)."""
    groups: dict[str, list[JudgmentCall]] = defaultdict(list)
    for call in calls:
        groups[call.task or "unknown"].append(call)
    for task, members in groups.items():
        pointer = verdict_field.get(task) if isinstance(verdict_field, dict) else verdict_field
        if pointer:
            for call in members:
                call.verdict_candidates = []
                apply_verdict(call, pointer)
        else:
            _resolve_field(members)
    return dict(groups)


def _resolve_field(members: list[JudgmentCall]) -> None:
    pending = [c for c in members if c.verdict_field is None and not c.verdict_candidates]
    known = Counter(c.verdict_field for c in members if c.verdict_field is not None)
    by_schema: dict[str, list[JudgmentCall]] = defaultdict(list)
    for call in pending:
        by_schema[ex.canonical_json(call.response_schema)].append(call)
    for calls in by_schema.values():
        schema = calls[0].response_schema
        if schema is None and known:
            pointer = known.most_common(1)[0][0]
            for call in calls:
                apply_verdict(call, pointer, inferred=True)
            _canonicalize(calls, _known_options(members))
            continue
        candidates = None
        if schema is not None:
            candidates = ex.select_verdict_field(schema).candidates
            if not candidates:
                continue
        choice = ex.infer_verdict_field([c.parsed_output for c in calls if c.parsed_output], candidates)
        for call in calls:
            if choice.pointer is not None:
                apply_verdict(call, choice.pointer, inferred=schema is None)
            elif choice.ambiguous:
                call.verdict_candidates = choice.candidates
        if choice.pointer is not None and schema is None:
            _canonicalize(calls)


def _known_options(calls: list[JudgmentCall]) -> list | None:
    counts = Counter(tuple(c.verdict_options) for c in calls if c.verdict_options)
    return list(counts.most_common(1)[0][0]) if counts else None


def _fold(value: Any) -> Any:
    return value.strip().casefold() if isinstance(value, str) else (type(value).__name__, value)


def _canonicalize(calls: list[JudgmentCall], options: list | None = None) -> None:
    """One spelling per schema-less verdict value, and the observed values as options, most frequent first."""
    forms: dict[Any, Counter] = defaultdict(Counter)
    for call in calls:
        if call.verdict is not None:
            forms[_fold(call.verdict)][call.verdict] += 1
    canonical = {key: counts.most_common(1)[0][0] for key, counts in forms.items()}
    for option in options or []:
        if _fold(option) in canonical:
            canonical[_fold(option)] = option
    for call in calls:
        if call.verdict is not None:
            call.verdict = canonical[_fold(call.verdict)]
    observed = Counter(call.verdict for call in calls if call.verdict is not None)
    ordered = [v for v, _ in sorted(observed.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    values = options or ordered
    kind = "enum" if values and all(isinstance(v, str) for v in values) else (ex.value_type(values[0]) if values else None)
    for call in calls:
        call.verdict_options, call.verdict_type = values or None, kind


def verdict_text(value: Any) -> str:
    """A verdict as Record.reference text: booleans become "true"/"false"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _part_text(part: dict) -> str:
    if part.get("type") == "text":
        return str(part.get("text") or "")
    if part.get("type") in ("image", "file"):
        return f"[{part['type']}]"
    return f"[{part.get('kind') or 'other'}]"


def _plain(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(_part_text(p) for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def render_input(messages: list[dict]) -> str:
    """The judged text: a lone turn as-is, several turns as "role: content" lines. System turns are dropped."""
    turns = [m for m in messages if m.get("role") != "system"]
    if len(turns) == 1:
        return _plain(turns[0].get("content"))
    return "\n".join(f"{m.get('role')}: {_plain(m.get('content'))}" for m in turns)


def _is_option(value: Any, options: list) -> bool:
    return any(o == value and isinstance(o, bool) == isinstance(value, bool) for o in options)


def _meta(call: JudgmentCall) -> dict:
    meta = {
        "task": call.task,
        "model": call.model,
        "timestamp": call.timestamp,
        "tool": call.tool,
        "api": call.source.get("api"),
        "verdict_field": call.verdict_field,
        "verdict_type": call.verdict_type,
        "parse_status": call.parse_status,
        "rationale": call.rationale,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cached_input_tokens": call.cached_input_tokens,
        "reasoning_tokens": call.reasoning_tokens,
        "cost_source": call.cost_source,
        "latency_ms": call.latency_ms,
        "labels": call.labels or None,
    }
    return {k: v for k, v in meta.items() if v is not None}


def to_records(calls: list[JudgmentCall]) -> list[Record]:
    """Records for the certify pipeline.

    Calls without a usable verdict (refusals, truncations, unparseable output,
    an ambiguous verdict field, or a value outside a known option set) are skipped.
    """
    records = []
    for call in calls:
        if call.verdict is None or (call.verdict_options and not _is_option(call.verdict, call.verdict_options)):
            continue
        records.append(
            Record(
                id=call.id,
                input=render_input(call.input_messages),
                reference=verdict_text(call.verdict),
                cost_usd=call.cost_usd,
                meta=_meta(call),
            )
        )
    return records


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + "…"


def task_spec(task_name: str, calls: list[JudgmentCall]) -> TaskSpec:
    """A TaskSpec from a task's calls: the common system prompt, and the known or observed labels."""
    prompts = Counter(c.system_prompt.strip() for c in calls if c.system_prompt and c.system_prompt.strip())
    if prompts:
        description = _trim(prompts.most_common(1)[0][0], DESCRIPTION_CHARS)
    else:
        description = f"Give the verdict the current LLM judge gives for this input (task: {task_name})."
    options = _known_options(calls)
    if options:
        labels = [verdict_text(o) for o in options]
    else:
        seen = Counter(verdict_text(c.verdict) for c in calls if c.verdict is not None)
        labels = [label for label, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]
    descriptions: dict[str, str] = {}
    for call in calls:
        found = ex.option_descriptions(call.response_schema, call.verdict_field)
        if found:
            descriptions = {verdict_text(k): str(v) for k, v in found.items() if verdict_text(k) in labels}
            break
    return TaskSpec(name=task_name, description=description, labels=labels, label_descriptions=descriptions)


def write_records_jsonl(records: list[Record], path: str | Path) -> None:
    """JSONL with the field names sundr.ingest.read_records reads by default."""
    with Path(path).open("w", encoding="utf-8") as f:
        for record in records:
            row = {"id": record.id, "input": record.input, "verdict": record.reference, "cost_usd": record.cost_usd}
            if record.gold is not None:
                row["gold"] = record.gold
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_task_yaml(spec: TaskSpec, path: str | Path) -> None:
    """A task YAML that TaskSpec.load reads back."""
    data: dict[str, Any] = {"name": spec.name, "description": spec.description, "labels": list(spec.labels)}
    if spec.label_descriptions:
        data["label_descriptions"] = dict(spec.label_descriptions)
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
