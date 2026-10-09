"""sundr.connectors.pull against mocked platform APIs (httpx.MockTransport; nothing leaves the process)."""

import base64
import functools
import json
from urllib.parse import parse_qs

import httpx
import pytest
from typer.testing import CliRunner

from sundr.cli import app
from sundr.compiler import TaskSpec
from sundr.connectors import group_tasks, to_records
from sundr.connectors import pull as pulling
from sundr.connectors.pull import PullError, credentials_from_env, fetch, pull
from sundr.ingest import read_records

LABELS = ["billing", "bug", "other"]
SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": LABELS}, "rationale": {"type": "string"}},
    "required": ["label", "rationale"],
    "additionalProperties": False,
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "ticket_label", "strict": True, "schema": SCHEMA}}
SYSTEM = "Classify the support ticket. Reply in JSON."
SECRET = "sk-very-secret-0123456789"


def messages(i):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Ticket {i}: something happened"}]


def verdict(i):
    return json.dumps({"label": LABELS[i % 3], "rationale": "r"})


def completion(i):
    return {
        "id": f"chatcmpl-{i}",
        "object": "chat.completion",
        "created": 1759932201 + i,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": verdict(i), "refusal": None}}],
        "usage": {"prompt_tokens": 60, "completion_tokens": 9, "total_tokens": 69},
    }


class Api:
    """A MockTransport routing each request to `respond(request)`; every request is kept for assertions."""

    def __init__(self, respond):
        self.respond, self.requests = respond, []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    def to(self, path):
        return [r for r in self.requests if r.url.path == path]


def params(request):
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(pulling, "_sleep", slept.append)
    return slept


def assert_tasks(calls, task, n):
    groups = group_tasks(calls)
    assert list(groups) == [task]
    records = to_records(groups[task])
    assert len(records) == n
    assert {r.reference for r in records} == set(LABELS[:n])
    assert records[0].input.startswith("Ticket ")
    return records


# ---------------------------------------------------------------- Langfuse


def langfuse_row(i, name="ticket-classifier"):
    return {
        "id": f"obs-{i}",
        "traceId": f"trace-{i}",
        "projectId": "clx9proj0000",
        "parentObservationId": None,
        "type": "GENERATION",
        "name": name,
        "level": "DEFAULT",
        "environment": "production",
        "startTime": f"2026-10-07T14:03:{20 + i:02d}.512Z",
        "endTime": f"2026-10-07T14:03:{21 + i:02d}.100Z",
        "input": json.dumps(messages(i)),
        "output": json.dumps({"role": "assistant", "content": verdict(i)}),
        "metadata": {"response_format": json.dumps(RESPONSE_FORMAT)},
        "model": "gpt-4o-mini",
        "usageDetails": {"input": 60, "output": 9, "total": 69},
        "costDetails": {"total": 0.0000144},
        "totalCost": 0.0000144,
        "traceName": "support-triage",
        "tags": ["support"],
    }


def test_langfuse_pages_by_cursor_retries_429_and_maps_like_an_export(waits):
    throttled = []

    def respond(request):
        cursor = params(request).get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"data": [langfuse_row(0), langfuse_row(1)], "meta": {"cursor": "page-2"}})
        assert cursor == "page-2"
        if not throttled:
            throttled.append(1)
            return httpx.Response(429, headers={"Retry-After": "12"}, json={"message": "rate limit exceeded"})
        return httpx.Response(200, json={"data": [langfuse_row(2)], "meta": {}})

    api = Api(respond)
    creds = {"public_key": "pk-lf-1", "secret_key": SECRET, "host": "https://us.cloud.langfuse.com/"}
    calls = pull("langfuse", creds, since_days=7, filters={"name": "ticket-classifier", "trace_name": "support-triage"},
                 transport=api.transport)
    assert len(api.requests) == 3 and len(waits) == 1 and 12 <= waits[0] <= 15
    first = api.requests[0]
    assert first.url.host == "us.cloud.langfuse.com" and first.url.path == "/api/public/v2/observations"
    assert first.headers["authorization"] == "Basic " + base64.b64encode(f"pk-lf-1:{SECRET}".encode()).decode()
    query = params(first)
    assert query["type"] == "GENERATION" and query["name"] == "ticket-classifier"
    assert set(query["fields"].split(",")) >= {"core", "io", "metadata", "model", "usage", "prompt", "trace_context"}
    assert "response_format" in query["expandMetadata"] and query["fromStartTime"] < query["toStartTime"]
    assert json.loads(query["filter"]) == [{"type": "string", "column": "traceName", "operator": "=", "value": "support-triage"}]
    assert [c.id for c in calls] == ["langfuse:obs-0", "langfuse:obs-1", "langfuse:obs-2"]
    records = assert_tasks(calls, "ticket-classifier", 3)
    assert records[0].cost_usd == pytest.approx(0.0000144)


