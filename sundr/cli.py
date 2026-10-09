"""Command line: draft questions, lint them, and run a Savings Audit."""

from __future__ import annotations

import asyncio
import importlib.util
import random
from pathlib import Path
from typing import Optional

import typer
from dotenv import find_dotenv, load_dotenv

from . import demo as demo_task
from . import spend
from .audit import DEFAULT_EPSILONS, AuditConfig, run_audit
from .backends.systemone import make_backend
from .compiler import TaskSpec, draft_questions, lint_bank, load_bank, save_bank, verdict_questions
from .ingest import FieldMap, read_records
from .report import write_report

app = typer.Typer(add_completion=False, help="Sundr: certified takeover of LLM judgment calls.")


def _with_verdicts(spec: TaskSpec, bank: list) -> list:
    """Banks written by hand may omit the direct verdict; add it so the baseline and combiner see it."""
    names = {q.name for q in bank}
    return [q for q in verdict_questions(spec) if q.name not in names] + bank


def _print_issues(issues) -> None:
    for issue in issues:
        mark = "fixed" if issue.fixed else "dropped"
        typer.echo(f"  {mark:7} {issue.question}: {issue.rule} ({issue.detail})")


@app.command()
def draft(
    task: Path = typer.Argument(..., help="Task YAML: name, description, labels, label_descriptions"),
    logs: Path = typer.Argument(..., help="Logged calls (JSONL or CSV)"),
    out: Path = typer.Option(Path("questions.yaml"), help="Where to write the question bank"),
    n: int = typer.Option(40, help="Candidate questions to draft"),
    examples: int = typer.Option(60, help="Logged examples shown to the drafter"),
    model: str = typer.Option("claude-opus-5-5", help="Claude model that drafts the questions"),
    input_field: str = "input",
    verdict_field: str = "verdict",
    seed: int = 0,
):
    """Draft a question bank with Claude, then lint it."""
    load_dotenv(find_dotenv(usecwd=True))
    spec = TaskSpec.load(task)
    records = read_records(logs, FieldMap(input=input_field, reference=verdict_field))
    random.Random(seed).shuffle(records)
    by_label: dict[str, list] = {}
    for r in records:
        by_label.setdefault(r.reference, []).append(r)
    shown, per_label = [], max(1, examples // max(len(by_label), 1))
    for rows in by_label.values():
        shown += rows[:per_label]
    typer.echo(f"Drafting {n} questions from {len(shown)} examples with {model}...")
    drafted = draft_questions(spec, shown, n_questions=n, model=model)
    kept, issues = lint_bank(verdict_questions(spec) + drafted)
    _print_issues(issues)
    save_bank(kept, out)
    typer.echo(f"Kept {len(kept)} questions -> {out}")


@app.command(name="import")
def import_export(
    source: str = typer.Argument(..., help="langfuse | helicone | langsmith | braintrust | raw"),
    export: Path = typer.Argument(..., help="JSON or JSONL export (optionally .gz)"),
    out_dir: Path = typer.Option(Path("imported"), help="One folder per task is written here"),
    task: Optional[str] = typer.Option(None, help="Only write this task"),
    verdict_field: Optional[str] = typer.Option(None, help="JSON Pointer to the verdict, e.g. /label"),
    task_key: str = typer.Option("task", help="Metadata/property key holding an explicit task name"),
):
    """Turn an observability-tool export into audit-ready logs.jsonl + task.yaml per task."""
    import re
    from collections import Counter
    from .connectors.load import group_tasks, load_export, task_spec, to_records, write_records_jsonl, write_task_yaml

    calls = load_export(export, source, task_key=task_key, verdict_field=verdict_field)
    groups = group_tasks(calls)
    if not groups:
        typer.echo("No LLM judgment calls with structured output were found.")
        raise typer.Exit(1)
    typer.echo(f"{len(calls):,} calls in {len(groups)} task(s):")
    for name, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if task and name != task:
            continue
        records = to_records(members)
        spec = task_spec(name, members)
        issues = Counter(c.parse_status for c in members if c.parse_status != "ok")
        field = members[0].verdict_field or "unresolved (pass --verdict-field)"
        typer.echo(f"  {name}: {len(members):,} calls, {len(records):,} usable, verdict {field}, "
                   f"{len(spec.labels)} labels" + (f", skipped {dict(issues)}" if issues else ""))
        if not records:
            continue
        folder = out_dir / re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:80]
        folder.mkdir(parents=True, exist_ok=True)
        write_records_jsonl(records, folder / "logs.jsonl")
        write_task_yaml(spec, folder / "task.yaml")
        typer.echo(f"    -> {folder}/logs.jsonl, {folder}/task.yaml")
    typer.echo("Next: sundr draft <folder>/task.yaml <folder>/logs.jsonl, then sundr audit.")


