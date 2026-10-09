# LLM log export formats and structured-output wire shapes

Research for **Sundr**, which ingests logged LLM "judgment" calls (classification, routing, LLM-as-judge) that return structured JSON. Researched 2026-10-09.

**How to read this document**

- **Verification tags.**
  - `[DOC]`: stated in the vendor's official docs.
  - `[SPEC]`: stated in the vendor's official OpenAPI spec.
  - `[SRC]`: read in the vendor's official source code. This is how it actually behaves, but it is not a documented contract.
  - **UNVERIFIED**: not confirmed in any official source.
- **Example records** use values that are illustrative but follow the documented schema, unless the text says otherwise. None of them were captured from live traffic.
- **Sources** are listed at the end of each section.

---

## 0. Decisions that change what we build

| # | Finding | Impact |
|---|---|---|
| 1 | **Langfuse Cloud is removing its v1 read endpoints on 2026-11-16.** The endpoints are `GET /api/public/observations`, `/observations/{id}`, `/traces`, `/traces/{id}` and `/sessions`. [DOC] | Build the Langfuse connector against `GET /api/public/v2/observations`, which uses cursor pagination, field groups, and returns `input`/`output` as raw strings. Use v1 only for self-hosted Langfuse v3. |
| 2 | **LangSmith deprecated `POST /api/v1/runs/query` and `client.list_runs` at the end of July 2026.** They will be removed on 2027-01-31. [DOC] | Build against `POST /api/v2/runs/query`. Its `selects` values are uppercase, and **if `selects` is omitted it returns only `ID`**. `min_start_time` silently defaults to 1 day ago. |
| 3 | **Braintrust's `/v1/project_logs/{id}/fetch` endpoint no longer accepts filters.** [SPEC] | Use `POST /btql` with a SQL query, and paginate with the `x-bt-cursor` response header. |
| 4 | **Helicone stores request and response bodies in one of two places, depending on `storage_location`.** [SRC] | Use `POST /v1/request/query-clickhouse`. Read bodies inline when `storage_location == "clickhouse"`. Otherwise download them from the 24-hour presigned `signed_body_url`. |
| 5 | **Anthropic:** `output_config.format` is the current structured-output parameter (GA, no beta header). `output_format` is deprecated. **Forced `tool_choice` (`any`/`tool`) returns HTTP 400 on Claude Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1.** [DOC] | The parser must handle three Anthropic patterns: JSON in a `text` block, `strict` tool `input`, and legacy forced-tool `input`. On current models, `content` can start with `thinking` blocks, so **never read `content[0]`**. |
| 6 | **Token accounting differs by source.** OpenAI's `prompt_tokens`/`input_tokens` *include* cached tokens. Anthropic's `input_tokens` *excludes* cache reads and writes. Langfuse *subtracts* detail buckets from `input`. | Section 5.4 gives one normalization rule per source. Otherwise, cross-source cost and token comparisons are wrong. |
| 7 | **Observability tools often lose the response schema.** Langfuse Python skips a Responses `text.format` schema. Chat Completions doesn't echo `response_format` in its response. LangChain puts the schema in `extra.options`. | Read `response_schema` from wherever it survives (see §6.3). Otherwise infer the verdict field from the data (§6.5). |

---

## 1. Langfuse

### 1.1 Export paths

| Path | Formats | Notes |
|---|---|---|
| **UI table "Export" button** | Docs say CSV and JSON [DOC]. The source code also offers **JSONL** [SRC]. | The tables are `traces`, `observations`, `events` (the v4 observations table), `sessions`, `scores`, `dataset_run_items`, `dataset_items` and `audit_logs` [SRC]. Table filters apply, but the export always includes every column [DOC]. The export is **asynchronous**: you get an email, and the file is behind a presigned S3 link. Observation export column names are not documented; the code uses camelCase domain fields plus `traceName`, `traceTags`, `userId`, `modelId`, `inputPrice`/`outputPrice`/`totalPrice`, one column per score, and `comments`. The exact columns are **UNVERIFIED**. Self-hosted row limit: `BATCH_EXPORT_ROW_LIMIT` = 1,500,000 [DOC]. Cloud limit: **UNVERIFIED**. |
| **Scheduled blob-storage export** | `JSON`, `CSV`, `JSONL`, `PARQUET` (default for new integrations). Optional gzip for the text formats. [DOC][SPEC] | Requires Pro + Teams, Enterprise, or self-hosting. Destinations: S3, S3-compatible storage (including GCS through its S3 interoperability endpoint) and Azure Blob. Set `exportSource: OBSERVATIONS_V2`. Frequency: `every_20_minutes`, `hourly`, `daily` or `weekly`. Field groups: `core` (required), `basic`, `time`, `io`, `metadata`, `model`, `usage`, `prompt`, `metrics`, `tools`, `trace_context`. |
| **Public API v2** | JSON | `GET /api/public/v2/observations`. **Primary path for Sundr.** |
| **Public API v1 (deprecated)** | JSON | `GET /api/public/observations`, `GET /api/public/traces`. Self-hosted v3 only, after 2026-11-16. |

**How to consume blob exports** [DOC]
- Files land at `{prefix}{projectId}/observations_v2/{ts}.{fmt}[.gz]`, `.../scores/...` and `.../manifests/{ts}.json`.
- A run is complete only once its manifest exists. Get the file list from `manifest.files[].key`; don't list directories.
- Adjacent windows share their boundary, so deduplicate rows by `id`.
- Timestamps are formatted `YYYY-MM-DD HH:MM:SS.ffffff` in UTC.
- Null strings are written as `""`, and a null `total_cost` as `0`.
- `metrics.latency` is in **seconds** for integrations created on or after 2026-04-01, and **milliseconds** for older ones.

Blob export `observations_v2` columns are snake_case [DOC]:

| Group | Columns |
|---|---|
| `core` | `id`, `trace_id`, `project_id`, `type`, `parent_observation_id`, `start_time`, `end_time` |
| `basic` | `environment`, `is_root_observation`, `name`, `level`, `status_message`, `version`, `user_id`, `session_id`, `bookmarked`, `public` |
| `io` | `input`, `output` (both strings) |
| `metadata` | `metadata` |
| `model` | `provided_model_name`, `model_parameters` (JSON string), `model_id`, `input_price`, `output_price`, `total_price` |
| `usage` | `usage_details`, `cost_details`, `total_cost`, `usage_pricing_tier_id`, `usage_pricing_tier_name` |
| `prompt` | `prompt_id`, `prompt_name`, `prompt_version` |
| `metrics` | `latency`, `time_to_first_token` |
| `tools` | `tool_calls`, `tool_call_names`, `tool_definitions` |
| `trace_context` | `trace_name`, `tags`, `release` |

### 1.2 API basics

- **Base URLs** [DOC]:
  - EU: `https://cloud.langfuse.com/api/public`
  - US: `https://us.cloud.langfuse.com/api/public`
  - JP: `https://jp.cloud.langfuse.com/api/public`
  - HIPAA: `https://hipaa.cloud.langfuse.com/api/public`
  - Self-hosted: `{host}/api/public`
- **Auth:** HTTP Basic, with the public key `pk-lf-…` as the username and the secret key `sk-lf-…` as the password [DOC].
- **Limits:**
  - Requests and responses are each capped at 5 MB, so keep `limit` at about 200–500 when requesting `io`.
  - Rate limits apply per organization. v2 observations allows 30/min on Hobby, 100/min on Core and 1,000/min on Pro or higher.
  - A 429 response includes a `Retry-After` header [DOC].

### 1.3 `GET /api/public/v2/observations` (use this)

| Param | Notes |
|---|---|
| `fields` | Comma-separated groups: `core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context`. Defaults to `core,basic`. **Request all of them.** The `tools` group is not available through the API. [DOC][SPEC] |
| `expandMetadata` | Comma-separated metadata keys. Metadata values over **200 characters are truncated** unless their key is listed here. **Pass `response_format,text_format`** so the schema survives. [DOC] |
| `type` | `GENERATION` for LLM calls. Other values: `SPAN`, `EVENT`, `AGENT`, `TOOL`, `CHAIN`, `RETRIEVER`, `EVALUATOR`, `EMBEDDING`, `GUARDRAIL`. |
| `name`, `userId`, `sessionId`, `traceId`, `version`, `level`, `parentObservationId`, `isRootObservation`, `environment[]` | Exact-match filters. |
| `fromStartTime` / `toStartTime` | ISO 8601. The range is `>=` / `<`. Always set both. |
| `filter` | URL-encoded JSON array of `{type, column, operator, value, key?}`. Takes precedence over the plain params. |
| `limit`, `cursor` | `limit` defaults to 50, maximum 1000. Follow `meta.cursor`; it is absent on the last page. |

- **Response:** `{ "data": ObservationV2[], "meta": { "cursor"?: string } }`, sorted by `startTime` descending.
- `input` and `output` are **raw strings**. Run `JSON.parse` on them, and fall back to plain text if parsing fails [DOC].
- There is no get-by-id route; filter on `id` instead.
- Data from Python SDK < 4.7.0 or JS SDK < 5.4.0 can lag by up to 15 minutes [DOC].

**Deprecated v1 endpoints**
- `GET /api/public/observations`
  - Params: `page` (1-based), `limit` (≤100), `name`, `userId`, `type`, `traceId`, `level`, `parentObservationId`, `environment`, `fromStartTime`, `toStartTime`, `version`, `filter`.
  - Response: `{data, meta:{page,limit,totalItems,totalPages}, _deprecation?}`.
  - `input`/`output` come back as parsed JSON. There are no `userId`/`sessionId` fields on observations.
- `GET /api/public/traces`: lists traces. `observations` and `scores` are returned as **ID arrays**. `GET /traces/{id}` embeds the full objects. [SPEC]

### 1.4 Observation fields (v2 unless noted)

| Field | Type | Notes |
|---|---|---|
| `id`, `traceId`, `parentObservationId`, `projectId` | string | |
| `type` | enum | `GENERATION` for LLM calls |
| `name` | string | **The Python OpenAI integration defaults to `"OpenAI-generation"`** [SRC]. Treat that value as generic. |
| `startTime`, `endTime`, `completionStartTime`, `createdAt`, `updatedAt` | ISO 8601 | |
| `input`, `output` | v2: string; v1: any | See §1.5 for the shapes. |
| `metadata` | object | Holds `response_format` / `text_format` from the Python integration. |
| `model` | string \| null | The model name **as provided by the caller**. `providedModelName` is the export column, filter alias and metrics dimension, not an API field. |
| `modelParameters` | object | Python always fills in `temperature`, `max_tokens`, `top_p`, `frequency_penalty` and `presence_penalty`, **using defaults when the caller didn't set them** [SRC]. The JS integration also puts `response_format` here. |
| `usageDetails` | map string→int | **Use this.** Keys are `input`, `output`, `total` and `input_*` / `output_*` buckets. **The bucket values are subtracted from `input`/`output`** [DOC]. |
| `costDetails` | map string→number (USD) | **Use this.** Keys include `input`, `output` and `total`. |
| `totalCost` | number \| null | v2 only. |
| `inputPrice`, `outputPrice`, `totalPrice` | v2: decimal string; v1: number | USD per unit. |
| `promptId`, `promptName`, `promptVersion` | | Langfuse Prompt Management link. Useful as a **task** key. |
| `latency`, `timeToFirstToken` | number (seconds) | |
| `level`, `statusMessage` | | Errors show up as `level: "ERROR"` plus `statusMessage`. |
| `userId`, `sessionId`, `environment`, `traceName`, `tags`, `release`, `isRootObservation` | | v2 only (`basic` and `trace_context` groups). |
| `usage` `{input,output,total,unit}`, `calculatedInputCost`, `calculatedOutputCost`, `calculatedTotalCost`, `promptTokens`, `completionTokens`, `totalTokens` | | **Deprecated, v1 only.** `usage.input` is the sum of every `usageDetails` key containing "input" [SRC]. |

**Trace fields (v1 only)**
- `id`, `timestamp`, `name`, `input`, `output`, `sessionId`, `release`, `version`, `userId`, `metadata`, `tags`, `public`, `environment`, `htmlPath`, `latency` (s), `totalCost` (USD), `observations`, `scores` [SPEC].
- Langfuse v4 has no trace objects. Group observations by `traceId` and use the root observation for trace-level input and output [DOC].

### 1.5 How OpenAI calls appear in `input` and `output`

These shapes come from the SDK source [SRC]. The docs don't describe them.

