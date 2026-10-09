---
name: sundr-audit
description: Run a Sundr Savings Audit. Finds the LLM judgment calls in this codebase (classification, routing, moderation, LLM-as-judge, any structured output with a fixed label set), exports a sample of their logged calls, shows the user what would be used, then measures how many a cheap decision model could answer within a certified error budget, locally with the user's own keys or on sundr.ai after explicit approval. Use when the user wants to cut the cost of classification-style LLM calls, asks what share of their LLM traffic a smaller model could handle, or mentions Sundr.
---

# Sundr Savings Audit

You are helping the user find out how much of their LLM traffic Sundr could take over. Sundr answers repetitive judgment calls with small decision models, inside an error budget it certifies statistically on the user's own logs. The audit reports, for each error budget, the share of calls a decision model would answer in agreement with their current LLM, and what that would save.

The `sundr` MCP server bundled with this plugin provides the tools named below. If they aren't available, the same steps work with the `sundr` CLI (`pip install "git+https://github.com/sundr-ai/sundr-audit"`) and the guide at https://sundr.ai/setup.md.

## Ground rules

- Nothing leaves this machine without the user's clear yes. That covers uploads to sundr.ai and paid calls to decision-model APIs.
- Never print, log or upload API keys or other secrets. If a key is missing, ask the user to set it in their environment or a `.env` file. Never ask them to paste it into the chat.
- Don't change application code. Write the export file and any one-off export script in a scratch or temp directory, not in the repo.
- If the logs hold data the user may not share with third parties (health records, payment card data, anything under a contract that forbids it), stop and say so.

## Workflow

1. **Get the guide.** Call `sundr_setup_guide` and follow it. It explains how to recognise judgment calls in code and how to write the export.
2. **Find the judgment calls.** Search for LLM calls whose output is one of a fixed set of labels: an `enum` in a JSON schema or tool schema, prompts like "respond with one of" or "classify", results that feed an `if` or `match`. List each with file and line, what it decides, the labels, the model and a volume guess. Ask which one to audit.
3. **Find the logs and export them.** Use the platform the calls are logged to (Langfuse, Helicone, LangSmith, Braintrust), the user's own database or log files, or saved request and response pairs. `sundr pull <platform>` followed by `sundr import <platform>` fetches and converts platform logs with keys already in the environment. Write `sundr-export.jsonl` with one `{"id", "input", "verdict"}` object per line (optionally `cost_usd` and a human `gold` label).
4. **Fill in the context.** One sentence on what the LLM decides, roughly how many calls a month, and what one call costs today.
5. **Show the user and let them choose.** Show the file path, row count, size, label distribution, three example rows (truncated), the decision sentence, volume and cost. Then offer two ways to run it:
   - **Local** (`sundr_local_audit`): the data stays on this machine apart from the decision-model APIs the user has keys for (Jev through OpenRouter or TypeSafe, Clef on Cloudflare Workers AI, or a local Ollama). It spends a little on those APIs, capped by `max_usd` (default $2). Say the cap before running.
   - **Hosted** (`sundr_hosted_audit`): free. The file is uploaded to sundr.ai and the report arrives by email.
6. **Run it.**
   - Local: call `sundr_local_audit` with `logs_path`, `decision`, `monthly_calls`, `usd_per_call` and `max_usd`. If it refuses because no backend key is configured, pass its `how_to_fix` on to the user. If it refuses over the spend cap, offer the smaller `limit` it suggests or ask before raising `max_usd`. `draft=true` drafts extra questions with Claude (needs `ANTHROPIC_API_KEY`) and usually raises coverage.
   - Hosted: call `sundr_hosted_audit` with `confirm=false` and show the user the summary it returns, including the email address the report goes to. Ask "Upload this to sundr.ai for a free audit?" and call again with `confirm=true` only after a clear yes.
7. **Report back.** Local: give the headline (coverage at the 1% budget, or the tightest one that certified, and the monthly saving) and the path to the HTML report. Hosted: tell the user to click the confirmation link Sundr emails them; the audit starts then and the report link follows by email. Suggest deleting the local export once they no longer need it.

## Operating the proxy

If the user already routes traffic through the Sundr proxy and `SUNDR_KEY` is set, `sundr_list_tasks` shows each task's state, certificate and live error, and `sundr_set_task_state` moves a task between `shadow`, `takeover` and `passthrough`. Changing a state changes who answers production traffic: confirm the task and the new state with the user first.