def test_langfuse_self_hosted_v3_falls_back_to_v1_pages():
    def respond(request):
        if request.url.path == "/api/public/v2/observations":
            return httpx.Response(404, json={"message": "Not found"})
        page = int(params(request)["page"])
        row = langfuse_row(page - 1, name="OpenAI-generation")
        row["input"], row["output"] = json.loads(row["input"]), json.loads(row["output"])  # v1 returns parsed I/O
        return httpx.Response(200, json={"data": [row], "meta": {"page": page, "limit": 100, "totalItems": 2, "totalPages": 2}})

    api = Api(respond)
    creds = {"public_key": "pk-lf-1", "secret_key": SECRET, "host": "langfuse.internal.example/api/public"}
    calls = pull("langfuse", creds, transport=api.transport)
    assert [params(r)["page"] for r in api.to("/api/public/observations")] == ["1", "2"]
    assert api.requests[0].url.host == "langfuse.internal.example"
    assert_tasks(calls, "support-triage", 2)  # generic generation name falls back to the trace name


# ---------------------------------------------------------------- Helicone


def helicone_row(i, *, s3=False):
    row = {
        "request_id": f"req-{i}",
        "request_created_at": f"2026-10-08T14:03:{20 + i:02d}.870Z",
        "request_body": {"model": "gpt-4o-mini", "messages": messages(i), "response_format": RESPONSE_FORMAT},
        "response_body": completion(i),
        "response_status": 200,
        "model": "gpt-4o-mini-2024-07-18",
        "provider": "OPENAI",
        "target_url": "https://api.openai.com/v1/chat/completions",
        "delay_ms": 642,
        "prompt_tokens": 60,
        "completion_tokens": 9,
        "cost": "0.0000144",
        "request_properties": {"environment": "production", "task": "ticket-classifier"},
        "cache_reference_id": "00000000-0000-0000-0000-000000000000",
        "storage_location": "clickhouse",
    }
    if s3:
        row.update(storage_location="s3", request_body="{\"model\":\"gpt-4o-mini\",\"messa", response_body="{\"id\":\"chatc")
        row["signed_body_url"] = f"https://helicone-bodies.s3.amazonaws.com/{i}.json?X-Amz-Signature=abc"
    return row


def test_helicone_offset_pages_retry_on_429_and_fetch_s3_bodies(monkeypatch, waits):
    monkeypatch.setattr(pulling, "HELICONE_PAGE", 2)
    pages = {0: [helicone_row(0), helicone_row(1, s3=True)], 2: [helicone_row(2)], 4: []}
    throttled = []

    def respond(request):
        if request.url.host == "helicone-bodies.s3.amazonaws.com":
            i = int(request.url.path.strip("/").split(".")[0])
            full = helicone_row(i)
            return httpx.Response(200, json={"request": full["request_body"], "response": {**full["response_body"], "streamed_data": []}})
        body = json.loads(request.content)
        if body["offset"] == 2 and not throttled:
            throttled.append(1)
            return httpx.Response(429, headers={"RateLimit-Reset": "7"}, json={"error": "Too many requests, please try again later."})
        return httpx.Response(201, json={"data": pages[body["offset"]], "error": None})

    api = Api(respond)
    creds = {"api_key": SECRET, "host": "https://eu.api.helicone.ai"}
    calls = pull("helicone", creds, filters={"property.Task": "ticket-classifier"}, transport=api.transport)
    queries = api.to("/v1/request/query-clickhouse")
    assert [json.loads(r.content)["offset"] for r in queries] == [0, 2, 2, 4]
    assert all(r.url.host == "eu.api.helicone.ai" and r.headers["authorization"] == f"Bearer {SECRET}" for r in queries)
    assert len(waits) == 1 and 7 <= waits[0] <= 9
    tree = json.dumps(json.loads(queries[0].content)["filter"])
    assert '"cached": {"equals": false}' in tree and '"task": {"equals": "ticket-classifier"}' in tree
    assert '"request_created_at": {"gte"' in tree and '"request_created_at": {"lt"' in tree
    body_fetch = [r for r in api.requests if r.url.host == "helicone-bodies.s3.amazonaws.com"]
    assert len(body_fetch) == 1 and "authorization" not in body_fetch[0].headers
    assert assert_tasks(calls, "ticket-classifier", 3)
    assert next(c for c in calls if c.id == "helicone:req-1").parse_status == "ok"


