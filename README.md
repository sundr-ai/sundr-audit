# Sundr audit

<!-- mcp-name: io.github.sundr-ai/sundr -->

Many LLM calls are judgment calls: classify this ticket, is this email phishing, which queue does this go to, does this answer pass the rubric. The model returns one of a fixed set of labels, and you pay frontier-model prices for each one.

`sundr` measures how many of those calls a cheaper model could answer instead, without guessing: a smaller LLM such as Claude Haiku 5.5 or GPT-6 Luna, or a decision model such as TypeSafe's Jev or Cloudflare's Clef. Point it at a week of logged calls and it reports, for each candidate and each error budget (0.5%, 1%, 2%, 5%), the share of calls it would answer in agreement with your current model, certified with 95% confidence on held-out calls, and what that would save each month.

It runs on your machine. Your logs go only to the model APIs you configure with your own keys.

## Quick start

```bash
pip install "git+https://github.com/sundr-ai/sundr-audit"
sundr demo            # synthetic phishing logs and simulated decision models: no keys, no network
open demo-audit.html
```

Python 3.11 or newer.

## Audit your own logs

`logs.jsonl` has one call per line, with the text your LLM judged and the label it returned:

```json
{"id": "call_8f2a", "input": "Hi, I was charged twice for my March invoice…", "verdict": "billing", "cost_usd": 0.004}
```

`cost_usd` is optional. So is `gold`, a human label, if you have some: the report then compares your LLM and the decision model with ground truth. Use `--input-field` and `--verdict-field` for other field names; dotted paths reach into nested JSON. CSV works too.

`task.yaml` says what is being decided:

```yaml
name: ticket-router
description: Route a support ticket to the team that should handle it.
labels: [billing, bug, how-to, other]
label_descriptions:
  billing: charges, refunds, invoices, plans
  bug: something in the product is broken
```

Then:

```bash
export OPENROUTER_API_KEY=… CLOUDFLARE_ACCOUNT_ID=… CLOUDFLARE_AUTH_TOKEN=…   # or put them in .env
sundr draft task.yaml logs.jsonl --out questions.yaml       # optional: Claude drafts narrow questions (ANTHROPIC_API_KEY)
sundr audit task.yaml logs.jsonl questions.yaml \
  --backend openrouter-jev --backend clef-flash \
  --monthly-calls 2000000 --max-usd 5 --out audit.html
```

