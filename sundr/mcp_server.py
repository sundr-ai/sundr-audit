"""MCP server: lets a coding agent set up a Savings Audit, run one locally, or operate the Sundr proxy.

    sundr mcp        # stdio transport; needs pip install 'sundr[mcp]'

Every tool is a plain function here, so it can be tested and reused without an MCP client;
`build_server` registers them with the MCP Python SDK (MCPServer in mcp 2.x, FastMCP in 1.x).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import httpx
from pydantic import Field

from . import __version__, spend
from .audit import AuditConfig, run_audit
from .backends.base import BackendError
from .backends.systemone import available_backends, make_backend
from .client import PROXY_URL, Control, SundrError
from .compiler import DRAFT_MAX_USD, TaskSpec, draft_questions, example_records, lint_bank, load_bank, verdict_questions
from .estimate import estimate_audit_tokens
from .ingest import FieldMap, read_records
from .report import write_report

SITE_URL = "https://sundr.ai"
HOSTED_MIN_ROWS = 300  # the hosted audit refuses smaller files
HOSTED_MAX_BYTES = 200 * 1024 * 1024
LOCAL_MIN_ROWS = 100
SOURCES = ("canonical", "langfuse", "helicone", "langsmith", "braintrust", "raw")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Tests swap in an httpx.MockTransport; None means real network.
_TRANSPORT: httpx.BaseTransport | None = None

NO_BACKEND_HELP = (
    "No decision-model backend is configured, so nothing was run or spent. Ask the user to set at least one of "
    "these in the environment the MCP server runs in (or in a .env file in the project directory), then restart "
    "the server: OPENROUTER_API_KEY (Jev through OpenRouter) or TYPESAFE_API_KEY (Jev direct); "
    "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AUTH_TOKEN (Clef-flash on Workers AI). A local System One server needs "
    "no key: pass backends=['ollama:<model>'] with Ollama 0.35+ running. Never ask the user to paste a key into "
    "the chat. To see a report without keys, run `sundr demo`; to have sundr.ai run the audit instead, use "
    "sundr_hosted_audit."
)

MCP_NOTE = """

---

## With the Sundr MCP tools

- Step 6: call `sundr_hosted_audit` with `confirm=false` first. Show the user the summary it returns, ask, and call it again with `confirm=true` only after a clear yes. Don't use curl.
- To keep the data on this machine, call `sundr_local_audit` instead. It runs the same audit locally with the user's own decision-model keys and writes an HTML report next to the export. Only the decision-model APIs the user configured see the text.
- With a workspace key in `SUNDR_KEY`, `sundr_list_tasks` and `sundr_set_task_state` operate the proxy.
"""


def _site() -> str:
    return os.environ.get("SUNDR_SITE_URL", SITE_URL).rstrip("/")


def _http(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=_TRANSPORT, follow_redirects=True)


def _refuse(error: str, **extra: Any) -> dict[str, Any]:
    return {"status": "refused", "error": error, **extra}


# ---------------------------------------------------------------------------------------------- setup guide

def bundled_setup_guide() -> str:
    """The copy of https://sundr.ai/setup.md that ships with this package."""
    from jinja2 import Environment

    here = Path(__file__).parent
    for path in (here / "setup.md", here / "web" / "templates" / "setup.md"):
        if path.exists():
            text = Environment(keep_trailing_newline=True).from_string(path.read_text()).render(min_rows=f"{HOSTED_MIN_ROWS:,}")
            return f"<!-- Copy bundled with sundr {__version__}; {_site()}/setup.md may be newer. -->\n{text}"
    raise FileNotFoundError("setup.md is missing from this installation")


def sundr_setup_guide() -> str:
    try:
        with _http(5) as http:
            response = http.get(f"{_site()}/setup.md", headers={"Accept": "text/markdown"})
        text = response.text if response.is_success else ""
    except httpx.HTTPError:
        text = ""
    if not text.lstrip().startswith("#"):
        text = bundled_setup_guide()
    return text.rstrip() + "\n" + MCP_NOTE


# ---------------------------------------------------------------------------------------------- local audit

def _spec(logs: Path, records, task_yaml_path: str | None, decision: str | None, labels: list[str] | None) -> TaskSpec:
    if task_yaml_path:
        return TaskSpec.load(task_yaml_path)
    if not decision:
        raise ValueError("Pass task_yaml_path, or decision (one sentence on what the LLM decides) with optional labels")
    found = [label for label, _ in Counter(r.reference for r in records).most_common(255)]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", logs.stem).strip("-") or "task"
    return TaskSpec(name=name, description=decision, labels=[str(x) for x in labels] if labels else found)