# ---------------------------------------------------------------- LangSmith

PROJECT_ID = "1ffd059c-3e8a-4f2b-9c6d-5a4b3c2d1e33"


def langsmith_row(i):
    return {
        "id": f"0ec6b845-18b9-4aa1-8f1b-6ba3f9fdef{i:02d}",
        "name": "ChatOpenAI",
        "run_type": "LLM",
        "status": "SUCCESS",
        "start_time": f"2026-09-14T17:02:{10 + i:02d}.120000Z",
        "end_time": f"2026-09-14T17:02:{11 + i:02d}.904000Z",
        "trace_id": f"0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c{i:02d}",
        "parent_run_ids": [f"0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c{i:02d}"],
        "project_id": PROJECT_ID,
        "inputs": {"model": "gpt-4o-mini", "messages": messages(i), "temperature": 0, "response_format": RESPONSE_FORMAT},
        "outputs": {**completion(i), "usage_metadata": {"input_tokens": 60, "output_tokens": 9, "total_tokens": 69}},
        "extra": {"metadata": {"ls_provider": "openai", "ls_model_name": "gpt-4o-mini"}},
        "metadata": {"ls_provider": "openai", "ls_model_name": "gpt-4o-mini"},
        "prompt_tokens": 60,
        "completion_tokens": 9,
        "total_cost": 0.0000144,
        "prompt_token_details": {"raw": {"cache_read": 0}},
        "tags": [],
        "feedback_stats": {},
    }


def test_langsmith_resolves_project_and_pages_by_next_cursor():
    def respond(request):
        if request.url.path == "/api/v1/sessions":
            assert params(request)["name"] == "support-bot"
            return httpx.Response(200, json=[{"id": PROJECT_ID, "name": "support-bot", "tenant_id": "t"}])
        body = json.loads(request.content)
        if body.get("cursor") is None:
            return httpx.Response(200, json={"items": [langsmith_row(0), langsmith_row(1)], "next_cursor": "eyJ2IjoxfQ"})
        assert body["cursor"] == "eyJ2IjoxfQ"
        return httpx.Response(200, json={"items": [langsmith_row(2)], "next_cursor": None})

    api = Api(respond)
    creds = {"api_key": SECRET, "project": "support-bot", "host": "https://eu.api.smith.langchain.com", "workspace_id": "ws-1"}
    calls = pull("langsmith", creds, since_days=3, filters={"name": "ChatOpenAI"}, transport=api.transport)
    queries = api.to("/api/v2/runs/query")
    assert len(queries) == 2 and queries[0].url.host == "eu.api.smith.langchain.com"
    assert queries[0].headers["x-api-key"] == SECRET and queries[0].headers["x-tenant-id"] == "ws-1"
    body = json.loads(queries[0].content)
    assert body["project_ids"] == [PROJECT_ID] and body["run_type"] == "LLM"
    assert {"INPUTS", "OUTPUTS", "EXTRA", "METADATA", "PARENT_RUN_IDS", "TOTAL_COST"} <= set(body["selects"])
    assert body["min_start_time"] < body["max_start_time"] and body["page_size"] == 100
    assert body["filter"] == 'eq(name, "ChatOpenAI")'
    assert_tasks(calls, "ticket_label", 3)  # generic run name, no parent rows: the schema name names the task