| Integration / API | `input` | `output` | Where the schema is |
|---|---|---|---|
| Python `langfuse.openai`, Chat Completions | A bare list `[{role, content}, …]`. With tools: `{"tools": […], "messages": […]}`. | `{"role":"assistant","content":"<JSON string>"}`, plus `tool_calls` when present. With `n` > 1, a list. `parsed` is not stored. | `metadata.response_format`. A Pydantic class is stored as its JSON schema. Whether it comes back as an object or a string is **UNVERIFIED**. |
| Python, Responses API | The raw `input`. `instructions` is prepended as `{"role":"system", …}`. With tools: `{input, tools, tool_choice, parallel_tool_calls}`. | Output item(s): a single item unwrapped, several as a list. | `metadata.text_format` for `.parse(text_format=…)`. **A `text={"format":…}` schema is NOT captured.** |
| JS `observeOpenAI`, Chat | Always `{messages, tools?, tool_choice?, …}`. | The full `choices[0].message`. | `modelParameters.response_format` |
| JS, Responses | `args.input` | `output_text` when non-empty, otherwise the output items. | **UNVERIFIED** |
| OTel / OpenInference | Taken from the first of `langfuse.observation.input`, `gen_ai.prompt`, `input.value`. | Depends on the instrumentation. **UNVERIFIED**. | Unmapped attributes go to `metadata.attributes.*`. |

**Parsing note.** A judgment verdict is double-encoded. You parse `output` (a string in v2), then parse `output.content` again, because it is a JSON string inside JSON.

### 1.6 Example GENERATION record (v2)

Request:
`GET /api/public/v2/observations?type=GENERATION&fromStartTime=2026-10-07T00:00:00Z&toStartTime=2026-10-08T00:00:00Z&fields=core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context&expandMetadata=response_format&limit=500`

```json
{
  "data": [{
    "id": "a1b2c3d4e5f60718",
    "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
    "projectId": "clx9proj0000",
    "parentObservationId": "f0e1d2c3b4a59687",
    "type": "GENERATION",
    "isRootObservation": false,
    "name": "ticket-classifier",
    "level": "DEFAULT",
    "statusMessage": null,
    "version": null,
    "environment": "production",
    "userId": "user-123",
    "sessionId": null,
    "startTime": "2026-10-07T14:03:21.512Z",
    "endTime": "2026-10-07T14:03:22.424Z",
    "completionStartTime": null,
    "input": "[{\"role\":\"system\",\"content\":\"Classify the ticket. Reply in JSON.\"},{\"role\":\"user\",\"content\":\"I was charged twice.\"}]",
    "output": "{\"role\":\"assistant\",\"content\":\"{\\\"label\\\":\\\"billing\\\",\\\"rationale\\\":\\\"Duplicate charge.\\\"}\"}",
    "metadata": {
      "response_format": "{\"type\":\"json_schema\",\"json_schema\":{\"name\":\"ticket_label\",\"strict\":true,\"schema\":{\"type\":\"object\",\"properties\":{\"label\":{\"type\":\"string\",\"enum\":[\"billing\",\"bug\",\"how_to\",\"other\"]},\"rationale\":{\"type\":\"string\"}},\"required\":[\"label\",\"rationale\"],\"additionalProperties\":false}}}"
    },
    "model": "gpt-4o-2024-08-06",
    "modelParameters": { "temperature": 0, "top_p": 1, "frequency_penalty": 0, "presence_penalty": 0 },
    "usageDetails": { "input": 412, "output": 18, "total": 430, "input_cached_tokens": 0, "output_reasoning_tokens": 0 },
    "costDetails": { "input": 0.00103, "output": 0.00018, "total": 0.00121 },
    "totalCost": 0.00121,
    "inputPrice": "0.0000025",
    "outputPrice": "0.00001",
    "totalPrice": null,
    "promptId": null, "promptName": null, "promptVersion": null,
    "latency": 0.912,
    "timeToFirstToken": null,
    "traceName": "classify-ticket",
    "tags": ["support"],
    "release": null
  }],
  "meta": { "cursor": "eyJsYXN0U3RhcnRUaW1lVG8iOi..." }
}
```

How the same record differs on v1:
- `input` and `output` are parsed JSON, and prices are numbers.
- It adds `usage:{unit:"TOKENS",input,output,total}` and the `calculated*Cost` fields.
- The envelope is `meta:{page,limit,totalItems,totalPages}` plus `_deprecation`.

**Sources:**
- https://langfuse.com/docs/api-and-data-platform/features/public-api
- https://langfuse.com/faq/all/deprecated-api-migration
- https://langfuse.com/docs/api-and-data-platform/features/export-from-ui
- https://langfuse.com/docs/api-and-data-platform/features/export-to-blob-storage
- https://langfuse.com/docs/api-and-data-platform/features/blob-storage-export-fields
- https://langfuse.com/faq/all/api-limits
- https://langfuse.com/docs/observability/features/token-and-cost-tracking
- https://cloud.langfuse.com/generated/api/openapi.yml
- Source: github.com/langfuse/langfuse (`packages/shared/src/features/batchExport/types.ts`, `worker/src/features/database-read-stream/observation-stream.ts`), github.com/langfuse/langfuse-python `langfuse/openai.py`, github.com/langfuse/langfuse-js `packages/openai/src/parseOpenAI.ts`

---

## 2. Helicone

### 2.1 Export paths

| Path | Formats | Notes |
|---|---|---|
| **UI Requests page, "Export data" button** | **CSV or JSONL** (no JSON option) [SRC] | Exports **only the rows currently loaded or selected**, capped at 500 (`MAX_EXPORT_ROWS`) [SRC]. Rows are the UI's `MappedLLMRequest` objects, **not** `HeliconeRequest` records. Their top-level keys are `_type`, `id`, `schema`, `preview`, `model`, `raw{request,response}` and `heliconeMetadata{requestId, createdAt, promptTokens, completionTokens, reasoningTokens, promptCacheReadTokens, promptCacheWriteTokens, latency, cost, customProperties, user, status, provider, promptId, …}`. In CSV, nested objects are JSON-stringified. Not suitable for bulk ingestion. |
| **`POST /v1/request/query-clickhouse`** | JSON | **Primary path for Sundr** [DOC][SRC]. |
| **`npx @helicone/export`** (official CLI) | `json`, `jsonl` (default), `csv` | Wraps `query-clickhouse`. Flags: `--start-date`, `--end-date`, `--limit`, `--format`, `--include-body`, `--property key=value`, `--region us\|eu`, `--batch-size` (1000), `--resume`. It writes a checkpoint to `.helicone-export-state.json`. **Its CSV has no `response_body` column**, so use JSONL. [DOC][SRC] |
| **HQL** (SQL over ClickHouse; selected workspaces) | CSV via `POST /v1/helicone-sql/download` | Limits: 300k rows per query, 30 s timeout [DOC]. |
| `POST /v1/request/query` | JSON | Legacy Postgres route. Bodies are always placeholders, and there are no cost or cache fields [SRC]. Avoid it. |
| `GET /v1/request/{id}?includeBody=true` | JSON | Single-record lookup with the body inline. Only searches the last 90 days [SRC]. |

### 2.2 API basics

- **Base URLs:** `https://api.helicone.ai` (US), `https://eu.api.helicone.ai` (EU) [DOC].
- **Auth headers:** `Authorization: Bearer <HELICONE_API_KEY>` and `Content-Type: application/json`.
- **The key needs write (`w`) permission even for the POST query routes.** A read-only `r` key gets a 401 [SRC].
- **Success status:** the query routes return **HTTP 201** [SRC].

**Request body: `RequestQueryParams`** [SPEC]

| Field | Notes |
|---|---|
| `filter` (required) | A filter tree, or the string `"all"`. Every leaf **must** be wrapped in `request_response_rmt`; otherwise the result is silently `[]` [DOC]. Combine leaves with `{left, operator:"and"\|"or", right}`. |
| `limit` | 0–1000, default 10. |
| `offset` | Default 0. |
| `sort` | e.g. `{"created_at":"asc"}`. |
| `isCached` | **Ignored** by `query-clickhouse`. Filter with `cached:{equals:false}` instead [SRC]. |
| `includeInputs` | Accepted but unused [SRC]. |
| `isScored` | Filters scored or unscored requests. |

Useful leaf fields:
- Time: `request_created_at`
- Identity and routing: `model`, `user_id`, `provider`, `status`, `prompt_id`
- Usage and cost: `cost` (USD), `prompt_tokens`, `completion_tokens`, `latency`
- Properties: `properties:{<key>:{equals}}`
- Scores: `scores:{<key>:{op}}`

**Response:** `{"data": HeliconeRequest[], "error": null}`, or `{"data": null, "error": "<msg>"}` on failure.

Example request body (window plus property filter):
```json
{
  "filter": {
    "left": {
      "left":  { "request_response_rmt": { "request_created_at": { "gte": "2026-09-01T00:00:00Z" } } },
      "operator": "and",
      "right": { "request_response_rmt": { "request_created_at": { "lt": "2026-09-02T00:00:00Z" } } }
    },
    "operator": "and",
    "right": { "request_response_rmt": { "properties": { "task": { "equals": "ticket-classifier" } } } }
  },
  "limit": 1000,
  "offset": 0,
  "sort": { "created_at": "asc" }
}
```

### 2.3 Bodies: inline or signed URL [SRC]

| `storage_location` | `request_body` / `response_body` | `signed_body_url` |
|---|---|---|
| `"clickhouse"` (bodies ≤ 10 MB; the usual case for recent data) | Full parsed JSON. If ClickHouse has no value, this is the placeholder object `{"helicone_message":"fetching body from signed_url..."}`. | absent |
| `"s3"` (bodies > 10 MB, **and all rows older than the column**, since its default is `'s3'`) | A truncated, ASCII-only **preview string**, not JSON. | Presigned GET, **expires in 24 h**, no auth needed. Returns `{"request": {...}, "response": {...}}`. |
| `"not_stored_exceeded_free"` | placeholder | absent |

**Streaming responses.** `response_body` holds the consolidated completion plus a `streamed_data` array of raw chunks. Drop `streamed_data`.

**Retention risk.** The self-hosted ClickHouse migrations put a 3-month TTL on the body columns. Whether Helicone Cloud does the same is **UNVERIFIED**.

### 2.4 `HeliconeRequest` fields returned by `query-clickhouse`

[SRC: `packages/llm-mapper/types.ts` plus the swagger spec.] Treat every field as optional. ClickHouse Int64 columns may come back as JSON strings, so coerce numbers.

