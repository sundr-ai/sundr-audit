import pytest


@pytest.fixture(autouse=True)
def isolated_spend_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SUNDR_SPEND_LEDGER", str(tmp_path / "spend.jsonl"))
    monkeypatch.setenv("SUNDR_MONTHLY_BUDGET_USD", "25")