def test_langsmith_unknown_project():
    api = Api(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(PullError, match="no project named 'nope'"):
        fetch("langsmith", {"api_key": SECRET, "project": "nope"}, transport=api.transport)


# ---------------------------------------------------------------- Braintrust


def braintrust_row(i):
    return {
        "id": f"385052b6-50a2-43b4-b52d-9afaa34f0b{i:02d}",
        "span_id": f"70b04fd2-0177-47a9-a70b-e32ca43db1{i:02d}",
        "root_span_id": f"70b04fd2-0177-47a9-a70b-e32ca43db1{i:02d}",
        "span_parents": [],
        "_xact_id": f"10001961234567{i:02d}",
        "_pagination_key": f"p0738451234567890{i}",
        "created": f"2026-09-14T17:02:{10 + i:02d}.120Z",
        "project_id": "d709efc0-ac9f-410d-8387-345e1e5074dc",
        "log_id": "g",
        "input": messages(i),
        "output": completion(i)["choices"],
        "metadata": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0, "response_format": RESPONSE_FORMAT},
        "metrics": {"start": 1757869331.12 + i, "end": 1757869331.9 + i, "prompt_tokens": 60, "completion_tokens": 9, "tokens": 69},
        "span_attributes": {"name": "Chat Completion", "type": "llm"},
    }


def test_braintrust_sql_pages_by_cursor_header_and_stops_at_limit():
    def respond(request):
        if request.url.path == "/v1/project":
            assert params(request)["project_name"] == "Support Bot"
            return httpx.Response(200, json={"objects": [{"id": "d709efc0-ac9f-410d-8387-345e1e5074dc", "name": "Support Bot"}]})
        query = json.loads(request.content)["query"]
        if "OFFSET" not in query:
            return httpx.Response(200, json={"data": [braintrust_row(0), braintrust_row(1)], "schema": {}}, headers={"x-bt-cursor": "cur-1"})
        return httpx.Response(200, json={"data": [braintrust_row(2), braintrust_row(3)], "schema": {}}, headers={"x-bt-cursor": "cur-2"})

    api = Api(respond)
    calls = pull("braintrust", {"api_key": SECRET, "project": "Support Bot"}, limit=3, filters={"name": "Chat Completion"},
                 transport=api.transport)
    queries = [json.loads(r.content) for r in api.to("/btql")]
    assert len(queries) == 2 and all(q["fmt"] == "json" for q in queries)
    first, second = queries[0]["query"], queries[1]["query"]
    assert "FROM project_logs('d709efc0-ac9f-410d-8387-345e1e5074dc', shape => 'spans')" in first
    assert "span_attributes.type = 'llm'" in first and "span_attributes.name = 'Chat Completion'" in first
    assert "created > now() - interval 14 day" in first and "ORDER BY _pagination_key DESC LIMIT 3" in first
    assert first.endswith("SETTINGS preview_length = -1")
    assert "LIMIT 1 OFFSET 'cur-1'" in second
    assert api.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert len(calls) == 3
    assert_tasks(calls, "ticket_label", 3)


def test_braintrust_project_id_skips_lookup_and_quotes_sql():
    api = Api(lambda request: httpx.Response(200, json={"data": [braintrust_row(0)]}))
    fetch("braintrust", {"api_key": SECRET, "project_id": "p-1"}, filters={"name": "it's"}, transport=api.transport)
    assert api.to("/v1/project") == []
    assert "span_attributes.name = 'it''s'" in json.loads(api.requests[0].content)["query"]


# ---------------------------------------------------------------- errors

CREDENTIALS = {
    "langfuse": {"public_key": "pk-lf-1", "secret_key": SECRET},
    "helicone": {"api_key": SECRET},
    "langsmith": {"api_key": SECRET, "project": PROJECT_ID},
    "braintrust": {"api_key": SECRET, "project_id": "p-1"},
}


@pytest.mark.parametrize("source", sorted(CREDENTIALS))
def test_auth_failure_is_a_pull_error_without_credentials(source):
    echo = lambda request: httpx.Response(401, text=f"invalid key {SECRET} / {request.headers.get('authorization')}")  # noqa: E731
    with pytest.raises(PullError) as info:
        fetch(source, CREDENTIALS[source], transport=Api(echo).transport)
    message = str(info.value)
    assert "rejected the API key (HTTP 401)" in message
    assert SECRET not in message and "Bearer" not in message and "Basic" not in message
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_server_errors_are_scrubbed_and_transport_errors_retried(waits):
    api = Api(lambda request: httpx.Response(400, text=f"bad request for key {SECRET}"))
    with pytest.raises(PullError) as info:
        fetch("helicone", {"api_key": SECRET}, transport=api.transport)
    assert "HTTP 400 from /v1/request/query-clickhouse" in str(info.value) and SECRET not in str(info.value)
    assert len(api.requests) == 1  # 4xx other than 408/429 are not retried

    def down(request):
        raise httpx.ConnectError(f"connection refused ({request.headers['authorization']})", request=request)

    api = Api(down)
    with pytest.raises(PullError) as info:
        fetch("braintrust", {"api_key": SECRET, "project_id": "p-1"}, transport=api.transport)
    assert "could not reach api.braintrust.dev (ConnectError)" in str(info.value) and SECRET not in str(info.value)
    assert info.value.__context__ is None
    assert len(api.requests) == 7 and len(waits) == 6 and waits[1] > waits[0]