@app.command(name="pull")
def pull_calls(
    source: str = typer.Argument(..., help="langfuse | helicone | langsmith | braintrust"),
    out: Path = typer.Option(Path("calls.jsonl"), help="Where to write the fetched rows (JSONL)"),
    limit: int = typer.Option(30_000, help="Stop after this many LLM calls"),
    since_days: float = typer.Option(14, help="How many days back to look"),
    filters: list[str] = typer.Option([], "--filter", help="Narrow the pull, e.g. --filter name=ticket-classifier"),
):
    """Fetch recent LLM calls with an API key from the environment, ready for sundr import.

    Langfuse: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST (optional, e.g. https://us.cloud.langfuse.com).
    Filters: name, trace_name, prompt.

    Helicone: HELICONE_API_KEY (needs write access), HELICONE_HOST (optional, e.g. https://eu.api.helicone.ai).
    Filters: prompt_id, property.<key>.

    LangSmith: LANGSMITH_API_KEY, LANGSMITH_PROJECT, LANGSMITH_ENDPOINT and LANGSMITH_WORKSPACE_ID (optional).
    Filters: name, trace_name.

    Braintrust: BRAINTRUST_API_KEY, BRAINTRUST_PROJECT or BRAINTRUST_PROJECT_ID, BRAINTRUST_API_URL (optional).
    Filters: name.
    """
    from .connectors import pull as pulling

    load_dotenv(find_dotenv(usecwd=True))
    try:
        narrow = dict(pulling.parse_filter(f) for f in filters)
        credentials = pulling.credentials_from_env(source)
        typer.echo(f"Pulling up to {limit:,} LLM calls from the last {since_days:g} days of {source}...")
        rows = pulling.fetch(source, credentials, limit=limit, since_days=since_days, filters=narrow)
    except pulling.PullError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    pulling.write_rows(rows, out)
    typer.echo(f"{len(rows):,} calls -> {out}")
    typer.echo(f"Next: sundr import {source} {out}")


@app.command()
def lint(questions: Path):
    """Check a question bank against the decision-model rules."""
    kept, issues = lint_bank(load_bank(questions))
    _print_issues(issues)
    typer.echo(f"{len(kept)} questions pass")


@app.command()
def audit(
    task: Path,
    logs: Path,
    questions: Path,
    backend: list[str] = typer.Option(["jev"], help="Repeat for several backends, e.g. --backend jev --backend clef"),
    out: Path = typer.Option(Path("audit.html")),
    monthly_calls: Optional[int] = typer.Option(None, help="Judgment calls per month, for savings"),
    usd_per_call: Optional[float] = typer.Option(None, help="What one call costs today (else read from logs)"),
    epsilon: list[float] = typer.Option(list(DEFAULT_EPSILONS), help="Error budgets to certify"),
    limit: Optional[int] = typer.Option(None, help="Use only the first N logged calls"),
    cache: Path = typer.Option(Path(".sundr/cache.sqlite"), help="Answer cache"),
    max_usd: float = typer.Option(5.0, help="Stop if this audit would spend more"),
    candidate: list[str] = typer.Option([], help="A cheaper LLM to certify too (OpenRouter id, e.g. "
                                                 "anthropic/claude-haiku-5.5); repeat, or 'default'"),
    input_field: str = "input",
    verdict_field: str = "verdict",
    cost_field: str = "cost_usd",
):
    """Run a Savings Audit on logged calls and write an HTML report."""
    load_dotenv(find_dotenv(usecwd=True))
    spec = TaskSpec.load(task)
    records = read_records(logs, FieldMap(input=input_field, reference=verdict_field, cost_usd=cost_field), limit)
    if usd_per_call is None:
        costs = [r.cost_usd for r in records if r.cost_usd]
        usd_per_call = sum(costs) / len(costs) if costs else None
    cache.parent.mkdir(parents=True, exist_ok=True)
    backends = [make_backend(b) for b in backend]
    config = AuditConfig(epsilons=tuple(sorted(epsilon)), monthly_calls=monthly_calls, usd_per_call=usd_per_call)
    typer.echo(f"Auditing {len(records):,} calls with {', '.join(backend)}...")

    from .candidates import DEFAULT_CANDIDATES, CandidateClient
    models = [m for c in candidate for m in (DEFAULT_CANDIDATES if c == "default" else [c])]

    async def go():
        clients = [CandidateClient(m, cache_path=cache) for m in models]
        try:
            return await run_audit(spec, records, _with_verdicts(spec, load_bank(questions)), backends, config,
                                   str(cache), max_usd, candidates=clients)
        finally:
            for b in backends:
                await b.aclose()
            for c in clients:
                await c.aclose()

    result = asyncio.run(go())
    write_report(result, out)
    _summary(result, out)


