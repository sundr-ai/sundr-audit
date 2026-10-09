import asyncio
import json

import httpx
import pytest

from sundr.backends.base import BackendError
from sundr.backends.systemone import Limits, SystemOneBackend, available_backends, question_payload
from sundr.types import Question

QUESTIONS = [
    Question("urgent", "noul", "Is this request urgent?"),
    Question("team", "choice", "Which team?", ("billing", "technical"), ("Refunds", "Errors")),
    Question("severity", "score", "How severe?", ("low", "medium", "high")),
]


def _reply(body: dict) -> dict:
    answers = {}
    for qid, q in body["questions"].items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.8}
        elif q["type"] == "choice":
            opts = list(q["criteria"])
            answers[qid] = {"type": "choice", "choice": opts[0], "probabilities": {opts[0]: 0.7, opts[1]: 0.3}, "confidence": 0.4}
        else:
            answers[qid] = {"type": "score", "score": 1.2, "probabilities": {"0": 0.2, "1": 0.4, "2": 0.4}}
    return {"model": body.get("model", "x"), "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}


def test_payload_shapes():
    noul, choice, score = (question_payload(q) for q in QUESTIONS)
    assert noul == {"type": "noul", "instructions": "Is this request urgent?"}
    assert choice["criteria"] == {"billing": "Refunds", "technical": "Errors"}
    assert score["criteria"] == ["low", "medium", "high"]


def test_round_trip_and_chunking():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json=_reply(body))

    backend = SystemOneBackend("jev", "https://x/v1/systemone", "jev-1.13.0", "k", 0.042,
                               Limits(max_questions=2), transport=httpx.MockTransport(handler))
    result = asyncio.run(backend.ask("Checkout is down", QUESTIONS))
    assert len(calls) == 2 and all(c["model"] == "jev-1.13.0" for c in calls)
    assert result.input_tokens == 200
    assert result.answers["urgent"].probs == {"yes": 0.8, "no": pytest.approx(0.2)}
    assert result.answers["severity"].probs == {"low": 0.2, "medium": 0.4, "high": 0.4}


def test_cloudflare_envelope_and_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"result": _reply(json.loads(request.content)), "success": True, "errors": []})

    backend = SystemOneBackend("clef", "https://cf/ai/run/@cf/cloudflare/clef", "clef", "t", 0.24,
                               envelope="cloudflare", transport=httpx.MockTransport(handler))
    result = asyncio.run(backend.ask("state", QUESTIONS[:1]))
    assert attempts["n"] == 2 and result.answers["urgent"].probs["yes"] == 0.8


def test_non_retryable_error_raises():
    backend = SystemOneBackend("jev", "https://x", "m", transport=httpx.MockTransport(lambda r: httpx.Response(422, text="bad")))
    with pytest.raises(BackendError, match="422"):
        asyncio.run(backend.ask("s", QUESTIONS[:1]))


def test_backend_selection_from_keys(monkeypatch):
    for key in ("SUNDR_BACKENDS", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_AUTH_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    assert available_backends() == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "x")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "x")
    assert available_backends() == ["openrouter-jev", "clef-flash"]
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert available_backends() == ["jev", "clef-flash"]
    monkeypatch.setenv("SUNDR_BACKENDS", "ollama:jev, clef")
    assert available_backends() == ["ollama:jev", "clef"]