Add `--candidate default` (or `--candidate anthropic/claude-haiku-5.5 --candidate openai/gpt-6-luna`, any OpenRouter id) to certify cheaper LLMs on the same calls. Each is certified alone ("switch everything") and gated by the decision models (it answers only the calls it's certified on); the report leads with whichever saves most.

`sundr audit` estimates the cost before it calls anything and stops if a run would spend more than `--max-usd` (default $5) or push this month past `SUNDR_MONTHLY_BUDGET_USD` (default $25). Spend is recorded in `.sundr/spend.jsonl`; `sundr spend` shows the month so far. Answers are cached in `.sundr/cache.sqlite`, so re-running an audit is free. Without `sundr draft`, write `questions.yaml` by hand or start from one in `bench/`.

Certifying a budget reliably takes roughly 120 ÷ budget held-out calls: about 12,000 for 1%. The audit holds out 40% of the logs, so 30,000 logged calls certify a 1% budget comfortably.

## Certify a model or prompt change in CI

`sundr certify` checks a candidate model, optionally with your own system prompt, against reference verdicts and writes a Markdown summary for a pull-request comment:

```bash
sundr certify evals/ticket-router.jsonl --candidate openai/gpt-6-luna \
  --system-prompt-file prompts/ticket-router.txt --epsilon 0.01 --epsilon 0.02 \
  --markdown summary.md --require-coverage 0.9
```

The GitHub Action in `integrations/github-action` runs it on every pull request and comments the result:

```yaml
- uses: sundr-ai/sundr-audit/integrations/github-action@v0.1.0
  with:
    evals: evals/ticket-router.jsonl
    candidate: openai/gpt-6-luna
    system-prompt-file: prompts/ticket-router.txt
    openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

## Pull logs from your observability tool

Logs in Langfuse, Helicone, LangSmith or Braintrust can be fetched with an API key instead of exported by hand:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-… LANGFUSE_SECRET_KEY=sk-lf-… sundr pull langfuse --out calls.jsonl --since-days 7
sundr import langfuse calls.jsonl     # one logs.jsonl + task.yaml per judgment task, under imported/
```

`sundr import` finds the calls that returned structured output with a fixed label set, groups them into tasks, and writes audit-ready files. It also reads raw OpenAI and Anthropic request/response pairs (`sundr import raw export.jsonl`). `sundr pull --help` lists each tool's environment variables and filters, e.g. `--filter name=ticket-classifier`. Helicone's query API only accepts keys with write access.

## Decision models (bring your own keys)

| `--backend` | Needs |
| --- | --- |
| `openrouter-jev` | `OPENROUTER_API_KEY` (TypeSafe Jev through OpenRouter) |
| `jev`, `jev-1.13.0` | `TYPESAFE_API_KEY` |
| `clef`, `clef-flash` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AUTH_TOKEN` (Workers AI) |
| `ollama:<model>` | Ollama 0.35 or newer running locally (`OLLAMA_HOST`) |
| `laya`, `strands`, `url:<base>#<model>` | a local System One server |

Question drafting (`sundr draft`, optional) uses Claude with `ANTHROPIC_API_KEY`. Keys are read from the environment or a `.env` file in the working directory, and are never written anywhere by `sundr`.

## Privacy

`sundr` runs locally. Logged text goes only to the model APIs you configure: the backends you pass with `--backend`, OpenRouter for any `--candidate`, and, if you draft questions, Anthropic. Nothing is sent to Sundr. With `ollama:<model>` or another local server, nothing leaves your machine at all.

## How it works

1. **Decompose.** Claude drafts 30–60 narrow questions per task; a linter removes patterns decision models handle badly (negation, "unsure" options, counting, dates).
2. **Ask.** Every question goes to every backend, choice questions in two option orders.
3. **Combine.** A logistic-regression combiner turns the answers into one verdict and a score.
4. **Certify.** Learn then Test picks the loosest score threshold whose disagreement with your LLM on held-out calls is at most ε with 95% confidence (`sundr/certify.py`).
5. **Monitor.** In production, an audit slice keeps checking live error with active statistical inference (`sundr/monitor.py`).

The guarantee is about agreement with your LLM, on traffic that resembles the logs. `docs/research/` has the notes behind the backends and log formats, and `bench/` the public benchmarks' task definitions and question banks (`sundr bench`, data via `scripts/`).

## Use it from your coding agent

`sundr mcp` serves an [MCP](https://modelcontextprotocol.io) server on stdio with five tools: `sundr_setup_guide` (how to find the judgment calls in a repo and export their logs), `sundr_local_audit` (the audit above, in-process, capped by `max_usd`), `sundr_hosted_audit` (a free audit on sundr.ai; it only summarises the file until the user approves the upload), and `sundr_list_tasks` / `sundr_set_task_state` for the hosted proxy.

**Claude Code plugin** (the MCP server plus a skill that walks through the audit):

```
/plugin marketplace add sundr-ai/sundr-audit
/plugin install sundr@sundr
```

**Any MCP client** (Claude Code, Cursor, Windsurf, Claude Desktop…), with [uv](https://docs.astral.sh/uv/) installed:

```json
{
  "mcpServers": {
    "sundr": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/sundr-ai/sundr-audit", "--with", "mcp>=1.0", "sundr", "mcp"]
    }
  }
}
```

or `claude mcp add sundr -- uvx --from git+https://github.com/sundr-ai/sundr-audit --with "mcp>=1.0" sundr mcp`. With pip: `pip install "sundr[mcp] @ git+https://github.com/sundr-ai/sundr-audit"`, then use `sundr mcp` as the command. The server reads keys from its environment or a `.env` file in its working directory.

## Hosted

[sundr.ai](https://sundr.ai) runs the other half: a drop-in OpenAI- and Anthropic-compatible proxy that learns each judgment call in shadow mode, trials a cheaper model on your own requests, certifies it on your live traffic, answers the certified share with the cheaper model or decision models, signs the certificate, and keeps auditing itself, handing traffic back to your LLM if the error budget slips. There's also a free [hosted audit](https://sundr.ai/setup) if you'd rather not run this yourself. See the [docs](https://sundr.ai/docs).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The certifier tests simulate hundreds of audits and check the error guarantee holds in at least 95% of them. No test calls a real API.

## License

MIT