| Field | Notes |
|---|---|
| `request_id` (uuid), `request_created_at` (ISO) | |
| `request_body` | The provider request JSON, e.g. OpenAI `{model, messages, response_format, …}`. |
| `response_id`, `response_created_at`, `response_status` | |
| `response_body` | The provider response JSON. |
| `model` | Computed: override → `request_model` → response model → `"unknown"`. |
| `request_model` | |
| `provider` | e.g. `"OPENAI"`, `"ANTHROPIC"`, or lowercase gateway names. |
| `target_url` | e.g. `https://api.openai.com/v1/chat/completions`. Use it to decide which API dialect the bodies are in. |
| `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `prompt_cache_read_tokens`, `prompt_cache_write_tokens` | Whether `prompt_tokens` includes cached tokens for each provider is **UNVERIFIED**. Prefer recomputing from `response_body.usage` (§5.4). |
| `total_tokens` | prompt + completion + **reasoning**, which may double-count for OpenAI (**UNVERIFIED**). Don't use it. |
| `cost` | **USD** (stored ×1e9 internally and divided back). There is **no `cost_usd` field**; `costUSD` exists only on the legacy route. |
| `delay_ms`, `time_to_first_token` | Latency in ms. |
| `request_user_id` | From the `Helicone-User-Id` header. |
| `request_properties` (and the duplicate `properties`) | Map of string to string (§2.5). |
| `scores` | `{name: number}`. User feedback appears as `scores["helicone-score-feedback"]` = 1 or 0. There is no `feedback` field on this route. |
| `prompt_id`, `prompt_version` | Prompts-2025 ids, `""` when none. These are **not** the `Helicone-Prompt-Id` header. |
| `cache_reference_id`, `cache_enabled` | `00000000-0000-0000-0000-000000000000` means not a cache hit. |
| `storage_location`, `signed_body_url`, `asset_ids`, `asset_urls` | See §2.3. |
| `country_code`, `request_referrer`, `updated_at`, `ai_gateway_body_mapping` | |
| Not returned by this route | `request_path`, `model_override`, `response_model`, `helicone_user`, `llmSchema`, `feedback_*` |

### 2.5 Custom properties, sessions and prompt IDs [SRC][DOC]

- **`Helicone-Property-<Name>: v`** is stored as `request_properties["<name>"] = "v"`. **The key is lowercased.** Filters are case-sensitive.
- **Session headers keep their casing:**
  - `request_properties["Helicone-Session-Id"]`
  - `["Helicone-Session-Path"]`
  - `["Helicone-Session-Name"]`
- **`Helicone-Prompt-Id`** (legacy header) is stored as `request_properties["Helicone-Prompt-Id"]`. **Good task key.**
- **System-added keys** to drop when treating properties as labels: `Helicone-Experiment-Id`, `Helicone-Cache-*`, `Helicone-Sent-To-Posthog`, `stripe_customer_id`.
- **User ID:** `Helicone-User-Id` is stored as `request_user_id`.

### 2.6 Example record: OpenAI Chat with `json_schema`, from `query-clickhouse`

```json
{
  "request_id": "5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33",
  "request_created_at": "2026-10-08T14:03:20.870Z",
  "request_body": {
    "model": "gpt-4o-mini",
    "temperature": 0,
    "messages": [
      { "role": "system", "content": "Classify the support ticket. Return JSON only." },
      { "role": "user", "content": "I was charged twice for my subscription, please refund one." }
    ],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "ticket_label",
        "strict": true,
        "schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["label", "confidence", "rationale"],
          "properties": {
            "label": { "type": "string", "enum": ["billing", "bug", "how_to", "other"] },
            "confidence": { "type": "number" },
            "rationale": { "type": "string" }
          }
        }
      }
    }
  },
  "response_id": "8f0c2a7e-3d1b-4c55-9a8e-2b6f1d0e4a11",
  "response_created_at": "2026-10-08T14:03:21.512Z",
  "response_status": 200,
  "response_body": {
    "id": "chatcmpl-AbC123",
    "object": "chat.completion",
    "created": 1759932201,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [{
      "index": 0,
      "finish_reason": "stop",
      "logprobs": null,
      "message": {
        "role": "assistant",
        "refusal": null,
        "content": "{\"label\":\"billing\",\"confidence\":0.93,\"rationale\":\"Mentions refund for duplicate charge.\"}"
      }
    }],
    "usage": {
      "prompt_tokens": 412, "completion_tokens": 38, "total_tokens": 450,
      "prompt_tokens_details": { "cached_tokens": 0 },
      "completion_tokens_details": { "reasoning_tokens": 0 }
    }
  },
  "model": "gpt-4o-mini-2024-07-18",
  "request_model": "gpt-4o-mini-2024-07-18",
  "provider": "OPENAI",
  "target_url": "https://api.openai.com/v1/chat/completions",
  "delay_ms": 642,
  "time_to_first_token": 0,
  "prompt_tokens": 412,
  "completion_tokens": 38,
  "reasoning_tokens": 0,
  "prompt_cache_read_tokens": 0,
  "prompt_cache_write_tokens": 0,
  "total_tokens": 450,
  "cost": 0.0000846,
  "request_user_id": "user-12345",
  "request_properties": {
    "environment": "production",
    "task": "ticket-classifier",
    "Helicone-Session-Id": "3c0e5a1f-0000-4000-8000-000000000001",
    "Helicone-Session-Path": "/triage/classify",
    "Helicone-Session-Name": "Ticket triage",
    "Helicone-Prompt-Id": "ticket_classifier"
  },
  "scores": { "helicone-score-feedback": 1 },
  "prompt_id": "",
  "prompt_version": "",
  "cache_reference_id": "00000000-0000-0000-0000-000000000000",
  "cache_enabled": false,
  "country_code": "US",
  "asset_ids": [],
  "storage_location": "clickhouse",
  "updated_at": "2026-10-08T14:03:22.004Z"
}
```

**Ingest loop**
1. Call `POST /v1/request/count/query` to size the window.
2. Page through `query-clickhouse` with `limit:1000` and `sort:{created_at:"asc"}`, over fixed windows that end a few minutes before now. Use windows rather than deep offsets.
3. Remove duplicate `request_id`s across pages.
4. Resolve the bodies as described in §2.3.
5. Drop cache hits (`cached:{equals:false}`).
6. Dispatch on `target_url` to the provider parser in §5.

**Sources:**
- Docs: https://docs.helicone.ai/rest/request/post-v1requestquery-clickhouse, https://docs.helicone.ai/tools/export, https://docs.helicone.ai/guides/cookbooks/etl, https://docs.helicone.ai/guides/cookbooks/getting-sessions, https://docs.helicone.ai/features/hql, https://docs.helicone.ai/helicone-headers/header-directory, https://docs.helicone.ai/features/advanced-usage/custom-properties
- Source (github.com/Helicone/helicone, `main`):
  - `valhalla/jawn/src/tsoa-build/public/swagger.json`
  - `valhalla/jawn/src/controllers/public/requestController.ts`
  - `valhalla/jawn/src/lib/handlers/LoggingHandler.ts`
  - `valhalla/jawn/src/middleware/auth.ts`
  - `packages/llm-mapper/types.ts`
  - `packages/filters/filterDefs.ts`
  - `worker/src/lib/models/HeliconeHeaders.ts`
  - `examples/export/typescript/index.ts`
  - `web/components/shared/themed/table/exportButton.tsx`
  - `web/lib/constants.ts`

---

## 3. LangSmith

- **Base URLs:** `https://api.smith.langchain.com`, or `https://eu.api.smith.langchain.com` for EU.
- **Auth:** the `X-Api-Key` header. Bulk export also sends `X-Tenant-Id: <workspace_id>` [DOC][SPEC].

### 3.1 Export paths

| Path | Formats | Notes |
|---|---|---|
| **`POST /api/v2/runs/query`** | JSON | **Primary path.** Rate limit: 300 requests per 10 s per key [DOC]. |
| `POST /api/v1/runs/query`, `client.list_runs` | JSON | **Deprecated** since late July 2026; removed 2027-01-31 (self-hosted: deprecated in v0.16, removed in v0.18). |
| **Bulk data export** | **Parquet only** (`zstandard` by default; also `gzip`, `snappy`, `none`) | Enterprise plan; accounts created before 2026-08-03 keep it on Plus until 2027-02-01. Writes to S3 or S3-compatible storage. Steps: create a destination with `POST /api/v1/bulk-exports/destinations`, create a job with `POST /api/v1/bulk-exports` (`session_id`, `start_time`, `end_time`, optional `filter` and `export_fields`, `format_version: "v2_beta"` for timezone-aware timestamps), then poll `GET /api/v1/bulk-exports/{id}`. Compare statuses case-insensitively: `Created`, `Running`, `Completed`, `Failed`, `Cancelled`, `TimedOut`, `IntervalScheduled`. [DOC][SPEC] |
| **CLI** | JSONL | `langsmith run export llm_calls.jsonl --project my-app --run-type llm --full`. Defaults to a 7-day window; widen it with `--since` [DOC]. |
| **Dataset export** | CSV, JSONL, OpenAI fine-tuning format | UI: Dataset → ⋮ → Download. API: `GET /api/v1/datasets/{id}/csv`, `/jsonl`, `/openai_ft` or `/openai` (optional `as_of`). Rows contain `id`, `inputs`, `outputs`, `metadata`, `source_run_id`, … [DOC]. The exact `openai_ft` line format is **UNVERIFIED**. |

**Bulk export details**
- **Exportable columns:** `id`, `tenant_id`, `session_id`, `trace_id`, `parent_run_id`, `parent_run_ids`, `reference_example_id`, `name`, `run_type`, `start_time`, `end_time`, `status`, `is_root`, `dotted_order`, `inputs`, `outputs`, `error`, `extra`, `events`, `tags`, `feedback_stats`, `feedbacks` (opt-in), `total_tokens`, `prompt_tokens`, `completion_tokens`, `total_cost`, `prompt_cost`, `completion_cost`, `first_token_time` [DOC].
- Whether `inputs`/`outputs`/`extra` are Parquet strings or structs is **UNVERIFIED**; handle both.
- **Path layout:** the docs disagree, so glob for both `.../session_id=<id>/runs/year=…` and `.../session_id=<id>/resource=runs/year=…`.

### 3.2 `POST /api/v2/runs/query` body [SPEC][DOC]

| Field | Notes |
|---|---|
| `project_ids` | uuid[]. UUIDs only, no names. Resolve a name with `GET /api/v1/sessions?name=<n>&limit=1`. Alternatively use `reference_dataset_id`. |
| `run_type` | **Uppercase**: `LLM`, `CHAIN`, `TOOL`, `RETRIEVER`, `EMBEDDING`, `PROMPT`, `PARSER`. |
| `min_start_time` / `max_start_time` | RFC 3339. **`min_start_time` defaults to 1 day ago**; always set it. |
| `filter` / `trace_filter` / `tree_filter` | Filter query language. Comparators: `eq neq gt gte lt lte has search in`, combined with `and()` / `or()`. Fields: `id name run_type status start_time end_time latency tags metadata_key metadata_value feedback_key feedback_score`. Example: `and(eq(metadata_key,"ls_model_name"), eq(metadata_value,"gpt-4o-mini"))`. You cannot filter on nested `inputs`/`outputs` paths. |
| `is_root`, `has_error`, `ids`, `trace_id` | |
| `selects` | **Uppercase enum[]. If omitted, only `ID` is returned.** For Sundr: `ID NAME RUN_TYPE STATUS START_TIME END_TIME INPUTS OUTPUTS EXTRA METADATA ERROR TAGS TRACE_ID PARENT_RUN_IDS IS_ROOT PROMPT_TOKENS COMPLETION_TOKENS TOTAL_TOKENS PROMPT_TOKEN_DETAILS COMPLETION_TOKEN_DETAILS PROMPT_COST COMPLETION_COST TOTAL_COST FIRST_TOKEN_TIME FEEDBACK_STATS REFERENCE_EXAMPLE_ID THREAD_ID` |
| `page_size`, `cursor` | Default 100, maximum 1000. Follow `next_cursor`. |

- **Response:** `{"items":[Run…], "next_cursor": "<opaque>" | null}`, sorted by `start_time` descending.
- **SDK (Python ≥ 0.10.15):** `client.runs.query(project_ids=[...], run_type="LLM", min_start_time=..., selects=[...])` is async [DOC].

**Changes from v1 to v2** [DOC]:
- `session_id` becomes `project_id`.
- `parent_run_id` is replaced by `parent_run_ids` (root first).
- `serialized` becomes `manifest`.
- `extra.metadata` is also returned as top-level `metadata`.
- `run_type` and `status` values are uppercase.
- Costs are **numbers** (v1 returned decimal strings).
- `*_token_details` / `*_cost_details` are wrapped as `{raw:{…}}`.

**v1 compatibility shape:** body `session[]`, lowercase `run_type`, `start_time`, `filter`, `select`, `limit` ≤ 100, `cursor`. Response `{runs, cursors:{next}}` [SPEC].

### 3.3 Run fields

| Field | Notes |
|---|---|
| `id`, `trace_id` (= root run id), `parent_run_ids`, `dotted_order` | |
| `name` | **`"ChatOpenAI"` for both LangChain `ChatOpenAI` and `wrap_openai`** [SRC]. Generic, so use the parent run's name for the task. |
| `run_type` | `llm` (v1) / `LLM` (v2) |
| `start_time`, `end_time`, `first_token_time` | v1 datetimes have no tz suffix and are UTC (**UNVERIFIED**); v2_beta bulk export is tz-aware. |
| `status` | `success`, `error`, `pending` (uppercase in v2) |
| `inputs`, `outputs`, `error` | See §3.4. |
| `extra` | `metadata`, `invocation_params`, `options`, `batch_size`, `runtime` |
| `metadata` (v2) | Same as `extra.metadata`. |
| `tags`, `feedback_stats` (e.g. `{"correctness":{"n":1,"avg":1.0}}`), `reference_example_id` | |
| `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_cost`, `completion_cost`, `total_cost` | Rolled up server-side from `usage_metadata`. |

**`usage_metadata` is not a top-level field.** It lives at `extra.metadata.usage_metadata` or `outputs.usage_metadata`, with keys `input_tokens`, `output_tokens`, `total_tokens`, `input_token_details{cache_read, cache_creation}` and `output_token_details{reasoning}` [DOC][SRC].

**Where the model name is:**
1. `extra.metadata.ls_model_name` (also `metadata.ls_model_name` in v2), with `ls_provider` alongside.
2. Fallback: `extra.metadata.ls_invocation_params.model`.
3. Fallback: `extra.invocation_params.model`.

These are the *requested* model. The resolved snapshot is `outputs.model` for `wrap_openai`, or `outputs.llm_output.model_name` for LangChain [DOC][SRC].

### 3.4 Two LLM run shapes [SRC]

| | LangChain `ChatOpenAI` / `ChatAnthropic` | `langsmith.wrappers.wrap_openai` |
|---|---|---|
| Detect by | `outputs.generations` present | `outputs.choices` present |
| `inputs` | `{"messages": [[<lc-serialized msg>, …]]}`, a **list of lists** with one inner list per batch item | The `create()` kwargs: `{model, messages:[{role,content}], temperature, response_format, …}` |
| `outputs` | `LLMResult`: `{generations:[[{text, generation_info{finish_reason}, message:{lc AIMessage}}]], llm_output:{token_usage, model_name, …}, type:"LLMResult"}` | ChatCompletion `model_dump()` with `usage` replaced by `usage_metadata` |
| Schema | `with_structured_output` writes the schema to `extra.options.ls_structured_output_format.schema` (`kwargs.method` is `json_schema` or `function_calling`) | `inputs.response_format`, and `extra.metadata.ls_invocation_params.response_format` |
| JSON output | `generations[0][0].message.kwargs.content` (json_schema mode), `…kwargs.tool_calls[0].args` (function-calling mode, **already an object**), or `additional_kwargs.parsed` | `outputs.choices[0].message.content` (string) |

