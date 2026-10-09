"""Point the OpenAI or Anthropic SDK at the Sundr proxy.

    from sundr.client import openai_client, task

    client = openai_client()  # workspace key from SUNDR_KEY, provider key as usual
    client.chat.completions.create(..., extra_headers=task("ticket-router"))

The factories only set the proxy base URL and the X-Sundr-Key header; every other argument
(api_key, timeout, max_retries, http_client, ...) goes to the SDK unchanged. The SDKs are
imported on first use, so neither is a dependency of this module.

By default the client also falls back to the provider: if the proxy can't be reached, or
something in front of it answers with a gateway error, the same request goes straight to
OpenAI or Anthropic without the Sundr headers. Pass `fallback=False` to turn that off; it is
also off when you bring your own `http_client`.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    import anthropic
    import openai

PROXY_URL = "https://proxy.sundr.ai"
OPENAI_PROVIDER_URL = "https://api.openai.com/v1"
ANTHROPIC_PROVIDER_URL = "https://api.anthropic.com"
# Gateway errors from a proxy that is down or restarting (including Cloudflare's 52x).
FALLBACK_STATUS = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 525, 526})
OPENAI_BASE_URL = PROXY_URL + "/openai/v1"
ANTHROPIC_BASE_URL = PROXY_URL + "/anthropic"
KEY_HEADER = "X-Sundr-Key"
TASK_HEADER = "X-Sundr-Task"
SERVED_BY_HEADER = "x-sundr-served-by"
MAX_TASK_NAME = 200  # the proxy stores task names in a String(200) column

State = Literal["shadow", "takeover", "passthrough"]


class SundrError(RuntimeError):
    """The control API answered with a non-2xx status."""

    def __init__(self, status_code: int, detail: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def _key(sundr_key: str | None) -> str:
    key = sundr_key or os.environ.get("SUNDR_KEY")
    if not key:
        raise ValueError("No Sundr workspace key: pass sundr_key= or set SUNDR_KEY")
    return key


def _sdk(package: str):
    try:
        return importlib.import_module(package)
    except ImportError as exc:
        raise ImportError(f"This sundr.client helper needs the {package} package: pip install {package}") from exc


def _rewrite(hx, request, proxy_base: str, provider_base: str):
    """The same request sent to the provider, minus the Sundr headers; None if it isn't a proxy request."""
    url = str(request.url)
    if not url.startswith(proxy_base):
        return None
    drop = {KEY_HEADER.lower(), TASK_HEADER.lower(), "host", "content-length"}
    headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in drop]
    return hx.Request(request.method, provider_base + url[len(proxy_base):], headers=headers,
                      content=request.content, extensions=request.extensions)


def _fallback_transports(hx):
    """Fallback transports for one httpx-compatible library (anthropic 1.x is built on httpx2)."""

    class FallbackTransport(hx.BaseTransport):
        """Send to the proxy; when it's unreachable or a gateway error comes back, send to the provider directly.

        Only connection failures and gateway statuses fall back, never read timeouts, so a slow
        LLM call is not sent twice.
        """

        def __init__(self, proxy_base: str, provider_base: str, inner=None):
            self.proxy_base, self.provider_base = proxy_base.rstrip("/"), provider_base.rstrip("/")
            self.inner = inner or hx.HTTPTransport()

        def handle_request(self, request):
            direct = _rewrite(hx, request, self.proxy_base, self.provider_base)
            try:
                response = self.inner.handle_request(request)
                if direct is None or response.status_code not in FALLBACK_STATUS:
                    return response
                response.close()
            except (hx.ConnectError, hx.ConnectTimeout):
                if direct is None:
                    raise
            return self.inner.handle_request(direct)

        def close(self) -> None:
            self.inner.close()

    class AsyncFallbackTransport(hx.AsyncBaseTransport):
        """`FallbackTransport` for the async clients."""

        def __init__(self, proxy_base: str, provider_base: str, inner=None):
            self.proxy_base, self.provider_base = proxy_base.rstrip("/"), provider_base.rstrip("/")
            self.inner = inner or hx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            direct = _rewrite(hx, request, self.proxy_base, self.provider_base)
            try:
                response = await self.inner.handle_async_request(request)
                if direct is None or response.status_code not in FALLBACK_STATUS:
                    return response
                await response.aclose()
            except (hx.ConnectError, hx.ConnectTimeout):
                if direct is None:
                    raise
            return await self.inner.handle_async_request(direct)

        async def aclose(self) -> None:
            await self.inner.aclose()

    return FallbackTransport, AsyncFallbackTransport


FallbackTransport, AsyncFallbackTransport = _fallback_transports(httpx)
_transports = {httpx: (FallbackTransport, AsyncFallbackTransport)}


def _httpx_of(sdk):
    """The httpx-compatible module an SDK's own client class is built on."""
    for cls in sdk.DefaultHttpxClient.__mro__:
        root = cls.__module__.split(".")[0]
        if cls.__name__ == "Client" and root.startswith("httpx"):
            return importlib.import_module(root)
    return httpx


