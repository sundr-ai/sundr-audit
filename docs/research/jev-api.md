# TypeSafe Jev (System One) API: implementation spec

Researched 2026-10-09. Sources: docs.typesafe.ai (every page in `llms.txt` downloaded), the live OpenAPI spec at `https://api.typesafe.ai/openapi.json`, the `typesafe-sdk` 0.6.0 and 0.7.2 sdists from PyPI (source read, not executed), npm `@typesafe-ai/sdk`, OpenRouter docs and model metadata API, and Vercel AI Gateway docs and models API. There was no API key, so no authenticated call was made. A few **unauthenticated** probes were run to see error shapes and which routes exist; these are marked **OBSERVED**.

Legend:
- **VERIFIED**: stated in a primary source (TypeSafe docs, OpenAPI, SDK source, or gateway docs).
- **OBSERVED**: seen in an unauthenticated live probe on 2026-10-09.
- **INFERRED**: worked out from documented examples. It is not stated anywhere.
- **UNVERIFIED**: not confirmed by any primary source. Treat it as an assumption and test it once you have a key.

---

## 1. Auth, base URL, endpoints

| Item | Value | Status |
|---|---|---|
| Base URL | `https://api.typesafe.ai` | VERIFIED (SDK `DEFAULT_BASE_URL`, docs) |
| Evaluate | `POST /v1/systemone` | VERIFIED |
| List models | `GET /v1/models` | VERIFIED |
| Auth header | `Authorization: Bearer <API_KEY>` (OpenAPI `HTTPBearer`) | VERIFIED |
| Content type | `Content-Type: application/json` | VERIFIED |
| API key env var | `TYPESAFE_API_KEY` | VERIFIED (SDK constants) |
| Other SDK env vars | `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL` (default `jev-latest`), `TYPESAFE_LOG_LEVEL` (`debug`/`info`/`warning`/`error`/`off`) | VERIFIED |
| Request-id response header | `x-typesafe-request-id`, e.g. `req_01a11e85...` | VERIFIED (SDK) + OBSERVED on every response, errors included |
| OpenAPI spec | `https://api.typesafe.ai/openapi.json` (OpenAPI 3.1, `info.version` "0.2.0"). `/docs` redirects. | OBSERVED |
| Keys / console | `https://console.typesafe.ai/keys`. Playground at `console.typesafe.ai/playground`. | VERIFIED |
| Streaming | None. The response is a single JSON body. | VERIFIED (Vercel says "No streaming"). Nothing in the TypeSafe docs mentions streaming. |
| Other headers the SDK sends | `User-Agent: typesafe-sdk/<ver>`, `X-TypeSafe-SDK: typesafe-sdk/<ver>`, `X-TypeSafe-Runtime: python/<ver> (...)`, `X-TypeSafe-Retry-Count: <n>` on retries | VERIFIED (SDK source). The server does not require them. |

`GET /v1/models` returns `{"models":[{"name","description","release_date"}]}`. It currently lists only the aliases. Versioned IDs such as `jev-1.13.0` are accepted even though they are not listed (VERIFIED, models page).

---

## 2. Request body: `POST /v1/systemone`

### 2.1 Top level

| Field | Type | Required | Notes |
|---|---|---|---|
| `state` | `string \| object \| array` | yes | The content every question is judged against. Text only, with no images, audio or video. Must not be `null`. Inner values may be `null`. |
| `model` | `string` | yes | `jev-latest`, `jev-preview`, or the versioned `jev-1.13.0`. See section 9. |
| `questions` | `object<string, Question>` | yes, `minProperties: 1` | You choose the keys, and answers come back under the same keys. The key is **not sent to the model**, so put the full question in `instructions`. |

**No other top-level fields are documented.** The SDK docs use `beam_width` and a per-question `weight` only as made-up examples of the `extra_body` escape hatch. Do not send them. Whether the server rejects unknown fields is UNVERIFIED: the OpenAPI spec does not set `additionalProperties: false` on the request.

### 2.2 Question objects (discriminated by `type`)

There are **no `options`, `levels` or `descriptions` fields.** Options, levels and yes/no descriptions all go in `criteria`, and its shape depends on the type. A "description" is just the value attached to an option or level in `criteria`.

`JSONContent` below means `string | object | array`. Objects and arrays can nest freely. Their field names are your own (e.g. `question`, `focus`, `what`, `not_for`, `examples`). None are reserved, and the model sees both the names and the values.

| Type | Field | Type | Required? | Constraints |
|---|---|---|---|---|
| `noul` (yes/no) | `type` | `"noul"` | yes | |
| | `instructions` | `JSONContent` | API ref: required. OpenAPI and SDK: optional and nullable. OpenRouter Decisions: required. | **Always send it.** |
| | `criteria` | `{"true": JSONContent\|null, "false": JSONContent\|null}` | optional | The keys are the literal JSON strings `"true"` and `"false"`. OpenRouter's Decisions schema requires **both** keys whenever `criteria` is present, so always send both. |
| `choice` | `type` | `"choice"` | yes | |
| | `instructions` | `JSONContent` | send it (same caveat as above) | |
| | `criteria` | `object<option_name, JSONContent \| null>` | **yes** | **Max 255 options** (VERIFIED in docs). The limit is not in the OpenAPI spec, so the server enforces it. `null` means the option is described by its name alone. Option names **are** sent to the model. |
| `score` | `type` | `"score"` | yes | |
| | `instructions` | `JSONContent` | send it | |
| | `criteria` | `array<JSONContent>`, ordered low to high | **yes** | The OpenAPI and SDK minimum is 1 item. Docs say use **at least 2**, and the API **accepts up to 10**. Items may **not** be `null`. Level index = array position, starting at 0. |