- **Other shapes:** `outputs.messages` means LangSmith's native typed-block format [DOC]. Anything else is custom `@traceable` I/O.
- **Serialized LangChain message:** `{"lc":1,"type":"constructor","id":["langchain","schema","messages","HumanMessage"],"kwargs":{"content":"…","type":"human"}}`. Map roles from `kwargs.type`: `system`, `human`→`user`, `ai`→`assistant`, `tool`.

### 3.5 Example run (v1 / bulk-export shape, LangChain `ChatOpenAI` + `with_structured_output`)

This example was built from the SDK source.

```json
{
  "id": "0ec6b845-18b9-4aa1-8f1b-6ba3f9fdefd6",
  "name": "ChatOpenAI",
  "run_type": "llm",
  "start_time": "2026-09-14T17:02:11.120000",
  "end_time": "2026-09-14T17:02:11.904000",
  "status": "success",
  "error": null,
  "tags": [],
  "trace_id": "0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c11",
  "parent_run_id": "a8024e23-7a4f-4bb1-8f0e-2b7f3c1d9e22",
  "session_id": "1ffd059c-3e8a-4f2b-9c6d-5a4b3c2d1e33",
  "dotted_order": "20260914T170211100000Z0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c11.20260914T170211120000Z0ec6b845-18b9-4aa1-8f1b-6ba3f9fdefd6",
  "inputs": { "messages": [[
    { "lc": 1, "type": "constructor", "id": ["langchain","schema","messages","SystemMessage"], "kwargs": { "content": "Classify the ticket. Return JSON {label}.", "type": "system" } },
    { "lc": 1, "type": "constructor", "id": ["langchain","schema","messages","HumanMessage"], "kwargs": { "content": "My card was charged twice", "type": "human" } }
  ]] },
  "outputs": {
    "generations": [[{
      "text": "{\"label\":\"billing\"}",
      "generation_info": { "finish_reason": "stop", "logprobs": null },
      "type": "ChatGeneration",
      "message": { "lc": 1, "type": "constructor", "id": ["langchain","schema","messages","AIMessage"],
        "kwargs": { "content": "{\"label\":\"billing\"}", "additional_kwargs": { "refusal": null },
          "response_metadata": { "finish_reason": "stop", "model_name": "gpt-4o-mini-2024-07-18" },
          "type": "ai", "tool_calls": [], "invalid_tool_calls": [],
          "usage_metadata": { "input_tokens": 52, "output_tokens": 6, "total_tokens": 58 } } }
    }]],
    "llm_output": { "token_usage": { "prompt_tokens": 52, "completion_tokens": 6, "total_tokens": 58 }, "model_name": "gpt-4o-mini-2024-07-18" },
    "run": null,
    "type": "LLMResult"
  },
  "extra": {
    "invocation_params": { "model": "gpt-4o-mini", "temperature": 0.0 },
    "options": { "ls_structured_output_format": { "kwargs": { "method": "json_schema" },
      "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["billing","bug","other"] } }, "required": ["label"] } } },
    "batch_size": 1,
    "metadata": { "ls_provider": "openai", "ls_model_name": "gpt-4o-mini", "ls_model_type": "chat", "ls_temperature": 0.0,
      "usage_metadata": { "input_tokens": 52, "output_tokens": 6, "total_tokens": 58 } }
  },
  "prompt_tokens": 52, "completion_tokens": 6, "total_tokens": 58,
  "prompt_cost": "0.0000078", "completion_cost": "0.0000036", "total_cost": "0.0000114",
  "first_token_time": null,
  "feedback_stats": {},
  "reference_example_id": null
}
```

For a `wrap_openai` run the envelope is the same; only the I/O differs:
```json
{
  "inputs":  { "model": "gpt-4o-mini", "messages": [{ "role": "system", "content": "…" }, { "role": "user", "content": "…" }], "temperature": 0,
               "response_format": { "type": "json_schema", "json_schema": { "name": "Label", "strict": true, "schema": { "type": "object", "properties": { "label": { "type": "string" } }, "required": ["label"], "additionalProperties": false } } } },
  "outputs": { "id": "chatcmpl-…", "object": "chat.completion", "model": "gpt-4o-mini-2024-07-18",
               "choices": [{ "index": 0, "message": { "role": "assistant", "content": "{\"label\":\"billing\"}", "refusal": null, "tool_calls": null }, "finish_reason": "stop", "logprobs": null }],
               "usage_metadata": { "input_tokens": 52, "output_tokens": 6, "total_tokens": 58 } }
}
```

On **v2**, the same run comes back with these differences:
- `run_type: "LLM"`, `status: "SUCCESS"`.
- `project_id` instead of `session_id`, and `parent_run_ids` instead of `parent_run_id`.
- Top-level `metadata`.
- Numeric costs.

Still **UNVERIFIED**:
- How a Pydantic `response_format` passed to `.parse()` is serialized into `inputs`.
- The TypeScript `wrapOpenAI` output shape.

**Sources:**
- https://docs.langchain.com/langsmith/run-data-format
- https://docs.langchain.com/langsmith/export-traces
- https://docs.langchain.com/langsmith/trace-query-syntax
- https://docs.langchain.com/langsmith/smithdb-sdk-migration
- https://docs.langchain.com/langsmith/smithdb-sdk-migration-query-runs
- https://docs.langchain.com/langsmith/data-export
- https://docs.langchain.com/langsmith/ls-metadata-parameters
- https://docs.langchain.com/langsmith/manage-datasets
- https://docs.langchain.com/langsmith/langsmith-cli
- https://docs.langchain.com/langsmith/langsmith-platform-openapi.json
- Source: `langsmith-sdk/python/langsmith/wrappers/_openai.py`, `langchain_core/tracers/langchain.py`

---

## 4. Braintrust

- **Base URLs:** `https://api.braintrust.dev`, or `https://api-eu.braintrust.dev` for EU.
- **Auth:** `Authorization: Bearer <API key>` [DOC][SPEC].

### 4.1 Export paths

| Path | Formats | Notes |
|---|---|---|
| **UI**: Logs page → filter → export icon | **JSON or CSV** | Only visible to users with Update or Delete permission [DOC]. CSV column layout is **UNVERIFIED**. |
| **`POST /btql`** | `fmt`: `json` (default), `jsonl`, `parquet` | **Primary path** (details below). |
| `GET`/`POST /v1/project_logs/{project_id}/fetch` | JSON | Body: `limit` (counts **traces**, not rows), `cursor`, `version`. **No `filters` parameter.** Response: `{"events":[…], "cursor": str\|null}`. Pages can repeat older versions of a row, so dedupe on `id` and keep the highest `_xact_id` [SPEC]. |
| **CLI** | NDJSON | `bt sync pull project_logs:<name-or-id> --window 7d [--filter <SQL expr>]` writes under `bt-sync/` [DOC]. |
| **Cloud storage export** (Enterprise) | `.jsonl.gz` or `.parquet` | Configured as a project automation with `event_type: "btql_export"`. Output goes to `{prefix}/date=YYYY-MM-DD/<uuid>.jsonl.gz` [DOC]. |

**`POST /btql` details**
- **Body:** `{"query": "<SQL>", "fmt": "json"}`.
- **Response:** `{"data":[rows], "schema":{…}}`.
- **Pagination:** the cursor comes back in the **`x-bt-cursor` response header**. Append `OFFSET '<cursor>'` to the next query and stop when no rows come back [DOC].
- **Limits:** 30 s timeout; about 20 requests/min on Starter/Pro [DOC KB].
- **Query requirements:**
  - Every `project_logs()` query needs a range on `created`/`_xact_id`/`_pagination_key`, or an `id`/`root_span_id` predicate.
  - Order by `_pagination_key`. Numeric `OFFSET` is unsupported.
  - Add `SETTINGS preview_length = -1` so long values aren't truncated [DOC].

```sql
SELECT * FROM project_logs('<project-id>', shape => 'spans')
WHERE span_attributes.type = 'llm' AND created > now() - interval 30 day
ORDER BY _pagination_key DESC
LIMIT 1000
SETTINGS preview_length = -1
```

### 4.2 Span (`ProjectLogsEvent`) fields [SPEC][DOC]

| Field | Notes |
|---|---|
| `id` | Row identity; dedupe on this. |
| `span_id`, `root_span_id` (= trace id), `span_parents` (empty for a root span), `is_root` | |
| `_xact_id`, `_pagination_key`, `created` (ISO) | |
| `org_id`, `project_id`, `log_id` | `log_id` is always `"g"` for project logs. |
| `input`, `output`, `expected`, `error` | any |
| `scores` | `{name: number 0–1 \| null}` |
| `metadata` | `metadata.model` is the only declared key. Wrappers add request params here (§4.3). |
| `metrics` | Declared: `start`, `end` (unix seconds), `prompt_tokens`, `completion_tokens`, `tokens`. Also documented: `prompt_cached_tokens`, `prompt_cache_creation_tokens`, `completion_reasoning_tokens`, `time_to_first_token` (s), `estimated_cost` (only if logged), `cached`. The SQL function `estimated_cost()` computes cost when it wasn't logged. |
| `span_attributes` | `name`, `type` (`llm`, `task`, `function`, `tool`, `score`, `eval`, …), `purpose`, `log_level` |
| `context` | `caller_functionname`, `caller_filename`, `caller_lineno` |
| `tags`, `origin`, `comments`, `audit_data`, `facets`, `classifications` | |

### 4.3 What `wrap_openai` / `wrapOpenAI` logs

[DOC for the overview, SRC for the details.]
- **Span:** named **`"Chat Completion"`**, type `llm`. The name is generic, so use the parent span name for the task.
- **`input`:** the `messages` list as sent.
- **`output`:** the response `choices` list, unchanged.
- **`metadata`:** `{"provider":"openai", "model": <requested>, temperature, response_format, tools, tool_choice, seed, reasoning_effort, …}`. Python uses an allowlist; TypeScript copies every param except `messages`.
- **The resolved response model is not logged** in Python. Whether TypeScript logs it is **UNVERIFIED**.
- **Streaming:** token metrics are recorded only with `stream_options.include_usage=true`.
- **Still UNVERIFIED:** whether `output` contains `message.parsed` when the call used `.parse()`.

### 4.4 Example span (built from the spec plus SDK source)

```json
{
  "id": "385052b6-50a2-43b4-b52d-9afaa34f0bff",
  "span_id": "70b04fd2-0177-47a9-a70b-e32ca43db131",
  "root_span_id": "70b04fd2-0177-47a9-a70b-e32ca43db131",
  "span_parents": [],
  "is_root": true,
  "_xact_id": "1000196123456789",
  "_pagination_key": "p07384512345678901",
  "created": "2026-09-14T17:02:11.120Z",
  "org_id": "5d2c1a0b-0000-4000-8000-000000000000",
  "project_id": "d709efc0-ac9f-410d-8387-345e1e5074dc",
  "log_id": "g",
  "input": [
    { "role": "system", "content": "Classify the ticket. Return JSON {label}." },
    { "role": "user", "content": "My card was charged twice" }
  ],
  "output": [{
    "index": 0, "finish_reason": "stop", "logprobs": null,
    "message": { "role": "assistant", "content": "{\"label\":\"billing\"}", "refusal": null, "tool_calls": null }
  }],
  "expected": null, "error": null, "scores": null, "tags": null,
  "metadata": {
    "provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
    "response_format": { "type": "json_schema", "json_schema": { "name": "Label", "strict": true,
      "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["billing","bug","other"] } }, "required": ["label"], "additionalProperties": false } } }
  },
  "metrics": { "start": 1757869331.120, "end": 1757869331.904, "prompt_tokens": 52, "completion_tokens": 6, "tokens": 58,
               "prompt_cached_tokens": 0, "completion_reasoning_tokens": 0, "time_to_first_token": 0.78 },
  "context": { "caller_functionname": "classify", "caller_filename": "/app/classify.py", "caller_lineno": 42 },
  "span_attributes": { "name": "Chat Completion", "type": "llm" },
  "origin": null
}
```

**Sources:**
- https://www.braintrust.dev/docs/api-reference/query
- https://www.braintrust.dev/docs/reference/sql
- https://www.braintrust.dev/docs/reference/sql/best-practices
- https://braintrust.dev/docs/annotate/export
- https://braintrust.dev/docs/admin/data-management/export
- https://www.braintrust.dev/docs/reference/cli/sync
- https://braintrust.dev/docs/instrument/advanced-tracing
- https://braintrust.dev/docs/integrations/ai-providers/openai
- https://braintrust.dev/docs/kb/btql-post-endpoint-payload-and-response-schema
- https://www.braintrust.dev/docs/openapi.json
- Source: `braintrust-sdk-python/py/src/braintrust/integrations/openai/tracing.py`, `braintrust-sdk-javascript/js/src/instrumentation/plugins/openai-plugin.ts`

