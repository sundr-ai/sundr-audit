import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from sundr import client as sundr

def test_task_names_the_judgment_call():
    assert sundr.task("ticket-router") == {"X-Sundr-Task": "ticket-router"}
    assert sundr.task("x" * 200) == {"X-Sundr-Task": "x" * 200}
    for bad in ["", "x" * 201, "ticket\nrouter", "ticket\rrouter", None]:
        with pytest.raises(ValueError, match="1-200 characters"):
            sundr.task(bad)


def test_a_workspace_key_is_required(monkeypatch):
    monkeypatch.delenv("SUNDR_KEY", raising=False)
    with pytest.raises(ValueError, match="SUNDR_KEY"):
        sundr.Control()
    for factory in [sundr.openai_client, sundr.async_openai_client, sundr.anthropic_client, sundr.async_anthropic_client]:
        with pytest.raises(ValueError, match="SUNDR_KEY"):
            factory(api_key="sk-provider")


@pytest.mark.parametrize("factory, package", [
    (sundr.openai_client, "openai"), (sundr.async_openai_client, "openai"),
    (sundr.anthropic_client, "anthropic"), (sundr.async_anthropic_client, "anthropic"),
])
def test_factories_explain_a_missing_sdk(monkeypatch, factory, package):
    monkeypatch.setitem(sys.modules, package, None)
    with pytest.raises(ImportError, match=f"needs the {package} package: pip install {package}"):
        factory("sk-sundr-test")


def test_anthropic_clients_point_at_the_proxy(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    monkeypatch.setenv("SUNDR_KEY", "sk-sundr-env")
    client = sundr.anthropic_client(api_key="sk-ant-test", max_retries=0, default_headers={"x-team": "support"})
    assert isinstance(client, anthropic.Anthropic)
    assert str(client.base_url) == "https://proxy.sundr.ai/anthropic/"
    assert client.default_headers["X-Sundr-Key"] == "sk-sundr-env" and client.default_headers["x-team"] == "support"
    assert client.api_key == "sk-ant-test" and client.max_retries == 0

    local = sundr.async_anthropic_client("sk-sundr-arg", base_url="http://localhost:8080/anthropic", api_key="k")
    assert isinstance(local, anthropic.AsyncAnthropic)
    assert str(local.base_url) == "http://localhost:8080/anthropic/" and local.default_headers["X-Sundr-Key"] == "sk-sundr-arg"


def test_openai_clients_point_at_the_proxy():
    openai = pytest.importorskip("openai")
    client = sundr.openai_client("sk-sundr-test", api_key="sk-test")
    assert isinstance(client, openai.OpenAI)
    assert str(client.base_url) == "https://proxy.sundr.ai/openai/v1/"
    assert client.default_headers["X-Sundr-Key"] == "sk-sundr-test"
    assert isinstance(sundr.async_openai_client("sk-sundr-test", api_key="sk-test"), openai.AsyncOpenAI)


def test_served_by_reads_raw_responses():
    assert sundr.served_by(httpx.Response(200, headers={"X-Sundr-Served-By": "sundr"})) == "sundr"
    assert sundr.served_by(SimpleNamespace(headers={"x-sundr-served-by": "llm"})) == "llm"
    assert sundr.served_by(httpx.Response(200)) is None
    with pytest.raises(TypeError, match="with_raw_response"):
        sundr.served_by(SimpleNamespace(choices=[]))  # a parsed result


def test_control_sends_the_key_and_encodes_task_names(monkeypatch):
    monkeypatch.setenv("SUNDR_KEY", "sk-sundr-env")
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "a/b c", "state": "shadow"}])
        return httpx.Response(200, json={"name": "a/b c", **json.loads(request.content)})

    http = httpx.Client(transport=httpx.MockTransport(api))
    with sundr.Control(base_url="http://localhost:8080/", http_client=http) as control:
        assert control.tasks() == [{"name": "a/b c", "state": "shadow"}]
        assert control.set_state("a/b c", "passthrough") == {"name": "a/b c", "state": "passthrough"}
    assert not http.is_closed  # a client you pass in stays yours
    listed, moved = seen
    assert str(listed.url) == "http://localhost:8080/sundr/v1/tasks" and listed.headers["x-sundr-key"] == "sk-sundr-env"
    assert moved.method == "POST" and moved.url.raw_path == b"/sundr/v1/tasks/a%2Fb%20c/state"
    assert json.loads(moved.content) == {"state": "passthrough"}