Use backtick paths inside `instructions` to point at nested parts of the state, e.g. ``"Does `ticket.messages[0].text` request a refund?"``. This is a documented convention.

### 2.3 Full example: all three types and every optional field

```json
{
  "model": "jev-1.13.0",
  "state": {
    "ticket": {
      "subject": "Charged twice, still can't log in",
      "body": "I was billed twice for March and now the app says my password is wrong. Please fix this today."
    },
    "customer": { "plan": "team", "tenure_months": 14 }
  },
  "questions": {
    "department": {
      "type": "choice",
      "instructions": {
        "question": "Which team should own `ticket`?",
        "focus": "Pick the team that resolves the customer's main problem."
      },
      "criteria": {
        "billing": "Charges, invoices, refunds",
        "account": { "what": "Login, password, permissions", "not_for": "Payment problems" },
        "technical": ["Bugs", "Outages", "Integrations"],
        "other": null
      }
    },
    "frustration": {
      "type": "score",
      "instructions": "How frustrated is the customer in `ticket.body`?",
      "criteria": [
        "Calm, just stating facts",
        { "level": "Frustrated but civil", "examples": ["This is getting annoying."] },
        "Very angry, strong language"
      ]
    },
    "wants_refund": {
      "type": "noul",
      "instructions": "Does `ticket.body` ask for money back?",
      "criteria": {
        "true": "Explicitly asks for a refund, credit, or charge reversal",
        "false": "No request for money back"
      }
    }
  }
}
```

---

## 3. Response body (200)

### 3.1 Schema

| Path | Type | Notes |
|---|---|---|
| `model` | string | The versioned ID that actually answered, e.g. `"jev-1.13.0"`. It can differ from the alias you sent. **Log it.** |
| `answers` | `object<question_id, Answer>` | One entry per question, under your IDs. `minProperties: 1`. |
| `usage.input_tokens` | integer | Billable input tokens. |
| `usage.output_tokens` | integer | Reported, but currently free. |

Each Answer carries `type`, which matches its question:

| `type` | Fields (all required) | Meaning |
|---|---|---|
| `noul` | `noul: number` | P(yes), from 0 to 1. There is **no `confidence` field.** |
| `choice` | `choice: string`, `probabilities: {option: number}`, `confidence: number` | `choice` is the argmax option. `probabilities` covers **every** option, zeros included, and sums to about 1. |
| `score` | `score: number`, `probabilities: {"0": number, ...}`, `legend: {"0": <criteria item>, ...}`, `confidence: number` | `score = Σ i·p_i`, so it can fall between levels. On the wire the keys of `legend` and `probabilities` are **string** level indices. The Python SDK turns them into `int`. |

All of the above is VERIFIED against the OpenAPI spec and the API reference.

Confidence formulas (VERIFIED, `/confidence`):
- Choice: `(p_max − 1/n) / (1 − 1/n)`. Only the top probability counts.
- Score: `max(0, 1 − Σ p_i·|i − m| / MAD_unif)`, where `m` is the argmax level and `MAD_unif = (1/n)·Σ|i − (n−1)/2|`. Mass on far levels costs more than mass on neighbouring levels.
- Noul: no confidence is returned. The docs suggest computing `|2p − 1|` yourself if you want one.

Probabilities in every documented example are rounded to 2 decimals. Whether that rounding is guaranteed is UNVERIFIED.

### 3.2 Example response for the request above