---

## 5. Provider wire formats for structured output

Every tool above eventually stores one of these three dialects. In Helicone, `target_url` tells you which one; elsewhere you detect it from the shape of the bodies.

The shared example is a spam/ham judge with this schema (`SPAM_SCHEMA`):
```json
{ "type": "object",
  "properties": { "label": { "type": "string", "enum": ["spam", "ham"] }, "reasoning": { "type": "string" } },
  "required": ["label", "reasoning"],
  "additionalProperties": false }
```

### 5.1 OpenAI Chat Completions: `POST /v1/chat/completions`

**Requesting structured output** [SPEC][DOC]. `response_format` takes one of three forms:

| Form | Shape | Notes |
|---|---|---|
| Text | `{"type":"text"}` | The default. |
| **JSON schema** | `{"type":"json_schema","json_schema":{"name":str,"description"?:str,"schema":object,"strict"?:bool}}` | **Nested under `json_schema`.** `name` matches `[a-zA-Z0-9_-]`, max 64 characters. `strict` defaults to `false`. |
| JSON mode | `{"type":"json_object"}` | Valid JSON, but no schema. The word "JSON" must appear somewhere in the context. |

**Alternative: strict function calling**
- Tool definition: `tools:[{"type":"function","function":{"name","description"?,"parameters":<schema>,"strict":true}}]`.
- Force the call with `tool_choice:{"type":"function","function":{"name":"…"}}` (or `"required"`), with `parallel_tool_calls:false`.

**Message roles** (`messages[]`):

| Role | Content | Extra fields |
|---|---|---|
| `developer` | string, or `text` parts | Replaces `system` on o1 and newer models. |
| `system` | string, or `text` parts | |
| `user` | string, or `text` / `image_url` / `input_audio` / `file` parts | |
| `assistant` | string or null | `refusal`, `tool_calls` |
| `tool` | string | `tool_call_id` |
| `function` | | Deprecated. |

**Where the output is** [SPEC][DOC]:
- **Schema or JSON mode:** `choices[i].message.content` is a **JSON-encoded string**; parse it.
- **Refusal:** `message.refusal` is a string and `content` is `null` or missing. The documented example has `finish_reason: "stop"`.
- **Tool calls:** `message.tool_calls[].function.arguments` is a **JSON-encoded string**, and `content` is usually null. Detect tool calls by a non-empty `tool_calls`, not by `finish_reason`; whether a forced call reports `"stop"` or `"tool_calls"` is **UNVERIFIED**.
- **`finish_reason`** is one of `stop`, `length` (likely truncated JSON), `tool_calls`, `content_filter`, `function_call` (deprecated).
- **Top-level response fields:** `id` (`chatcmpl-…`), `object:"chat.completion"`, `created` (unix s), `model` (the resolved snapshot), `choices`, `usage`, `service_tier`. `system_fingerprint` is marked deprecated.
- **The response does not echo `response_format`**, so the schema must come from the logged request.
- **SDK-only fields** never appear on the wire: `message.parsed` and `parsed_arguments` from `client.chat.completions.parse()`.

**Usage** [SPEC]:
- `prompt_tokens`, `completion_tokens`, `total_tokens`.
- `prompt_tokens_details{cached_tokens, audio_tokens, text_tokens, image_tokens, cache_write_tokens}`.
- `completion_tokens_details{reasoning_tokens, audio_tokens, accepted_prediction_tokens, rejected_prediction_tokens}`.
- Detail fields may be absent.
- Cached tokens are a subset of prompt tokens: the caching guide computes uncached = input − cached − cache_write. Reasoning tokens are billed as output and fall within the output count [DOC].

