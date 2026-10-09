import asyncio

from sundr import demo
from sundr.audit import AuditConfig, run_audit
from sundr.report import render_report


def test_audit_end_to_end_on_simulated_backends(tmp_path):
    recs = demo.records(n=8000)
    result = asyncio.run(
        run_audit(
            demo.TASK,
            recs,
            demo.questions(),
            demo.backends(recs),
            AuditConfig(epsilons=(0.01, 0.02, 0.05), monthly_calls=1_000_000, usd_per_call=0.004),
            cache_path=str(tmp_path / "cache.sqlite"),
        )
    )
    assert result.n_records == 8000
    # Splitting the question should beat asking it once.
    assert result.agreement_all > result.baseline_agreement_all
    coverages = [row.coverage for row in result.rows]
    assert coverages == sorted(coverages) and coverages[-1] > 0.3
    certified = [row for row in result.rows if row.certificate]
    assert all(row.certificate.upper_bound <= row.epsilon for row in certified)
    assert all(row.monthly_net_usd > 0 for row in certified)

    html = render_report(result)
    assert "Savings Audit" in html and "<svg" in html


def test_two_stage_audit_scores_kept_questions_only(tmp_path):
    recs = demo.records(n=4000)
    questions = demo.questions()
    config = AuditConfig(epsilons=(0.02, 0.05), max_questions=2, selection_rows=500)
    assert len(questions) > config.max_questions + 1
    result = asyncio.run(run_audit(demo.TASK, recs, questions, demo.backends(recs), config,
                                   cache_path=str(tmp_path / "cache.sqlite")))
    assert len(result.questions_kept) <= 2
    assert result.baseline_agreement_all is not None
    assert result.agreement_all > result.baseline_agreement_all
    assert all(row.certificate.upper_bound <= row.epsilon for row in result.rows if row.certificate)


def test_human_labels_are_compared_when_present(tmp_path):
    from dataclasses import replace

    recs = demo.records(n=3000)
    # Human labels: the truth, except the LLM's verdict is wrong on some (simulated) share.
    labelled = [replace(r, gold=r.reference) for r in recs]
    labelled[0] = replace(labelled[0], gold="legitimate" if labelled[0].reference == "phishing" else "phishing")
    result = asyncio.run(run_audit(demo.TASK, labelled, demo.questions(), demo.backends(recs),
                                   AuditConfig(epsilons=(0.02, 0.05)), cache_path=str(tmp_path / "c.sqlite")))
    assert result.gold is not None and result.gold.n > 500
    assert result.gold.llm_error < 0.01
    html = render_report(result)
    assert "Against your human labels" in html


class FakeCandidate:
    """A cheaper LLM that agrees with the reference except on a fixed 2% of calls."""

    def __init__(self, model="anthropic/claude-haiku-5.5"):
        self.model, self.spent_usd = model, 0.0

    async def answer_all(self, task, records):
        from sundr.candidates import CandidateAnswer
        out = []
        for i, r in enumerate(records):
            wrong = i % 50 == 0
            other = next(label for label in task.labels if label != r.reference)
            out.append(CandidateAnswer(other if wrong else r.reference, 300, 6, 400.0))
        return out


def test_candidates_are_certified_alongside_decision_models(tmp_path):
    recs = demo.records(n=6000)
    config = AuditConfig(epsilons=(0.01, 0.05), monthly_calls=1_000_000, usd_per_call=0.004)
    result = asyncio.run(run_audit(demo.TASK, recs, demo.questions(), demo.backends(recs), config,
                                   cache_path=str(tmp_path / "c.sqlite"), candidates=[FakeCandidate()]))
    [cand] = result.candidates
    assert cand.name == "claude-haiku-5.5" and 0.97 < cand.agreement < 0.99
    assert cand.switch_all_bound < 0.05
    at5 = [r for r in cand.rows if r.epsilon == 0.05][0]
    assert at5.certificate is not None and at5.coverage > 0.9 and at5.monthly_net_usd > 0
    at1 = [r for r in cand.rows if r.epsilon == 0.01][0]
    assert at1.certificate is None or at1.certificate.upper_bound <= 0.01
    assert abs(cand.usd_per_call - (300 * 0.10 + 6 * 0.50) / 1e6) < 1e-12
    html = render_report(result)
    assert "claude-haiku-5.5" in html