@pytest.mark.parametrize("response, detail", [
    (httpx.Response(400, json={"detail": "state must be one of ('shadow',)"}), "state must be one of ('shadow',)"),
    (httpx.Response(502, text="  <html>bad gateway</html>\n"), "<html>bad gateway</html>"),
    (httpx.Response(404), "Not Found"),
])
def test_control_raises_on_non_2xx(response, detail):
    http = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(sundr.SundrError) as err:
        sundr.Control("sk-sundr-test", http_client=http).set_state("ticket-router", "shadow")
    assert err.value.status_code == response.status_code and err.value.detail == detail
    assert str(err.value) == f"POST /sundr/v1/tasks/ticket-router/state returned {response.status_code}: {detail}"


def _proxy_down(status=None, error=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "proxy.sundr.ai":
            if error:
                raise error
            return httpx.Response(status, text="gateway")
        return httpx.Response(200, json={"ok": True})

    return seen, httpx.MockTransport(handler)


@pytest.mark.parametrize("status,error", [(None, httpx.ConnectError("refused")), (502, None), (525, None)])
def test_fallback_sends_the_request_straight_to_the_provider(status, error):
    seen, inner = _proxy_down(status, error)
    transport = sundr.FallbackTransport(sundr.ANTHROPIC_BASE_URL, sundr.ANTHROPIC_PROVIDER_URL, inner)
    with httpx.Client(transport=transport) as http:
        response = http.post("https://proxy.sundr.ai/anthropic/v1/messages", json={"model": "m"},
                             headers={"x-api-key": "sk-ant", "X-Sundr-Key": "sk-sundr", "X-Sundr-Task": "t"})
    assert response.json() == {"ok": True}
    direct = seen[-1]
    assert str(direct.url) == "https://api.anthropic.com/v1/messages"
    assert direct.headers["x-api-key"] == "sk-ant" and "x-sundr-key" not in direct.headers
    assert "x-sundr-task" not in direct.headers and json.loads(direct.content) == {"model": "m"}


def test_fallback_leaves_real_answers_and_read_timeouts_alone():
    seen, inner = _proxy_down(500)
    transport = sundr.FallbackTransport(sundr.OPENAI_BASE_URL, sundr.OPENAI_PROVIDER_URL, inner)
    with httpx.Client(transport=transport) as http:
        assert http.post("https://proxy.sundr.ai/openai/v1/chat/completions", json={}).status_code == 500
    assert len(seen) == 1  # a 500 is the provider's (or Sundr's) answer, not an outage

    seen, inner = _proxy_down(error=httpx.ReadTimeout("slow"))
    transport = sundr.FallbackTransport(sundr.OPENAI_BASE_URL, sundr.OPENAI_PROVIDER_URL, inner)
    with httpx.Client(transport=transport) as http, pytest.raises(httpx.ReadTimeout):
        http.post("https://proxy.sundr.ai/openai/v1/chat/completions", json={})
    assert len(seen) == 1  # never send a slow call twice


async def _async_fallback():
    seen, inner = _proxy_down(error=httpx.ConnectError("refused"))

    async def handler(request):
        return inner.handle_request(request)

    transport = sundr.AsyncFallbackTransport(sundr.OPENAI_BASE_URL, sundr.OPENAI_PROVIDER_URL,
                                             httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.post("https://proxy.sundr.ai/openai/v1/chat/completions", json={})
    return response, seen


def test_async_fallback():
    import asyncio

    response, seen = asyncio.run(_async_fallback())
    assert response.status_code == 200 and str(seen[-1].url) == "https://api.openai.com/v1/chat/completions"


def test_factories_install_fallback_unless_told_not_to(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("SUNDR_KEY", "sk-sundr-env")
    client = sundr.anthropic_client(api_key="k")
    assert type(client._client._transport).__name__ == "FallbackTransport"
    assert type(sundr.anthropic_client(api_key="k", fallback=False)._client._transport).__name__ != "FallbackTransport"
    assert type(sundr.async_anthropic_client(api_key="k")._client._transport).__name__ == "AsyncFallbackTransport"