Request:
```json
{
  "model": "gpt-4o-mini",
  "messages": [
    { "role": "developer", "content": "Classify the email as spam or ham." },
    { "role": "user", "content": [{ "type": "text", "text": "WIN A FREE CRUISE!!! Click now" }] }
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "spam_classification",
      "description": "Spam verdict",
      "strict": true,
      "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["spam","ham"] }, "reasoning": { "type": "string" } }, "required": ["label","reasoning"], "additionalProperties": false }
    }
  }
}
```
Response:
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1760000000,
  "model": "gpt-4o-mini-2024-07-18",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "{\"label\":\"spam\",\"reasoning\":\"Unsolicited prize offer with urgent call to action.\"}", "refusal": null, "annotations": [] },
    "logprobs": null,
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 61, "completion_tokens": 18, "total_tokens": 79,
    "prompt_tokens_details": { "cached_tokens": 0, "audio_tokens": 0 },
    "completion_tokens_details": { "reasoning_tokens": 0, "audio_tokens": 0, "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0 }
  },
  "service_tier": "default"
}
```
Tool-call variant of the response message:
`{"role":"assistant","content":null,"tool_calls":[{"id":"call_abc","type":"function","function":{"name":"record_verdict","arguments":"{\"label\":\"ham\",\"reasoning\":\"…\"}"}}]}`

### 5.2 OpenAI Responses API: `POST /v1/responses`

**Requesting structured output** [SPEC][DOC]:
- Use `text.format`, which is **flat** (`name` and `schema` sit next to `type`): `{"type":"json_schema","name":str,"schema":object,"description"?:str,"strict"?:bool}`. Other forms: `{"type":"json_object"}`, `{"type":"text"}`.
- The spec says `strict` defaults to `false`, but the schema is tagged `json_schema_default_strict`. Which one an omitted `strict` actually follows is **UNVERIFIED**.
- `instructions` (string) acts as the system/developer message.
- `input` is a string (treated as a user message) or a list of items. Message items look like `{"role":"user"|"assistant"|"system"|"developer","content":str|[{"type":"input_text","text"}|{"type":"input_image",…}|{"type":"input_file",…}]}`. Earlier output items and `function_call_output` items can also appear.
- Function tools are flat as well: `{"type":"function","name","parameters","strict"}`. Force a call with `tool_choice:{"type":"function","name":"…"}`.

**Where the output is** [SPEC]:
- `output[]` is a union discriminated on `type`, and its order varies. **Don't index `output[0]`.**
  - `{"type":"message","role":"assistant","phase"?:"commentary"|"final_answer"|null,"content":[{"type":"output_text","text":str,"annotations":[],"logprobs":[]} | {"type":"refusal","refusal":str}]}`
  - `{"type":"reasoning","summary":[…]}`: skip it.
  - `{"type":"function_call","call_id","name","arguments":<JSON string>}`
- **`output_text` is an SDK-only convenience property**, not wire format. It also concatenates `phase:"commentary"` messages. Parse only messages whose `phase` is `final_answer` or null, as `responses.parse()` does [SRC].
- **Status fields:** `status` ∈ `completed|failed|in_progress|cancelled|queued|incomplete`. `incomplete_details.reason` ∈ `max_output_tokens|max_messages|content_filter|steered` means the JSON may be truncated.
- **Echoed config:** the response echoes `instructions`, `text` (including `text.format.name` and `schema`), `tools`, `temperature`, `metadata`, and more. **That makes the response alone sufficient to recover the schema.**
- **Timestamps:** `created_at` and `completed_at`, in unix seconds.

**Usage** [SPEC]: `input_tokens`, `input_tokens_details{cached_tokens, cache_write_tokens}`, `output_tokens`, `output_tokens_details{reasoning_tokens}`, `total_tokens`. `usage` may be null.

Request:
```json
{
  "model": "gpt-4o-mini",
  "instructions": "Classify the email as spam or ham.",
  "input": [{ "role": "user", "content": [{ "type": "input_text", "text": "WIN A FREE CRUISE!!! Click now" }] }],
  "text": { "format": {
    "type": "json_schema", "name": "spam_classification", "description": "Spam verdict", "strict": true,
    "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["spam","ham"] }, "reasoning": { "type": "string" } }, "required": ["label","reasoning"], "additionalProperties": false }
  } }
}
```
Response (a reasoning model, so it includes a `reasoning` item):
```json
{
  "id": "resp_abc", "object": "response", "created_at": 1760000000, "completed_at": 1760000001,
  "status": "completed", "error": null, "incomplete_details": null,
  "instructions": "Classify the email as spam or ham.",
  "model": "o4-mini-2025-04-16",
  "output": [
    { "type": "reasoning", "id": "rs_abc", "summary": [] },
    { "type": "message", "id": "msg_abc", "status": "completed", "role": "assistant",
      "content": [{ "type": "output_text", "text": "{\"label\":\"spam\",\"reasoning\":\"Unsolicited prize offer with urgent call to action.\"}", "annotations": [], "logprobs": [] }] }
  ],
  "text": { "format": { "type": "json_schema", "name": "spam_classification", "description": "Spam verdict", "strict": true,
    "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["spam","ham"] }, "reasoning": { "type": "string" } }, "required": ["label","reasoning"], "additionalProperties": false } },
    "verbosity": "medium" },
  "tools": [], "tool_choice": "auto", "parallel_tool_calls": true, "store": true, "metadata": {},
  "usage": { "input_tokens": 58, "input_tokens_details": { "cached_tokens": 0, "cache_write_tokens": 0 },
             "output_tokens": 120, "output_tokens_details": { "reasoning_tokens": 102 }, "total_tokens": 178 }
}
```
Variants:
- Refusal: `"content":[{"type":"refusal","refusal":"I'm sorry, I cannot assist with that request."}]`
- Truncated: `"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}`

**Strict-mode schema subset (OpenAI)** [DOC]:
- **Root:** must be an `object` (not `anyOf`).
- **Every property must be in `required`**; emulate optional fields with `"type":["string","null"]`.
- **`additionalProperties:false`** on every object.
- **Supported:** `enum`, `anyOf`, `$defs`/`$ref`, recursion, `pattern`, `format`, `minimum`/`maximum`/`multipleOf`, `minItems`/`maxItems`.
- **Unsupported:** `allOf`, `not`, `if`/`then`/`else`, `dependent*`.
- **Limits:** 5,000 properties, 10 levels of nesting, 1,000 enum values.
- **Key order:** output keys follow schema order, so a "reasoning before label" schema puts the rationale first in the text.

### 5.3 Anthropic Messages API: `POST /v1/messages`

**Request shape** [DOC]:
- `system` is a **top-level** string, or an array of `{"type":"text","text"}` blocks.
- `messages[]` use roles `user` and `assistant`, with string content or block content (`text`, `image`, `document`, `tool_use`, `tool_result`, `thinking`, …).
- The API reference schema also lists a `system` role, for mid-conversation system messages on newer models. Treat a non-leading `system` message as part of the conversation.

**Three structured-output patterns.** Sundr must parse all three.

| Pattern | Request | Where the JSON is | Status |
|---|---|---|---|
| **A. JSON outputs** (recommended) | `"output_config": {"format": {"type":"json_schema","schema":<schema>}}`. **There is no `name` field.** | The `text` content block: `content[k].text` (a JSON string) where `content[k].type == "text"`. `stop_reason: "end_turn"`. | GA, no beta header [DOC]. **`output_format` (top-level) is deprecated** and now needs the `structured-outputs-2025-11-13` beta header, otherwise it returns 400. **Older logs will contain `output_format`, so read both.** |
| **B. Strict tool use** | `tools:[{"name","description","strict":true,"input_schema":<schema>}]` with `tool_choice: {"type":"auto"}` plus a prompt instruction | `content[k].input` (**already a JSON object**) where `content[k].type == "tool_use"` and `content[k].name == <tool>`. `stop_reason: "tool_use"`. | GA. With `auto`, a call isn't guaranteed; Claude may answer in text instead [DOC]. |
| **C. Forced tool use (legacy "JSON via tools")** | `tool_choice: {"type":"tool","name":"record_verdict"}` or `{"type":"any"}` | Same as B. No text comes before the `tool_use` block, because the API prefills [DOC]. | **Returns HTTP 400 on Claude Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1, and with manual extended thinking (`thinking.type:"enabled"`).** It still works on, for example, Claude Opus 5 and Haiku 5.5 [DOC]. Expect it in historical logs. |

**Response fields** [SRC: `anthropic-sdk-python` `types/message.py`]:
- Top level: `id` (`msg_…`), `type:"message"`, `role:"assistant"`, `model`, `content[]`, `stop_reason`, `stop_sequence`, `stop_details` (set only on refusal: `{type:"refusal", category, explanation}`), `usage`, `container`.
- **The response contains no timestamp**, so the time must come from the logger.
- **`content[]` can begin with `thinking` blocks.** On Opus 5.5, thinking can't be turned off, and by default its text is empty. **Always select blocks by `type`.**
- **`stop_reason`** is one of `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded`.
- **On `refusal`**, the output "may not match your schema". **On `max_tokens`** it may be incomplete [DOC].

**Usage** [SRC: `types/usage.py`]:
- `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
- `cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`.
- `output_tokens_details{thinking_tokens}`; `thinking_tokens` ≤ `output_tokens`, and `output_tokens` stays the billed total.
- `server_tool_use`, `service_tier` (`standard|priority|batch`), `inference_geo`.
- **Total input = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.** Unlike OpenAI, `input_tokens` excludes cache reads and writes.

**Schema subset (Anthropic)** [DOC]:
- **Supported:**
  - `enum` (scalars only), `const`, `anyOf`, `allOf` (not combined with `$ref`).
  - `$ref`/`$defs` (internal only), `default`.
  - String `format`s.
  - `minItems` of 0 or 1 only.
- **Not supported:**
  - `additionalProperties` other than `false`, which every object must set.
  - Numeric constraints (`minimum`, `maximum`, `multipleOf`) and string-length constraints.
  - Recursion.
- **Limits:** 20 strict tools, 24 optional parameters and 16 union-typed parameters per request.
- **Rationale fields:** a field that asks for "reasoning" can trigger a `reasoning_extraction` refusal. The docs recommend asking for a short explanation instead. So **in Anthropic logs, the rationale field is often called `explanation`.**

Pattern A request:
```json
{
  "model": "claude-haiku-5-5",
  "max_tokens": 1024,
  "system": "Classify the email as spam or ham.",
  "messages": [{ "role": "user", "content": "WIN A FREE CRUISE!!! Click now" }],
  "output_config": { "format": { "type": "json_schema",
    "schema": { "type": "object", "properties": { "label": { "type": "string", "enum": ["spam","ham"] }, "explanation": { "type": "string" } }, "required": ["label","explanation"], "additionalProperties": false } } }
}
```
Pattern A response (the `thinking` block is optional and appears only when the model thinks):
```json
{
  "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
  "type": "message",
  "role": "assistant",
  "model": "claude-haiku-5-5",
  "content": [
    { "type": "thinking", "thinking": "", "signature": "EqQBCgIYAhIM…" },
    { "type": "text", "text": "{\"label\":\"spam\",\"explanation\":\"Unsolicited prize offer with urgent call to action.\"}" }
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "stop_details": null,
  "usage": {
    "input_tokens": 74,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation": { "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0 },
    "output_tokens": 41,
    "output_tokens_details": { "thinking_tokens": 12 },
    "service_tier": "standard"
  }
}
```
Pattern B/C response (tool input is an object, not a string):
```json
{
  "id": "msg_01Aq9w938a90dw8q",
  "type": "message",
  "role": "assistant",
  "model": "claude-opus-5",
  "content": [
    { "type": "tool_use", "id": "toolu_01A09q90qw90lq917835lq9", "name": "record_verdict",
      "input": { "label": "ham", "explanation": "Personal message from a known contact." } }
  ],
  "stop_reason": "tool_use",
  "stop_sequence": null,
  "usage": { "input_tokens": 512, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 38 }
}
```

### 5.4 Token normalization (canonical: `input_tokens` = all prompt tokens including cache, `output_tokens` = all generated tokens including reasoning)

| Source | `input_tokens` | `output_tokens` | `cached_input_tokens` | `reasoning_tokens` |
|---|---|---|---|---|
| OpenAI Chat | `usage.prompt_tokens` | `usage.completion_tokens` | `prompt_tokens_details.cached_tokens` | `completion_tokens_details.reasoning_tokens` |
| OpenAI Responses | `usage.input_tokens` | `usage.output_tokens` | `input_tokens_details.cached_tokens` | `output_tokens_details.reasoning_tokens` |
| Anthropic | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` | `output_tokens` | `cache_read_input_tokens` | `output_tokens_details.thinking_tokens` |
| Langfuse (`usageDetails`) | **sum of every key containing `input`** (`input` + `input_*`), because the buckets are subtracted from `input` | sum of every key containing `output` | `input_cached_tokens` (OpenAI-mapped). For other providers the key names are **UNVERIFIED**. | `output_reasoning_tokens` |
| Helicone | **Prefer recomputing from `response_body.usage`** with the provider rules above. Fall back to `prompt_tokens` (whether it includes cache is **UNVERIFIED**). | `completion_tokens`, or recompute | `prompt_cache_read_tokens` | `reasoning_tokens` |
| LangSmith | `prompt_tokens` (rolled up from `usage_metadata.input_tokens`; LangChain's convention counts all input types including cache, **UNVERIFIED** this session) | `completion_tokens` | `prompt_token_details` → `cache_read` | `completion_token_details` → `reasoning` |
| Braintrust | `metrics.prompt_tokens` (for the Anthropic wrapper, whether it includes cache is **UNVERIFIED**) | `metrics.completion_tokens` | `metrics.prompt_cached_tokens` | `metrics.completion_reasoning_tokens` |

Never use `total_tokens` from Helicone; it adds reasoning tokens again.

---

## 6. Canonical `JudgmentCallRecord` for Sundr (recommendation)

### 6.1 Schema

The core fields are the ones requested. The fields marked *(ext)* are recommended extensions; each one fixes an ambiguity found in the research above.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://sundr.ai/schemas/judgment-call-record/v1.json",
  "title": "JudgmentCallRecord",
  "type": "object",
  "required": ["id", "timestamp", "task", "model", "input_messages", "system_prompt", "response_schema",
               "verdict_field", "verdict", "input_tokens", "output_tokens"],
  "properties": {
    "id":              { "type": "string", "description": "'<source>:<native id>', e.g. 'langfuse:a1b2c3d4e5f60718'. Stable across re-ingestion." },
    "timestamp":       { "type": "string", "format": "date-time", "description": "Request start, UTC, ISO 8601." },
    "task":            { "type": "string", "description": "Logical judgment task (see 6.4). Records with the same task share a prompt and schema family." },
    "model":           { "type": "string", "description": "Resolved model id from the response if known, else the requested model." },
    "input_messages":  { "type": "array", "description": "Conversation turns after the leading system/developer messages are removed.",
                         "items": { "type": "object", "required": ["role", "content"],
                                    "properties": { "role": { "enum": ["user", "assistant", "tool", "system"] },
                                                    "content": { "description": "A string when every part is text; otherwise an array of {type:'text'|'image'|'file'|'other', ...}." } } } },
    "system_prompt":   { "type": ["string", "null"], "description": "Leading system/developer messages, OpenAI 'instructions', or the Anthropic top-level 'system', joined with \\n\\n." },
    "response_schema": { "type": ["object", "null"], "description": "The JSON Schema the output was constrained to, or null if none was recoverable." },
    "verdict_field":   { "type": ["string", "null"], "description": "RFC 6901 JSON Pointer into the parsed output, e.g. '/label'." },
    "verdict":         { "type": ["string", "boolean", "integer", "number", "null"] },
    "rationale":       { "type": ["string", "null"] },
    "input_tokens":    { "type": ["integer", "null"], "description": "All prompt tokens including cached ones (see 5.4)." },
    "output_tokens":   { "type": ["integer", "null"], "description": "All generated tokens including reasoning." },
    "cost_usd":        { "type": ["number", "null"] },

    "source":          { "type": "object", "description": "(ext) Provenance.",
                         "properties": { "tool": { "enum": ["langfuse", "helicone", "langsmith", "braintrust", "openai_raw", "anthropic_raw"] },
                                         "native_id": { "type": "string" }, "trace_id": { "type": ["string", "null"] },
                                         "project": { "type": ["string", "null"] }, "api": { "enum": ["openai.chat", "openai.responses", "anthropic.messages", "unknown"] } } },
    "verdict_type":    { "enum": ["enum", "boolean", "integer", "number", "string", null], "description": "(ext)" },
    "verdict_options": { "type": ["array", "null"], "description": "(ext) The schema enum, or [true, false] for booleans." },
    "parsed_output":   { "type": ["object", "null"], "description": "(ext) The full parsed JSON, so other fields such as confidence are kept." },
    "raw_output":      { "type": ["string", "null"], "description": "(ext) Output text exactly as emitted (JSON text, or a serialized tool input)." },
    "output_channel":  { "enum": ["content", "tool_call", "none"], "description": "(ext) Whether the JSON came from message text or from tool arguments." },
    "schema_source":   { "type": ["string", "null"], "description": "(ext) e.g. 'openai.response_format', 'openai.text.format', 'anthropic.output_config.format', 'anthropic.output_format', 'tool:record_verdict', 'langchain.ls_structured_output_format', 'inferred'." },
    "parse_status":    { "enum": ["ok", "refusal", "truncated", "content_filtered", "invalid_json", "schema_mismatch", "no_structured_output", "body_unavailable"], "description": "(ext)" },
    "cached_input_tokens": { "type": ["integer", "null"], "description": "(ext)" },
    "reasoning_tokens":    { "type": ["integer", "null"], "description": "(ext)" },
    "cost_source":     { "enum": ["tool", "computed", null], "description": "(ext) Whether the cost came from the tool or was computed from a price table." },
    "latency_ms":      { "type": ["number", "null"], "description": "(ext)" },
    "labels":          { "type": "object", "description": "(ext) Existing human or automated scores/feedback on this call (Langfuse scores, Helicone scores, LangSmith feedback_stats, Braintrust scores). Useful as ground truth." },
    "attributes":      { "type": "object", "description": "(ext) Pass-through: tags, user_id, session_id, environment, custom properties." }
  }
}
```

Example canonical record, produced from the Helicone record in §2.6:
```json
{
  "id": "helicone:5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33",
  "timestamp": "2026-10-08T14:03:20.870Z",
  "task": "ticket-classifier",
  "model": "gpt-4o-mini-2024-07-18",
  "system_prompt": "Classify the support ticket. Return JSON only.",
  "input_messages": [{ "role": "user", "content": "I was charged twice for my subscription, please refund one." }],
  "response_schema": {
    "type": "object", "additionalProperties": false, "required": ["label", "confidence", "rationale"],
    "properties": { "label": { "type": "string", "enum": ["billing","bug","how_to","other"] }, "confidence": { "type": "number" }, "rationale": { "type": "string" } }
  },
  "verdict_field": "/label",
  "verdict": "billing",
  "rationale": "Mentions refund for duplicate charge.",
  "input_tokens": 412,
  "output_tokens": 38,
  "cost_usd": 0.0000846,
  "source": { "tool": "helicone", "native_id": "5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33", "trace_id": "3c0e5a1f-0000-4000-8000-000000000001", "project": null, "api": "openai.chat" },
  "verdict_type": "enum",
  "verdict_options": ["billing", "bug", "how_to", "other"],
  "parsed_output": { "label": "billing", "confidence": 0.93, "rationale": "Mentions refund for duplicate charge." },
  "raw_output": "{\"label\":\"billing\",\"confidence\":0.93,\"rationale\":\"Mentions refund for duplicate charge.\"}",
  "output_channel": "content",
  "schema_source": "openai.response_format",
  "parse_status": "ok",
  "cached_input_tokens": 0,
  "reasoning_tokens": 0,
  "cost_source": "tool",
  "latency_ms": 642,
  "labels": { "helicone-score-feedback": 1 },
  "attributes": { "environment": "production", "user_id": "user-12345", "session_path": "/triage/classify" }
}
```

### 6.2 Mapping from each observability tool

The tables reference these extraction routines, all defined in §6.3:
- `chat(req, resp)`: OpenAI Chat
- `responses(req, resp)`: OpenAI Responses
- `anthropic(req, resp)`: Anthropic Messages
- `lc(run)`: LangChain-serialized I/O

**Identity, time, task, model**

| Canonical | Langfuse (v2 observation) | Helicone (`query-clickhouse`) | LangSmith (v2 run) | Braintrust (span) |
|---|---|---|---|---|
| `id` | `"langfuse:"+id` | `"helicone:"+request_id` | `"langsmith:"+id` | `"braintrust:"+id` (dedupe by highest `_xact_id`) |
| `source.trace_id` | `traceId` | `request_properties["Helicone-Session-Id"]` | `trace_id` | `root_span_id` |
| `timestamp` | `startTime` | `request_created_at` | `start_time` (v1: naive; treat as UTC) | `metrics.start` (unix s → ISO), else `created` |
| `task` (§6.4) | `promptName` → `name` unless it is `OpenAI-generation` → `traceName` | `request_properties.task` or another configured property key → `["Helicone-Prompt-Id"]` → last segment of `["Helicone-Session-Path"]` | parent run `name` (the LLM run is named `ChatOpenAI`) → `metadata` task key | parent span `span_attributes.name` (the LLM span is named `Chat Completion`) → `metadata` task key |
| `model` | `model` (as provided) | `response_body.model` → `model` | `outputs.model` / `outputs.llm_output.model_name` → `metadata.ls_model_name` | `metadata.model` (requested only) |
| `latency_ms` | `latency`×1000 | `delay_ms` | (`end_time`−`start_time`) | (`metrics.end`−`metrics.start`)×1000 |

**Prompt and schema**

| Canonical | Langfuse | Helicone | LangSmith | Braintrust |
|---|---|---|---|---|
| `system_prompt` + `input_messages` | `JSON.parse(input)`: a list, or `.messages`, or `.input`. Split off leading `system`/`developer` messages. | `request_body.messages` (Chat), `request_body.instructions` + `.input` (Responses), or `request_body.system` + `.messages` (Anthropic) | `wrap_openai`: `inputs.messages`. LangChain: `lc(inputs.messages[0])`. | `input` (the messages list) |
| `response_schema` | `metadata.response_format.json_schema.schema` (Python; parse it if it's a string) → `modelParameters.response_format.json_schema.schema` (JS) → `metadata.text_format` → `null` | `request_body.response_format.json_schema.schema` / `request_body.text.format.schema` / `request_body.output_config.format.schema` / `request_body.output_format.schema` / tool `parameters`/`input_schema` | `inputs.response_format…` → `extra.metadata.ls_invocation_params.response_format…` → `extra.options.ls_structured_output_format.schema` | `metadata.response_format.json_schema.schema` → `metadata.tools[...]` |

**Output, tokens, cost, labels**

| Canonical | Langfuse | Helicone | LangSmith | Braintrust |
|---|---|---|---|---|
| output → verdict | `JSON.parse(output)`. Chat: `.content` (string) or `.tool_calls[0].function.arguments`. Responses: the output items. Then §6.5. | The full provider response, so run `chat()`, `responses()` or `anthropic()` on `response_body` | `wrap_openai`: `outputs.choices[0].message`. LangChain: `generations[0][0].message.kwargs.content`, or `.tool_calls[0].args` (an object). | `output[0].message` (the choices list) |
| refusal / truncation | `output.refusal`; `level=="ERROR"` | `response_body` as in §5 | `additional_kwargs.refusal`, `generation_info.finish_reason`, `status`/`error` | `output[0].message.refusal`, `output[0].finish_reason`, `error` |
| `input_tokens` / `output_tokens` | per §5.4 | per §5.4 | `prompt_tokens` / `completion_tokens` | `metrics.prompt_tokens` / `metrics.completion_tokens` |
| `cost_usd` | `totalCost` → `costDetails.total` | `cost` | `total_cost` (v1 returns a string; parse it) | `metrics.estimated_cost`, else the SQL `estimated_cost()`, else computed |
| `labels` | `GET /api/public/v3/scores` by observation/trace | `scores` | `feedback_stats` | `scores` |
| `attributes` | `userId`, `sessionId`, `tags`, `environment`, `metadata` | `request_properties` (minus system keys), `request_user_id` | `tags`, `metadata` | `tags`, `metadata`, `context` |

**Raw provider logs** (teams logging request and response JSON themselves): use `chat()`, `responses()` or `anthropic()` directly.
- `timestamp`: Chat `created` and Responses `created_at` are in unix seconds. Anthropic has **no timestamp**, so it must come from the log envelope.
- `cost_usd` is always computed (`cost_source: "computed"`) from a versioned price table keyed by `model`.

### 6.3 Structured-output extraction (per dialect)

```python
# Returns (payload, channel, schema, schema_source, status). `payload` is a JSON string or a dict.
def chat(req, resp):
    rf = req.get("response_format") or {}
    schema, src = None, None
    if rf.get("type") == "json_schema":
        schema, src = rf["json_schema"].get("schema"), "openai.response_format"
    choice = resp["choices"][0]                     # n>1 → emit one record per choice (rare for judges)
    msg = choice["message"]
    if msg.get("refusal"):                          return None, "none", schema, src, "refusal"
    if msg.get("tool_calls"):
        call = msg["tool_calls"][0]["function"]     # with parallel calls, pick the tool whose schema is the verdict schema
        tool = next((t["function"] for t in req.get("tools", []) if t.get("function", {}).get("name") == call["name"]), {})
        return call["arguments"], "tool_call", tool.get("parameters"), f"tool:{call['name']}", status_from(choice["finish_reason"])
    return msg.get("content"), "content", schema, src or ("openai.json_object" if rf.get("type") == "json_object" else None), status_from(choice["finish_reason"])

def responses(req, resp):
    fmt = (resp.get("text") or {}).get("format") or (req.get("text") or {}).get("format") or {}
    schema, src = (fmt.get("schema"), "openai.text.format") if fmt.get("type") == "json_schema" else (None, None)
    if resp.get("status") == "incomplete":
        status = {"max_output_tokens": "truncated", "content_filter": "content_filtered"}.get(resp["incomplete_details"]["reason"], "truncated")
    else:
        status = "ok"
    texts = []
    for item in resp.get("output", []):
        if item["type"] == "function_call":
            return item["arguments"], "tool_call", tool_schema(req, item["name"]), f"tool:{item['name']}", status
        if item["type"] == "message" and item.get("phase") != "commentary":
            for c in item["content"]:
                if c["type"] == "refusal":          return None, "none", schema, src, "refusal"
                if c["type"] == "output_text":      texts.append(c["text"])
    return "".join(texts) or None, "content", schema, src, status

def anthropic(req, resp):
    fmt = (req.get("output_config") or {}).get("format") or req.get("output_format") or {}
    schema = fmt.get("schema") if fmt.get("type") == "json_schema" else None
    src = "anthropic.output_config.format" if "output_config" in req else ("anthropic.output_format" if schema else None)
    status = {"refusal": "refusal", "max_tokens": "truncated", "model_context_window_exceeded": "truncated"}.get(resp["stop_reason"], "ok")
    for b in resp["content"]:                       # never content[0]: thinking blocks may come first
        if b["type"] == "tool_use":
            tool = next((t for t in req.get("tools", []) if t.get("name") == b["name"]), {})
            return b["input"], "tool_call", tool.get("input_schema"), f"tool:{b['name']}", status   # input is already a dict
    text = "".join(b["text"] for b in resp["content"] if b["type"] == "text")
    return text or None, "content", schema, src, status

def status_from(finish_reason):
    return {"length": "truncated", "content_filter": "content_filtered"}.get(finish_reason, "ok")
```

**Parsing `payload`**
1. If it's a dict, use it.
2. Otherwise `json.loads` it.
3. If that fails, strip a Markdown code fence (```` ```json … ``` ````) and retry. This is common in `json_object` mode and with prompt-only JSON.
4. If it still fails, extract the first balanced `{…}` and retry.
5. Set `parse_status = "invalid_json"` only after all of these fail.
6. If a schema is known and the parsed value fails validation, set `parse_status = "schema_mismatch"` but **keep** the record.

**Dialect detection when the tool doesn't tell you**
- `resp.object == "chat.completion"`, or `choices` is present → Chat.
- `resp.object == "response"`, or `output` is a list of typed items → Responses.
- `resp.type == "message"` with `content` blocks → Anthropic.
- Otherwise, Helicone's `target_url` path (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`).