This is **illustrative**: it follows the documented schema, but the numbers are made up.

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "department": {
      "type": "choice",
      "choice": "billing",
      "confidence": 0.47,
      "probabilities": { "billing": 0.6, "account": 0.38, "technical": 0.02, "other": 0.0 }
    },
    "frustration": {
      "type": "score",
      "score": 1.12,
      "confidence": 0.82,
      "legend": {
        "0": "Calm, just stating facts",
        "1": { "level": "Frustrated but civil", "examples": ["This is getting annoying."] },
        "2": "Very angry, strong language"
      },
      "probabilities": { "0": 0.0, "1": 0.88, "2": 0.12 }
    },
    "wants_refund": { "type": "noul", "noul": 0.41 }
  },
  "usage": { "input_tokens": 512, "output_tokens": 61 }
}
```

The response header `x-typesafe-request-id` is always present.

---

## 4. Limits

| Limit | Value | Status |
|---|---|---|
| Total context per request | **64k tokens**: state + **all** questions combined | VERIFIED (models page, Vercel model metadata, OpenRouter `context_length: 64000`) |
| State + longest single question | **32k tokens** | VERIFIED (models page; OpenRouter `max_prompt_tokens: 32000`). The OpenRouter hub page simplifies this to "32,000 tokens" of context. |
| Max questions per request | **No hard cap documented.** The OpenAPI spec has `minProperties: 1` and no max. In practice you are bounded by the 64k budget. The docs show 62 questions in one request, and one Choice with 218 options. | UNVERIFIED (hard cap) |
| Max options per Choice | **255** | VERIFIED (docs; not enforced in the OpenAPI spec) |
| Score levels | at least 2 recommended (minimum accepted is 1), **max 10** | VERIFIED (docs) |
| Max question length | No character limit documented. Bounded only by the 32k (state + longest question) and 64k budgets. | UNVERIFIED |
| Input | Text only: string, JSON object, or array. English is best. Other languages, CJK included, work but are less accurate. | VERIFIED |
| Rate limits (jev-1.13.0) | **100K tokens/s and 80 requests/s.** Going over either returns `429`. The docs warn these "can change without notice" while capacity is added. Higher limits come with custom or enterprise plans. | VERIFIED. Whether the scope is per key or per org is UNVERIFIED. |
| 429 / 529 behaviour | Retry with exponential backoff. The SDK honours **`retry-after`** (seconds, or an HTTP date) and **`retry-after-ms`** headers "when the response carries one". | VERIFIED (docs + SDK `parse_retry_after`). Whether the server actually sends `retry-after` is UNVERIFIED. No `x-ratelimit-*` headers are documented. |
| Practical concurrency | The cookbooks use 4 to 8 concurrent workers, and warn that 8 "is already enough to hit a rate limit on a shared key". | VERIFIED (cookbook guidance) |
| Timeouts | SDK default: **10 s per HTTP operation** (`DEFAULT_TIMEOUT`). `RetryPolicy.timeout` gives a **30 s total** budget per call, delays included. The cookbooks set `timeout=120.0` for large states. | VERIFIED (SDK). The server-side request timeout is UNVERIFIED. |
| Latency | The vendor claims 70 to 500 ms end to end. Cookbooks show 0.27 s for 13 questions over a ~54k-character doc, and 0.51 s for 62 questions. | VERIFIED as a vendor claim |

The error returned when you exceed the context limit (status and body) is **UNVERIFIED**. It is not documented; expect `422` or `400`.

---

## 5. Errors

### 5.1 Documented status codes (`/api`)

| Status | Meaning |
|---|---|
| 401 | Missing or invalid API key |
| 422 | Body failed validation. FastAPI-style `HTTPValidationError` (see below). |
| 429 | Rate limit exceeded. Back off and retry. |
| 529 | Overloaded. Back off and retry. |

### 5.2 Body shapes

**422** (VERIFIED from the OpenAPI spec; not seen live):
```json
{"detail": [{"loc": ["body", "questions", "urgency", "score", "criteria"],
             "msg": "List should have at least 1 item after validation, not 0",
             "type": "too_short", "input": [], "ctx": {"min_length": 1}}]}
```
`loc`, `msg` and `type` are required. `input` and `ctx` are optional. (The `msg`/`type` text above is an example. The OpenAPI spec only shows `"Field required"`/`"missing"`.)

**Auth errors** (OBSERVED 2026-10-09). The bodies differ from the 422 shape:
- **No `Authorization` header → `403`**, not the documented 401. Body: `{"detail":{"error_type":"authentication_error","message":"Must supply an API key! Check your request and try again."}}`. The SDK maps this to `TypeSafePermissionDeniedError`.
- **Invalid key → `401`**. Body: `{"detail":{"error_type":"authentication_error","message":"Cannot authenticate with the server. Please check your API key and try again."}}`
- Auth is checked **before** body validation: an invalid key plus an empty `questions` returned 401, not 422.

**Unknown path → `404`**: `{"detail":"Not Found"}` (OBSERVED).

**429 / 529 bodies**: UNVERIFIED. They are probably `{"detail":{"error_type":..., "message":...}}`.

**Implementation note:** `detail` can be an **object**, an **array** or a **string**, so parse it defensively. The gateways use different envelopes again (section 8).

### 5.3 Python SDK exceptions (0.7.2, VERIFIED source)

```
TypeSafeError                                  # base. Also raised client-side: missing/invalid key format, empty questions, empty score criteria, bad timeout
├── TypeSafeAPIError                           # .status .body .headers .endpoint .request_id
│   ├── TypeSafeBadRequestError        (400)
│   ├── TypeSafeAuthenticationError    (401)
│   ├── TypeSafePermissionDeniedError  (403)   # <- what a missing key actually produces
│   ├── TypeSafeNotFoundError          (404)
│   ├── TypeSafeUnprocessableEntityError (422)
│   ├── TypeSafeRateLimitError         (429)   # .retry_after_ms (float | None)
│   ├── TypeSafeInternalServerError    (5xx, includes 529)
│   └── TypeSafeAPIResponseValidationError     # 200 with malformed body. .field_path e.g. "answers.tone.confidence"
└── TypeSafeAPIConnectionError (also ConnectionError)
    └── TypeSafeAPITimeoutError (also TimeoutError)   # .timeout
