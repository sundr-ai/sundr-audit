# Sundr certify (GitHub Action)

Certify, on every pull request that touches a prompt or a model, that a cheaper model (or the changed prompt) agrees with your reference verdicts within an error budget, at 95% confidence. The result is posted as a pull-request comment and in the job summary.

```yaml
# .github/workflows/sundr.yml
name: Certify judgment calls
on:
  pull_request:
    paths: ["prompts/**", "evals/**"]
permissions:
  contents: read
  pull-requests: write
jobs:
  certify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sundr-ai/sundr-audit/integrations/github-action@v0.1.0
        with:
          evals: evals/ticket-router.jsonl          # input + verdict per line
          candidate: openai/gpt-6-luna,anthropic/claude-haiku-5.5
          system-prompt-file: prompts/ticket-router.txt
          epsilon: "0.01,0.02"
          require-coverage: "0.9"                    # optional: fail below 90% certified at 1%
          openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

`evals` holds calls your current model has already judged, for example a week exported with `sundr pull`, one JSON object per line: `{"input": "...", "verdict": "billing"}`. About 1,200 rows certifies a 5% budget, 6,000 a 2% budget, and 12,000 a 1% budget.

The candidate models are asked with your system prompt if you pass one, else with a generic prompt built from `task`. Calls go to OpenRouter on your key; a run over 2,000 calls with GPT-6 Luna costs about $0.10. Answers are cached between steps of one run only.

What "certified" means: [sundr.ai/docs](https://sundr.ai/docs#certificate). To take over the certified share in production, with live audit and automatic rollback, see [sundr.ai](https://sundr.ai).
