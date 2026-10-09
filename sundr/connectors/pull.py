"""Pull recent LLM calls from Langfuse, Helicone, LangSmith or Braintrust with an API key (log-formats.md §8).

Each `fetch_<tool>` returns rows exactly as that tool's export holds them, so
`map_rows` (and everything after it) treats a pull like an uploaded export.
`pull()` does both steps; `fetch()` stops at the raw rows (what `sundr pull` writes).

API references, checked 2026-10-09:
  Langfuse   https://langfuse.com/docs/api-and-data-platform/features/public-api
             https://cloud.langfuse.com/generated/api/openapi.yml
             https://langfuse.com/faq/all/api-limits
             https://langfuse.com/faq/all/deprecated-api-migration
             https://langfuse.com/self-hosting/upgrade/versioning
  Helicone   https://docs.helicone.ai/rest/request/post-v1requestquery-clickhouse
             https://docs.helicone.ai/tools/export
             github.com/Helicone/helicone: valhalla/jawn/src/controllers/public/requestController.ts,
             valhalla/jawn/src/middleware/auth.ts, packages/filters/filterDefs.ts, examples/export/typescript/index.ts
  LangSmith  https://api.smith.langchain.com/openapi.json (POST /api/v2/runs/query, GET /api/v1/sessions)
             https://docs.langchain.com/langsmith/smithdb-sdk-migration-query-runs
             https://docs.langchain.com/langsmith/trace-query-syntax
             https://docs.langchain.com/langsmith/create-account-api-key
  Braintrust https://www.braintrust.dev/docs/api-reference/query
             https://www.braintrust.dev/docs/reference/sql/query-structure
             https://www.braintrust.dev/docs/reference/sql/best-practices
             https://github.com/braintrustdata/braintrust-openapi (GET /v1/project)
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .load import map_rows
from .sources import JudgmentCall

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}
MAX_RETRY_AFTER_S = 300.0  # Helicone's rate-limit window is 5 minutes
DEFAULT_TIMEOUT_S = 1800.0
NAMES = {"langfuse": "Langfuse", "helicone": "Helicone", "langsmith": "LangSmith", "braintrust": "Braintrust"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_sleep = time.sleep


class PullError(Exception):
    """A failure to show the user as-is: bad key, unknown project, nothing to pull. Never holds credentials."""


# ---------------------------------------------------------------- HTTP


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait: Retry-After (seconds or HTTP date), else the RateLimit-Reset delta Helicone's limiter sends."""
    value = response.headers.get("retry-after") or response.headers.get("ratelimit-reset")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Client:
    """One tool's HTTP client: retries 429/5xx with backoff, honours Retry-After, keeps to a total time budget.

    Errors are PullErrors that name only the status and path, with every
    credential scrubbed from any response text they quote, and no chained
    httpx exception (whose request would carry the auth header).
    """

    def __init__(
        self,
        name: str,
        base_url: str = "",
        *,
        secrets: tuple[str, ...] = (),
        headers: dict | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        deadline: float | None = None,
        max_retries: int = 6,
        auth_hint: str = "",
    ):
        self.name, self.max_retries, self.timeout_s, self.auth_hint = name, max_retries, timeout_s, auth_hint
        self.secrets = tuple(s for s in secrets if s)
        self.deadline = deadline or time.monotonic() + timeout_s
        self.http = httpx.Client(
            base_url=base_url, headers=headers, auth=auth, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0)
        )

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.http.close()

    def scrub(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PullError(f"{self.name}: gave up after {self.timeout_s:.0f}s; pull fewer calls (limit or since_days)")
        return remaining

    def request(self, method: str, url: str, *, passthrough: tuple[int, ...] = (), **kwargs: Any) -> httpx.Response:
        """The response, after retries; statuses in `passthrough` come back instead of raising."""
        delay, path = 1.0, urlparse(url).path or url
        for attempt in range(self.max_retries + 1):
            remaining = self._remaining()
            failure, fatal = None, False
            try:
                response = self.http.request(method, url, timeout=min(60.0, remaining), **kwargs)
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
                failure, fatal = type(exc).__name__, True
            except httpx.HTTPError as exc:  # raised below, outside this block, so no httpx request is chained on
                failure, fatal = type(exc).__name__, not isinstance(exc, httpx.TransportError)
            if failure:
                if fatal or attempt == self.max_retries:
                    raise PullError(f"{self.name}: could not reach {self.http.base_url.host or 'the API'} ({failure})")
            else:
                status = response.status_code
                if status in passthrough:
                    return response
                if status in (401, 403):
                    raise PullError(f"{self.name} rejected the API key (HTTP {status}).{self.auth_hint}")
                if status < 400:
                    return response
                if status not in RETRY_STATUS or attempt == self.max_retries:
                    detail = self.scrub(response.text[:300]).strip()
                    raise PullError(f"{self.name}: HTTP {status} from {path}" + (f": {detail}" if detail else ""))
                wait = _retry_after(response)
                if wait is not None:
                    if wait > min(MAX_RETRY_AFTER_S, remaining):
                        raise PullError(
                            f"{self.name} asked us to wait {wait:.0f}s (rate limit); try again later or pull fewer calls"
                        )
                    delay = max(delay, wait)
            _sleep(min(delay + random.uniform(0, delay / 4), self._remaining()))
            delay = min(delay * 2, 60.0)
        raise PullError(f"{self.name}: retries exhausted on {path}")

    def json(self, method: str, url: str, *, expect: type = dict, **kwargs: Any) -> Any:
        return _body(self, self.request(method, url, **kwargs), expect)


def _body(client: Client, response: httpx.Response, expect: type = dict) -> Any:
    """The parsed JSON body, which must be an `expect` (a JSON object unless stated)."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, expect):
        raise PullError(f"{client.name}: unexpected response from {response.request.url.path}")
    return body


# ---------------------------------------------------------------- shared helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _window(since_days: float) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=since_days), now


def _host(credentials: dict, default: str, *suffixes: str) -> str:
    host = str(credentials.get("host") or default).strip().rstrip("/")
    if "://" not in host:
        host = "https://" + host
    for suffix in suffixes:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
    return host


def _need(credentials: dict, name: str, *keys: str) -> list[str]:
    missing = [k for k in keys if not credentials.get(k)]
    if missing:
        raise PullError(f"{name} needs {' and '.join(missing)} in the credentials")
    return [str(credentials[k]).strip() for k in keys]


def _check_filters(name: str, filters: dict | None, allowed: tuple[str, ...], prefixes: tuple[str, ...] = ()) -> dict:
    filters = {str(k): str(v) for k, v in (filters or {}).items() if v not in (None, "")}
    unknown = [k for k in filters if k not in allowed and not any(k.startswith(p) and len(k) > len(p) for p in prefixes)]
    if unknown:
        options = [*allowed, *(p + "<key>" for p in prefixes)]
        raise PullError(f"{name} can't filter on {', '.join(unknown)}; use {', '.join(options) or 'no filters'}")
    return filters


def _rows(items: Any) -> list[dict]:
    return [row for row in items or [] if isinstance(row, dict)]


# ---------------------------------------------------------------- Langfuse

LANGFUSE_FIELDS = "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context"
LANGFUSE_PAGE = 100  # requests and responses are capped at 5 MB, and io rows can be large
LANGFUSE_V1_PAGE = 100  # the v1 maximum
LANGFUSE_COLUMNS = {"trace_name": "traceName", "prompt": "promptName"}


def fetch_langfuse(
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """GENERATION observations, newest first, from GET /api/public/v2/observations (v1 on self-hosted Langfuse v3).

    credentials: public_key, secret_key, host (default EU cloud; e.g. https://us.cloud.langfuse.com or self-hosted).
    filters: name (observation name), trace_name, prompt (prompt-management name).
    """
    public, secret = _need(credentials, "Langfuse", "public_key", "secret_key")
    host = _host(credentials, "https://cloud.langfuse.com", "/api/public")
    filters = _check_filters("Langfuse", filters, ("name", "trace_name", "prompt"))
    start, end = _window(since_days)
    params: dict[str, Any] = {"type": "GENERATION", "fromStartTime": _iso(start), "toStartTime": _iso(end)}
    if "name" in filters:
        params["name"] = filters["name"]
    conditions = [
        {"type": "string", "column": column, "operator": "=", "value": filters[key]}
        for key, column in LANGFUSE_COLUMNS.items()
        if key in filters
    ]
    if conditions:
        params["filter"] = json.dumps(conditions)
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    hint = " Check the public/secret key pair, and that the host matches the project's region."
    with Client(
        "Langfuse", host, secrets=(public, secret, token), auth=httpx.BasicAuth(public, secret),
        transport=transport, timeout_s=timeout_s, auth_hint=hint,
    ) as client:
        rows: list[dict] = []
        cursor = None
        while len(rows) < limit:
            page = {**params, "fields": LANGFUSE_FIELDS, "expandMetadata": "response_format,text_format"}
            page["limit"] = min(LANGFUSE_PAGE, limit - len(rows))
            if cursor:
                page["cursor"] = cursor
            response = client.request("GET", "/api/public/v2/observations", params=page, passthrough=(404,))
            if response.status_code == 404:
                if rows:
                    raise PullError("Langfuse: HTTP 404 from /api/public/v2/observations")
                return _langfuse_v1(client, params, limit)
            body = _body(client, response)
            rows += _rows(body.get("data"))
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor or not body.get("data"):
                break
    return rows[:limit]


def _langfuse_v1(client: Client, params: dict, limit: int) -> list[dict]:
    """Self-hosted Langfuse v3 has no v2 observations API (it needs v4): page through the deprecated v1 one."""
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        body = client.json("GET", "/api/public/observations", params={**params, "page": page, "limit": LANGFUSE_V1_PAGE})
        data = _rows(body.get("data"))
        rows += data
        if not data or page >= int((body.get("meta") or {}).get("totalPages") or 0):
            break
        page += 1
    return rows[:limit]


# ---------------------------------------------------------------- Helicone

HELICONE_PAGE = 1000  # the route's maximum, and the official export CLI's batch size
HELICONE_BODY_WORKERS = 8


def _leaf(field: str, condition: dict) -> dict:
    return {"request_response_rmt": {field: condition}}


def _all_of(leaves: list[dict]) -> dict:
    tree = leaves[0]
    for leaf in leaves[1:]:
        tree = {"left": tree, "operator": "and", "right": leaf}
    return tree


def _property_key(key: str) -> str:
    """Helicone lowercases Helicone-Property-<Name> keys but keeps Helicone-Session-* and similar as sent."""
    return key if key.lower().startswith("helicone-") else key.lower()


def fetch_helicone(
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Non-cached requests, newest first, from POST /v1/request/query-clickhouse; S3-stored bodies are fetched too.

    credentials: api_key (Helicone requires write access on this route), host (default US; https://eu.api.helicone.ai).
    filters: prompt_id, property.<key> (a custom property, e.g. property.task, or property.Helicone-Prompt-Id).
    """
    (key,) = _need(credentials, "Helicone", "api_key")
    host = _host(credentials, "https://api.helicone.ai")
    filters = _check_filters("Helicone", filters, ("prompt_id",), ("property.",))
    start, end = _window(since_days)
    leaves = [
        _leaf("request_created_at", {"gte": _iso(start)}),
        _leaf("request_created_at", {"lt": _iso(end)}),
        _leaf("cached", {"equals": False}),
    ]
    for name, value in filters.items():
        if name == "prompt_id":
            leaves.append(_leaf("prompt_id", {"equals": value}))
        else:
            leaves.append(_leaf("properties", {_property_key(name.removeprefix("property.")): {"equals": value}}))
    hint = " Helicone's query API needs a key with write access; a read-only key is refused."
    with Client(
        "Helicone", host, secrets=(key,), headers={"Authorization": f"Bearer {key}"},
        transport=transport, timeout_s=timeout_s, auth_hint=hint,
    ) as client:
        rows: list[dict] = []
        seen: set = set()
        offset = 0
        while len(rows) < limit:
            size = min(HELICONE_PAGE, limit - len(rows))
            body = client.json(
                "POST",
                "/v1/request/query-clickhouse",
                json={"filter": _all_of(leaves), "limit": size, "offset": offset, "sort": {"created_at": "desc"}},
            )
            if body.get("error"):
                raise PullError(f"Helicone: {client.scrub(str(body['error']))[:300]}")
            fresh = [r for r in _rows(body.get("data")) if r.get("request_id") not in seen]
            if not fresh:  # rows sharing a request_id are merged after the limit, so only an empty page is the end
                break
            rows += fresh
            seen.update(r.get("request_id") for r in fresh)
            offset += size
        rows = rows[:limit]
        _helicone_bodies(rows, transport, client.deadline, timeout_s)
    return rows


def _helicone_bodies(rows: list[dict], transport: httpx.BaseTransport | None, deadline: float, timeout_s: float) -> None:
    """Swap each S3-stored row's signed_body_url for the {request, response} it points at (no Helicone key sent).

    A body that can't be fetched in time is left out; that row then maps with parse_status "body_unavailable".
    """
    pending = [r for r in rows if r.get("signed_body_url") and r.get("storage_location") not in (None, "", "clickhouse")]
    if not pending:
        return
    with Client("Helicone body storage", transport=transport, deadline=deadline, timeout_s=timeout_s, max_retries=3) as s3:

        def fetch_one(row: dict) -> None:
            try:
                body = s3.json("GET", str(row["signed_body_url"]))
            except PullError:
                return
            if isinstance(body, dict):
                if isinstance(body.get("response"), dict):
                    body["response"].pop("streamed_data", None)
                row["signed_body"] = {"request": body.get("request"), "response": body.get("response")}
                row.pop("signed_body_url", None)

        with ThreadPoolExecutor(HELICONE_BODY_WORKERS) as pool:
            list(pool.map(fetch_one, pending))


# ---------------------------------------------------------------- LangSmith

LANGSMITH_PAGE = 100  # the default; runs carry full inputs and outputs
LANGSMITH_SELECTS = [
    "ID", "NAME", "RUN_TYPE", "STATUS", "START_TIME", "END_TIME", "FIRST_TOKEN_TIME", "ERROR", "EXTRA", "METADATA",
    "INPUTS", "OUTPUTS", "PARENT_RUN_IDS", "PROJECT_ID", "TRACE_ID", "THREAD_ID", "IS_ROOT", "TAGS", "FEEDBACK_STATS",
    "REFERENCE_EXAMPLE_ID", "PROMPT_TOKENS", "COMPLETION_TOKENS", "TOTAL_TOKENS", "PROMPT_TOKEN_DETAILS",
    "COMPLETION_TOKEN_DETAILS", "PROMPT_COST", "COMPLETION_COST", "TOTAL_COST",
]


def _langsmith_project(client: Client, project: str) -> str:
    if _UUID.match(project):
        return project
    found = client.json("GET", "/api/v1/sessions", params={"name": project, "limit": 1}, expect=list)
    match = next((p for p in _rows(found) if p.get("name") == project and p.get("id")), None)
    if match is None:
        raise PullError(f"LangSmith has no project named {project!r} that this API key can see")
    return str(match["id"])


def fetch_langsmith(
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """LLM runs from one project, newest first, from POST /api/v2/runs/query.

    credentials: api_key, project (name or id), host (default US; https://eu.api.smith.langchain.com),
    workspace_id (only for keys that span several workspaces).
    filters: name (run name), trace_name (the root run's name).
    """
    key, project = _need(credentials, "LangSmith", "api_key", "project")
    host = _host(credentials, "https://api.smith.langchain.com", "/api/v1", "/api")
    filters = _check_filters("LangSmith", filters, ("name", "trace_name"))
    headers = {"X-Api-Key": key}
    if credentials.get("workspace_id"):
        headers["X-Tenant-Id"] = str(credentials["workspace_id"]).strip()
    start, end = _window(since_days)
    query: dict[str, Any] = {
        "run_type": "LLM",
        "min_start_time": _iso(start),  # omitted, it silently becomes one day ago
        "max_start_time": _iso(end),
        "selects": LANGSMITH_SELECTS,  # omitted, only ids come back
    }
    if "name" in filters:
        query["filter"] = f"eq(name, {json.dumps(filters['name'], ensure_ascii=False)})"
    if "trace_name" in filters:
        query["trace_filter"] = f"eq(name, {json.dumps(filters['trace_name'], ensure_ascii=False)})"
    hint = " Check the key; a key covering several workspaces also needs the workspace id."
    with Client(
        "LangSmith", host, secrets=(key,), headers=headers, transport=transport, timeout_s=timeout_s, auth_hint=hint
    ) as client:
        query["project_ids"] = [_langsmith_project(client, project)]
        rows: list[dict] = []
        cursor = None
        while len(rows) < limit:
            page = {**query, "page_size": min(LANGSMITH_PAGE, limit - len(rows))}
            if cursor:
                page["cursor"] = cursor
            response = client.request("POST", "/api/v2/runs/query", json=page, passthrough=(404,))
            if response.status_code == 404:
                raise PullError(
                    f"LangSmith project {project!r} was not found (self-hosted LangSmith needs v0.16+ for this API)"
                )
            body = _body(client, response)
            rows += _rows(body.get("items"))
            cursor = body.get("next_cursor")
            if not cursor or not body.get("items"):
                break
    return rows[:limit]


# ---------------------------------------------------------------- Braintrust

BRAINTRUST_PAGE = 200  # the SQL linter suggests small pages for full input/output; queries time out at 30 s


def _sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _braintrust_project(client: Client, credentials: dict) -> str:
    if credentials.get("project_id"):
        return str(credentials["project_id"]).strip()
    name = str(credentials.get("project") or "").strip()
    if not name:
        raise PullError("Braintrust needs project_id or project (the project name) in the credentials")
    found = client.json("GET", "/v1/project", params={"project_name": name}).get("objects")
    match = next((p for p in _rows(found) if p.get("name") == name and p.get("id")), None)
    if match is None:
        raise PullError(f"Braintrust has no project named {name!r} that this API key can see")
    return str(match["id"])


def fetch_braintrust(
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """llm spans from one project's logs, newest first, via POST /btql (SQL), paged with the x-bt-cursor header.

    credentials: api_key, project_id or project (name), host (default US; https://api-eu.braintrust.dev).
    filters: name (span name).
    """
    (key,) = _need(credentials, "Braintrust", "api_key")
    host = _host(credentials, "https://api.braintrust.dev")
    filters = _check_filters("Braintrust", filters, ("name",))
    with Client(
        "Braintrust", host, secrets=(key,), headers={"Authorization": f"Bearer {key}"},
        transport=transport, timeout_s=timeout_s,
    ) as client:
        project = _braintrust_project(client, credentials)
        where = ["span_attributes.type = 'llm'", f"created > now() - interval {max(1, math.ceil(since_days))} day"]
        if "name" in filters:
            where.append(f"span_attributes.name = {_sql(filters['name'])}")
        rows: list[dict] = []
        cursor = None
        while len(rows) < limit:
            query = (
                f"SELECT * FROM project_logs({_sql(project)}, shape => 'spans') WHERE {' AND '.join(where)}"
                f" ORDER BY _pagination_key DESC LIMIT {min(BRAINTRUST_PAGE, limit - len(rows))}"
                + (f" OFFSET {_sql(cursor)}" if cursor else "")
                + " SETTINGS preview_length = -1"
            )
            response = client.request("POST", "/btql", json={"query": query, "fmt": "json"})
            data = _rows(_body(client, response).get("data"))
            rows += data
            cursor = response.headers.get("x-bt-cursor") or response.headers.get("x-amz-meta-bt_cursor")
            if not data or not cursor:
                break
    return rows[:limit]


# ---------------------------------------------------------------- dispatch

FETCHERS = {
    "langfuse": fetch_langfuse,
    "helicone": fetch_helicone,
    "langsmith": fetch_langsmith,
    "braintrust": fetch_braintrust,
}

# credential key -> environment variables, first set wins; `required` lists alternatives per slot.
ENV: dict[str, dict[str, tuple[str, ...]]] = {
    "langfuse": {
        "public_key": ("LANGFUSE_PUBLIC_KEY",),
        "secret_key": ("LANGFUSE_SECRET_KEY",),
        "host": ("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    },
    "helicone": {"api_key": ("HELICONE_API_KEY",), "host": ("HELICONE_HOST",)},
    "langsmith": {
        "api_key": ("LANGSMITH_API_KEY",),
        "project": ("LANGSMITH_PROJECT",),
        "host": ("LANGSMITH_ENDPOINT",),
        "workspace_id": ("LANGSMITH_WORKSPACE_ID",),
    },
    "braintrust": {
        "api_key": ("BRAINTRUST_API_KEY",),
        "project": ("BRAINTRUST_PROJECT",),
        "project_id": ("BRAINTRUST_PROJECT_ID",),
        "host": ("BRAINTRUST_API_URL",),
    },
}
REQUIRED: dict[str, list[tuple[str, ...]]] = {
    "langfuse": [("public_key",), ("secret_key",)],
    "helicone": [("api_key",)],
    "langsmith": [("api_key",), ("project",)],
    "braintrust": [("api_key",), ("project", "project_id")],
}


def _source(source: str) -> str:
    source = source.strip().lower()
    if source not in FETCHERS:
        if source == "raw":
            raise PullError("Raw provider logs have no API to pull from; export them to JSONL and use sundr import raw")
        raise PullError(f"Unknown source {source!r}; expected one of {', '.join(FETCHERS)}")
    return source


def credentials_from_env(source: str, environ: dict | None = None) -> dict:
    """Credentials for `source` from environment variables (see ENV); a PullError names any that are missing."""
    source, environ = _source(source), os.environ if environ is None else environ
    found = {}
    for key, names in ENV[source].items():
        value = next((environ[n].strip() for n in names if environ.get(n, "").strip()), None)
        if value:
            found[key] = value
    missing = [
        " or ".join(ENV[source][k][0] for k in slot) for slot in REQUIRED[source] if not any(k in found for k in slot)
    ]
    if missing:
        raise PullError(f"Set {' and '.join(missing)} to pull from {NAMES[source]}")
    return found


def parse_filter(text: str) -> tuple[str, str]:
    """`name=value` from the command line."""
    key, sep, value = text.partition("=")
    if not sep or not key.strip() or not value.strip():
        raise PullError(f"Filters look like name=value, not {text!r}")
    return key.strip(), value.strip()


def fetch(
    source: str,
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Raw LLM-call rows from `source`, newest first, in the shape `load_export(path, source)` reads."""
    source = _source(source)
    if limit < 1 or since_days <= 0:
        raise PullError("limit and since_days must be positive")
    rows = FETCHERS[source](
        credentials, limit=limit, since_days=since_days, filters=filters, transport=transport, timeout_s=timeout_s
    )
    if not rows:
        narrowed = f" matching {', '.join(f'{k}={v}' for k, v in filters.items())}" if filters else ""
        raise PullError(
            f"{NAMES[source]} has no LLM calls from the last {since_days:g} days{narrowed}. "
            "Widen the window, or check the project and filters."
        )
    return rows


def pull(
    source: str,
    credentials: dict,
    *,
    limit: int = 30_000,
    since_days: float = 14,
    filters: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    task_key: str | None = "task",
    verdict_field: str | None = None,
) -> list[JudgmentCall]:
    """Fetch recent LLM calls from `source` and map them as `load_export` would; blocking, so run it off the event loop.

    Raises PullError (safe to show the user) on bad credentials, an unknown
    project or filter, rate limits that outlast the time budget, or nothing to pull.
    """
    rows = fetch(
        source, credentials, limit=limit, since_days=since_days, filters=filters, transport=transport, timeout_s=timeout_s
    )
    calls = map_rows(rows, _source(source), task_key=task_key, verdict_field=verdict_field)
    if not calls:
        raise PullError(f"{NAMES[_source(source)]} returned {len(rows):,} rows, but none were LLM calls Sundr can read")
    return calls


def write_rows(rows: list[dict], path: str | Path) -> None:
    """JSONL that `load_export(path, source)` and `sundr import` read back."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