def _budget_rows(result) -> list[dict[str, Any]]:
    return [
        {
            "error_budget": row.epsilon,
            "coverage": round(row.coverage, 4),
            "certified_bound": round(row.certificate.upper_bound, 5) if row.certificate else None,
            "one_question_coverage": round(row.baseline_coverage, 4),
            "monthly_net_usd": round(row.monthly_net_usd, 2) if row.monthly_net_usd is not None else None,
        }
        for row in result.rows
    ]


def _headline(result) -> str:
    best = result.best_row()
    if not best:
        return "No error budget could be certified on these logs (usually too few calls, or labels the decision models can't separate)."
    saving = f", about ${best.monthly_net_usd:,.0f} a month net of fees" if best.monthly_net_usd else ""
    return (f"At a {best.epsilon:.1%} error budget a decision model could answer {best.coverage:.0%} of these calls"
            f"{saving}, certified with 95% confidence on {result.n_calibration:,} held-out calls.")


async def sundr_local_audit(
    logs_path: Annotated[str, Field(description="JSONL or CSV of logged calls: one call per row with the judged text and the label your LLM returned.")],
    task_yaml_path: Annotated[Optional[str], Field(description="task.yaml with name, description, labels and optional label_descriptions. Give this or decision.")] = None,
    decision: Annotated[Optional[str], Field(description="One sentence on what the LLM decides, used when there is no task.yaml.")] = None,
    labels: Annotated[Optional[list[str]], Field(description="The label set, when there is no task.yaml. Default: the verdicts seen in the logs.")] = None,
    backends: Annotated[Optional[list[str]], Field(description="Decision-model backends, e.g. ['openrouter-jev', 'clef-flash'] or ['ollama:<model>']. Default: SUNDR_BACKENDS, else every hosted backend whose key is set.")] = None,
    questions_path: Annotated[Optional[str], Field(description="A question bank YAML from `sundr draft`. Default: only the direct-verdict questions.")] = None,
    draft: Annotated[bool, Field(description="Draft ~40 extra questions with Claude first (needs ANTHROPIC_API_KEY; up to $0.50 of max_usd). Usually raises coverage.")] = False,
    monthly_calls: Annotated[Optional[int], Field(description="Roughly how many of these calls happen per month, for the savings estimate.")] = None,
    usd_per_call: Annotated[Optional[float], Field(description="What one call costs today. Default: the mean cost_usd in the logs.")] = None,
    max_usd: Annotated[float, Field(description="Hard cap on API spend for this run, in US dollars. The run is refused if the estimate is higher.")] = 2.0,
    limit: Annotated[Optional[int], Field(description="Use only the first N logged calls.")] = None,
    input_field: Annotated[str, Field(description="Field holding the judged text (dotted paths reach into nested JSON).")] = "input",
    verdict_field: Annotated[str, Field(description="Field holding the label your LLM returned.")] = "verdict",
    out_path: Annotated[Optional[str], Field(description="Where to write the HTML report. Default: sundr-audit.html next to the logs.")] = None,
) -> dict[str, Any]:
    logs = Path(logs_path).expanduser()
    if not logs.is_file():
        return _refuse(f"No file at {logs}")
    try:
        records = read_records(logs, FieldMap(input=input_field, reference=verdict_field), limit)
        spec = _spec(logs, records, task_yaml_path, decision, labels)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _refuse(f"Couldn't read the inputs: {str(exc)[:300]}")
    if len(records) < LOCAL_MIN_ROWS:
        return _refuse(f"Found {len(records):,} usable calls in {logs.name} (rows with both {input_field!r} and "
                       f"{verdict_field!r}); a local audit needs at least {LOCAL_MIN_ROWS}, and about 12,000 to certify a 1% budget.")
    if len(set(r.reference for r in records)) < 2:
        return _refuse("Every logged verdict is the same label; there is nothing to audit.")

    names = backends or available_backends()
    if not names:
        return _refuse("No decision-model backend configured.", how_to_fix=NO_BACKEND_HELP)
    if draft and not os.environ.get("ANTHROPIC_API_KEY"):
        return _refuse("draft=true needs ANTHROPIC_API_KEY in the server's environment. Run without draft, or ask "
                       "the user to set the key (not in the chat).")
    budget = max_usd - (DRAFT_MAX_USD if draft else 0.0)
    if budget <= 0:
        return _refuse(f"max_usd=${max_usd:.2f} doesn't cover drafting (up to ${DRAFT_MAX_USD:.2f}) plus the audit.")

    built = []
    try:
        for name in names:
            built.append(make_backend(name))
    except BackendError as exc:
        for b in built:
            await b.aclose()
        return _refuse(str(exc), how_to_fix=NO_BACKEND_HELP)

    before = spend.month_to_date()  # drafting and the audit both record to the ledger
    try:
        if questions_path:
            bank = load_bank(questions_path)
            have = {q.name for q in bank}
            bank = [q for q in verdict_questions(spec) if q.name not in have] + bank
        elif draft:
            try:
                drafted = await asyncio.to_thread(draft_questions, spec, example_records(records), 40)
            except spend.BudgetExceeded:
                raise
            except Exception as exc:  # anthropic errors, refusals: report them rather than crash the tool
                return {"status": "error", "error": f"Question drafting failed: {str(exc)[:300]}"}
            bank, _ = lint_bank(verdict_questions(spec) + drafted)
        else:
            bank = verdict_questions(spec)

        price = sum(b.usd_per_million_input_tokens for b in built)
        estimate = estimate_audit_tokens(records, bank) * price / 1e6
        try:
            spend.check(estimate, budget)
        except spend.BudgetExceeded as exc:
            fits = int(len(records) * budget / estimate) if estimate else len(records)
            return _refuse(str(exc), estimated_usd=round(estimate, 4),
                           how_to_fix=f"Pass limit={max(fits, LOCAL_MIN_ROWS)} to audit fewer calls, or a higher max_usd "
                                      "once the user approves the spend. Cached answers from earlier runs are free.")

        if usd_per_call is None:
            costs = [r.cost_usd for r in records if r.cost_usd]
            usd_per_call = sum(costs) / len(costs) if costs else None
        cache = Path(".sundr/cache.sqlite")
        cache.parent.mkdir(parents=True, exist_ok=True)
        config = AuditConfig(monthly_calls=monthly_calls, usd_per_call=usd_per_call)
        result = await run_audit(spec, records, bank, built, config, str(cache), budget)
    except spend.BudgetExceeded as exc:
        return _refuse(str(exc))
    except (BackendError, OSError, ValueError, KeyError) as exc:
        return {"status": "error", "error": str(exc)[:500]}
    finally:
        for b in built:
            await b.aclose()

    out = Path(out_path).expanduser() if out_path else logs.with_name("sundr-audit.html")
    write_report(result, out)
    return {
        "status": "done",
        "task": spec.name,
        "headline": _headline(result),
        "calls_audited": result.n_records,
        "held_out_calls": result.n_calibration,
        "backends": result.backends,
        "agreement_with_your_llm": round(result.agreement_all, 4),
        "one_question_agreement": None if result.baseline_agreement_all is None else round(result.baseline_agreement_all, 4),
        "budgets": _budget_rows(result),
        "questions_kept": [q.name for q in result.questions_kept],
        "spent_usd": round(spend.month_to_date() - before, 4),
        "report_html": str(out.resolve()),
        "report_json": str(out.with_suffix(".json").resolve()),
        "next": "Show the user the headline and open the HTML report. To run this in production with live "
                f"monitoring, see {_site()}/docs.",
    }


