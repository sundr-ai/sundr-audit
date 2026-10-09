# Sundr plugin for Claude Code

Finds the LLM judgment calls in your codebase (classification, routing, moderation, LLM-as-judge: structured output with a fixed label set), exports a sample of their logged calls, and measures how many a cheap decision model could answer within a certified error budget.

It adds:

- the **`sundr-audit` skill**, which walks Claude through the audit: find the calls, export the logs, show you exactly what would be used, then run it locally or upload it to sundr.ai once you say yes;
- the **`sundr` MCP server** (`sundr mcp`), with the tools `sundr_setup_guide`, `sundr_local_audit`, `sundr_hosted_audit`, `sundr_list_tasks` and `sundr_set_task_state`.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and git on your `PATH`; the MCP server runs with `uvx` straight from this repository.

In Claude Code:

```
/plugin marketplace add sundr-ai/sundr-audit
/plugin install sundr@sundr
```

Or from a shell:

```bash
claude plugin marketplace add sundr-ai/sundr-audit
claude plugin install sundr@sundr
```

Then ask Claude something like "Run a Sundr audit on this repo", or invoke the skill directly with `/sundr:sundr-audit`. Update with `claude plugin update sundr@sundr`.

## Keys

The local audit calls decision models with your own keys. Set one or more in your shell before starting Claude Code, or in a `.env` file in the project:

| Backend | Environment |
| --- | --- |
| Jev through OpenRouter | `OPENROUTER_API_KEY` |
| Jev direct | `TYPESAFE_API_KEY` |
| Clef-flash on Cloudflare Workers AI | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AUTH_TOKEN` |
| Ollama 0.35+ (local, no key) | pass `backends=["ollama:<model>"]` |
| Question drafting (optional) | `ANTHROPIC_API_KEY` |
| Proxy control tools | `SUNDR_KEY` (a Sundr workspace key) |

No keys? The hosted audit on sundr.ai is free and needs none; Claude shows you a summary of the file and uploads it only after you approve.

## Privacy

The local audit runs on your machine; logged text goes only to the decision-model APIs you configure (and to Anthropic if you turn on question drafting). The hosted audit uploads the export to sundr.ai, only after you approve it; see https://sundr.ai/privacy.

## Without the plugin

Add just the MCP server:

```bash
claude mcp add sundr -- uvx --from git+https://github.com/sundr-ai/sundr-audit --with "mcp>=1.0" sundr mcp
```
