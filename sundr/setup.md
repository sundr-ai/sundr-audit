# Set up a Sundr Savings Audit

You are a coding agent helping a user find out which of their LLM judgment calls (classify, moderate, route, grade, extract a label) could run on a cheaper model. Sundr tests cheaper candidates, smaller LLMs such as Claude Haiku 5.5 and decision models such as Jev and Clef, against the model the user runs today, and certifies on the user's own logs how often each would disagree with it. The audit is free: the user uploads a sample of logged calls and gets a report by email, with certified coverage and savings for each candidate.

Your job: find the judgment calls in this codebase, export a sample of them from the user's logs into one file, show the user exactly what will be uploaded, and upload it only once they agree.

## Ground rules

- Ask before anything leaves this machine. Show the user the summary in step 5 and wait for a clear yes before uploading.
- Never print, log or upload API keys or other secrets. Use credentials already configured in the user's environment; if one is missing, ask the user to set it and don't ask them to paste it to you.
- Don't change application code. This is read-only apart from the export file and any one-off export script you write (put those in a scratch or temp directory, not the repo).
- If the logs hold data the user may not share with third parties (health records, payment card data, anything under a contract that forbids it), stop and say so. Sundr sends logged text to the model providers that run the audit, listed at https://sundr.ai/subprocessors.

## 1. Find the judgment calls

Search the code for LLM calls (OpenAI, Anthropic, Gemini, LiteLLM, LangChain, Vercel AI SDK, raw HTTP to a model API) whose output is one of a fixed set of labels. Good signs:

- structured output or a tool schema with an `enum` field (`response_format`, Responses API `text.format`, Anthropic `output_config.format`, or a single forced tool)
- a prompt that says "answer only with", "respond with one of", "yes or no", "classify", "categorize", "route", "is this…"
- the result feeds an `if`, `switch` or `match` on a small set of values

Skip open-ended generation (chat replies, summaries, code, free-text extraction). Sundr only takes over decisions with a fixed label set; up to 255 labels is fine.

List what you find for the user: file and line, what it decides, the label set, the model, and a guess at monthly volume. Ask which one to audit. If there's only one, confirm it.

## 2. Find the logs

Work out where those calls are logged. In rough order of convenience:

- **Langfuse, Helicone, LangSmith or Braintrust**: export observations, requests, runs or logs for this call (filter by name, tag, prompt or model if you can) through the platform's API or UI export, using the user's existing keys.
- **Your own database or log files**: query the table or parse the log lines that record this call's input and output.
- **OpenAI or Anthropic request and response pairs** saved as JSON.

If the calls aren't logged anywhere, tell the user. The simplest fix is to log the input and the returned label for a week, then come back. Don't add logging to their code yourself unless they ask.

## 3. Write the export file

Write `sundr-export.jsonl`, one JSON object per line:

```json
{"id": "call_8f2a", "input": "Hi, I was charged twice for my March invoice…", "verdict": "billing"}
```

- `input`: the text the model judged. If a prompt template wraps user content in instructions, keep the content and drop the fixed instructions; describe those in the decision sentence instead. If several fields go into the prompt, join them with labelled lines (`Subject: …`, `Body: …`).
- `verdict`: the label the LLM returned, exactly as your code reads it.
- `id`: optional but useful, your call or trace id.
- `cost_usd`: optional, what that call cost, if the logs record it.
- `gold`: optional, a human label for the call, if the user has some (a few hundred is enough). The report then compares the LLM and Sundr with them.

Take the most recent calls, up to 100,000 rows and 200 MB; Sundr samples from the file if it needs fewer. The audit needs at least {{ min_rows }} rows. About 30,000 certifies a 1% error budget; about 12,000 certifies 2%. With fewer, the report shows the tightest budget it can honestly promise.

If the user would rather upload a platform's own export file unchanged, that works too: skip this step and set `source` in step 6 to `langfuse`, `helicone`, `langsmith`, `braintrust` or `raw`.

## 4. Fill in the context

Work out three things from the code, the logs and the user:

- **decision**: one sentence on what the LLM decides, e.g. "Route support tickets to billing, bug, how-to or other."
- **monthly_calls**: roughly how many of these calls happen per month.
- **usd_per_call**: what one call costs today. Use `cost_usd` from the logs if present; otherwise estimate from the model's per-token prices and typical token counts.

## 5. Show the user, and ask

Before uploading, show:

- the file path, row count and size
- the label distribution (top labels with counts)
- three example rows, with long inputs truncated
- the decision sentence, monthly volume and cost per call
- the email address the report will go to (ask for their work email)

Then ask: "Upload this to sundr.ai for a free audit?" Upload only on a clear yes.

## 6. Upload

```bash
curl -sS https://sundr.ai/audits \
  -H "Accept: application/json" \
  -F email="you@company.com" \
  -F file=@sundr-export.jsonl \
  -F source=canonical \
  -F decision="Route support tickets to billing, bug, how-to or other." \
  -F monthly_calls=2000000 \
  -F usd_per_call=0.004
```

A success returns `202` with JSON like `{"id": "…", "status": "awaiting_confirmation", "rows": 31204, "task": "…", "next": "…"}`. An error returns JSON with an `error` message; fix the file and try again. Each email can start two audits a day.

## 7. Tell the user what happens next

Sundr emails a confirmation link to the address they gave. The audit starts when they click it, and the report link arrives by email when it's done; the link in the confirmation shows progress meanwhile. The report shows, for each candidate model and error budget, the share of calls it is certified to answer on their data, and what that would have saved.

The uploaded file is encrypted at rest and deleted as soon as the report is written, or after two days if the audit is never confirmed. Reports are deleted after 30 days. Delete the local `sundr-export.jsonl` once the user no longer needs it.

Questions, or to run Sundr in production: hello@sundr.ai. Privacy policy: https://sundr.ai/privacy