```

Default `RetryPolicy`: `max_retries=2`, `backoff_initial=0.5`, `backoff_max=5.0`, `backoff_jitter=0.25`, `http_statuses={408, 429, 500..599}` (so **529 is retried**), `respect_retry_after=True`, `api_connection_error=True`, `api_timeout_error=True`, `timeout=30.0` (total budget). Pass `RetryPolicy(max_retries=0)` to disable retries.

---

## 6. Pricing and billing

| Item | Value | Status |
|---|---|---|
| Input | **$0.042 per Mtok** ($42 per Btok), i.e. **$4.2e-8 per input token** | VERIFIED (models page, launch blog, OpenRouter, Vercel) |
| Output | **Free.** `output_tokens` is reported but not charged. | VERIFIED |
| Cost formula | `cost_usd = usage.input_tokens * 0.042 / 1_000_000` | VERIFIED via gateway examples: 476 tokens → $0.000019992 (OpenRouter), 275 tokens → $0.00001155 (Vercel). Both match exactly. |

**How tokens are counted when many questions share one state:**
- Jev reads the `state` **once per request** and evaluates every question against it in parallel (VERIFIED, models page). You pay for the state once, plus the tokens of each question's `instructions` and `criteria`. Option names and descriptions are part of what is sent.
- Question **IDs are not sent to the model** (VERIFIED). Whether they count toward billed tokens is UNVERIFIED, but probably not.
- Evidence: in the parallel-questions cookbook, 13 questions over a ~54k-character article cost $0.000497 in one call versus $0.006090 as 13 separate calls, **12.2x cheaper** and 10x faster, with identical answers. (The primitives page gives an older figure of 11.5x / 9.6x for the same comparison.)
- **INFERRED fixed overhead per request:** a single short Noul over a one-sentence state reports about 275 to 307 `input_tokens`. Three short questions over the same kind of state report 392. So roughly 250 to 280 tokens of overhead appear to be billed on every request. The practical rule: batch questions, and avoid many tiny requests.
- Gateways price Jev identically: OpenRouter at $0.042/M input and $0 output, and Vercel at `input: 0.000000042, output: 0` with `surchargeCost: "0"` in its example. Any platform fee on buying gateway credits is out of scope here (UNVERIFIED).
- Vercel decision fallbacks bill **both** stages when they trigger.

---

## 7. Python SDK: `typesafe-sdk` (current: **0.7.2**, released 2026-09-26)

- Install with `pip install "typesafe-sdk>=0.7.2,<0.8"`. Add the extra `typesafe-sdk[http2]` for HTTP/2. Requires Python ≥ 3.10. Dependencies: `httpx2>=2.0.0` (note: **httpx2**, not httpx), `pydantic>=2.12`, `pydantic-core>=2.41.1`, `tenacity>=9`, `typing-extensions`.
- Repo: `github.com/typesafe-ai/typesafe-sdk-python`. Import name: `typesafe_sdk`.

### 7.1 Public API (exports are identical in 0.6.0 and 0.7.2)

- Clients:
  - `TypeSafeClient(*, api_key=None, model=None, retry=None, timeout=None, headers=None, transport=None, http_client=None, base_url=None)`
  - `AsyncTypeSafeClient(...)`, with the same parameters and async `transport`/`http_client` types.
  - Explicit arguments take precedence over environment variables.
  - The sync client is a context manager and has `.close()`.
  - The async client supports `async with` and has `await .aclose()`.
- `client.system_one(state, questions, *, model=None, retry=None, timeout=None, extra_headers=None, extra_body=None, response_model=None) -> SystemOneResponse`
  - `state` and `questions` can be passed positionally.
  - `extra_body` is shallow-merged over the top-level body, last write wins.
- `client.models.list(*, retry=None, timeout=None, extra_headers=None) -> ListModelsResponse` returns `.models`, a tuple of `ModelMetadata(name, description, release_date)`.
- Question models (pydantic, `extra="forbid"`):
  - `Noul(instructions=..., criteria=NoulCriteria(true=..., false=...))`
  - `Choice(instructions=..., criteria={...})`
  - `Score(instructions=..., criteria=[...])`
  - Raw dicts (`NoulModel`, `ChoiceModel`, `ScoreModel` TypedDicts) are also accepted, and you can mix them with question objects.
- Response models (pydantic, `frozen=True, strict=True, extra="ignore"`):
  - `SystemOneResponse`: `.model: str`, `.usage: Usage`, `.answers: dict[str, Answer]`
    - Typed views: `.nouls: dict[str, NoulAnswer]`, `.choices: dict[str, ChoiceAnswer]`, `.scores: dict[str, ScoreAnswer]`
    - `.request_id: str` (taken from the `x-typesafe-request-id` header)
    - `.raw_http_response: httpx2.Response`
  - `Usage`: `.input_tokens: int | None`, `.output_tokens: int | None`
  - `NoulAnswer`: `.type`, `.noul: float`
  - `ChoiceAnswer`: `.type`, `.choice: str`, `.confidence: float`, `.probabilities: dict[str, float]`
  - `ScoreAnswer`: `.type`, `.score: float`, `.confidence: float`, `.probabilities: dict[int, float]`, `.legend: dict[int, str | dict | list]`. The SDK converts the keys to **int**.
  - `Answer` is a discriminated union on `type`.
  - Unknown answer types are logged and skipped. Read them from `raw_http_response.json()`.

### 7.2 Sync

```python
import os
from typesafe_sdk import (
    Choice, Noul, NoulCriteria, RetryPolicy, Score, TypeSafeClient,
    TypeSafeAPIError, TypeSafeRateLimitError, TypeSafeAPITimeoutError,
)