# ---------------------------------------------------------------------------------------------- hosted audit

def _file_summary(path: Path, source: str, input_field: str, verdict_field: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"file": str(path.resolve()), "bytes": path.stat().st_size,
                               "size": f"{path.stat().st_size / 1e6:,.1f} MB", "source": source}
    if source == "canonical":
        records = read_records(path, FieldMap(input=input_field, reference=verdict_field))
        summary["task"] = None
    else:
        from .connectors.load import group_tasks, load_export, to_records

        groups = group_tasks(load_export(path, source))
        tasks = {name: len(items) for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))}
        summary["tasks_found"] = tasks
        summary["task"] = next(iter(tasks), None)
        records = to_records(groups[summary["task"]]) if tasks else []
    counts = Counter(r.reference for r in records)
    summary.update(
        usable_rows=len(records),
        labels=len(counts),
        top_labels=dict(counts.most_common(10)),
        examples=[{"id": r.id, "input": r.input[:200] + ("…" if len(r.input) > 200 else ""), "verdict": r.reference}
                  for r in records[:3]],
        has_cost_usd=any(r.cost_usd for r in records),
        has_gold_labels=any(r.gold is not None for r in records),
    )
    return summary


def sundr_hosted_audit(
    file_path: Annotated[str, Field(description="The export to upload, usually sundr-export.jsonl (one call per line: id, input, verdict, optional cost_usd and gold).")],
    email: Annotated[str, Field(description="The user's work email. Sundr emails a confirmation link there, and the report link once it's done.")],
    confirm: Annotated[bool, Field(description="false: only summarise the file (nothing is sent). true: upload it. Set true only after the user has seen the summary and clearly said yes.")] = False,
    decision: Annotated[Optional[str], Field(description="One sentence on what the LLM decides, e.g. 'Route support tickets to billing, bug, how-to or other.'")] = None,
    monthly_calls: Annotated[Optional[int], Field(description="Roughly how many of these calls happen per month.")] = None,
    usd_per_call: Annotated[Optional[float], Field(description="What one call costs today, in US dollars.")] = None,
    source: Annotated[Literal["canonical", "langfuse", "helicone", "langsmith", "braintrust", "raw"], Field(description="'canonical' for the id/input/verdict JSONL from the setup guide, or the platform whose unchanged export this is.")] = "canonical",
    input_field: Annotated[str, Field(description="For canonical files: field holding the judged text.")] = "input",
    verdict_field: Annotated[str, Field(description="For canonical files: field holding the LLM's label.")] = "verdict",
) -> dict[str, Any]:
    path = Path(file_path).expanduser()
    if not path.is_file():
        return _refuse(f"No file at {path}")
    if source not in SOURCES:
        return _refuse(f"source must be one of {', '.join(SOURCES)}")
    email = email.strip().lower()
    if not EMAIL.match(email):
        return _refuse("Ask the user for a valid work email address.")
    if path.stat().st_size > HOSTED_MAX_BYTES:
        return _refuse(f"{path.name} is over the 200 MB upload limit; keep the most recent calls, up to 100,000 rows.")
    try:
        summary = _file_summary(path, source, input_field, verdict_field)
    except Exception as exc:  # a malformed export: tell the agent what to fix
        return _refuse(f"Couldn't read {path.name}: {str(exc)[:300]}")
    summary.update(decision=decision, monthly_calls=monthly_calls, usd_per_call=usd_per_call, report_email=email)
    if summary["usable_rows"] < HOSTED_MIN_ROWS:
        return _refuse(f"Found {summary['usable_rows']:,} usable calls; the hosted audit needs at least {HOSTED_MIN_ROWS}.",
                       upload=summary)
    destination = f"{_site()}/audits"
    if not confirm:
        return {
            "status": "needs_approval",
            "upload": summary,
            "destination": destination,
            "ask_user": f"Upload {path.name} ({summary['usable_rows']:,} calls, {summary['size']}) to sundr.ai for a free audit?",
            "next": "Show the user this summary: file, size, row count, label distribution, the example rows, the "
                    "decision, volume, cost and the email. Call again with confirm=true only after a clear yes. "
                    "To keep the data on this machine instead, use sundr_local_audit.",
        }

    fields = {"email": email, "source": source, "input_field": input_field, "verdict_field": verdict_field,
              "decision": decision or "", "monthly_calls": "" if monthly_calls is None else str(monthly_calls),
              "usd_per_call": "" if usd_per_call is None else str(usd_per_call)}
    content_type = "text/csv" if path.suffix.lower() == ".csv" else "application/json"
    try:
        with _http(300) as http, path.open("rb") as fh:
            response = http.post(destination, data=fields, files={"file": (path.name, fh, content_type)},
                                 headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        return {"status": "error", "error": f"Upload failed: {exc!r}"}
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text.strip()[:300]}
    if response.status_code == 202:
        return {"status": "uploaded", "audit": body,
                "tell_user": "Sundr emailed a confirmation link; the audit starts when they click it, and the report "
                             "link arrives by email. The upload is deleted once the report is written. They can "
                             "delete the local export when they no longer need it."}
    return {"status": "error", "http_status": response.status_code, "error": body.get("error") or str(body)[:300]}