def _client(package: str, sundr_key: str | None, base_url: str, kwargs: dict[str, Any], provider_url: str,
            fallback: bool, is_async: bool):
    headers = {**(kwargs.get("default_headers") or {}), KEY_HEADER: _key(sundr_key)}
    options = {**kwargs, "base_url": base_url, "default_headers": headers}
    sdk = _sdk(package)
    if fallback and "http_client" not in kwargs:
        hx = _httpx_of(sdk)
        sync_cls, async_cls = _transports.setdefault(hx, _fallback_transports(hx))
        if is_async:
            options["http_client"] = sdk.DefaultAsyncHttpxClient(transport=async_cls(base_url, provider_url))
        else:
            options["http_client"] = sdk.DefaultHttpxClient(transport=sync_cls(base_url, provider_url))
    return sdk, options


def openai_client(sundr_key: str | None = None, *, base_url: str = OPENAI_BASE_URL, fallback: bool = True,
                  **kwargs: Any) -> openai.OpenAI:
    """An `openai.OpenAI` that sends every call through the Sundr proxy.

    `sundr_key` defaults to SUNDR_KEY; other keyword arguments go to `openai.OpenAI`.
    """
    sdk, options = _client("openai", sundr_key, base_url, kwargs, OPENAI_PROVIDER_URL, fallback, False)
    return sdk.OpenAI(**options)


def async_openai_client(sundr_key: str | None = None, *, base_url: str = OPENAI_BASE_URL,
                        fallback: bool = True, **kwargs: Any) -> openai.AsyncOpenAI:
    """`openai_client`, returning an `openai.AsyncOpenAI`."""
    sdk, options = _client("openai", sundr_key, base_url, kwargs, OPENAI_PROVIDER_URL, fallback, True)
    return sdk.AsyncOpenAI(**options)


def anthropic_client(sundr_key: str | None = None, *, base_url: str = ANTHROPIC_BASE_URL,
                     fallback: bool = True, **kwargs: Any) -> anthropic.Anthropic:
    """An `anthropic.Anthropic` that sends every call through the Sundr proxy.

    `sundr_key` defaults to SUNDR_KEY; other keyword arguments go to `anthropic.Anthropic`.
    """
    sdk, options = _client("anthropic", sundr_key, base_url, kwargs, ANTHROPIC_PROVIDER_URL, fallback, False)
    return sdk.Anthropic(**options)


def async_anthropic_client(sundr_key: str | None = None, *, base_url: str = ANTHROPIC_BASE_URL,
                           fallback: bool = True, **kwargs: Any) -> anthropic.AsyncAnthropic:
    """`anthropic_client`, returning an `anthropic.AsyncAnthropic`."""
    sdk, options = _client("anthropic", sundr_key, base_url, kwargs, ANTHROPIC_PROVIDER_URL, fallback, True)
    return sdk.AsyncAnthropic(**options)


def task(name: str) -> dict[str, str]:
    """Headers naming the judgment call, for a request's `extra_headers=`.

    Use one name per decision, not per prompt version. Calls without a task pass straight through.
    """
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_TASK_NAME or "\n" in name or "\r" in name:
        raise ValueError(f"A task name is 1-{MAX_TASK_NAME} characters with no line breaks")
    return {TASK_HEADER: name}


def served_by(response: Any) -> str | None:
    """Who answered a call: "sundr", "llm", or None when the proxy didn't tag it (calls without a task).

    The SDKs' parsed results don't keep headers, so ask for the raw response:

        raw = client.chat.completions.with_raw_response.create(..., extra_headers=task("ticket-router"))
        served_by(raw)            # "sundr" or "llm"
        completion = raw.parse()  # the usual ChatCompletion

    Anything with `.headers` works, including an `httpx.Response`.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        raise TypeError("served_by needs a raw response: call .with_raw_response.create(...) and pass its result")
    return httpx.Headers(headers).get(SERVED_BY_HEADER)


class Control:
    """The proxy's control API: list a workspace's tasks and move them between states.

        control = Control()                                # workspace key from SUNDR_KEY
        control.tasks()                                    # states, certificates and live error
        control.set_state("ticket-router", "passthrough")  # pause takeover

    Pass `http_client` to bring your own `httpx.Client` (proxies, timeouts, tests).
    """

    def __init__(self, sundr_key: str | None = None, *, base_url: str = PROXY_URL,
                 http_client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self._headers = {KEY_HEADER: _key(sundr_key)}
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=30)

    def tasks(self) -> list[dict[str, Any]]:
        """Every task in the workspace: name, state, epsilon, calls, verdict_field, certificate, monitor."""
        return self._request("GET", "/sundr/v1/tasks")

    def set_state(self, name: str, state: State) -> dict[str, Any]:
        """Move a task to shadow, takeover (needs a certificate) or passthrough."""
        return self._request("POST", f"/sundr/v1/tasks/{quote(name, safe='')}/state", json={"state": state})

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Control:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, self.base_url + path, headers=self._headers, **kwargs)
        if not response.is_success:
            detail = _detail(response)
            raise SundrError(response.status_code, detail,
                             f"{method} {path} returned {response.status_code}: {detail}")
        return response.json()


def _detail(response: httpx.Response) -> str:
    """FastAPI's {"detail": ...} when present, else the start of the body."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail) if detail else (response.text.strip()[:300] or response.reason_phrase)