QUESTIONS = {
    "department": Choice(
        instructions="Which team should own `ticket`?",
        criteria={"billing": "Charges, invoices, refunds",
                  "account": "Login, password, permissions",
                  "other": None},
    ),
    "frustration": Score(
        instructions="How frustrated is the customer in `ticket.body`?",
        criteria=["Calm", "Frustrated but civil", "Very angry"],
    ),
    "wants_refund": Noul(
        instructions="Does `ticket.body` ask for money back?",
        criteria=NoulCriteria(true="Explicit refund/credit request", false="No money-back request"),
    ),
}

state = {"ticket": {"subject": "Charged twice", "body": "Billed twice for March. Fix today."}}

with TypeSafeClient(
    model="jev-1.13.0",                      # pin; see section 9
    timeout=30.0,                            # per HTTP operation (default 10 s)
    retry=RetryPolicy(max_retries=3, timeout=60.0),
) as client:
    try:
        r = client.system_one(state, QUESTIONS)
    except TypeSafeRateLimitError as e:
        print("429 after retries", e.retry_after_ms, e.request_id)
        raise
    except TypeSafeAPITimeoutError:
        raise
    except TypeSafeAPIError as e:
        print(e.status, e.body, e.request_id)
        raise

dept = r.choices["department"]
print(r.model, r.request_id, r.usage.input_tokens)
print(dept.choice, dept.confidence, dept.probabilities)            # str, float, dict[str, float]
fr = r.scores["frustration"]
print(fr.score, fr.confidence, fr.probabilities[1], fr.legend[2])  # int keys
print(r.nouls["wants_refund"].noul)
cost_usd = (r.usage.input_tokens or 0) * 0.042 / 1_000_000
```

### 7.3 Async (fan-out with bounded concurrency)

```python
import asyncio
from typesafe_sdk import AsyncTypeSafeClient

async def run(states: list[dict]) -> list:
    sem = asyncio.Semaphore(4)               # the cookbooks use 4 to 8 workers
    async with AsyncTypeSafeClient(model="jev-1.13.0", timeout=30.0) as client:
        async def one(s):
            async with sem:
                return await client.system_one(s, QUESTIONS)
        return await asyncio.gather(*(one(s) for s in states))

results = asyncio.run(run([state]))
```

### 7.4 Typed responses (0.7.0 and later only)

```python
from typesafe_sdk import ChoiceAnswer, NoulAnswer, SystemOneResponse

class Triage(SystemOneResponse):
    department: ChoiceAnswer
    wants_refund: NoulAnswer

r = client.system_one(state, QUESTIONS, response_model=Triage)
r.department.choice; r.wants_refund.noul      # plus r.nouls, r.choices, r.request_id ...
```

A `response_model` can also be any pydantic `BaseModel` describing the raw body (`answers: <Model>`). A mismatch raises `TypeSafeAPIResponseValidationError`.

### 7.5 Breaking changes from 0.6.x to 0.7.x (VERIFIED changelog + source diff)

| Version | Date | Change |
|---|---|---|
| 0.5.7 | 2026-09-14 | First public release. `Score.criteria` was a dict keyed by int. |
| **0.6.0** | 2026-09-15 | **Breaking:** `Score.criteria` became an ordered **sequence**. Accepts abstract `Mapping`/`Sequence` inputs. Errors include HTTP details. Exceptions and responses are picklable. Serialisation used **msgspec** (`msgspec>=0.21.1`). |
| **0.7.0** | 2026-09-18 | **Breaking:** serialisation switched from msgspec to **pydantic**. Questions and responses are now pydantic models, not `msgspec.Struct`: `msgspec.structs.asdict`/`msgspec.json.encode` stop working, so use `.model_dump()`. Question models now `extra="forbid"`, so unknown kwargs raise. **New:** `response_model=` on `system_one`. **Fix:** `str` subclasses (e.g. `StrEnum`) are now sent as strings; in 0.6 they could be serialised as a list of characters. |
| 0.7.1 | 2026-09-21 | The API key format is validated at client construction (`TypeSafeError` before any request). The key is excluded from logged exceptions. Gateway examples added to the docs. |
| 0.7.2 | 2026-09-26 | `http2` extra. |

Unchanged across 0.6.0 and 0.7.2: class and exception names, the `system_one` parameters (apart from the added `response_model`), the `.nouls/.choices/.scores/.answers/.usage/.model/.request_id/.raw_http_response` accessors, env vars, and `RetryPolicy`. Recommended pin: `typesafe-sdk>=0.7.2,<0.8`.

### 7.6 JavaScript SDK (`@typesafe-ai/sdk`, latest **0.6.0**, 2026-09-15, Node ≥ 20)

- Usage: `new TypeSafeClient({ apiKey, baseURL, defaultModel, dangerouslyAllowBrowser, ... })`, then `await client.systemOne({ model?, state, questions })`.
- Helper functions `choice()`, `noul()` and `score()` build questions, and answer types are inferred from them.
- It reads the same environment variables as the Python SDK.
- Its only breaking change so far is the same `Score.criteria` sequence change in 0.6.0.

---

## 8. Gateways

Neither gateway serves Jev through **Chat Completions**:
- Vercel says decision models are not supported via Chat Completions, Responses, or the Anthropic- or Cohere-compatible endpoints.
- OpenRouter documents only the two decision surfaces below. Its Jev endpoint lists `supported_parameters: []` and output modality `"decisions"`.
- `typesafe/jev-router` on OpenRouter is a **different product**: a chat-completions router that uses Jev internally to pick another LLM. It is not Jev's decision API.

### 8.1 OpenRouter (VERIFIED from docs; routes OBSERVED to exist: unauthenticated POSTs return 401, not 404)

Model IDs (from OpenRouter's model metadata API):

| ID | Notes |
|---|---|
| **`typesafe/jev-1.13`** | Version family. Context 64000, `max_prompt_tokens` 32000, prompt price `0.000000042`, completion `0`, released 2026-09-18. Responses report the dated snapshot **`typesafe/jev-1.13-20260917`**. |
| **`~typesafe/jev-latest`** | Alias for the newest Jev release. |

`typesafe/jev-1.13.0`, `typesafe/jev` and `~typesafe/jev-preview` return 404 in the metadata API. Jev does **not** appear in the default `/api/v1/models` listing; only `typesafe/jev-router` does.

**Surface A: System One passthrough (TypeSafe-compatible).** Use this to keep the TypeSafe SDK.
- `POST https://openrouter.ai/api/v1/systemone`, header `Authorization: Bearer $OPENROUTER_API_KEY`.
- The request body is identical to TypeSafe's. The SDK appends `/v1/systemone` to `base_url="https://openrouter.ai/api"`.
- Bare IDs are mapped: `jev-1.13` → `typesafe/jev-1.13`, and `jev-latest` → `~typesafe/jev-latest`. Prefixed IDs pass through unchanged. Whether `jev-1.13.0` is mapped is UNVERIFIED.
- The response is TypeSafe's shape **plus** `id`, `provider` and `usage.cost` (USD). For example:
  ```json
  {"id":"gen-dec-...","model":"typesafe/jev-1.13-20260917","provider":"TypeSafe",
   "answers":{"refund":{"type":"noul","noul":0.98}},
   "usage":{"input_tokens":275,"output_tokens":20,"cost":0.00003}}
  ```
  The `cost` of 0.00003 is copied from OpenRouter's docs. It does not match 275 × $4.2e-8 = $0.00001155, so it looks like a placeholder.