# ---------------------------------------------------------------------------------------------- proxy control

NO_KEY_HELP = ("SUNDR_KEY isn't set in the MCP server's environment. It holds a Sundr workspace key "
               "(hello@sundr.ai gives one out during the design-partner preview). Ask the user to set it and "
               "restart the server; never ask them to paste it into the chat.")


def _control_call(fn) -> dict[str, Any]:
    if not os.environ.get("SUNDR_KEY"):
        return _refuse("No Sundr workspace key.", how_to_fix=NO_KEY_HELP)
    try:
        with _http(30) as http:
            return fn(Control(base_url=os.environ.get("SUNDR_PROXY_URL", PROXY_URL), http_client=http))
    except SundrError as exc:
        return {"status": "error", "http_status": exc.status_code, "error": exc.detail}
    except httpx.HTTPError as exc:
        return {"status": "error", "error": f"Couldn't reach the Sundr proxy: {exc!r}"}


def sundr_list_tasks() -> dict[str, Any]:
    return _control_call(lambda control: {"status": "ok", "tasks": control.tasks()})


def sundr_set_task_state(
    name: Annotated[str, Field(description="The task name, as sent in the X-Sundr-Task header.")],
    state: Annotated[Literal["shadow", "takeover", "passthrough"], Field(description="shadow: log only; takeover: Sundr answers calls above the certified threshold (needs a certificate); passthrough: everything goes to the LLM.")],
) -> dict[str, Any]:
    return _control_call(lambda control: {"status": "ok", **control.set_state(name, state)})


