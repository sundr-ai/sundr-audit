"""Map one exported row from an observability tool to a canonical JudgmentCall (log-formats.md §6.1–6.2)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import extract as ex
from .extract import as_dict, loads, to_float, to_int
from .prices import compute_cost

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
HELICONE_SYSTEM_KEYS = re.compile(r"^(helicone-.*|stripe_customer_id)$", re.I)
LC_PROVIDERS = {"openai": ex.CHAT, "azure": ex.CHAT, "azure_openai": ex.CHAT, "anthropic": ex.ANTHROPIC}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRACTION = re.compile(r"(\.\d{6})\d+")


@dataclass
class JudgmentCall:
    """One logged LLM judgment call in Sundr's canonical shape (§6.1).

    `verdict_candidates` is set only when the verdict field is ambiguous and
    the user must choose one; `verdict` is then None.
    """

    id: str
    timestamp: str | None = None
    task: str | None = None
    model: str | None = None
    input_messages: list[dict] = field(default_factory=list)
    system_prompt: str | None = None
    response_schema: dict | None = None
    verdict_field: str | None = None
    verdict: Any = None
    rationale: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    source: dict = field(default_factory=dict)
    verdict_type: str | None = None
    verdict_options: list | None = None
    verdict_candidates: list[str] = field(default_factory=list)
    parsed_output: dict | None = None
    raw_output: str | None = None
    output_channel: str = "none"
    schema_source: str | None = None
    schema_name: str | None = None
    parse_status: str = "ok"
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_source: str | None = None
    latency_ms: float | None = None
    labels: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)

    @property
    def tool(self) -> str | None:
        return self.source.get("tool")


@dataclass
class Context:
    """Export-wide facts a row needs: user config, and the names and parents of other spans."""

    task_key: str | None = "task"
    verdict_field: str | None = None
    names: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)

    def ancestor_names(self, parent_id: Any) -> list[str]:
        names, seen = [], set()
        while isinstance(parent_id, str) and parent_id and parent_id not in seen:
            seen.add(parent_id)
            if parent_id in self.names:
                names.append(self.names[parent_id])
            parent_id = self.parents.get(parent_id)
        return names


# ---------------------------------------------------------------- shared helpers


def _pick(row: dict, *keys: str) -> Any:
    """The first key whose value is set; blob exports write null strings as ""."""
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def _datetime(value: Any) -> datetime | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    number = to_float(value)
    try:
        if number is not None:
            seconds = number / 1000 if number > 1e11 else number
            return _EPOCH + timedelta(milliseconds=round(seconds * 1000))
        dt = datetime.fromisoformat(_FRACTION.sub(r"\1", str(value).strip()).replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_utc(value: Any) -> str | None:
    """ISO 8601 UTC (millisecond precision) from an ISO string, naive UTC string, or unix seconds."""
    dt = _datetime(value)
    if dt is None:
        return None if value in (None, "") else str(value)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms_between(start: Any, end: Any) -> float | None:
    a, b = _datetime(start), _datetime(end)
    return None if a is None or b is None else round((b - a).total_seconds() * 1000, 3)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _raw_text(payload: Any) -> str | None:
    if payload is None or isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _with_schema_hint(out: ex.Extracted, hints: list[tuple[Any, str]]) -> ex.Extracted:
    """Fill a schema the bodies lost from wherever the tool kept it (§6.2 'response_schema' row)."""
    if out.schema is not None:
        return out
    for value, source in hints:
        schema, name = ex.unwrap_schema(value)
        if schema:
            return out._replace(schema=schema, schema_source=source, schema_name=out.schema_name or name)
    return out


def _task_key(ctx: Context, d: dict) -> Any:
    return d.get(ctx.task_key) if ctx.task_key else None


def apply_verdict(call: JudgmentCall, pointer: str | None, inferred: bool = False) -> None:
    """Read and normalise the verdict at `pointer` (§6.5 steps 4–5); refusals keep verdict None."""
    pointer = ex.normalize_pointer(pointer)
    node, nullable = ex.schema_node(call.response_schema, pointer)
    call.verdict_field = pointer
    if inferred and call.response_schema is None:
        call.schema_source = "inferred"
    if node is not None:
        shape = ex.normalize_verdict(None, node, True)
        call.verdict_type, call.verdict_options = shape.type, shape.options
    if call.parsed_output is None:
        call.verdict, call.rationale = None, None
        return
    missing = ex.missing_required(call.parsed_output, call.response_schema)
    call.parse_status = "schema_mismatch" if missing else "ok"
    call.rationale = ex.rationale_of(call.response_schema, call.parsed_output)
    if pointer is None:
        call.verdict = None
        return
    found, value = ex.get_pointer(call.parsed_output, pointer)
    verdict = ex.normalize_verdict(value, node, nullable)
    call.verdict, call.verdict_type = verdict.value, verdict.type
    call.verdict_options = verdict.options if node is not None else call.verdict_options
    if not found or not verdict.ok:
        call.parse_status = "schema_mismatch"


def _fill_cost(call: JudgmentCall, tool_cost: Any) -> None:
    cost = to_float(tool_cost)
    if cost is not None and cost > 0:
        call.cost_usd, call.cost_source = cost, "tool"
        return
    computed = compute_cost(call.model, call.input_tokens, call.output_tokens)
    if computed is not None:
        call.cost_usd, call.cost_source = computed, "computed"
    elif cost is not None:
        call.cost_usd, call.cost_source = cost, "tool"


def _set_tokens(call: JudgmentCall, tokens: ex.Tokens) -> None:
    call.input_tokens, call.output_tokens = tokens.input, tokens.output
    call.cached_input_tokens, call.reasoning_tokens = tokens.cached, tokens.reasoning


def finish(call: JudgmentCall, out: ex.Extracted, ctx: Context, names: list[Any], tool_cost: Any = None) -> JudgmentCall:
    """Parse the payload, choose the verdict field, derive the task and fill in cost."""
    call.response_schema = out.schema if isinstance(out.schema, dict) else None
    call.schema_source, call.schema_name, call.output_channel = out.schema_source, out.schema_name, out.channel
    call.model = call.model or out.model
    call.raw_output = _raw_text(out.payload)
    call.parse_status = out.status
    if out.status == "ok":
        ok, parsed = ex.parse_json(out.payload)
        if out.payload in (None, ""):
            call.parse_status = "no_structured_output"
        elif not ok:
            call.parse_status = "invalid_json"
        elif not isinstance(parsed, dict):
            call.parse_status = "no_structured_output"
        else:
            call.parsed_output = parsed
    pointer = ctx.verdict_field
    if pointer is None and call.response_schema:
        choice = ex.select_verdict_field(call.response_schema)
        pointer = choice.pointer
        if choice.ambiguous:
            call.verdict_candidates = choice.candidates
    apply_verdict(call, pointer)
    call.task = ex.derive_task(names, call.schema_name, call.system_prompt, call.response_schema)
    _fill_cost(call, tool_cost)
    return call


# ---------------------------------------------------------------- Langfuse


def _langfuse_request(value: Any) -> dict:
    if isinstance(value, list):
        return {"messages": value}
    if isinstance(value, dict):
        return value if "messages" in value or "input" in value else {}
    if isinstance(value, str) and value.strip():
        return {"messages": [{"role": "user", "content": value}]}
    return {}


def _langfuse_tokens(row: dict) -> ex.Tokens:
    details = as_dict(_pick(row, "usageDetails", "usage_details"))
    tokens = ex.langfuse_tokens(details) if details else ex.Tokens()
    if tokens.input is None and tokens.output is None:
        usage = as_dict(row.get("usage"))
        tokens.input = to_int(_pick(usage, "input") if usage else row.get("promptTokens"))
        tokens.output = to_int(_pick(usage, "output") if usage else row.get("completionTokens"))
    return tokens


def from_langfuse(row: dict, ctx: Context | None = None) -> JudgmentCall | None:
    """A Langfuse observation: v2 API (string I/O), v1 API (parsed I/O) or snake_case blob export."""
    ctx = ctx or Context()
    kind = _pick(row, "type")
    if kind and str(kind).upper() != "GENERATION":
        return None
    metadata = as_dict(_pick(row, "metadata"))
    params = as_dict(_pick(row, "modelParameters", "model_parameters"))
    req = _langfuse_request(loads(_pick(row, "input")))
    dialect, resp = ex.coerce_response(loads(_pick(row, "output")))
    out = _with_schema_hint(
        ex.extract_output(req, resp, dialect),
        [
            (metadata.get("response_format"), "openai.response_format"),
            (params.get("response_format"), "openai.response_format"),
            (metadata.get("text_format"), "openai.text.format"),
        ],
    )
    system, messages = ex.request_messages(req)
    start, end = _pick(row, "startTime", "start_time"), _pick(row, "endTime", "end_time")
    latency = _ms_between(start, end)
    if latency is None and to_float(row.get("latency")) is not None:
        latency = round(to_float(row["latency"]) * 1000, 3)
    native = str(_pick(row, "id"))
    costs = as_dict(_pick(row, "costDetails", "cost_details"))
    hidden = {"response_format", "text_format", ctx.task_key}
    call = JudgmentCall(
        id=f"langfuse:{native}",
        timestamp=iso_utc(start),
        model=_pick(row, "model", "providedModelName", "provided_model_name"),
        input_messages=messages,
        system_prompt=system,
        source={
            "tool": "langfuse",
            "native_id": native,
            "trace_id": _pick(row, "traceId", "trace_id"),
            "project": _pick(row, "projectId", "project_id"),
            "api": dialect,
        },
        latency_ms=latency,
        labels=as_dict(row.get("scores")),
        attributes=_clean(
            {
                "user_id": _pick(row, "userId", "user_id"),
                "session_id": _pick(row, "sessionId", "session_id"),
                "environment": _pick(row, "environment"),
                "tags": _pick(row, "tags"),
                "release": _pick(row, "release"),
                "version": _pick(row, "version"),
                "prompt_version": _pick(row, "promptVersion", "prompt_version"),
                "metadata": {k: v for k, v in metadata.items() if k not in hidden},
                "error": _pick(row, "statusMessage", "status_message") if str(row.get("level")).upper() == "ERROR" else None,
            }
        ),
    )
    _set_tokens(call, _langfuse_tokens(row))
    parent = _pick(row, "parentObservationId", "parent_observation_id")
    names = [
        _task_key(ctx, metadata),
        _pick(row, "promptName", "prompt_name"),
        _pick(row, "name"),
        *ctx.ancestor_names(parent),
        _pick(row, "traceName", "trace_name"),
    ]
    cost = _pick(row, "totalCost", "total_cost")
    if cost is None:
        cost = costs.get("total") if costs.get("total") is not None else row.get("calculatedTotalCost")
    return finish(call, out, ctx, names, cost)


# ---------------------------------------------------------------- Helicone


def _helicone_bodies(row: dict) -> tuple[dict, Any, bool]:
    """(request, response, available). S3-stored rows are previews unless the signed-URL body is supplied."""
    supplied = loads(_pick(row, "signed_body", "signed_body_content"))
    if isinstance(supplied, dict) and ("request" in supplied or "response" in supplied):
        return as_dict(supplied.get("request")), loads(supplied.get("response")), True
    if row.get("storage_location") not in (None, "", "clickhouse"):
        return {}, None, False
    req, resp = loads(row.get("request_body")), loads(row.get("response_body"))
    usable = [isinstance(b, dict) and "helicone_message" not in b for b in (req, resp)]
    return (req if usable[0] else {}), (resp if usable[1] else None), all(usable)


def from_helicone(row: dict, ctx: Context | None = None) -> JudgmentCall | None:
    """A HeliconeRequest from POST /v1/request/query-clickhouse (or the official export CLI). Cache hits are dropped."""
    ctx = ctx or Context()
    if row.get("cache_reference_id") not in (None, "", ZERO_UUID) or row.get("cached") is True:
        return None
    props = as_dict(row.get("request_properties") or row.get("properties"))
    req, resp, available = _helicone_bodies(row)
    dialect = ex.detect_dialect(resp, req, row.get("target_url"))
    if available:
        out = ex.extract_output(req, resp, dialect)
    else:
        schema, name = ex.unwrap_schema(req.get("response_format"))
        out = ex.Extracted(None, "none", schema, "openai.response_format" if schema else None, "body_unavailable", name)
    system, messages = ex.request_messages(req)
    usage = ex.usage_tokens(as_dict(resp).get("usage"), dialect)
    tokens = ex.Tokens(
        usage.input if usage.input is not None else to_int(row.get("prompt_tokens")),
        usage.output if usage.output is not None else to_int(row.get("completion_tokens")),
        usage.cached if usage.cached is not None else to_int(row.get("prompt_cache_read_tokens")),
        usage.reasoning if usage.reasoning is not None else to_int(row.get("reasoning_tokens")),
    )
    task_key = ctx.task_key.lower() if ctx.task_key else None
    path = str(props.get("Helicone-Session-Path") or "")
    custom = {k: v for k, v in props.items() if not HELICONE_SYSTEM_KEYS.match(k) and k != task_key}
    native = str(row.get("request_id"))
    call = JudgmentCall(
        id=f"helicone:{native}",
        timestamp=iso_utc(row.get("request_created_at")),
        model=as_dict(resp).get("model") or _pick(row, "model", "request_model"),
        input_messages=messages,
        system_prompt=system,
        source={
            "tool": "helicone",
            "native_id": native,
            "trace_id": props.get("Helicone-Session-Id"),
            "project": None,
            "api": dialect,
        },
        latency_ms=to_float(row.get("delay_ms")),
        labels=as_dict(row.get("scores")),
        attributes=_clean(
            {
                **custom,
                "user_id": row.get("request_user_id"),
                "session_path": path,
                "session_name": props.get("Helicone-Session-Name"),
            }
        ),
    )
    _set_tokens(call, tokens)
    names = [
        props.get(task_key) if task_key else None,
        props.get("Helicone-Prompt-Id"),
        row.get("prompt_id"),
        path.rstrip("/").rsplit("/", 1)[-1] if path else None,
    ]
    return finish(call, out, ctx, names, row.get("cost"))


# ---------------------------------------------------------------- LangSmith


def _unraw(value: Any) -> dict:
    d = as_dict(value)
    return as_dict(d.get("raw")) if isinstance(d.get("raw"), dict) else d


def _first_int(*values: Any) -> int | None:
    return to_int(next((v for v in values if v is not None), None))


def _langsmith_tokens(row: dict, outputs: dict, metadata: dict) -> ex.Tokens:
    """Rolled-up run totals, else usage_metadata, else the legacy llm_output.token_usage."""
    usage = as_dict(metadata.get("usage_metadata") or outputs.get("usage_metadata"))
    legacy = as_dict(as_dict(outputs.get("llm_output")).get("token_usage"))
    prompt, completion = _unraw(row.get("prompt_token_details")), _unraw(row.get("completion_token_details"))
    return ex.Tokens(
        _first_int(row.get("prompt_tokens"), usage.get("input_tokens"), legacy.get("prompt_tokens")),
        _first_int(row.get("completion_tokens"), usage.get("output_tokens"), legacy.get("completion_tokens")),
        _first_int(prompt.get("cache_read"), as_dict(usage.get("input_token_details")).get("cache_read")),
        _first_int(completion.get("reasoning"), as_dict(usage.get("output_token_details")).get("reasoning")),
    )


def _langsmith_parent(row: dict) -> str | None:
    parents = row.get("parent_run_ids")
    if isinstance(parents, list) and parents:
        return str(parents[-1])
    return row.get("parent_run_id")


def _langsmith_ancestors(row: dict, ctx: Context) -> list[str]:
    """Parent names nearest first; v2 `parent_run_ids` already lists the whole chain, root first."""
    chain = row.get("parent_run_ids")
    if isinstance(chain, list) and chain:
        return [ctx.names[str(p)] for p in reversed(chain) if str(p) in ctx.names]
    return ctx.ancestor_names(row.get("parent_run_id"))


def from_langsmith(row: dict, ctx: Context | None = None) -> JudgmentCall | None:
    """A LangSmith run (v2 query, v1 query or bulk export): LangChain LLMResult, wrap_openai, or native messages."""
    ctx = ctx or Context()
    if str(row.get("run_type") or "").lower() != "llm":
        return None
    inputs, outputs, extra = as_dict(row.get("inputs")), as_dict(row.get("outputs")), as_dict(row.get("extra"))
    metadata = as_dict(row.get("metadata")) or as_dict(extra.get("metadata"))
    invocation = as_dict(metadata.get("ls_invocation_params")) or as_dict(extra.get("invocation_params"))
    if "generations" in outputs:
        raw = inputs.get("messages")
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            raw = raw[0]
        if raw is None and isinstance(inputs.get("prompts"), list) and inputs["prompts"]:
            raw = [{"role": "user", "content": inputs["prompts"][0]}]
        req = {"messages": [m for m in (ex.lc_message(m) for m in raw or []) if m]}
        dialect = LC_PROVIDERS.get(str(metadata.get("ls_provider") or "").lower(), ex.UNKNOWN)
        out = ex.langchain_output(outputs)
    else:
        req = inputs
        dialect, resp = ex.coerce_response(outputs)
        out = ex.extract_output(inputs, resp, dialect)
    if out.channel == "tool_call" and out.schema is None:
        out = out._replace(schema=ex.tool_schema(invocation.get("tools"), out.schema_name))
    structured = as_dict(as_dict(extra.get("options")).get("ls_structured_output_format"))
    out = _with_schema_hint(
        out,
        [
            (inputs.get("response_format"), "openai.response_format"),
            (invocation.get("response_format"), "openai.response_format"),
            (as_dict(extra.get("invocation_params")).get("response_format"), "openai.response_format"),
            (structured.get("schema"), "langchain.ls_structured_output_format"),
        ],
    )
    system, messages = ex.request_messages(req)
    llm_output = as_dict(outputs.get("llm_output"))
    model = outputs.get("model") or llm_output.get("model_name") or out.model
    model = model or metadata.get("ls_model_name") or invocation.get("model")
    native = str(row.get("id"))
    status = str(row.get("status") or "").lower()
    call = JudgmentCall(
        id=f"langsmith:{native}",
        timestamp=iso_utc(row.get("start_time")),
        model=model,
        input_messages=messages,
        system_prompt=system,
        source={
            "tool": "langsmith",
            "native_id": native,
            "trace_id": row.get("trace_id"),
            "project": row.get("session_id") or row.get("project_id"),
            "api": dialect,
        },
        latency_ms=_ms_between(row.get("start_time"), row.get("end_time")),
        labels=as_dict(row.get("feedback_stats")),
        attributes=_clean(
            {
                "tags": row.get("tags"),
                "metadata": {k: v for k, v in metadata.items() if not k.startswith("ls_") and k not in ("usage_metadata", ctx.task_key)},
                "error": row.get("error") if status == "error" else None,
            }
        ),
    )
    _set_tokens(call, _langsmith_tokens(row, outputs, metadata))
    names = [_task_key(ctx, metadata), row.get("name"), *_langsmith_ancestors(row, ctx)]
    cost = row.get("total_cost")
    if to_float(cost) is None and to_float(row.get("prompt_cost")) is not None:
        cost = to_float(row["prompt_cost"]) + (to_float(row.get("completion_cost")) or 0)
    return finish(call, out, ctx, names, cost)


# ---------------------------------------------------------------- Braintrust


def dedupe_braintrust(rows: list[dict]) -> list[dict]:
    """Keep one row per `id`: the version with the highest `_xact_id`."""
    best: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("id"))
        if key not in best or (to_int(row.get("_xact_id")) or 0) > (to_int(best[key].get("_xact_id")) or 0):
            best[key] = row
    return list(best.values())


def from_braintrust(row: dict, ctx: Context | None = None) -> JudgmentCall | None:
    """A Braintrust ProjectLogsEvent span. Non-llm spans and cached calls are dropped."""
    ctx = ctx or Context()
    span = as_dict(row.get("span_attributes"))
    if span.get("type") != "llm":
        return None
    metrics = as_dict(row.get("metrics"))
    if metrics.get("cached") in (1, True, "1", "true"):
        return None
    metadata = as_dict(row.get("metadata"))
    req = dict(metadata)
    given = loads(row.get("input"))
    if isinstance(given, dict):
        req.update(given)
    elif given is not None:
        req["messages"] = given
    dialect, resp = ex.coerce_response(loads(row.get("output")))
    out = ex.extract_output(req, resp, dialect)
    system, messages = ex.request_messages(req)
    start, end = to_float(metrics.get("start")), to_float(metrics.get("end"))
    native = str(row.get("id"))
    hidden = {"response_format", "tools", "tool_choice", "messages", ctx.task_key}
    call = JudgmentCall(
        id=f"braintrust:{native}",
        timestamp=iso_utc(start if start is not None else row.get("created")),
        input_messages=messages,
        system_prompt=system,
        source={
            "tool": "braintrust",
            "native_id": native,
            "trace_id": row.get("root_span_id"),
            "project": row.get("project_id"),
            "api": dialect,
        },
        latency_ms=round((end - start) * 1000, 3) if start is not None and end is not None else None,
        labels={k: v for k, v in as_dict(row.get("scores")).items() if v is not None},
        attributes=_clean(
            {
                "tags": row.get("tags"),
                "metadata": {k: v for k, v in metadata.items() if k not in hidden},
                "context": row.get("context"),
                "error": row.get("error"),
            }
        ),
    )
    _set_tokens(
        call,
        ex.Tokens(
            to_int(metrics.get("prompt_tokens")),
            to_int(metrics.get("completion_tokens")),
            to_int(metrics.get("prompt_cached_tokens")),
            to_int(metrics.get("completion_reasoning_tokens")),
        ),
    )
    parents = row.get("span_parents") or []
    names = [_task_key(ctx, metadata), span.get("name"), *ctx.ancestor_names(parents[0] if parents else None)]
    return finish(call, out, ctx, names, metrics.get("estimated_cost"))


# ---------------------------------------------------------------- raw provider logs


def from_raw(row: dict, ctx: Context | None = None) -> JudgmentCall | None:
    """A self-logged line {"request", "response", "timestamp"?, "id"?} for OpenAI Chat/Responses or Anthropic."""
    ctx = ctx or Context()
    req, resp = as_dict(row.get("request")), loads(row.get("response"))
    if not req and resp is None:
        return None
    dialect = ex.detect_dialect(resp, req, row.get("url") or row.get("target_url"))
    out = ex.extract_output(req, resp, dialect)
    body = as_dict(resp)
    system, messages = ex.request_messages(req)
    native = str(row.get("id") or body.get("id") or hashlib.sha256(ex.canonical_json(row).encode()).hexdigest()[:16])
    tool = "anthropic_raw" if dialect == ex.ANTHROPIC else "openai_raw"
    latency = to_float(row.get("latency_ms"))
    if latency is None and to_float(body.get("completed_at")) and to_float(body.get("created_at")):
        latency = round((to_float(body["completed_at"]) - to_float(body["created_at"])) * 1000, 3)
    metadata = as_dict(row.get("metadata"))
    call = JudgmentCall(
        id=f"{tool}:{native}",
        timestamp=iso_utc(_pick(row, "timestamp") or _pick(body, "created", "created_at")),
        input_messages=messages,
        system_prompt=system,
        source={"tool": tool, "native_id": native, "trace_id": row.get("trace_id"), "project": None, "api": dialect},
        latency_ms=latency,
        labels=as_dict(row.get("labels")),
        attributes=_clean({"metadata": metadata}),
    )
    _set_tokens(call, ex.usage_tokens(body.get("usage"), dialect))
    return finish(call, out, ctx, [row.get("task"), _task_key(ctx, metadata)])


# ---------------------------------------------------------------- dispatch

MAPPERS: dict[str, Callable[[dict, Context | None], JudgmentCall | None]] = {
    "langfuse": from_langfuse,
    "helicone": from_helicone,
    "langsmith": from_langsmith,
    "braintrust": from_braintrust,
    "raw": from_raw,
}


def map_row(row: dict, source: str, ctx: Context | None = None) -> JudgmentCall | None:
    """Map one exported row; None for rows that are not LLM judgment calls (other spans, cache hits)."""
    if source not in MAPPERS:
        raise ValueError(f"unknown source {source!r}; expected one of {sorted(MAPPERS)}")
    return MAPPERS[source](row, ctx)


def build_context(rows: list[dict], source: str, task_key: str | None = "task", verdict_field: str | None = None) -> Context:
    """Index span names and parents so generic LLM span names can fall back to their parent's name."""
    ctx = Context(task_key=task_key, verdict_field=ex.normalize_pointer(verdict_field))
    for row in rows:
        if not isinstance(row, dict):
            continue
        if source == "langfuse":
            node, name, parent = _pick(row, "id"), _pick(row, "name"), _pick(row, "parentObservationId", "parent_observation_id")
        elif source == "langsmith":
            node, name, parent = row.get("id"), row.get("name"), _langsmith_parent(row)
        elif source == "braintrust":
            parents = row.get("span_parents") or []
            node, name, parent = row.get("span_id"), as_dict(row.get("span_attributes")).get("name"), parents[0] if parents else None
        else:
            continue
        if node is None:
            continue
        if name:
            ctx.names[str(node)] = str(name)
        if parent:
            ctx.parents[str(node)] = str(parent)
    return ctx