### 6.4 Task derivation

Use the first rule that yields a non-generic value.
1. An explicit user mapping, e.g. "Helicone property `task`" or "LangSmith metadata key `judge`".
2. A prompt-management id: Langfuse `promptName` (with `@promptVersion` kept as an attribute), Helicone `Helicone-Prompt-Id` / `prompt_id`.
3. The tool's span name, **ignoring the generic defaults**: `OpenAI-generation`, `ChatOpenAI`, `ChatAnthropic`, `Chat Completion`, `openai.chat`. For a generic name, move up to the parent span or run.
4. The schema name: OpenAI `json_schema.name` / `text.format.name`, or the Anthropic/OpenAI tool name (`record_verdict`). Anthropic `output_config.format` has no name.
5. A fingerprint: `"task_" + sha256(normalize(system_prompt) + canonical_json(response_schema))[:8]`. To normalize the system prompt, mask digits, UUIDs and quoted literals, so that templated variables don't split one task into many.

### 6.5 Selecting `verdict_field` and normalizing `verdict`

**Step 1: an explicit config wins.** If the task has a configured `verdict_field` (a JSON Pointer such as `/label` or `/decision/route`), use it.

**Step 2: choose from the schema** when `response_schema` is known. Score every scalar property at the top level, and inside nested objects up to depth 2:

| Signal | Score |
|---|---|
| Has `enum` with string, integer or boolean members, or is an `anyOf` of `const`s | +4 |
| `type: "boolean"` | +3 |
| `type: "integer"` with `enum` or `minimum`/`maximum` spanning ≤ 11 values (e.g. a 1–5 or 0–10 rubric) | +3 |
| `type: "integer"` with no bounds | +1 |
| Name matches `^(verdict\|label\|decision\|classification\|class\|category\|route\|routing\|intent\|outcome\|result\|judg(e)?ment\|grade\|score\|rating\|answer\|pass(ed)?\|approved\|valid\|is_.+\|should_.+\|has_.+)$` (case-insensitive) | +2 |
| Property is in `required` | +1 |
| `type: "string"` with no `enum` | 0 (eligible only through the name rule) |
| Name matches `reason\|rationale\|explanation\|justification\|thought\|analysis\|evidence\|comment\|notes` | **Excluded.** This is the rationale candidate. |
| Name matches `confidence\|probability\|certainty\|likelihood` with `type: number` | **Excluded.** Keep it in `parsed_output`. |

- Pick the highest score.
- If two candidates tie with score ≥ 4, set `verdict_field = null`, mark the task `verdict_field_ambiguous`, and ask the user. Rubric judges that output several booleans are common.
- Don't use schema key order as a tiebreaker: OpenAI output follows schema order, and "reasoning-first" schemas put the label last.

**Step 3: infer from the data** when there is no schema (`json_object` mode, prompt-only JSON, or a schema the tool dropped):
1. Over all `parsed_output`s in the task, collect the top-level scalar keys present in ≥ 95% of records.
2. Pick the key with the lowest cardinality, provided it has ≤ 20 distinct values and a distinct/total ratio ≤ 0.05.
3. Apply the same name boosts and exclusions as in Step 2.
4. Set `schema_source = "inferred"`. Optionally synthesize a `response_schema` with the observed `enum`.

**Step 4: normalize the verdict value** read at `verdict_field`.

