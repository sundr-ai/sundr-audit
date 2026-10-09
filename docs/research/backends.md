# Alternative decision-model backends for Sundr

Research date: 2026-10-09. Scope: backends other than TypeSafe Jev, which is covered separately. No API keys were available, so nothing here was called live. Every fact comes from docs, model cards, READMEs or press coverage.

**Evidence labels**

- **[P]**: verified from a primary source (the vendor's own docs, model card, repo README, or launch post).
- **[S]**: secondary source only (press, blogs, aggregators). Treat as UNVERIFIED until checked.
- **UNVERIFIED**: no source confirms it; it is an inference or a gap. The reason is given inline.
- **"Illustrative"** on a JSON block means the values are made up. The shape follows the cited schema.

---

## 0. TL;DR for implementation

1. **One wire format covers almost everything.** TypeSafe's "System One" API (`POST /v1/systemone`; `state` + `questions` map in, `model` + `answers` map + `usage` out) is implemented by Ollama 0.35+, Laya (`laya-serve`), AWS Strands Decider (`strands-decider serve`), Ollaya, ollajev, OpenRouter and LLM Gateway. Cloudflare Clef takes the same body and returns the same answers, but at a different URL and inside a different envelope. **OpenAI Decisions is the only one with a different schema.**
2. Build a single `SystemOneBackend(base_url, path, api_key, model, envelope=None)` for all of them. Add a small `CloudflareClefBackend` subclass that changes the path and unwraps `result`. Write a separate `OpenAIDecisionsBackend` translator.
3. **Limits differ per server** even when the wire format is the same: question count, option count, body size, context. Validate each request against a per-backend limits table (section 7) and split question sets that are too large.
4. **Do not trust vendor `confidence`.** Definitions differ between servers (section 7.3). Sundr should use `probabilities` / `noul` and compute any confidence itself.

---

## 1. Baseline: the System One / Jev wire format

This is the reference format every compatible backend copies. Source: TypeSafe API reference (docs.typesafe.ai/api.md) [P].

- `POST https://api.typesafe.ai/v1/systemone`, with headers `Authorization: Bearer <key>` and `Content-Type: application/json`.
- Request fields:
  - `model` (string, e.g. `jev-latest`)
  - `state` (string | object | array)
  - `questions`: a map from caller-chosen id to a question. The ids are not sent to the model.
- Question fields: `type` and `instructions` (string | object | array) are both required. Per type:
  - `noul`: `criteria` is optional, `{ "true": ..., "false": ... }`
  - `choice`: `criteria` is required. It maps option id to a description (string | object | array | null). Maximum 255 options.
  - `score`: `criteria` is required. It is an array of level descriptions, lowest first, with 2 to 10 levels.
- Response fields: `model`, `answers` (keyed by question id, each with a `type`), and `usage` (`input_tokens`, `output_tokens`).
  - `noul`: `{type, noul}`, where `noul` is P(yes) in [0,1]. There is no `confidence`.
  - `choice`: `{type, choice, probabilities: {option: p}, confidence}`
  - `score`: `{type, score, legend: {"0": desc, ...}, probabilities: {"0": p, ...}, confidence}`. `score` is the expected level index. For example, 0·p0 + 1·p1 + 2·p2.
- Errors: 401, 422 (the body names the offending field), 429, 529. Retry 429 and 529 with backoff.
- Jev price is $0.042 per 1M input tokens, and output is free (TypeSafe launch blog [P]). Jev's documented choice cap is 255 ("cardinality up to 255", [P]).

---

## 2. Cloudflare Clef / Clef-flash (Workers AI)

Announced 2026-10-01 (blog.cloudflare.com/clef-decision-models [P]). Docs: developers.cloudflare.com/workers-ai/models/clef and /clef-flash [P].

### 2.1 Models

| | Clef | Clef-flash |
|---|---|---|
| Workers AI model ID | `@cf/cloudflare/clef` [P] | `@cf/cloudflare/clef-flash` [P] |
| Body `model` value | `"clef"` [P] | `"clef-flash"` [P] |
| Base model | Qwen3.8-27B + vision encoder [P] | Qwen3.5-9B [P] |
| Params / dtype | 27B, BF16 [P] | 9B, BF16 [P] |
| Hosted context | 65,536 tokens [P] | 65,536 tokens [P] |
| Modality | text + up to 4 images [P] | text + up to 4 images [P] |
| Price | $0.24 / 1M input tokens = 21,818 neurons/M [P] | $0.09 / 1M input tokens = 8,182 neurons/M [P] |
| Output tokens | not billed (no output price listed) [P] | same [P] |
| Median / p95 latency | 209.3 / 238.6 ms [P, vendor-run benchmark] | 38.8 / 122.4 ms [P, vendor-run] |
| HF weights | `Cloudflare/clef`, Apache-2.0 [P] | `Cloudflare/clef-flash`, Apache-2.0 [P] |

Both are built the same way [P]: a frozen backbone, rank-256 LoRA adapters, and a "joint schema head" that outputs one logit per allowed option per question. Inference is a single prefill pass with no autoregressive decoding. Calibration comes from a Brier loss plus an RL stage Cloudflare calls RLCD. In the same vendor run, Cloudflare measured Jev at 524.1 / 536.0 ms median / p95 [P]. That figure is Cloudflare's measurement, not TypeSafe's.

### 2.2 Endpoint and auth

```
POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/clef
POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/clef-flash
Authorization: Bearer $CLOUDFLARE_AUTH_TOKEN
Content-Type: application/json
```

- Token permissions: a custom API token needs both "Workers AI - Read" and "Workers AI - Edit" (Workers AI REST getting-started docs [P]).
- Inside a Worker, call `env.AI.run("@cf/cloudflare/clef-flash", { model, state, questions })` instead [P].
- Routing through AI Gateway, for logging or future RL dataset capture: the usual pattern is `https://gateway.ai.cloudflare.com/v1/{account}/{gateway}/workers-ai/@cf/cloudflare/clef`. This is UNVERIFIED for Clef specifically. It is the general Workers AI provider pattern and was not tested with this model.

### 2.3 Request schema

From `schema-input.json` [P]:

| Field | Required | Constraint |
|---|---|---|
| `model` | yes | regex `^\s*(clef\|clef-flash)\s*$` |
| `state` | yes | string, object or array. Long text is truncated to fit the context. |
| `questions` | yes | 1 to 64 entries. Id regex `[A-Za-z0-9_.-]`, max 100 chars. |
| `questions.*.type` | yes | `noul` \| `choice` \| `score` |
| `questions.*.instructions` | yes | non-empty string, or an object/array |
| `noul.criteria` | no | `{ "true": ..., "false": ... }` |
| `choice.criteria` | yes | map of option id to description (string/object/array/null), **2 to 255 options** |
| `score.criteria` | yes | array of **2 to 10** level descriptions, lowest first, indexed from 0 |
| `images` | no | Clef-only extension. Max 4. Each is a data URL or `{content_type, base64}` (png/jpeg/webp). Max 4 MiB and 16 MP each, 8 MiB decoded in total, 13 MiB request body. **No remote URLs.** Images are placed before the state. |

Example request (the shape follows the Cloudflare docs example; the content is mine):

```bash
curl -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/ai/run/@cf/cloudflare/clef-flash" \
  -H "Authorization: Bearer $CLOUDFLARE_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "clef-flash",
    "state": "Checkout has returned 500 errors for all EU customers since 09:10 UTC.",
    "questions": {
      "urgent":   { "type": "noul", "instructions": "Is this request urgent?" },
      "team": {
        "type": "choice",
        "instructions": "Which team should handle this request?",
        "criteria": {
          "billing":   "Payments, invoices, refunds",
          "technical": "Outages, errors, configuration",
          "sales":     "Plans and upgrades"
        }
      },
      "severity": {
        "type": "score",
        "instructions": "How severe is the customer impact?",
        "criteria": ["No impact", "Minor", "Major", "Critical"]
      }
    }
  }'
```

### 2.4 Response schema

From `schema-output.json` [P]:

- `model`, `answers` and `usage` are all required. `usage` holds `input_tokens` and `output_tokens`, both required integers.
- Each answer has a `type` field that tells you its shape:
  - `noul`: `noul` in [0,1]
  - `choice`: `choice` (the argmax), `probabilities` (sum to 1) and `confidence`
  - `score`: `score` (probability-weighted level, can be fractional), `legend`, `probabilities` and `confidence`

**REST envelope.** Over REST, the Jev-shaped object sits inside the standard Workers AI wrapper `{result, success, errors, messages}`. The wrapper itself is documented for Workers AI REST in general [P]. That Clef answers sit under `result` was observed in a hands-on test (flaviocopes.com/clef) [S]. The Worker binding returns the inner object with no wrapper [S].

Illustrative REST response:

```json
{
  "result": {
    "model": "clef-flash",
    "answers": {
      "urgent":   { "type": "noul", "noul": 0.93 },
      "team": {
        "type": "choice",
        "choice": "technical",
        "probabilities": { "billing": 0.04, "technical": 0.94, "sales": 0.02 },
        "confidence": 0.76
      },
      "severity": {
        "type": "score",
        "score": 2.59,
        "legend": { "0": "No impact", "1": "Minor", "2": "Major", "3": "Critical" },
        "probabilities": { "0": 0.01, "1": 0.04, "2": 0.30, "3": 0.65 },
        "confidence": 0.41
      }
    },
    "usage": { "input_tokens": 395, "output_tokens": 0 }
  },
  "success": true,
  "errors": [],
  "messages": []
}
```

- `usage.output_tokens` was 0 in the observed response [S].
- Errors use the standard Cloudflare `errors: [{code, message}]` form. Leaving `"model": "jev-latest"` in the body is reported to fail with code `5006` [S].
- UNVERIFIED: what happens when the URL model and the body `model` disagree. Keep them consistent.

### 2.5 Jev compatibility verdict

The blog calls Clef fully compatible with the Jev API and says it can be swapped in directly [P]. In practice it is **body-compatible, not URL-compatible**.

- Same: question types, field names, answer shapes, and the 255-option and 10-level caps.
- Different:
  1. The path is `/ai/run/@cf/...`, not `/v1/systemone`, so stock `TYPESAFE_BASE_URL` switching does not work.
  2. REST adds the `result` envelope.
  3. `model` must be `clef` or `clef-flash`.
  4. There is a hard cap of 64 questions; Jev's docs state no maximum.
  5. The optional `images` field is a Clef extension.
  6. The context is 64k, against 32k for Jev, per Cloudflare [P].
- The TypeSafe JS SDK works only through a custom fetch adapter, and only `systemOne()` works; `models.list()` does not [S].
- At launch, image requests took 13 to 30 seconds and image payloads counted as text tokens [S].

### 2.6 Rate limits and quotas

- Free allocation is 10,000 neurons per day on every plan, and usage above that is $0.011 per 1,000 neurons on Workers Paid [P]. 10k neurons comes to about 458k Clef tokens or about 1.22M Clef-flash tokens per day (my arithmetic).
- **Requests per minute for Clef: UNVERIFIED.** The Workers AI limits page does not list Clef [P]. The model page labels Clef "text generation", whose default is 300 rpm [P]. Text Classification is 2,000 rpm [P]. Which one applies is not stated. Plan for 300 rpm until it is measured, and handle 429 and back off.

### 2.7 Open weights and self-hosting

- The HF repos `Cloudflare/clef` and `Cloudflare/clef-flash` are Apache-2.0. Each ships sharded safetensors, `joint_head.safetensors`, `joint_head_config.json` and `joint_schema_model.py` [P].
- **The supported path is native PyTorch / transformers with the repo's own code** [P]. It was tested with torch 2.11 and transformers 5.10.2 on a single H200. Images need `pillow`.

  ```python
  import sys
  from huggingface_hub import snapshot_download
  path = snapshot_download("Cloudflare/clef-flash")
  sys.path.insert(0, path)          # untrusted remote code: review joint_schema_model.py first
  from joint_schema_model import load_release_model, systemone

  model, processor = load_release_model(path, device="cuda")
  resp = systemone(model, processor, {
      "model": "clef-flash",
      "state": "Our checkout is down.",
      "questions": {"outage": {"type": "noul", "instructions": "Is a service down?"}},
  })
  resp["answers"]   # same shape as the hosted API, with no envelope
  ```

  - `systemone()` takes a body in the shape of a `POST /v1/systemone` request [P]. It is a Python function, not an HTTP server. Wrap it in your own FastAPI route, or use ollajev (below).
  - The self-hosted context defaults to `encode_record(max_length=16384)` tokens and is configurable. There is also `max_state_tokens` [P]. This is lower than the hosted 65,536.
- **vLLM / SGLang / Ollama: there is no decision-head support.** The vLLM, SGLang and Docker Model Runner snippets on the HF card are generic HF "use this model" snippets for chat generation. Nothing on the card says they load the joint head [P]. This is my inference: they would serve the backbone as a chat LLM without calibrated per-option probabilities. The same applies to the 23 to 42 community GGUF quantizations: there is no evidence they keep the head.
- **Third-party servers that do serve Clef behind `/v1/systemone`:**
  - ollajev 0.2.0 lists `Cloudflare/clef-flash` (19.1 GB) and `Cloudflare/clef` (55 GB) [P, the PyPI page].
  - Ollaya has a `clef` tag, which is Clef-Flash 9B [P, the Ollaya README].
  - Neither has been verified to reproduce the hosted probabilities.
- VRAM:
  - At 64k context and single concurrency, Clef-flash needs at least 41 GB and Clef needs 85 GB. Cloudflare's Michelle Chen gave these figures to The Register [S].
  - The BF16 weights alone are about 54 GB (Clef) and about 18 GB (Flash) (my arithmetic).

### 2.8 RL fine-tuning platform

From the blog [P]:

- Today the service is run by Cloudflare's forward-deployed engineering team, for design partners. A self-serve product is planned.
- The pipeline:
  1. AI Gateway captures production traffic as a dataset.
  2. Workers AI generates rollouts from base Clef.
  3. Containers act as the RL sandbox and scoring environment.
  4. A new "Trainer" component updates the weights.
  5. Workers AI BYO Model redeploys the result.
- Access is through the interest form at cloudflare.com/resource/clef-rl-interest. The form page gives no pricing, eligibility or timeline [P].
- Data handling: Cloudflare says it does not read, store or train on requests and responses unless you opt into fine-tuning [P].

---

## 3. AWS Strands Decider (about 2B, open source)

Repo: `github.com/strands-labs/strands-decider` (Strands Labs, AWS) [P]. Released around 2026-10-01 [S].

- **Weights:** `StrandsAgents/strands-decider-2B-hobson-v21` on HF, Apache-2.0 [P]. v19 is still published [P]. Press coverage disagrees on the version (v19, v20, v21); the repo says v21 is current.
- **Architecture:** a Qwen3.5-2B-Base decoder torso with its LM head removed, a pointer head of about 1M params in fp32, and a rank-16 LoRA. About 1.9B params in total [P].
- **Not the same model as Mapika `decider-2b`.** Ollaya's `decider` tag and ollajev's default `Mapika/decider-4b-GGUF` are Mapika models, not AWS [P].
- **Install:** `pip install strands-decider`. Extras: `[mlx]`, `[cuda]`, `[mps]`, `[cpu]` and `[vision]`, where vision needs transformers 5.18 or later. The `mlx` extra ships in the next release; until then install from a clone with `pip install -e ".[mlx]"` [P].
- **CLI:**
  ```bash
  strands-decider ask StrandsAgents/strands-decider-2B-hobson-v21 \
    --state "Payouts have failed for 3 days" \
    --choice "Which team?=billing,sales,retail" --noul "Is this urgent?" \
    --score "How frustrated?=calm,frustrated,furious" [--device mlx|cuda|mps|cpu] [--image f.png]
  ```
- **Server:** `strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --port 8000 [--vision]`. It binds to 127.0.0.1 and has **no authentication** [P].
- **HTTP:** `POST /v1/systemone` [P]. That is the only documented route: no `/v1/models` and no batch route.
  - The documented request carries `state` and `questions` only. Whether `model` is required or ignored is undocumented.
  - The response includes `model`, `answers`, `usage` and an extra `latency_ms` field [P].
  - The README shows a `noul` HTTP response only. The `choice` and `score` HTTP shapes are UNVERIFIED. The CLI output shows choice, confidence and per-option probabilities, which suggests the Jev shape.

  Illustrative response, in the shape the README documents for `noul`:

  ```json
  {
    "model": "strands-decider-2B-hobson-v21",
    "answers": { "is_urgent": { "type": "noul", "noul": 0.87 } },
    "usage": { "input_tokens": 86, "output_tokens": 1 },
    "latency_ms": 140.0
  }
  ```

- **Images:** base64 `images` "as part of the state" when the server runs with `--vision`. The exact field structure is UNVERIFIED [P, vague].
- **Limits:** no stated cap on options. Evaluation uses a 4096-token window, but no maximum context is stated [P].
- **Hardware and latency:** the README gives no VRAM figure [P]. Measured latency [P]:
  - RTX 3090 (WSL2): 115 ms median, 299 ms p95
  - M3 Pro: 153 ms median, 234 ms p95 (warm, under 300 tokens)
  - Rough sizing: 2B in BF16 is about 4 to 5 GB (my estimate).
- **Accuracy and calibration:** JevBench (231 tasks) 0.762, Brier 0.323, ECE 0.064 [P]. The README says answers at confidence ≥ 0.9 are right about 95% of the time on unseen short classification [P]. It uses one temperature per primitive.
- **Training:** about 11 hours on one RTX 3090, and the training data and scripts are included [P].
- **Jev-compatible?** The route name and noul shape match. The README never claims SDK compatibility [P]. Treat it as "probably compatible" and run a contract test.

---

## 4. Laya (open decision models)

Repo: `github.com/NandhaKishorM/laya`, Apache-2.0 [P].

- **What it is:** an encoder-based (ModernBERT / mmBERT) non-autoregressive decision engine [P]. It is much smaller and faster than the decoder-based models.
- **Checkpoints on HF (`convaiinnovations/*`)** [P]:

  | Repo | Backbone | Params | Context | Notes |
  |---|---|---|---|---|
  | `convaiinnovations/laya` | ModernBERT-large | 421M | 512 | English |
  | `convaiinnovations/laya-multilingual` | mmBERT-base | 322M | 1024 (up to 8192 with `max_len=8192`) | 100+ languages |
  | `convaiinnovations/laya-typed-decisions` | ModernBERT-large | 421M | 1024 | typed-decision workflows |

  A `Router()` picks a checkpoint per request by detecting script and language [P].
- **Install:** `pip install laya` for the library, or `pip install "laya[serve]"` for the HTTP server. Python 3.10 or later [P].
- **Server:** `laya-serve` binds 0.0.0.0:8000 by default.
  - Configuration is through the env vars `LAYA_HOST` and `LAYA_PORT`.
  - Flags: `--no-preload` and `--device cuda|cpu|mps` [P].
  - Auth is optional: when `LAYA_API_KEY` is set, clients must send `Authorization: Bearer <key>` [P].
- **HTTP** [P]:
  - `POST /v1/systemone` is described as the same wire protocol as Jev; a Jev client only needs its base URL repointed.
  - `POST /v1/systemone/batch` takes `{"states": [...], "questions": {...}}`, with at most 64 states.
- **Extra response fields** [P]:
  - `routing`: `{model, repo, reason, detection, workflow}`
  - per-answer `answer_confidence`: the probability of the chosen answer
  - `usage`
- **Documented differences from Jev** [P]:
  - `confidence` is 1 − normalised entropy, and Jev thresholds do not transfer.
  - Score levels must have descriptions; `null` returns 422.
  - Options share a per-checkpoint token budget: 192 tokens for `laya`, 256 for the others. Over budget returns 422, and more than 100 choice options returns 413.
  - Hook fields in the body return 422.
- **`model` values:** not fully enumerated. `english` appears in examples. An unpublished id or a filesystem path returns 422 [P]. UNVERIFIED: whether omitting `model` lets the Router decide.

Illustrative request and response (Jev shape plus Laya's documented extras):

```bash
curl -s localhost:8000/v1/systemone -H 'content-type: application/json' -d '{
  "state": {"body": "We were billed twice for March. Please refund it today."},
  "questions": {
    "department": {"type": "choice", "instructions": "Which department?",
                   "criteria": {"billing": "Refunds and charges", "other": "Everything else"}},
    "refund":     {"type": "noul", "instructions": "Does the customer ask for a refund?"}
  }
}'
```

```json
{
  "model": "english",
  "answers": {
    "department": {"type": "choice", "choice": "billing",
                   "probabilities": {"billing": 0.97, "other": 0.03},
                   "confidence": 0.81, "answer_confidence": 0.97},
    "refund": {"type": "noul", "noul": 0.95}
  },
  "usage": {"input_tokens": 61, "output_tokens": 0},
  "routing": {"model": "english", "repo": "convaiinnovations/laya",
              "reason": "...", "detection": {"script": "Latin", "language": "en"}, "workflow": null}
}
```

- **Hardware:** runs on CUDA, MPS, XPU or CPU [P]. No GPU is needed.
  - T4: 32.8 to 39.5 ms for one question, about 7.2 ms per question when batched, 103 to 332 questions per second [P].
  - Apple MPS: 121 to 146 ms for a 4-question call. A 56-core CPU: 192 to 453 ms [P].
  - In Cloudflare's benchmark Laya had the lowest median latency, 5.8 ms, at lower quality [P, Cloudflare blog].
- **Ollama:** Laya is **not** in Ollama's decision-model lineup (nimble and tev1 only [P]). It runs under **Ollaya** (`laya`, a router over `laya:en` and `laya:multilingual`) and under **ollajev** (all three `convaiinnovations/*` repos) [P].

---

## 5. Ollama 0.35+ and Ollaya

### 5.1 Ollama (official)

Ollama blog post of 2026-09-29, "Ollama now supports Jev-style decision models" [P].

- **Requires Ollama 0.35 or later.** The endpoint is `POST http://localhost:11434/v1/systemone` and it needs no auth locally [P].
  - The TypeSafe SDK setup uses `TYPESAFE_BASE_URL=http://localhost:11434` with `TYPESAFE_API_KEY=ollama` as a placeholder.
  - Pydantic AI uses `SYSTEM_ONE_BASE_URL=http://localhost:11434`, with no `/v1` suffix [P].
- **Models** [P]:

  | Tag | Params | Origin | Size | Status |
  |---|---|---|---|---|
  | `nimble` / `nimble:9b` | 9B | Bespoke Labs, Apache-2.0 | about 9.3 to 9.5 GB | available |
  | `tev1` | 4B | Together AI | not given | experimental |
  | `tev1:0.8b` | 0.8B | Together AI | about 812 MB [S] | experimental |

  Pull with `ollama pull nimble`. **Do not use `ollama run`.** These models are API-only, and they are not yet in the Ollama CLI or the Python/JS libraries [P].
- **Limits (nimble library page)** [P]:
  - 1 to 64 questions; **2 to 26 options** for choice and score; **64 KiB request body**.
  - Each question's prompt must fit an **8,192-token** context. The 256K shown in the listing is not usable.
  - Text only.
  - Questions are scored independently; nimble does not check that answers are consistent with each other.
- **Hardware and latency:** nimble averages 91 ms per decision on an M5 Max [P, vendor demo]. No RAM requirement is published.
  - My estimate: about 12 GB of free unified or GPU memory for nimble at Q4/Q8 (UNVERIFIED).
- **Troubleshooting:** a 404 on every call means a server older than 0.35, an old instance still running, or a model that was never pulled [S].
- **Response shape:** the same as Jev. The blog's full example has `model`, `answers` with typed choice/noul/score entries (including `legend`), and `usage` [P].
- **Confidence:** the nimble page says `confidence` measures how concentrated the probabilities are, not the chance of being correct [P]. I recomputed the blog's example numbers and they match 1 − H(p)/ln(k) exactly.

Example request:

```bash
curl http://localhost:11434/v1/systemone -H 'Content-Type: application/json' -d '{
  "model": "nimble",
  "state": {"ticket": "I was charged twice, please refund the duplicate."},
  "questions": {
    "team":    {"type": "choice", "instructions": "Which team handles this?",
                "criteria": {"billing": "Payments, refunds", "technical": "Bugs", "other": "Other"}},
    "refund":  {"type": "noul", "instructions": "Does the customer ask for a refund?"},
    "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["Routine", "Soon", "Urgent"]}
  }
}'
```

Illustrative response:

```json
{
  "model": "nimble",
  "answers": {
    "team":    {"type": "choice", "choice": "billing",
                "probabilities": {"billing": 0.98, "technical": 0.015, "other": 0.005}, "confidence": 0.90},
    "refund":  {"type": "noul", "noul": 0.99},
    "urgency": {"type": "score", "score": 0.82, "legend": {"0": "Routine", "1": "Soon", "2": "Urgent"},
                "probabilities": {"0": 0.37, "1": 0.44, "2": 0.19}, "confidence": 0.05}
  },
  "usage": {"input_tokens": 840, "output_tokens": 4}
}
```

### 5.2 Ollaya (third-party, not affiliated with Ollama)

Repo: `github.com/ollaya-dev/ollaya`, Apache-2.0 [P]. It is an Ollama-style daemon dedicated to decision models.

- **Install:** `curl -fsSL https://ollaya.dev/install.sh | sh` on macOS or Linux, or the PowerShell installer on Windows.
  - Docker: `ghcr.io/ollaya-dev/ollaya:cuda` or the CPU-only image [P].
  - **These are remote installer scripts. Review them before running.**
- **Server:** default port **11435** [P].
  - TypeSafe-compatible routes: `POST /v1/systemone`, `POST /v1/decisions` (an exact alias, **not** OpenAI-compatible) and `GET /v1/models`.
  - Native routes: `/api/decide`, `/api/pull`, `/api/tags` and others.
- **Auth:** when `OLLAYA_API_KEY` is unset there is no auth. When set, requests need `Authorization: Bearer <key>`, otherwise `401 UNAUTHORIZED` [P].
- **Models:** `laya`, `clef` (Clef-Flash 9B), `decider` (Mapika), `kev` (Jared Palmer), `nimble`, `winnow:e4b` (the recommended one), `von`, `clm` and others [P]. Each keeps its own license.
- **Limits (docs/api.md)** [P]:
  - Body: 8 MiB. Questions: 1 to 256. Choice: 2 to 255 options. Score: 2 to 10 levels. State: up to 65,536 tokens.
  - Per-model option caps: about 125 for `laya:en` and 250 for `laya:multilingual`.
  - Queue: 512, returning `503 QUEUE_FULL` with `Retry-After: 1`.
- **Response:** exactly `model`, `answers` and `usage`, with `output_tokens` always 0 [P]. For a router model, `model` is the routed target (for example `laya:en`).
- **Errors:** `{error, code[, detail]}`, with stable codes such as `MODEL_NOT_FOUND`, `TOO_MANY_OPTIONS`, `INPUT_TOO_LONG` and `QUEUE_FULL` [P]. Branch on `code`.
- **Hardware:** CPU works. NVIDIA CUDA needs driver R525 or later. Apple Metal and Vulkan are supported for GGUF models [P]. Per-model RAM figures are not published.

---

## 6. OpenAI Decisions API

Announced at DevDay on 2026-09-29. Guide: developers.openai.com/api/docs/guides/decisions [P].

- **Status:** **conflicting.**
  - The official guide says **public beta**, with GA expected in the coming weeks [P].
  - Press and testers in early October describe a limited preview for selected customers [S]. One test got HTTP 403 with "Decision API is not enabled for this user" [S].
  - Expect gating per account and handle 403 as "not enabled".
- **Endpoint:** `POST https://api.openai.com/v1/decisions`, `Authorization: Bearer $OPENAI_API_KEY` [P].
- **Model:** `gpt-6-luna`, the only one available [P].
- **SDK:** `client.decisions.create(...)` in Python (openai-python ≥ 3.26.0), JS (≥ 7.30.0) and Ruby. Go uses `client.Decisions.New`, Java uses `client.decisions().create` [P].
- **Not Jev-compatible.** Different names and container types:

  | Concept | System One / Jev | OpenAI Decisions |
  |---|---|---|
  | Context | `state` (string/object/array) | `input` (string, or a message array with `input_text` and `input_image` parts) |
  | Questions | map keyed by id | **array**, each with `name` |
  | Yes/no | `noul` | `predicate` |
  | Choice options | `criteria: {id: desc}` | `choices: [{value, description}]` |
  | Score levels | `criteria: [desc, ...]` | `levels: [{label, description}]`, lowest first |
  | Yes/no output | `noul: p` | `probability: p` |
  | Choice probabilities | `{id: p}` | `[{value, probability}]` |
  | Score probabilities | `{"0": p}` + `legend` | `[{value: idx, label, probability}]` |
  | Refusal | none | answer `type: "refusal"` (only `name` is documented) |
  | Usage | `usage{input_tokens, output_tokens}` | **not shown in the docs samples** (UNVERIFIED) |

- **Images:** inline base64 data URLs only. There is no HTTP URL or `file_id` input [P].
- **Pricing:** **$0.10 per 1M input tokens**, with no output or caching charges [P]. Regional-processing and long-context multipliers apply, and their values are not given.
- **Limits:** max questions, options, context and rate limits are **not documented** [P].
- **Latency:** UNVERIFIED.
  - The vendor claim is roughly 150 ms per decision [S], or "under a few hundred ms" from an OpenAI staffer [S].
  - One independent test measured a 1.46 s median [S].
- **Compliance:** ZDR and HIPAA for eligible customers. US and EU data residency [P].

Example request (field names per the guide; content is mine):

```bash
curl https://api.openai.com/v1/decisions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" -d '{
  "model": "gpt-6-luna",
  "input": "Discount code is rejected at checkout and I cannot pay.",
  "questions": [
    {"type": "predicate", "name": "urgent", "instructions": "Is this request urgent?"},
    {"type": "choice", "name": "team", "instructions": "Which team should own this?",
     "choices": [{"value": "billing", "description": "Charges, refunds"},
                 {"value": "technical", "description": "Bugs, errors"},
                 {"value": "other", "description": "Anything else"}]},
    {"type": "score", "name": "severity", "instructions": "How blocked is the customer?",
     "levels": [{"label": "Cosmetic", "description": "No real impact"},
                {"label": "Workaround", "description": "Can proceed another way"},
                {"label": "Blocked", "description": "Cannot complete the task"}]}
  ]
}'
```

Illustrative response (shape per the guide's sample excerpts):

```json
{
  "answers": [
    {"type": "predicate", "name": "urgent", "probability": 0.71},
    {"type": "choice", "name": "team", "choice": "technical",
     "probabilities": [{"value": "billing", "probability": 0.30},
                       {"value": "technical", "probability": 0.66},
                       {"value": "other", "probability": 0.04}],
     "confidence": 0.62},
    {"type": "score", "name": "severity", "score": 1.8,
     "probabilities": [{"value": 0, "label": "Cosmetic", "probability": 0.05},
                       {"value": 1, "label": "Workaround", "probability": 0.10},
                       {"value": 2, "label": "Blocked", "probability": 0.85}],
     "confidence": 0.60},
    {"type": "refusal", "name": "..."}
  ]
}
```

- A workaround while gated: the Responses API with reasoning off and a strict JSON-schema enum. That returns a label, not calibrated probabilities [S].

---

## 7. The "Jev-compatible API" convention

### 7.1 What it is

There is **no formal open spec or OpenAPI document** that I could find. The de-facto standard is TypeSafe's `POST /v1/systemone` wire format (section 1), defined by TypeSafe's docs and SDKs. Implementers advertise compatibility as "point `TYPESAFE_BASE_URL` at it".

**Servers / providers** (base URL, then status):

| Implementation | Base URL / path | Status |
|---|---|---|
| TypeSafe Jev | `https://api.typesafe.ai/v1/systemone` | origin [P] |
| Ollama ≥ 0.35 | `http://localhost:11434/v1/systemone` | [P] |
| Laya `laya-serve` | `http://<host>:8000/v1/systemone` (+ `/batch`) | [P] |
| Strands Decider `serve` | `http://127.0.0.1:8000/v1/systemone` | route [P], full shape UNVERIFIED |
| Ollaya | `http://127.0.0.1:11435/v1/systemone` (+ `/v1/decisions` alias, `/v1/models`) | [P] |
| ollajev 0.2.0 (PyPI, 2026-10-05) | `http://127.0.0.1:8000/v1/systemone`, `/v1/models` | [P]. Serves Clef, Laya, Kev, Mapika decider and others from HF |
| LLM Gateway | `https://api.llmgateway.io/v1/systemone` | proxies Jev (`typesafe/jev-1.13.0`) [P] |
| OpenRouter | SDK `systemOne.create`, model `typesafe/jev-1.13` | the HTTP path is not shown in the docs: UNVERIFIED |
| Cloudflare Clef | `/client/v4/accounts/{id}/ai/run/@cf/cloudflare/clef[-flash]` + `result` envelope | body-compatible only [P]/[S] |
| Clef HF `systemone()` | in-process Python function | body-compatible [P] |
| von (`von serve`, HF `wfzyx/von-1.0`), Yas (Docker, Kev 0.8B), Openjev | `/v1/systemone` | [S] only |

**Clients that target any of these:**

- `typesafe-sdk` via `TYPESAFE_BASE_URL` [P, Ollama blog]
- Pydantic AI `SystemOneModel` / `SystemOneProvider(base_url, api_key)` via `SYSTEM_ONE_BASE_URL` / `SYSTEM_ONE_API_KEY` [P]
- LangChain / LangSmith "TypeSafe-compatible model" [S]

### 7.2 Recommended Sundr client design

These notes map onto `sundr/backends/base.py`, `Backend.ask(state, questions) -> BackendResult`.

```python
@dataclass
class SystemOneTarget:
    name: str                 # "ollama", "laya", "strands", "ollaya", "clef", ...
    url: str                  # full POST URL
    model: str | None         # body "model"; None => omit (Strands/Laya router)
    api_key: str | None
    envelope: str | None      # "cloudflare" => unwrap resp["result"], check resp["success"]
    limits: Limits            # see 7.4
    usd_per_million_input_tokens: float

# Request build (from sundr.types.Question):
#   noul   -> {"type":"noul","instructions":q.instructions}  (+ criteria {"true","false"} if available)
#   choice -> {"type":"choice","instructions":..., "criteria": dict(zip(q.options, q.descriptions or [None]*n))}
#   score  -> {"type":"score","instructions":..., "criteria": list(q.descriptions or q.options)}  # low -> high
# Response parse -> Answer(probs):
#   noul   -> {"yes": a["noul"], "no": 1 - a["noul"]}          # matches Question.option_names()
#   choice -> a["probabilities"]                                # keys are q.options
#   score  -> {q.options[int(k)]: p for k, p in a["probabilities"].items()}   # keys are "0".."n-1"
# input_tokens <- resp["usage"]["input_tokens"]; model <- resp["model"]; latency measured client-side.
```

- **OpenAI adapter:**
  - Build `questions` as an array with `name = q.name`. Map `noul` to `predicate`, choices to `[{value, description}]` and score levels to `[{label, description}]`.
  - Parse `probability`, then `{p["value"]: p["probability"]}` for choice, then `{q.options[p["value"]]: ...}` for score.
  - Treat `type == "refusal"` as a missing answer.
  - If there is no `usage` field, estimate tokens with a tokenizer for cost accounting.
- **Cloudflare adapter:**
  - Use URL `.../ai/run/@cf/cloudflare/{clef|clef-flash}` and send body `model` set to the same short name.
  - If `success` is false, raise `BackendError` with `errors[0].code`.
  - Optionally pass `images` (data URLs only).

### 7.3 Confidence is not portable

- **Ollama/nimble:** `confidence` is 1 − normalised entropy. I verified this numerically against Ollama's published example.
- **Laya:** also 1 − normalised entropy, and its README says this differs from Jev's [P].
- **OpenAI:** the sample `confidence` values do not match the entropy formula (my check), so the definition is unknown.
- **Strands:** calibrated "confidence" with one temperature per primitive [P].

**Recommendation:** ignore vendor `confidence`. Use the probability vectors, and recalibrate per backend on your own labels. Sundr's certify and monitor steps already need this.

### 7.4 Per-backend limits to enforce before sending

| Backend | Max questions | Choice options | Score levels | Body | Context / state |
|---|---|---|---|---|---|
| Jev | not stated | ≤ 255 | 2–10 | not stated | 32k per Cloudflare [P] |
| Clef / Clef-flash (hosted) | 1–64 | 2–255 | 2–10 | 13 MiB | 65,536 tokens (truncates) |
| Clef self-host (`systemone`) | not stated | as hosted | as hosted | n/a | `max_length` default 16,384 |
| Ollama nimble / tev1 | 1–64 | **2–26** | **2–26** | **64 KiB** | **8,192 tokens per question** |
| Laya | not stated | ≤ 100 (413), plus a token budget of 192/256 | needs descriptions | not stated | 512 / 1024 (8,192 multilingual) |
| Ollaya | 1–256 | 2–255 (laya:en ~125) | 2–10 | 8 MiB | 65,536 tokens |
| Strands Decider | not stated | no cap stated | not stated | not stated | about 4,096 (eval window) |
| OpenAI Decisions | not documented | not documented | not documented | not documented | not documented |

---

## 8. Comparison table

Latency figures are vendor-reported unless marked otherwise. Hosted latencies exclude network time to the client.

| Backend | Hosting | Median latency | Price (input; output free everywhere) | Context | Open weights | Jev-compatible? |
|---|---|---|---|---|---|---|
| **Clef** `@cf/cloudflare/clef` | Cloudflare Workers AI, or self-host | 209 ms (p95 239) [P] | $0.24/M (21,818 neurons/M) [P] | 64k hosted, 16k self-host default | Yes, Apache-2.0, `Cloudflare/clef` (27B) | **Body yes, URL no** (envelope, model name, 64-question cap) |
| **Clef-flash** `@cf/cloudflare/clef-flash` | Workers AI, or self-host | 39 ms (p95 122) [P] | $0.09/M (8,182 neurons/M) [P] | 64k hosted, 16k self-host default | Yes, Apache-2.0, `Cloudflare/clef-flash` (9B) | same as Clef |
| **Strands Decider 2B v21** | local only | 115 ms on RTX 3090; 153 ms on M3 Pro [P] | free (self-host) | about 4k (eval window) | Yes, Apache-2.0, `StrandsAgents/strands-decider-2B-hobson-v21` | `/v1/systemone` route, noul shape [P]; choice/score UNVERIFIED |
| **Laya** (421M / 322M) | local only (CPU OK) | 33 ms per question on T4 [P]; 5.8 ms in Cloudflare's benchmark [P] | free | 512–1024 (8k multilingual) | Yes, Apache-2.0, `convaiinnovations/laya*` | Yes, with documented deviations |
| **Ollama nimble 9B** | local (Ollama ≥ 0.35) | 91 ms on M5 Max [P] | free | 8,192 per question | Yes, Apache-2.0 | Yes; limits 64 questions, 26 options, 64 KiB |
| **Ollama tev1 4B / 0.8B** | local | not published | free | as nimble (UNVERIFIED) | Together AI, experimental; license not checked | Yes |
| **Ollaya** (runtime) | local, port 11435 | depends on model | free | 65,536 state | runtime Apache-2.0; per-model licenses | Yes, claims wire-identical |
| **OpenAI Decisions** `gpt-6-luna` | OpenAI only | ~150 ms claimed [S]; 1.46 s measured [S] | $0.10/M + regional/long-context multipliers [P] | not documented | No | **No** (needs a translation adapter) |
| *Ref: TypeSafe Jev* | TypeSafe | 524 ms (Cloudflare's run) [P]; 70–500 ms (TypeSafe) [P] | $0.042/M [P] | 32k (per Cloudflare) | No | origin |

**Picks for Sundr**

- **Cheapest hosted with open weights and a 64k context:** Clef-flash.
- **Zero-cost local default for development and CI:** Ollama `nimble`. Mind its 26-option and 64 KiB caps. Laya, through `laya-serve` or Ollaya, suits CPU-only or very-low-latency work, but its 512 to 1024 token context limits long states.
- **Fully open, trainable and tiny:** Strands Decider.
- **OpenAI Decisions:** worth an adapter only when an account has access; limits and usage reporting are still undocumented.

---

## 9. Open questions to resolve with live calls

1. Clef requests-per-minute limit, and how a URL / body `model` mismatch behaves.
2. The Clef REST error envelope and code list. Only code 5006 has been seen [S].
3. Strands Decider: the HTTP shapes for `choice` and `score`, and whether `model` is accepted.
4. Laya: the accepted `model` values, and whether omitting the field routes automatically.
5. OpenAI: whether a `usage` field exists, the limits, the refusal payload, and access for this org.
6. Whether ollajev's and Ollaya's Clef builds reproduce the hosted Clef probabilities. Run a contract test on a fixed set of states.

## Sources

Primary:
- Cloudflare blog: https://blog.cloudflare.com/clef-decision-models/
- Workers AI model pages:
  - https://developers.cloudflare.com/workers-ai/models/clef/
  - https://developers.cloudflare.com/workers-ai/models/clef-flash/
- Workers AI schemas:
  - https://developers.cloudflare.com/workers-ai/models/clef/schema-input.json
  - https://developers.cloudflare.com/workers-ai/models/clef/schema-output.json
- Workers AI platform docs:
  - https://developers.cloudflare.com/workers-ai/platform/pricing/
  - https://developers.cloudflare.com/workers-ai/platform/limits/
  - https://developers.cloudflare.com/workers-ai/get-started/rest-api/
- Clef model cards: https://huggingface.co/Cloudflare/clef and https://huggingface.co/Cloudflare/clef-flash
- Clef RL interest form: https://www.cloudflare.com/resource/clef-rl-interest
- Strands Decider: https://github.com/strands-labs/strands-decider and https://huggingface.co/StrandsAgents/strands-decider-2B-hobson-v21
- Laya: https://github.com/NandhaKishorM/laya
- Ollama: https://ollama.com/blog/ollama-now-supports-jev-style-decision-models and https://ollama.com/library/nimble
- Ollaya: https://github.com/ollaya-dev/ollaya and https://github.com/ollaya-dev/ollaya/blob/main/docs/api.md
- ollajev: https://pypi.org/project/ollajev/
- OpenAI: https://developers.openai.com/api/docs/guides/decisions
- TypeSafe: https://docs.typesafe.ai/api.md and https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Pydantic AI: https://pydantic.dev/docs/ai/models/system-one/
- LLM Gateway: https://docs.llmgateway.io/features/system-one
- OpenRouter: https://openrouter.ai/docs/client-sdks/typescript/sdks/systemone/README.md

Secondary [S]:
- Clef hands-on test (envelope, error 5006): https://flaviocopes.com/clef/
- Clef pricing write-up: https://www.developersdigest.tech/blog/cloudflare-clef-decision-models-2026
- Clef VRAM figures: https://www.theregister.com/a/5300649
- OpenAI Decisions coverage:
  - https://www.eesel.ai/blog/openai-decisions-api
  - https://www.aifreeapi.com/en/posts/openai-decisions-api
- Strands Decider coverage: https://siliconangle.com/?p=848982 and https://aiweekly.co/alerts/amazon-ships-strands-decider-2b-an-open-source-jev-rival
- Ollama troubleshooting: https://llmconfigurator.com/en/guides/troubleshooting/ollama-systemone-errors