@app.command()
def certify(
    logs: Path = typer.Argument(..., help="JSONL or CSV: the input each call judged and the reference verdict"),
    candidate: list[str] = typer.Option(..., help="Model to certify (OpenRouter id, e.g. openai/gpt-6-luna); repeat"),
    task: Optional[Path] = typer.Option(None, help="task.yaml; else labels come from the data"),
    decision: str = typer.Option("Reproduce the verdicts in these logs.", help="What the model decides (no task.yaml)"),
    system_prompt_file: Optional[Path] = typer.Option(None, help="Your own system prompt, instead of a generic one"),
    epsilon: list[float] = typer.Option([0.01, 0.02, 0.05], help="Error budgets to certify"),
    require_coverage: Optional[float] = typer.Option(None, help="Exit 1 unless the first candidate is certified "
                                                                "for at least this share at the smallest budget"),
    markdown: Optional[Path] = typer.Option(None, help="Write a Markdown summary (for a pull-request comment)"),
    json_out: Optional[Path] = typer.Option(None, "--json", help="Write the results as JSON"),
    max_usd: float = typer.Option(2.0, help="Stop if this would spend more"),
    limit: Optional[int] = typer.Option(None),
    input_field: str = "input",
    verdict_field: str = "verdict",
    cache: Path = typer.Option(Path(".sundr/cache.sqlite")),
):
    """Certify cheaper models (or a new prompt) against reference verdicts: for CI and quick checks."""
    import json as _json

    import numpy as np
    from sklearn.model_selection import train_test_split

    from .candidates import CandidateClient, evaluate
    from .spend import check

    load_dotenv(find_dotenv(usecwd=True))
    records = read_records(logs, FieldMap(input=input_field, reference=verdict_field), limit)
    if len(records) < 100:
        raise typer.BadParameter(f"{len(records)} usable rows; certifying needs at least 100 (more for tight budgets)")
    if task:
        spec = TaskSpec.load(task)
    else:
        from collections import Counter
        spec = TaskSpec("task", decision, [label for label, _ in Counter(r.reference for r in records).most_common()])
    system = system_prompt_file.read_text() if system_prompt_file else None
    y = np.array([r.reference for r in records], dtype=object)
    idx_train, idx_cal = train_test_split(np.arange(len(records)), train_size=0.4, random_state=0)
    epsilons = tuple(sorted(epsilon))
    cache.parent.mkdir(parents=True, exist_ok=True)

    async def go():
        from .candidates import estimate_usd
        results = []
        for model in candidate:
            check(estimate_usd(model, spec, records), max_usd)
            client = CandidateClient(model, cache_path=cache, system=system)
            try:
                answers = await client.answer_all(spec, records)
            finally:
                await client.aclose()
            results.append(evaluate(model, answers, y, spec.labels, idx_train, idx_cal, None, epsilons, 0.05,
                                    0.0, lambda row, cost: None))
        return results

    results = asyncio.run(go())
    lines = [f"### Sundr certification · {len(records):,} reference verdicts", "",
             "| Model | Agreement | Disagreement bound (95%) | " + " | ".join(f"Certified at {e:.0%}" for e in epsilons) + " |",
             "|---|---|---|" + "---|" * len(epsilons)]
    for c in results:
        lines.append(f"| `{c.model}` | {c.agreement:.1%} | ≤ {c.switch_all_bound:.2%} | "
                     + " | ".join(("yes" if r.coverage >= 0.999 else f"{r.coverage:.0%}") if r.certificate else "no"
                                  for r in c.rows) + " |")
    lines += ["", f"Certified means: on held-out calls, disagreement with the reference is at most the budget with "
                  f"95% confidence{' (own system prompt)' if system else ' (generic prompt from the task)'}. "
                  "[What this means](https://sundr.ai/docs#certificate)"]
    summary = "\n".join(lines)
    typer.echo(summary)
    if markdown:
        markdown.write_text(summary + "\n")
    if json_out:
        json_out.write_text(_json.dumps([{"model": c.model, "agreement": c.agreement,
                                          "disagreement_bound": c.switch_all_bound, "usd_per_call": c.usd_per_call,
                                          "certified": {str(r.epsilon): r.coverage for r in c.rows}}
                                         for c in results], indent=2))
    if require_coverage is not None:
        first = results[0].rows[0]
        if first.coverage < require_coverage:
            typer.echo(f"{results[0].model} is certified for {first.coverage:.0%} at {first.epsilon:.0%}; "
                       f"required {require_coverage:.0%}", err=True)
            raise typer.Exit(1)