| Schema type | Rule |
|---|---|
| `enum` (string) | Must equal a member. Otherwise, try a case-folded and whitespace-trimmed match against the members and keep the canonical member. Otherwise set `parse_status = "schema_mismatch"` and keep the raw value. `verdict_options` is the enum. |
| `boolean` | Must be a JSON `true`/`false`. When there is no strict schema, accept `"true"/"false"/"yes"/"no"` strings and flag `schema_mismatch`. `verdict_options = [true, false]`. |
| `integer` | Must be an integer; `4.0` is accepted as `4`, and `4.5` is a mismatch. Check it against `enum`/`minimum`/`maximum`. Note that OpenAI strict mode enforces `minimum`/`maximum` but **Anthropic does not**, so out-of-range values can appear in Anthropic logs. |
| `anyOf` of `const` | Treat as an enum of the const values. |
| `["T","null"]` (OpenAI's optional-field pattern) | A `null` verdict is valid. It usually means "abstain"; keep it and don't count it as a parse error. |

**Step 5: rationale.** Use the highest-ranked string property matching the rationale name pattern. If there are several, prefer `rationale` > `explanation` > `reasoning` > `justification`. If there's no schema and no match, `rationale = null`. Never fill `rationale` from thinking or reasoning blocks: those are often empty or summarized, and they aren't the model's stated rationale.

### 6.6 Validation and dedupe checklist for ingestion

- **Dedupe:**
  - Langfuse: by `id`.
  - Helicone: by `request_id`, across pages.
  - LangSmith: by `id`.
  - Braintrust: by `id`, keeping the max `_xact_id`.
- **Drop:**
  - Helicone cache hits (`cache_reference_id` ≠ zero UUID), or mark them with `attributes.cached = true`.
  - Braintrust rows with `metrics.cached = 1`.
- **Skip:** non-LLM spans (Langfuse `type != GENERATION`, LangSmith `run_type != LLM`, Braintrust `span_attributes.type != "llm"`).
- **Keep:** refusals and truncations, with `verdict = null` and the matching `parse_status`. They are judgment outcomes too, and Sundr should surface the rate.
- **Coerce:** numeric strings (Helicone Int64, LangSmith v1 costs, Langfuse v2 prices).

---

## 7. Open items (UNVERIFIED, worth a live test before shipping the connectors)

1. **Langfuse**
   - Whether `metadata.response_format` comes back as an object or a JSON string.
   - The UI observation export's exact columns.
   - The Cloud batch-export row limit.
   - The `usageDetails` key names for Anthropic-sourced generations.
2. **Helicone**
   - Whether `prompt_tokens` includes cached tokens, per provider.
   - Whether `total_tokens` double-counts reasoning for OpenAI.
   - Whether Cloud applies a 3-month TTL to inline bodies.
   - Whether Int64 values are quoted on Cloud.
3. **LangSmith**
   - Whether bulk-export Parquet stores `inputs`/`outputs` as strings or structs.
   - The casing of `run_type`/`status` in Parquet.
   - The `openai_ft` dataset line format.
   - How a Pydantic `response_format` given to `.parse()` is serialized.
   - Whether v1 `start_time` is always UTC.
4. **Braintrust**
   - The UI CSV columns.
   - Whether `output` includes `message.parsed`.
   - Whether the TypeScript wrapper logs the resolved model.
   - Whether `/btql` also returns the cursor in the body.
5. **OpenAI**
   - Whether an omitted `strict` on Responses `text.format` defaults to strict.
   - Whether a forced function call reports `finish_reason` `"stop"` or `"tool_calls"`.
   - Whether SDK-serialized logs include `output_text` or `parsed`.
6. **Anthropic**
   - The exact `inference_geo` values (not needed for Sundr).
   - The response shape for the deprecated `output_format` beta path. It is presumed identical to `output_config.format`: JSON in a `text` block.

### Provider sources

- **OpenAI:**
  - OpenAPI spec at commit `0ef225c4` (2026-10-08): https://raw.githubusercontent.com/openai/openai-openapi/0ef225c4/openapi.json
  - https://developers.openai.com/api/docs/guides/structured-outputs
  - https://developers.openai.com/api/docs/guides/function-calling
  - https://developers.openai.com/api/docs/guides/migrate-to-responses
  - https://developers.openai.com/api/docs/guides/prompt-caching
  - https://developers.openai.com/api/docs/guides/reasoning
  - openai-python `src/openai/types/responses/response.py` and `helpers.md`
- **Anthropic:**
  - https://platform.claude.com/docs/en/build-with-claude/structured-outputs
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools (forced tool use restrictions)
  - https://platform.claude.com/docs/en/api/messages
  - anthropic-sdk-python `src/anthropic/types/{message,usage,output_tokens_details,cache_creation}.py`

---

## 8. Pulling with an API key (`sundr/connectors/pull.py`)

Researched 2026-10-09 against the docs, specs and source listed below; tags as at the top. `sundr pull SOURCE` and `pull()` fetch only LLM records, newest first, and keep each row exactly as the tool's API returns it, so `map_rows` / `load_export` read a pull like an export. Every request retries 408/429/5xx with exponential backoff and jitter. It honours `Retry-After` (in seconds or as an HTTP date), falling back to `RateLimit-Reset`, and waits at most 5 minutes per retry. It gives up when the total time budget runs out (30 min by default). On 401/403, an unknown project or an empty window, it raises `PullError` with a message that never quotes a credential.

### 8.1 What each pull sends

| | Langfuse | Helicone | LangSmith | Braintrust |
|---|---|---|---|---|
| **Route** | `GET /api/public/v2/observations` [SPEC] | `POST /v1/request/query-clickhouse` [DOC][SRC] | `POST /api/v2/runs/query` [SPEC] | `POST /btql` [DOC] |
| **Default host** | `https://cloud.langfuse.com` (EU). Also `us.`, `jp.`, `hipaa.cloud.langfuse.com`, or self-hosted [DOC] | `https://api.helicone.ai`; EU `https://eu.api.helicone.ai` [DOC] | `https://api.smith.langchain.com`; EU `https://eu.api.smith.langchain.com` [DOC] | `https://api.braintrust.dev`; EU `https://api-eu.braintrust.dev`; self-hosted data plane URL [DOC] |
| **Auth** | HTTP Basic: public key as the username, secret key as the password [SPEC] | `Authorization: Bearer <key>` [SPEC] | `X-Api-Key: <key>`. Add `X-Tenant-Id` only when the key spans several workspaces [DOC] | `Authorization: Bearer <key>` [DOC] |
| **LLM-only filter** | `type=GENERATION` | Time window plus `cached: {equals: false}`. Cache hits are also dropped by the mapper. [SRC] | `run_type: "LLM"`, uppercase in the body [SPEC] | `span_attributes.type = 'llm'` [DOC] |
| **Window** | `fromStartTime` (inclusive) / `toStartTime` (exclusive) [SPEC] | `request_created_at` `gte` / `lt` leaves, fixed when the pull starts, so offsets stay stable [SRC] | `min_start_time` / `max_start_time`. **Omitting `min_start_time` silently means the last 24 h.** [SPEC][DOC] | `created > now() - interval N day`. Every `project_logs()` query needs a range on `created`. [DOC] |
| **Pagination** | `limit` (max 1000, default 50) plus `meta.cursor`, which is absent on the last page [SPEC] | `limit` (0–1000) plus `offset`, `sort: {created_at: "desc"}`. Stop **only on an empty page**: rows sharing a `request_id` are merged after the limit, so pages can come back short. [SRC] | `page_size` (1–1000, default 100) plus `next_cursor`, null on the last page [SPEC] | Cursor in the `x-bt-cursor` response header (the docs' sample also checks `x-amz-meta-bt_cursor`). Pass it back as `OFFSET '<cursor>'`, `ORDER BY _pagination_key DESC`. No cursor or empty `data` means done. [DOC] |
| **Page size used** | 100. Responses are capped at 5 MB and `io` rows can be large. [DOC] | 1000, the route maximum and the export CLI's batch size [SRC] | 100 | 200. No maximum is documented; the SQL linter suggests ≤ 100 for large fields, and queries time out after 30 s. [DOC] |
| **Fields** | `fields=core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context`; unknown groups are silently dropped [SRC]. `expandMetadata=response_format,text_format`, otherwise metadata values over 200 characters are truncated. [SPEC] | Whole `HeliconeRequest` rows | `selects` listing every field the mapper reads. **Omitted, only `id` comes back** (46 values in `query.RunSelectField`). [SPEC] | `SELECT *` with `SETTINGS preview_length = -1`, so long values aren't truncated [DOC] |
| **Project** | Implied by the key; the endpoint has no project parameter [SRC] | Implied by the key (organisation) | Name → id via `GET /api/v1/sessions?name=<n>&limit=1`, which returns an array of sessions. A UUID is used as given. [SPEC] | Name → id via `GET /v1/project?project_name=<n>` → `{objects: [{id, name, …}]}`, or pass `project_id` [SPEC] |
| **`filters`** | `name` (plain param); `trace_name` and `prompt` as `filter=[{"type":"string","column":"traceName"\|"promptName","operator":"=","value":…}]`. Plain params and `filter` combine with AND. [SPEC][SRC] | `prompt_id` (leaf); `property.<key>` → `properties: {<key>: {equals}}`. Custom keys are lowercased, as Helicone stores them. [SRC] | `name` → `filter: eq(name, "…")`; `trace_name` → `trace_filter: eq(name, "…")` [DOC] | `name` → `span_attributes.name = '…'` [DOC] |
| **Rate limits** | Per organisation, per fixed minute. v2: 30 / 100 / 1,000 req/min on Hobby / Core / Pro+. v1: 15 / 30 / 100. 429s carry `Retry-After`. Self-hosted has no hard limit. [DOC] | Per organisation over 5 minutes: 1,000 (free), 10,000 (paid), 100,000 (enterprise). 429s carry `RateLimit-*` headers. [SRC] | v2: 300 requests per 10 s, per key. 429s should be retried with backoff and jitter. [DOC] | Per endpoint, scoped to the organisation and/or project. No figure is published for `/btql` (§4.1's "about 20/min" figure could not be re-confirmed). [DOC] |
| **Bad key** | 401; 403 for the wrong key type or scope [SPEC][SRC] | 401. A key without **write** permission also gets 401 (below). If authentication throws, the route returns 400 "Invalid token." [SRC] | 401; 403 for insufficient permission or an org-scoped key without a workspace [SPEC][SRC] | 401 on the `/v1` routes [SPEC] |

### 8.2 Things that change the integration

- **Helicone has no read-only key that works here.** `auth.ts` requires the `w` permission for every POST, and the query routes are POSTs. A key with read access only gets a 401 [SRC]. `sundr pull` says so in its error message. Tell users to create a key with write access, or wait until Helicone offers a GET query route.
- **Helicone S3 bodies.** For rows whose `storage_location` isn't `"clickhouse"`, `pull` downloads `signed_body_url` (a presigned S3 GET, valid for 24 h, returning `{request, response}`) **without** the Helicone key. It drops `streamed_data` and stores the result as `signed_body`, which `from_helicone` already reads. Downloads run 8 at a time. A body that fails to download leaves its row mapping as `body_unavailable`. The export CLI does the same, 10 at a time [SRC].
- **Langfuse self-hosted.** The v2 observations API requires Langfuse v4. OSS v3 doesn't have it [DOC], and a v4 deployment that isn't in v4 write mode returns 404 [SRC]. When the first v2 page returns 404, `pull` falls back to `GET /api/public/observations` with `page`/`limit` (≤ 100), stopping at `meta.totalPages`. On Cloud, v1 is removed on 2026-11-16, and organisations created after a cutoff already get 410 from it [SPEC][SRC], so Cloud always uses v2.
- **LangSmith self-hosted** needs v0.16 or later for `/api/v2/runs/query` [SPEC]. A 404 from it is reported as "project not found, or the server is too old". There is no v1 fallback: v1 is deprecated (`Sunset` 2027-01-31) and is limited to 10 requests per 10 s [DOC].
- **Generic span names lose their parent.** `pull` fetches only LLM records, so the parent spans that §6.4 step 3 climbs to (LangChain chains, Braintrust task spans) aren't in the rows. Tasks then come from the schema name or the fingerprint. Langfuse is unaffected because `trace_context` carries `traceName`.

### 8.3 Still UNVERIFIED

- **Langfuse:** what happens when a page would exceed 5 MB (status and body). Whether the v1 endpoint accepts the same `filter` columns (`traceName`, `promptName`) as v2.
- **Helicone:** whether 429s carry `Retry-After` (Helicone's code doesn't set it; the rate-limit middleware might). Whether Cloud enables the Redis-backed limiter for every organisation.
- **LangSmith:** whether 429s carry `Retry-After`. How the filter language escapes quotes inside strings (`pull` sends JSON string escapes). Whether every key type works without `X-Tenant-Id` (the v2 spec lists the key and the tenant id together in one security entry; the docs say the tenant id is needed only for multi-workspace keys).
- **Braintrust:** the maximum `LIMIT`. `/btql`'s 401/429 behaviour (the spec documents it only for `/v1`). Whether backslashes inside SQL string literals are escapes (`pull` only doubles single quotes).

**Sources:**
- Langfuse:
  - https://langfuse.com/docs/api-and-data-platform/features/public-api
  - https://cloud.langfuse.com/generated/api/openapi.yml
  - https://langfuse.com/faq/all/api-limits
  - https://langfuse.com/faq/all/deprecated-api-migration
  - https://langfuse.com/self-hosting/upgrade/versioning
  - Source: github.com/langfuse/langfuse `legacyApiOrganizationCutoff.ts`, the `deriveFilters` observation filter code
- Helicone:
  - https://docs.helicone.ai/rest/request/post-v1requestquery-clickhouse
  - https://docs.helicone.ai/tools/export
  - Source (github.com/Helicone/helicone, `main`):
    - `valhalla/jawn/src/controllers/public/requestController.ts`
    - `valhalla/jawn/src/tsoa-build/public/swagger.json`
    - `valhalla/jawn/src/managers/request/RequestManager.ts`
    - `valhalla/jawn/src/lib/stores/request/request.ts`
    - `valhalla/jawn/src/middleware/auth.ts`
    - `valhalla/jawn/src/middleware/ratelimitter.ts`
    - `valhalla/jawn/src/lib/shared/db/s3Client.ts`
    - `packages/filters/filterDefs.ts`
    - `packages/filters/filters.ts`
    - `examples/export/typescript/index.ts`
- LangSmith:
  - https://api.smith.langchain.com/openapi.json (`query.QueryRunsRequestBody`, `query.RunSelectField`, `query.RunResponse`, `GET /api/v1/sessions`)
  - https://docs.langchain.com/langsmith/smithdb-sdk-migration-query-runs
  - https://docs.langchain.com/langsmith/smithdb-sdk-migration
  - https://docs.langchain.com/langsmith/trace-query-syntax
  - https://docs.langchain.com/langsmith/create-account-api-key
  - https://docs.langchain.com/langsmith/usage-and-billing
  - https://docs.langchain.com/langsmith/changelog
  - Source: github.com/langchain-ai/langsmith-sdk `python/langsmith/client.py`
- Braintrust:
  - https://www.braintrust.dev/docs/api-reference
  - https://www.braintrust.dev/docs/api-reference/query
  - https://www.braintrust.dev/docs/reference/btql
  - https://www.braintrust.dev/docs/reference/sql/query-structure
  - https://www.braintrust.dev/docs/reference/sql/best-practices
  - https://www.braintrust.dev/docs/plans-and-limits
  - https://github.com/braintrustdata/braintrust-openapi (`openapi/spec.json`: `GET /v1/project`, `/v1/project_logs/{id}/fetch`)