# ---------------------------------------------------------------------------------------------- server

INSTRUCTIONS = (
    "Sundr measures how many of a team's LLM judgment calls (classification, routing, moderation, LLM-as-judge: "
    "structured output with a fixed label set) a cheap decision model can answer inside a certified error budget. "
    "Start with sundr_setup_guide: it explains how to find the judgment calls in a codebase and export a sample of "
    "logged calls. Then either run sundr_local_audit (data stays on this machine apart from the decision-model "
    "APIs the user configured) or sundr_hosted_audit (uploads to sundr.ai, only after the user approves)."
)

TOOLS = {
    "sundr_setup_guide": (
        sundr_setup_guide,
        "Step-by-step instructions for setting up a Sundr Savings Audit in this codebase: find LLM judgment calls "
        "(structured output with a fixed label set), locate their logs, write sundr-export.jsonl, and show the user "
        "before anything leaves the machine. Call this first, then follow it.",
        {"title": "Sundr setup guide", "readOnlyHint": True, "openWorldHint": True},
    ),
    "sundr_local_audit": (
        sundr_local_audit,
        "Run the Savings Audit on this machine: certifies, per error budget, the share of logged calls a decision "
        "model answers in agreement with the user's LLM, and writes an HTML report. Logged text goes only to the "
        "decision-model APIs configured with the user's own keys (Jev via OpenRouter or TypeSafe, Clef on Cloudflare, "
        "or a local Ollama), never to sundr.ai. Spends real money on those APIs, capped by max_usd (default $2); "
        "tell the user the cap before calling. Refuses with setup instructions when no backend is configured. Takes "
        "minutes on large files.",
        {"title": "Run a local Savings Audit", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    ),
    "sundr_hosted_audit": (
        sundr_hosted_audit,
        "Upload an export to sundr.ai for a free hosted Savings Audit; the report arrives by email. ALWAYS call with "
        "confirm=false first: that sends nothing and returns a summary (file, size, rows, label distribution, "
        "examples). Show the user that summary and the email address, ask whether to upload, and call again with "
        "confirm=true only after an explicit yes. Never set confirm=true on your own judgement.",
        {"title": "Upload for a hosted Savings Audit", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    ),
    "sundr_list_tasks": (
        sundr_list_tasks,
        "List the tasks in the user's Sundr proxy workspace: name, state (shadow, takeover, passthrough), error "
        "budget, call count, certificate and live error. Needs SUNDR_KEY in the server's environment.",
        {"title": "List Sundr proxy tasks", "readOnlyHint": True, "openWorldHint": True},
    ),
    "sundr_set_task_state": (
        sundr_set_task_state,
        "Move a Sundr proxy task to shadow, takeover or passthrough. This changes who answers the user's production "
        "traffic: confirm the task and the new state with the user before calling. takeover needs a certificate; "
        "passthrough sends everything back to the LLM at once. Needs SUNDR_KEY.",
        {"title": "Change a Sundr task's state", "readOnlyHint": False, "destructiveHint": True,
         "idempotentHint": True, "openWorldHint": True},
    ),
}


def build_server():
    """The MCP server with every tool registered. Raises ImportError without the `mcp` package."""
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
    from mcp.types import ToolAnnotations

    server = Server("sundr", instructions=INSTRUCTIONS)
    for name, (fn, description, hints) in TOOLS.items():
        server.add_tool(fn, name=name, description=description, annotations=ToolAnnotations.model_validate(hints))
    return server


def main() -> None:
    """Serve on stdio. Keys come from the environment or a .env file in the working directory."""
    from dotenv import find_dotenv, load_dotenv

    server = build_server()
    load_dotenv(find_dotenv(usecwd=True))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