- In the SDK: `TypeSafeClient(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api", model="typesafe/jev-1.13")`. The TypeSafe docs show `model="~typesafe/jev-latest"`.
- **`client.models.list()` fails** against OpenRouter, because OpenRouter's `/api/v1/models` has a different shape.

**Surface B: Decisions API (alpha), native OpenRouter.**
- `POST https://openrouter.ai/api/alpha/decisions`. Note this is outside `/api/v1`, so OpenRouter SDK clients need `serverURL: "https://openrouter.ai"`.
- Body: `model`, `state`, `questions` (same question shapes, but `instructions` is required, and Noul `criteria` must contain both `true` and `false`). Optional: `provider` (routing preferences), `session_id` (≤ 256 chars), `trace`, `user` (≤ 256).
- Response example (from OpenRouter's docs):
  ```json
  {"id":"gen-dec-1789738314-...","model":"typesafe/jev-1.13-20260917","provider":"TypeSafe",
   "answers":{"is_bug":{"type":"noul","noul":0.96},
     "team":{"type":"choice","choice":"payments","confidence":0.75,
             "probabilities":{"account":0,"frontend":0.16,"payments":0.84}},
     "urgency":{"type":"score","score":1.99,"confidence":0.99,
             "legend":{"0":"Can wait for the next release","1":"Should be fixed this week","2":"Blocking revenue right now"},
             "probabilities":{"0":0,"1":0.01,"2":0.99}}},
   "usage":{"input_tokens":476,"output_tokens":70,"cost":0.000019992}}
  ```
- Its schema marks `confidence`, `probabilities` and `legend` as **optional**, so default them defensively.
- The OpenRouter SDKs expose this as `open_router.alpha.decisions.create(...)`, and the passthrough as `open_router.system_one.create(...)`.

**OpenRouter errors:** `{"error":{"code":<int>,"message":"..."}}`. A request with no key returned 401 with message "No cookie auth credentials found" (OBSERVED). Documented statuses: 400, 401, **402** (insufficient credits), 403, 404, 413 (payload too large), 429, 500, 502, 503, **524** (edge timeout), 529. OpenRouter's own rate limits for Jev are UNVERIFIED.

### 8.2 Vercel AI Gateway (VERIFIED from docs; route OBSERVED to exist: unauthenticated POST returns 401, `x-matched-path: /typesafe/v1/systemone`)

- Model ID: **`typesafe-ai/jev`**. It is the only Jev ID in the gateway's `/v1/models`. Metadata: `type: "evaluation"`, `context_window: 32000`, a description noting the 64k-total / 32k-state limits, `pricing: {input: "0.000000042", output: "0"}`, `zdr: "some"`, `no_training: "all"`.
- **No versioned Jev ID** is exposed, so you **cannot pin** a Jev version through Vercel. Whether a hidden ID is accepted is UNVERIFIED.
- Auth: `Authorization: Bearer $AI_GATEWAY_API_KEY`, or a Vercel OIDC token. BYOK is supported if you add a TypeSafe key, in which case TypeSafe bills you directly.

**Surface A: TypeSafe-compatible API.**
- Base URL `https://ai-gateway.vercel.sh/typesafe`. Routes: `POST /typesafe/v1/systemone` and `GET /typesafe/v1/models`.
- The request body is TypeSafe's. The response is TypeSafe's shape plus `provider_metadata`:
  ```json
  {"model":"typesafe-ai/jev",
   "answers":{"refund":{"type":"noul","noul":0.98}},
   "usage":{"input_tokens":275,"output_tokens":20},
   "provider_metadata":{"gateway":{"routing":{"originalModelId":"typesafe-ai/jev","resolvedProvider":"typesafe-ai",
       "canonicalSlug":"typesafe-ai/jev","finalProvider":"typesafe-ai"},
     "cost":"0.00001155","marketCost":"0.00001155","surchargeCost":"0","gatewayCost":"0.00001155","generationId":"gen_..."}}}
  ```
  `model` echoes `typesafe-ai/jev`, not the version that answered. `cost` is a **string**.
- In the SDK: `TypeSafeClient(api_key=os.environ["AI_GATEWAY_API_KEY"], base_url="https://ai-gateway.vercel.sh/typesafe", model="typesafe-ai/jev")`.
- **Errors:** a flat `{"message": "...", "error_type": "..."}`, with no `detail` wrapper. That is different from TypeSafe direct, even though Vercel calls it "TypeSafe's shape". OBSERVED 401: `{"message":"Authentication failed","error_type":"authentication_error"}`. Errors from the provider are passed through unchanged.
- **Decision fallbacks** (a Vercel-only extension, effectively a server-side confidence cascade):
  - Add `"providerOptions": {"gateway": {"models": [{"model": "<other model>", "when": <condition>}]}}`.
  - Conditions:
    - `{question?, confidenceBelow: x}`, for Choice and Score
    - `{question?, probabilityBetween: [lo, hi]}`, for Noul and Boolean
    - `{any: [...]}` and `{all: [...]}`
  - If the condition matches, the whole request is rerun on the fallback model, and both stages are billed and added up.
  - Response headers: `x-ai-gateway-decision-fallback-triggered`, `-final-model`, `-primary-model`, `-triggering-questions`, `-triggering-questions-truncated`.
  - If the fallback is an LLM, Choice and Score answers may come back with `confidence: 0` and `probabilities: {}`, which means the values are *unavailable*, not that confidence is zero.
- Rate limits for Jev through Vercel are UNVERIFIED.

**Surface B: generic Decision API.**
- `POST https://ai-gateway.vercel.sh/v1/evaluate`. Also available through the AI SDK's `experimental_decide` (`ai` ≥ 7.0.128) or `gateway.decisionModel("typesafe-ai/jev")`.
- Differences from TypeSafe's shape:
  - Noul is called **`type: "boolean"`**, and its answer is `{"type":"boolean","probability":0.98}`.
  - Usage is camelCase: `{"inputTokens","outputTokens"}`.
  - Metadata is under `providerMetadata`.
- Supports `providerOptions.gateway` (`zeroDataRetention`, `only`, `models`).
- An OpenAI-compatible Decisions API also exists at `https://ai-gateway.vercel.sh/v1`, with `predicate`/`choice`/`score` question types. It is not examined further here.

### 8.3 Pydantic AI Gateway (from the TypeSafe SDK docs, briefly)

`base_url="https://gateway-us.pydantic.dev/proxy/typesafe"`, `model="jev-latest"`, key in `PYDANTIC_AI_GATEWAY_API_KEY`. Nothing else was checked.

---

## 9. Version pinning and determinism

**Model IDs on TypeSafe direct** (VERIFIED, models page, last reviewed 2026-10):

| Name | Points to | Notes |
|---|---|---|
| `jev-1.13.0` | itself | The only versioned ID documented. Accepted even though `GET /v1/models` doesn't list it. Its rate, context and price limits are the ones above. |
| `jev-latest` | `jev-1.13.0` | Latest stable release. The SDK default. **Moves without notice when a new release ships.** |
| `jev-preview` | `jev-1.13.0` | Latest release, preview or not. There is currently no preview build. |

- **Pin `jev-1.13.0`** wherever you have tuned confidence thresholds. TypeSafe recommends this explicitly. Log the response's `model` to record which version answered.
- The docs are inconsistent here:
  - The jaggedness page uses `TypeSafeClient(model="jev-1.13")`.
  - The SDK usage page uses `model="jev"`.
  - Most cookbooks use `jev-1.12`, which predates 1.13.
  
  Whether `api.typesafe.ai` accepts `jev-1.13`, `jev` or `jev-1.12` is **UNVERIFIED**. Only use the three IDs in the table.
- Through gateways, pin with OpenRouter's `typesafe/jev-1.13`. That ID still resolves to the latest dated 1.13 snapshot, currently `-20260917`. Vercel exposes only the unpinned `typesafe-ai/jev`.
- Jev is not fine-tuned per customer. Every account uses the same weights, so customisation happens only through `state`, `instructions` and `criteria`.

**Determinism** (VERIFIED behaviour notes; no guarantee is documented anywhere):
- There are no `temperature`, `seed` or sampling parameters.
- Results are **mostly but not fully deterministic.**
  - In TypeSafe's parallel-questions cookbook, 11 of 13 answers were identical across 5 repeats (std dev 0.0). Two Nouls had small run-to-run noise, with std devs of about 0.005 to 0.008.
  - In the self-consistency cookbook (15 repeats, with a throwaway `uid` field changed in the state each time), the mean per-question std dev was **0.0102**. One Noul ranged from 0.43 to 0.53, which crosses a 0.5 threshold.
  - OpenRouter's tutorial says the probabilities "will differ slightly from run to run". The same request in two OpenRouter examples gave 0.84 and 0.78 for the same option.
- **Batching does not change answers.** Questions are evaluated independently and cannot see each other's answers. Adding or removing questions does not change the others.
- The **order of Choice options can bias the answer**: jev-1.13 leans toward the first option (jaggedness #8). For important decisions, check consistency by permuting the options.
- To make decisions stable, use confidence bands or human review near thresholds, not a single cut-off. Whether the server caches identical requests is UNVERIFIED.

**Known jev-1.13 weak spots** (jaggedness page, reviewed 2026-10-02): literal reading; maths and counting (do them in code); comparing dates and times; multi-hop indirection; large irrelevant state ("context rot", so filter first); adversarial content in state; contradictory instructions and criteria; option-order bias; generating text (it is not a generative model).

---

## 10. Cascade and confidence-routing patterns (for Sundr's design)

- **TypeSafe confidence-gated routing:** act automatically when confidence is high, confirm or flag in the middle, and go to a human or fallback when it is low. The example thresholds are a 0.6 floor and >0.85 for high-stakes actions. For Noul, threshold `noul` directly, sending values in a middle band (e.g. 0.3 to 0.7) to review.
- **TypeSafe SDE cascade:**
  1. A cheap LLM extracts the fields.
  2. Jev asks one Noul per field, worded so that "true" means "this field is wrong".
  3. Escalate to the expensive model if **any** P(wrong) > 0.7.
- **OpenRouter Jev-verified cascade:**
  1. A cheap model drafts an answer.
  2. Jev asks a Choice of `supported` / `unsupported` / `declined` against the retrieved context.
  3. Send the draft if it is `supported` with confidence ≥ 0.8. Hand off if `declined`. Otherwise escalate once to the frontier model and verify again.
- **Vercel decision fallbacks:** the same idea run server-side (section 8.2).

---

## 11. UNVERIFIED and contradiction checklist (test these once you have a key)

1. Status and body for exceeding the context limit (64k total / 32k state + question).
2. Whether there is a hard cap on questions per request.
3. Whether there is a character limit per question.
4. 429 and 529 bodies, whether `retry-after` is actually sent, and whether rate limits apply per key or per org.
5. Whether the server rejects unknown request fields, or ignores them.
6. Whether `instructions` is really optional server-side. The docs conflict, so always send it.
7. Whether model IDs `jev-1.13`, `jev` and `jev-1.12` are accepted directly, and whether OpenRouter maps `jev-1.13.0`.
8. Missing key returns **403**, while the docs say 401 (OBSERVED). Handle both.
9. Whether question IDs count toward billed `input_tokens`, and the size of the per-request overhead (inferred at ~250 to 280 tokens).
10. Whether probabilities are always rounded to 2 decimals.
11. Whether identical requests are cached server-side.
12. Whether Vercel accepts any versioned Jev ID. Gateway-specific rate limits.

## 12. Sources

- TypeSafe docs: https://docs.typesafe.ai/llms.txt, /api.md, /models.md, /primitives.md, /primitives/{choice,score,noul,advanced}.md, /confidence.md, /concepts/state.md, /model-jaggedness/jev-1.13.md, /patterns/{fan-out,confidence-routing}.md, /cookbooks/{parallel_questions,consistency_noul_cookbook,semantic_find,sde_cascade,classifying_rag_passages,autoresearch_feature_discovery}.md, /sdk/python*.md (usage, changelog, constants, clients, types, retries, exceptions), /sdk/javascript*.md
- OpenAPI: https://api.typesafe.ai/openapi.json
- Launch post: https://typesafe.ai/blog/introducing-system-one-models-and-jev (byline 2026-09-15)
- PyPI JSON: https://pypi.org/pypi/typesafe-sdk/json (sdists 0.6.0 and 0.7.2 read). GitHub: https://github.com/typesafe-ai/typesafe-sdk-python
- npm: https://registry.npmjs.org/@typesafe-ai/sdk
- OpenRouter: https://openrouter.ai/docs/guides/community/typesafe-sdk.md, /docs/guides/community/jev.md, /docs/guides/community/jev-tutorial.md, /docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request.md, /docs/client-sdks/python/sdks/{systemone,decisions}/README.md, /docs/cookbook/evaluate-and-optimize/jev-verified-cascade.md, /docs/guides/routing/routers/jev-router.md; model metadata https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints and /~typesafe/jev-latest/endpoints; model page https://openrouter.ai/typesafe/jev-1.13
- Vercel: https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe.md, /docs/ai-gateway/modalities/decision.md, /docs/ai-gateway/models-and-providers/decision-fallbacks.md; models https://ai-gateway.vercel.sh/v1/models
