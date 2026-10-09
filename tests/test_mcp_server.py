import asyncio
import json

import httpx
import pytest

from sundr import demo, mcp_server
from sundr.backends.simulated import SimulatedBackend
from sundr.compiler import save_bank

BACKEND_KEYS = ("SUNDR_BACKENDS", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_AUTH_TOKEN",
                "CLOUDFLARE_ACCOUNT_ID", "ANTHROPIC_API_KEY")


@pytest.fixture
def network(monkeypatch):
    """Every request the tools make lands here; nothing reaches the internet."""
    seen: list[httpx.Request] = []
    replies: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.get((request.method, str(request.url)))
        if reply is None:
            raise httpx.ConnectError("no network in tests", request=request)
        return reply

    monkeypatch.setattr(mcp_server, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.delenv("SUNDR_SITE_URL", raising=False)
    monkeypatch.delenv("SUNDR_PROXY_URL", raising=False)
    return seen, replies


@pytest.fixture
def no_keys(monkeypatch):
    for key in BACKEND_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_logs(path, n):
    recs = demo.records(n=n)
    path.write_text("\n".join(json.dumps({"id": r.id, "input": r.input, "verdict": r.reference, "cost_usd": 0.004})
                              for r in recs) + "\n")
    return recs


def test_setup_guide_prefers_the_live_copy(network):
    seen, replies = network
    replies[("GET", "https://sundr.ai/setup.md")] = httpx.Response(200, text="# Set up a Sundr Savings Audit\n\nlive copy\n")
    guide = mcp_server.sundr_setup_guide()
    assert guide.startswith("# Set up a Sundr Savings Audit\n\nlive copy")
    assert "sundr_hosted_audit" in guide and "confirm=false" in guide  # how to use the tools instead of curl
    assert len(seen) == 1


@pytest.mark.parametrize("reply", [None, httpx.Response(503, text="down"), httpx.Response(200, text="<html>login</html>")])
def test_setup_guide_falls_back_to_the_bundled_copy(network, reply):
    _, replies = network
    if reply is not None:
        replies[("GET", "https://sundr.ai/setup.md")] = reply
    guide = mcp_server.sundr_setup_guide()
    assert "# Set up a Sundr Savings Audit" in guide and "Copy bundled with sundr" in guide
    assert "at least 300 rows" in guide and "{{" not in guide
    assert "sundr_local_audit" in guide


def test_local_audit_refuses_without_backend_keys(tmp_path, monkeypatch, no_keys):
    monkeypatch.chdir(tmp_path)
    _write_logs(tmp_path / "logs.jsonl", 400)
    result = asyncio.run(mcp_server.sundr_local_audit(str(tmp_path / "logs.jsonl"), decision="Is this email phishing?"))
    assert result["status"] == "refused"
    assert "OPENROUTER_API_KEY" in result["how_to_fix"] and "CLOUDFLARE_AUTH_TOKEN" in result["how_to_fix"]
    assert "paste" in result["how_to_fix"]  # never ask for keys in the chat
    assert not (tmp_path / "sundr-audit.html").exists() and not (tmp_path / ".sundr").exists()

    named = asyncio.run(mcp_server.sundr_local_audit(str(tmp_path / "logs.jsonl"), decision="Is this email phishing?",
                                                     backends=["clef-flash"]))
    assert named["status"] == "refused" and "CLOUDFLARE_ACCOUNT_ID" in named["error"]


def test_local_audit_checks_its_inputs(tmp_path, no_keys):
    missing = asyncio.run(mcp_server.sundr_local_audit(str(tmp_path / "nope.jsonl"), decision="x"))
    assert missing["status"] == "refused" and "No file" in missing["error"]
    _write_logs(tmp_path / "small.jsonl", 20)
    small = asyncio.run(mcp_server.sundr_local_audit(str(tmp_path / "small.jsonl"), decision="x"))
    assert small["status"] == "refused" and "at least 100" in small["error"]
    _write_logs(tmp_path / "logs.jsonl", 400)
    no_task = asyncio.run(mcp_server.sundr_local_audit(str(tmp_path / "logs.jsonl")))
    assert no_task["status"] == "refused" and "task_yaml_path" in no_task["error"]


@pytest.fixture
def simulated(monkeypatch, tmp_path, no_keys):
    monkeypatch.chdir(tmp_path)
    recs = _write_logs(tmp_path / "logs.jsonl", 3000)
    truth = {r.input: r.reference for r in recs}
    built = []

    def make(spec):
        built.append(spec)
        return SimulatedBackend(truth, demo.TASK.labels, name=spec, verdict_strength=1.6, signal_strength=1.2)

    monkeypatch.setattr(mcp_server, "make_backend", make)
    save_bank(demo.questions(), tmp_path / "questions.yaml")
    return built


def test_local_audit_runs_in_process_and_writes_a_report(tmp_path, simulated):
    result = asyncio.run(mcp_server.sundr_local_audit(
        str(tmp_path / "logs.jsonl"), decision=demo.TASK.description, backends=["jev-sim"],
        questions_path=str(tmp_path / "questions.yaml"), monthly_calls=1_000_000))
    assert result["status"] == "done", result
    assert simulated == ["jev-sim"]
    assert result["task"] == "logs" and result["calls_audited"] == 3000 and result["held_out_calls"] == 1200
    assert [b["error_budget"] for b in result["budgets"]] == [0.005, 0.01, 0.02, 0.05]
    assert 0.5 < result["agreement_with_your_llm"] <= 1
    assert result["spent_usd"] == 0  # simulated backends are never billed
    html = tmp_path / "sundr-audit.html"
    assert result["report_html"] == str(html.resolve()) and "Savings Audit" in html.read_text()
    assert json.loads((tmp_path / "sundr-audit.json").read_text())["n_records"] == 3000
    json.dumps(result)  # the MCP layer serialises it


def test_local_audit_respects_the_spend_cap(tmp_path, simulated):
    result = asyncio.run(mcp_server.sundr_local_audit(
        str(tmp_path / "logs.jsonl"), decision=demo.TASK.description, backends=["jev-sim"], max_usd=0.000001))
    assert result["status"] == "refused" and "exceeds this run's cap" in result["error"]
    assert result["estimated_usd"] > 0 and "limit=" in result["how_to_fix"]
    assert not (tmp_path / "sundr-audit.html").exists()


def test_local_audit_drafting_needs_a_claude_key(tmp_path, simulated):
    result = asyncio.run(mcp_server.sundr_local_audit(
        str(tmp_path / "logs.jsonl"), decision=demo.TASK.description, backends=["jev-sim"], draft=True))
    assert result["status"] == "refused" and "ANTHROPIC_API_KEY" in result["error"]
    assert simulated == []  # refused before any backend was built


def _export(path, n, labels=("billing", "bug", "how-to")):
    path.write_text("\n".join(json.dumps({"id": f"c{i}", "input": f"Ticket {i}: " + "words " * 60,
                                          "verdict": labels[i % len(labels)]}) for i in range(n)) + "\n")


def test_hosted_audit_only_summarises_until_confirmed(tmp_path, network):
    seen, _ = network
    _export(tmp_path / "sundr-export.jsonl", 330)
    result = mcp_server.sundr_hosted_audit(str(tmp_path / "sundr-export.jsonl"), "Ana@Acme.com",
                                           decision="Route support tickets.", monthly_calls=2_000_000)
    assert result["status"] == "needs_approval" and seen == []  # nothing left the machine
    upload = result["upload"]
    assert upload["usable_rows"] == 330 and upload["labels"] == 3
    assert upload["top_labels"] == {"billing": 110, "bug": 110, "how-to": 110}
    assert len(upload["examples"]) == 3 and upload["examples"][0]["input"].endswith("…")
    assert upload["report_email"] == "ana@acme.com" and upload["bytes"] == (tmp_path / "sundr-export.jsonl").stat().st_size
    assert result["destination"] == "https://sundr.ai/audits" and "confirm=true" in result["next"]


def test_hosted_audit_uploads_once_confirmed(tmp_path, network):
    seen, replies = network
    replies[("POST", "https://sundr.ai/audits")] = httpx.Response(202, json={
        "id": "a1", "status": "awaiting_confirmation", "rows": 330, "task": "uploaded-task",
        "next": "Sundr emailed a confirmation link to ana@acme.com."})
    _export(tmp_path / "sundr-export.jsonl", 330)
    result = mcp_server.sundr_hosted_audit(str(tmp_path / "sundr-export.jsonl"), "ana@acme.com", confirm=True,
                                           decision="Route support tickets.", monthly_calls=2_000_000, usd_per_call=0.004)
    assert result["status"] == "uploaded" and result["audit"]["id"] == "a1" and result["audit"]["rows"] == 330
    assert "confirmation link" in result["tell_user"]
    [request] = seen
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content.decode()
    for field, value in [("email", "ana@acme.com"), ("source", "canonical"), ("decision", "Route support tickets."),
                         ("monthly_calls", "2000000"), ("usd_per_call", "0.004")]:
        assert f'name="{field}"\r\n\r\n{value}\r\n' in body
    assert 'filename="sundr-export.jsonl"' in body and '"verdict": "how-to"' in body


def test_hosted_audit_reports_server_errors(tmp_path, network):
    _, replies = network
    replies[("POST", "https://sundr.ai/audits")] = httpx.Response(
        429, json={"error": "You've already started two audits today."})
    _export(tmp_path / "e.jsonl", 330)
    result = mcp_server.sundr_hosted_audit(str(tmp_path / "e.jsonl"), "ana@acme.com", confirm=True)
    assert result == {"status": "error", "http_status": 429, "error": "You've already started two audits today."}


def test_hosted_audit_refuses_what_the_server_would(tmp_path, network):
    seen, _ = network
    _export(tmp_path / "small.jsonl", 50)
    small = mcp_server.sundr_hosted_audit(str(tmp_path / "small.jsonl"), "ana@acme.com", confirm=True)
    assert small["status"] == "refused" and "at least 300" in small["error"]
    _export(tmp_path / "ok.jsonl", 330)
    assert mcp_server.sundr_hosted_audit(str(tmp_path / "ok.jsonl"), "not-an-email", confirm=True)["status"] == "refused"
    assert mcp_server.sundr_hosted_audit(str(tmp_path / "gone.jsonl"), "ana@acme.com", confirm=True)["status"] == "refused"
    assert seen == []


def test_proxy_tools_need_a_workspace_key(network, monkeypatch):
    monkeypatch.delenv("SUNDR_KEY", raising=False)
    for result in (mcp_server.sundr_list_tasks(), mcp_server.sundr_set_task_state("router", "passthrough")):
        assert result["status"] == "refused" and "SUNDR_KEY" in result["how_to_fix"]
    assert network[0] == []


def test_proxy_tools_call_the_control_api(network, monkeypatch):
    seen, replies = network
    monkeypatch.setenv("SUNDR_KEY", "sk-sundr-test")
    replies[("GET", "https://proxy.sundr.ai/sundr/v1/tasks")] = httpx.Response(
        200, json=[{"name": "ticket router", "state": "shadow", "calls": 12}])
    replies[("POST", "https://proxy.sundr.ai/sundr/v1/tasks/ticket%20router/state")] = httpx.Response(
        200, json={"name": "ticket router", "state": "passthrough"})
    assert mcp_server.sundr_list_tasks() == {"status": "ok", "tasks": [{"name": "ticket router", "state": "shadow", "calls": 12}]}
    assert mcp_server.sundr_set_task_state("ticket router", "passthrough") == {
        "status": "ok", "name": "ticket router", "state": "passthrough"}
    listed, moved = seen
    assert listed.headers["x-sundr-key"] == "sk-sundr-test" and json.loads(moved.content) == {"state": "passthrough"}

    replies[("POST", "https://proxy.sundr.ai/sundr/v1/tasks/ticket%20router/state")] = httpx.Response(
        409, json={"detail": "This task has no certified model yet"})
    assert mcp_server.sundr_set_task_state("ticket router", "takeover") == {
        "status": "error", "http_status": 409, "error": "This task has no certified model yet"}


def test_proxy_tools_report_an_unreachable_proxy(network, monkeypatch):
    monkeypatch.setenv("SUNDR_KEY", "sk-sundr-test")
    monkeypatch.setenv("SUNDR_PROXY_URL", "http://localhost:8100")
    result = mcp_server.sundr_list_tasks()
    assert result["status"] == "error" and "Couldn't reach" in result["error"]
    assert str(network[0][0].url) == "http://localhost:8100/sundr/v1/tasks"


def test_the_server_registers_every_tool():
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == set(mcp_server.TOOLS)
    hosted = tools["sundr_hosted_audit"]
    assert "confirm=false first" in hosted.description
    schema = getattr(hosted, "input_schema", None) or hosted.inputSchema
    assert schema["required"] == ["file_path", "email"] and schema["properties"]["confirm"]["default"] is False
