import json

from typer.testing import CliRunner

from sundr import candidates
from sundr.cli import app


class FakeClient:
    def __init__(self, model, cache_path=None, system=None):
        self.model, self.system, self.spent_usd = model, system, 0.0

    async def answer_all(self, task, records):
        # Agrees except on every 100th call.
        return [candidates.CandidateAnswer(
            ("no" if r.reference == "yes" else "yes") if i % 100 == 0 else r.reference, 200, 5, 300.0)
            for i, r in enumerate(records)]

    async def aclose(self):
        pass


def test_certify_writes_a_pr_summary_and_gates_on_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(candidates, "CandidateClient", FakeClient)
    logs = tmp_path / "evals.jsonl"
    logs.write_text("\n".join(json.dumps({"input": f"case {i}", "verdict": "yes" if i % 3 else "no"})
                              for i in range(3000)))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Decide yes or no.")
    md, js = tmp_path / "out.md", tmp_path / "out.json"
    result = CliRunner().invoke(app, ["certify", str(logs), "--candidate", "openai/gpt-6-luna",
                                      "--system-prompt-file", str(prompt), "--markdown", str(md), "--json", str(js),
                                      "--cache", str(tmp_path / "c.sqlite")])
    assert result.exit_code == 0, result.output
    text = md.read_text()
    assert "| `openai/gpt-6-luna` | 98." in text and "| no | yes | yes |" in text and "own system prompt" in text
    [row] = json.loads(js.read_text())
    assert row["certified"]["0.05"] == 1.0 and row["certified"]["0.01"] == 0.0
    strict = CliRunner().invoke(app, ["certify", str(logs), "--candidate", "openai/gpt-6-luna", "--epsilon", "0.01",
                                      "--require-coverage", "0.5", "--cache", str(tmp_path / "c.sqlite")])
    assert strict.exit_code == 1