def _dataset(path: Path, reference: Optional[Path], limit: Optional[int]):
    from .types import Record
    records = read_records(path, FieldMap(reference="label", gold="label"), limit)
    if reference:
        verdicts = {r.id: r.reference for r in read_records(reference, FieldMap(input="verdict", reference="verdict"))}
        records = [Record(r.id, r.input, verdicts[r.id], gold=r.gold) for r in records if r.id in verdicts]
    return records


@app.command()
def label(
    dataset: Path = typer.Argument(..., help="normalized.jsonl with id, input, label"),
    task: Path = typer.Argument(...),
    out: Path = typer.Option(Path("reference.jsonl")),
    model: str = typer.Option("claude-opus-5-5"),
    limit: Optional[int] = None,
    yes: bool = typer.Option(False, "--yes", help="Actually submit the batch (spends money)"),
):
    """Label a dataset with Claude (Batch API) to simulate a customer's LLM judge."""
    from . import label as labeling
    load_dotenv(find_dotenv(usecwd=True))
    spec, records = TaskSpec.load(task), _dataset(dataset, None, limit)
    cost = labeling.estimate_usd(records, spec, *spend.CLAUDE_PRICES.get(model, (5.0, 25.0)))
    typer.echo(f"{len(records):,} items with {model}: about ${cost:,.2f} at batch prices")
    if not yes:
        typer.echo("Dry run. Re-run with --yes to submit.")
        raise typer.Exit()
    spend.check(cost)
    batch_id = labeling.submit(records, spec, model)
    typer.echo(f"Submitted batch {batch_id}; waiting for results...")
    verdicts = labeling.collect(batch_id, out)
    spend.record("anthropic", cost, f"label batch {batch_id}")
    typer.echo(f"{len(verdicts):,} verdicts -> {out}")


@app.command()
def bench(
    dataset: Path = typer.Argument(..., help="normalized.jsonl with id, input, label"),
    task: Path = typer.Argument(...),
    questions: Path = typer.Argument(...),
    backend: list[str] = typer.Option(["jev"]),
    reference: Optional[Path] = typer.Option(None, help="LLM verdicts (agreement mode); default is gold labels"),
    splits: int = 100,
    limit: Optional[int] = None,
    out: Path = typer.Option(Path("out/bench.md")),
    cache: Path = typer.Option(Path(".sundr/cache.sqlite")),
    dry_run: bool = typer.Option(False, "--dry-run", help="Estimate tokens and cost, call nothing"),
    max_usd: float = typer.Option(2.0, help="Stop if this run would spend more (cached answers are free)"),
):
    """Benchmark the certifier: repeated splits, realised error vs certified budget."""
    import json
    import numpy as np
    from .bench import format_summary, run_bench
    from .features import FeatureRunner
    load_dotenv(find_dotenv(usecwd=True))
    spec = TaskSpec.load(task)
    bank = _with_verdicts(spec, load_bank(questions))
    records = _dataset(dataset, reference, limit)
    if dry_run:
        from .estimate import estimate_tokens
        tokens = estimate_tokens(records, bank)
        typer.echo(f"{dataset.parent.name}: {len(records):,} items, {len(bank)} questions, ~{tokens / 1e6:.1f}M input tokens per backend")
        for name, price in (("jev", 0.042), ("clef-flash", 0.09), ("clef", 0.24)):
            typer.echo(f"  {name:10} ~${tokens * price / 1e6:,.2f}")
        raise typer.Exit()
    cache.parent.mkdir(parents=True, exist_ok=True)
    backends = [make_backend(b) for b in backend]

    runner = FeatureRunner(backends, cache_path=str(cache), max_usd=max_usd)
    estimate = runner.uncached_usd(records, bank)
    typer.echo(f"Estimated new spend: ${estimate:.2f} (this month so far: ${spend.month_to_date():.2f})")
    spend.check(estimate, max_usd)

    async def features():
        try:
            return await runner.run(records, bank)
        finally:
            for b in backends:
                await b.aclose()

    typer.echo(f"Scoring {len(records):,} items x {len(bank)} questions on {', '.join(backend)}...")
    table = asyncio.run(features())
    y = np.array([r.reference for r in records], dtype=object)
    summary = run_bench(dataset.parent.name, table, y, spec.labels, backends[0].name, splits=splits)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(format_summary(summary) + "\n")
    out.with_suffix(".json").write_text(json.dumps(summary.to_dict(), indent=2))
    typer.echo(format_summary(summary))


