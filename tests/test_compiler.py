from sundr.compiler import TaskSpec, lint_bank, load_bank, save_bank, verdict_questions
from sundr.types import Question


def test_linter_fixes_and_drops():
    bank = [
        Question("ok", "noul", "The email asks the reader to sign in."),
        Question("negated", "noul", "The email does not mention a refund."),
        Question("counting", "noul", "The email has more than 3 links."),
        Question("abstain", "choice", "What tone does the email use?", ("formal", "casual", "unsure")),
        Question("ok", "noul", "Duplicate name."),
        Question("opaque", "choice", "Pick one", ("A", "B")),
    ]
    kept, issues = lint_bank(bank)
    names = [q.name for q in kept]
    assert names == ["ok", "abstain"]
    assert kept[1].options == ("formal", "casual")
    rules = {i.rule for i in issues}
    assert {"negation", "numeric", "abstain-option", "duplicate-name", "opaque-option-name"} <= rules


def test_verdict_questions_binary_adds_mirror():
    task = TaskSpec("t", "Is this spam?", ["spam", "ham"])
    names = [q.name for q in verdict_questions(task)]
    assert names == ["verdict", "verdict_alt", "verdict_mirror"]
    assert len(verdict_questions(TaskSpec("t", "Intent?", ["a1", "b2", "c3"]))) == 2


def test_bank_round_trip(tmp_path):
    bank = [Question("q", "choice", "Tone?", ("formal", "casual"), ("polite", "chatty"))]
    save_bank(bank, tmp_path / "bank.yaml")
    assert load_bank(tmp_path / "bank.yaml") == bank


def test_yes_no_verdicts_survive_the_linter():
    task = TaskSpec("t", "Does the clause grant audit rights?", ["Yes", "No"])
    kept, _ = lint_bank(verdict_questions(task))
    assert [q.name for q in kept] == ["verdict", "verdict_alt", "verdict_mirror"]
