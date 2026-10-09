import json

from typer.testing import CliRunner

from sundr.cli import app
from sundr.compiler import TaskSpec
from sundr.ingest import read_records

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["label", "rationale"],
          "properties": {"label": {"type": "string", "enum": ["billing", "bug", "other"]}, "rationale": {"type": "string"}}}


def _row(i, label):
    request = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": "Classify the support ticket."},
                     {"role": "user", "content": f"Ticket {i}: please help"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "ticket_label", "strict": True, "schema": SCHEMA}},
    }
    response = {"object": "chat.completion", "created": 1760000000 + i, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps({"label": label, "rationale": "x"})}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}}
    return {"id": f"r{i}", "request": request, "response": response}


def test_import_raw_openai_export(tmp_path):
    export = tmp_path / "raw.jsonl"
    labels = ["billing", "bug", "other"]
    export.write_text("\n".join(json.dumps(_row(i, labels[i % 3])) for i in range(30)))
    result = CliRunner().invoke(app, ["import", "raw", str(export), "--out-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    folder = next((tmp_path / "out").iterdir())
    records = read_records(folder / "logs.jsonl")
    spec = TaskSpec.load(folder / "task.yaml")
    assert len(records) == 30 and records[0].input.startswith("Ticket 0")
    assert set(spec.labels) == set(labels) and "Classify the support ticket" in spec.description
    assert records[0].cost_usd and records[0].cost_usd > 0