@app.command(name="spend")
def show_spend():
    """Paid API spend recorded this month, against the monthly budget."""
    load_dotenv(find_dotenv(usecwd=True))
    typer.echo(f"${spend.month_to_date():.2f} of ${spend.monthly_cap():.2f} billable this month (ledger: {spend.ledger_path()})")
    used, included = spend.month_neurons(), spend.included_neurons()
    if used:
        typer.echo(f"Cloudflare neurons: {used:,.0f} of {included:,.0f} included ({used / included:.1%})")


@app.command()
def demo(out: Path = typer.Option(Path("demo-audit.html")), n: int = 30_000):
    """Run a full audit on synthetic phishing logs with simulated backends (no API keys)."""
    recs = demo_task.records(n=n)
    config = AuditConfig(monthly_calls=2_000_000, usd_per_call=0.004)
    result = asyncio.run(run_audit(demo_task.TASK, recs, demo_task.questions(), demo_task.backends(recs), config))
    write_report(result, out)
    _summary(result, out)


def _summary(result, out: Path) -> None:
    typer.echo(f"Agreement with your judge: {result.agreement_all:.1%}"
               + (f" (one question: {result.baseline_agreement_all:.1%})" if result.baseline_agreement_all is not None else ""))
    for row in result.rows:
        saving = f", net ${row.monthly_net_usd:,.0f}/month" if row.monthly_net_usd else ""
        typer.echo(f"  budget {row.epsilon:.1%}: {row.coverage:.0%} of calls certified{saving}")
    for c in getattr(result, "candidates", []):
        safe = "safe" if c.switch_all_bound is not None and c.switch_all_bound <= 0.01 else f"up to {c.switch_all_bound:.1%} off"
        typer.echo(f"{c.name}: agreement {c.agreement:.1%}, switch everything: {safe}, ${c.usd_per_call * 1000:.3f}/1k calls")
        for row in c.rows:
            saving = f", net ${row.monthly_net_usd:,.0f}/month" if row.monthly_net_usd else ""
            typer.echo(f"  budget {row.epsilon:.1%}: {row.coverage:.0%} certified ({row.strategy}){saving}")
    for model, reason in getattr(result, "candidate_notes", []):
        typer.echo(f"{model}: not evaluated ({reason})")
    typer.echo(f"Report -> {out}")


@app.command()
def mcp():
    """Run the Sundr MCP server on stdio, for Claude Code, Cursor and other agents (needs the mcp extra)."""
    from . import mcp_server
    try:
        server = mcp_server.build_server()
    except ImportError as exc:
        typer.echo(f"{exc}. Install the MCP extra: pip install 'sundr[mcp]'", err=True)
        raise typer.Exit(1)
    load_dotenv(find_dotenv(usecwd=True))
    server.run(transport="stdio")


# The hosted proxy, web app and server are not part of the open-source CLI; add their commands when present.
if importlib.util.find_spec("sundr.proxy") is not None:
    from .proxy.cli import register as _register_proxy_commands

    _register_proxy_commands(app)


if __name__ == "__main__":
    app()
