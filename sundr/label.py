"""Reference labels from Claude via the Message Batches API (50% off), for agreement-mode benchmarks.

In agreement mode Sundr must reproduce an LLM judge's verdicts. Public
datasets carry human labels, so this simulates the customer's judge: Claude
labels each item once, and those verdicts become the reference.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import claude
from .compiler import TaskSpec
from .types import Record

LABEL_MODEL = "claude-opus-5-5"


def _schema(task: TaskSpec) -> dict:
    return {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": task.labels}},
        "required": ["label"],
        "additionalProperties": False,
    }


def _system(task: TaskSpec) -> str:
    labels = "\n".join(
        f"- {label}" + (f": {task.label_descriptions[label]}" if label in task.label_descriptions else "")
        for label in task.labels
    )
    return f"{task.description}\n\nAnswer with exactly one label:\n{labels}"


def estimate_usd(records: list[Record], task: TaskSpec, input_usd_per_m: float = 4.0, output_usd_per_m: float = 20.0) -> float:
    """Rough batch cost: ~4 characters per token, 50% batch discount, ~300 output tokens with thinking."""
    system_tokens = len(_system(task)) / 4
    input_tokens = sum(len(r.input) / 4 + system_tokens for r in records)
    output_tokens = 300 * len(records)
    return 0.5 * (input_tokens * input_usd_per_m + output_tokens * output_usd_per_m) / 1e6


def submit(records: list[Record], task: TaskSpec, model: str = LABEL_MODEL, client=None) -> str:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = client or claude.client()
    system = [{"type": "text", "text": _system(task), "cache_control": {"type": "ephemeral"}}]
    requests = [
        Request(
            custom_id=r.id[:64],
            params=MessageCreateParamsNonStreaming(
                model=model,
                max_tokens=2048,
                system=system,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(task)}},
                messages=[{"role": "user", "content": r.input}],
            ),
        )
        for r in records
    ]
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def collect(batch_id: str, out: str | Path, client=None, poll_s: float = 60.0) -> dict[str, str]:
    """Wait for a batch, then write {id, verdict} JSONL. Failed items are skipped and counted."""
    client = client or claude.client()
    while client.messages.batches.retrieve(batch_id).processing_status != "ended":
        time.sleep(poll_s)
    verdicts, failed = {}, 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded" or result.result.message.stop_reason == "refusal":
            failed += 1
            continue
        text = next((b.text for b in result.result.message.content if b.type == "text"), "")
        try:
            verdicts[result.custom_id] = json.loads(text)["label"]
        except (json.JSONDecodeError, KeyError):
            failed += 1
    with Path(out).open("w") as f:
        for rid, label in verdicts.items():
            f.write(json.dumps({"id": rid, "verdict": label}) + "\n")
    if failed:
        print(f"{failed} items failed or were declined and were skipped")
    return verdicts