def test_long_retry_after_gives_up_with_a_clear_message():
    api = Api(lambda request: httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(PullError, match="asked us to wait 3600s"):
        fetch("langsmith", CREDENTIALS["langsmith"], transport=api.transport)


def test_empty_result_unknown_filter_and_unpullable_sources():
    empty = Api(lambda request: httpx.Response(200, json={"data": [], "meta": {}}))
    with pytest.raises(PullError, match="no LLM calls from the last 14 days matching name=x"):
        pull("langfuse", CREDENTIALS["langfuse"], filters={"name": "x"}, transport=empty.transport)
    with pytest.raises(PullError, match="can't filter on model; use prompt_id, property.<key>"):
        fetch("helicone", {"api_key": SECRET}, filters={"model": "gpt-4o"}, transport=empty.transport)
    html = Api(lambda request: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(PullError, match="unexpected response from /btql"):
        fetch("braintrust", CREDENTIALS["braintrust"], transport=html.transport)
    with pytest.raises(PullError, match="no API to pull from"):
        fetch("raw", {})
    with pytest.raises(PullError, match="needs secret_key"):
        fetch("langfuse", {"public_key": "pk"})
    spans = Api(lambda request: httpx.Response(200, json={"data": [{**langfuse_row(0), "type": "SPAN"}], "meta": {}}))
    with pytest.raises(PullError, match="1 rows, but none were LLM calls"):
        pull("langfuse", CREDENTIALS["langfuse"], transport=spans.transport)


def test_credentials_from_env():
    env = {"BRAINTRUST_API_KEY": SECRET, "BRAINTRUST_PROJECT_ID": "p-1", "BRAINTRUST_API_URL": "https://api-eu.braintrust.dev"}
    assert credentials_from_env("braintrust", env) == {"api_key": SECRET, "project_id": "p-1", "host": "https://api-eu.braintrust.dev"}
    assert credentials_from_env("langfuse", {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk", "LANGFUSE_BASE_URL": "h"})["host"] == "h"
    with pytest.raises(PullError, match="Set LANGSMITH_PROJECT to pull from LangSmith"):
        credentials_from_env("langsmith", {"LANGSMITH_API_KEY": SECRET})
    with pytest.raises(PullError, match="BRAINTRUST_PROJECT or BRAINTRUST_PROJECT_ID"):
        credentials_from_env("braintrust", {"BRAINTRUST_API_KEY": SECRET})


# ---------------------------------------------------------------- CLI


def test_cli_pull_writes_rows_that_import_reads(tmp_path, monkeypatch):
    rows = [langfuse_row(i) for i in range(30)]
    api = Api(lambda request: httpx.Response(200, json={"data": rows, "meta": {}}))
    monkeypatch.setattr(pulling, "fetch", functools.partial(pulling.fetch, transport=api.transport))
    monkeypatch.chdir(tmp_path)
    for name in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET)
    runner = CliRunner()

    result = runner.invoke(app, ["pull", "langfuse", "--out", "calls.jsonl", "--since-days", "7", "--filter", "name=ticket-classifier"])
    assert result.exit_code == 0, result.output
    assert "30 calls -> calls.jsonl" in result.output and SECRET not in result.output
    assert params(api.requests[0])["name"] == "ticket-classifier"

    result = runner.invoke(app, ["import", "langfuse", "calls.jsonl", "--out-dir", "imported"])
    assert result.exit_code == 0, result.output
    records = read_records(tmp_path / "imported" / "ticket-classifier" / "logs.jsonl")
    spec = TaskSpec.load(tmp_path / "imported" / "ticket-classifier" / "task.yaml")
    assert len(records) == 30 and spec.labels == LABELS and spec.description == SYSTEM

    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    result = runner.invoke(app, ["pull", "langfuse"])
    assert result.exit_code == 1 and "Set LANGFUSE_SECRET_KEY" in result.output
