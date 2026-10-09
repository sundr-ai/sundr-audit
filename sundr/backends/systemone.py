"""Clients for decision models that speak TypeSafe's System One wire format.

TypeSafe Jev, Ollama 0.35+, Laya, Strands Decider and Ollaya all accept
`POST /v1/systemone`. Cloudflare Clef takes the same body at a Workers AI URL
and wraps the reply in a `result` envelope. See docs/research/backends.md.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass

import httpx

from ..types import Answer, Question
from .base import BackendError, BackendResult

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


@dataclass(frozen=True)
class Limits:
    max_questions: int = 64
    max_options: int = 255
    max_score_levels: int = 10


def question_payload(q: Question) -> dict:
    """One question in System One form."""
    body: dict = {"type": q.type, "instructions": q.instructions}
    if q.type == "choice":
        descriptions = q.descriptions if len(q.descriptions) == len(q.options) else (None,) * len(q.options)
        body["criteria"] = {opt: (desc or None) for opt, desc in zip(q.options, descriptions)}
    elif q.type == "score":
        levels = q.descriptions if len(q.descriptions) == len(q.options) and all(q.descriptions) else q.options
        body["criteria"] = list(levels)
    elif q.criteria:
        body["criteria"] = {"true": q.criteria, "false": None}
    return body


def parse_answer(q: Question, raw: dict) -> Answer:
    """System One answer to an Answer keyed by Sundr option names."""
    if q.type == "noul":
        p = float(raw["noul"])
        return Answer({"yes": p, "no": 1.0 - p})
    probs = raw.get("probabilities") or {}
    if q.type == "score":
        return Answer({q.options[int(k)]: float(v) for k, v in probs.items() if int(k) < len(q.options)})
    return Answer({k: float(v) for k, v in probs.items()})


class SystemOneBackend:
    """Any server implementing `POST /v1/systemone`."""

    def __init__(
        self,
        name: str,
        url: str,
        model: str | None,
        api_key: str | None = None,
        usd_per_million_input_tokens: float = 0.0,
        limits: Limits = Limits(),
        timeout_s: float = 30.0,
        max_retries: int = 5,
        envelope: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.url = url
        self.model = model or name
        self._body_model = model
        self.usd_per_million_input_tokens = usd_per_million_input_tokens
        self.limits = limits
        self.max_retries = max_retries
        self.envelope = envelope
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(headers=headers, timeout=timeout_s, transport=transport)

    def _check(self, questions: list[Question]) -> None:
        for q in questions:
            if q.type == "choice" and len(q.options) > self.limits.max_options:
                raise BackendError(f"{self.name}: '{q.name}' has {len(q.options)} options (max {self.limits.max_options})")
            if q.type == "score" and not 2 <= len(q.options) <= self.limits.max_score_levels:
                raise BackendError(f"{self.name}: '{q.name}' needs 2-{self.limits.max_score_levels} levels")

    async def _post(self, body: dict) -> dict:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.post(self.url, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise BackendError(f"{self.name}: {exc!r}") from exc
            else:
                if response.status_code < 400:
                    data = response.json()
                    if self.envelope == "cloudflare":
                        if not data.get("success", True):
                            raise BackendError(f"{self.name}: {data.get('errors')}")
                        data = data["result"]
                    return data
                if response.status_code not in RETRY_STATUS or attempt == self.max_retries:
                    raise BackendError(f"{self.name}: HTTP {response.status_code}: {response.text[:500]}")
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 30.0)
        raise BackendError(f"{self.name}: retries exhausted")

    async def ask(self, state: str, questions: list[Question]) -> BackendResult:
        self._check(questions)
        answers: dict[str, Answer] = {}
        tokens, model, started = 0, self.model, time.perf_counter()
        step = self.limits.max_questions
        for i in range(0, len(questions), step):
            chunk = questions[i : i + step]
            body: dict = {"state": state, "questions": {q.name: question_payload(q) for q in chunk}}
            if self._body_model:
                body["model"] = self._body_model
            data = await self._post(body)
            raw_answers = data.get("answers") or {}
            for q in chunk:
                if q.name not in raw_answers:
                    raise BackendError(f"{self.name}: no answer for '{q.name}'")
                answers[q.name] = parse_answer(q, raw_answers[q.name])
            tokens += int((data.get("usage") or {}).get("input_tokens", 0))
            model = data.get("model") or model
        latency = (time.perf_counter() - started) * 1000
        return BackendResult(answers=answers, input_tokens=tokens, latency_ms=latency, model=model)

    async def aclose(self) -> None:
        await self.client.aclose()


def jev(model: str = "jev-1.13.0", **kwargs) -> SystemOneBackend:
    """TypeSafe Jev. Pinned to a version: never `jev-latest`, so certificates stay valid."""
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise BackendError("Set TYPESAFE_API_KEY to use Jev")
    return SystemOneBackend("jev", f"{base}/v1/systemone", model, key, 0.042, **kwargs)


def openrouter_jev(model: str = "typesafe/jev-1.13", **kwargs) -> SystemOneBackend:
    """Jev through OpenRouter's System One passthrough (no TypeSafe waitlist)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BackendError("Set OPENROUTER_API_KEY to use Jev through OpenRouter")
    return SystemOneBackend("jev", "https://openrouter.ai/api/v1/systemone", model, key, 0.042, **kwargs)


def clef(flash: bool = False, **kwargs) -> SystemOneBackend:
    """Cloudflare Clef or Clef-flash on Workers AI."""
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_AUTH_TOKEN")
    if not (account and token):
        raise BackendError("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AUTH_TOKEN to use Clef")
    short = "clef-flash" if flash else "clef"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{short}"
    price = 0.09 if flash else 0.24
    backend = SystemOneBackend(short, url, short, token, price, Limits(max_questions=64), envelope="cloudflare", **kwargs)
    backend.neurons_per_million_tokens = 8182 if flash else 21818  # Workers AI pricing page, Oct 2026
    return backend


def local(name: str, base_url: str, model: str | None, limits: Limits = Limits(), **kwargs) -> SystemOneBackend:
    """A self-hosted System One server (Ollama, Laya, Strands Decider, Ollaya)."""
    return SystemOneBackend(name, f"{base_url.rstrip('/')}/v1/systemone", model, os.environ.get("SYSTEMONE_API_KEY"), 0.0, limits, **kwargs)


def _ollama_url() -> str:
    """Ollama's address from OLLAMA_HOST, as Ollama itself reads it ("host:port" or a URL); localhost by default."""
    host = os.environ.get("OLLAMA_HOST", "").strip() or "localhost:11434"
    host = host if "://" in host else f"http://{host}"
    return host.replace("://0.0.0.0", "://localhost")


def available_backends() -> list[str]:
    """Backend specs to use by default: SUNDR_BACKENDS if set, else the hosted ones whose keys are set."""
    configured = os.environ.get("SUNDR_BACKENDS")
    if configured:
        return [b.strip() for b in configured.split(",") if b.strip()]
    found = []
    if os.environ.get("TYPESAFE_API_KEY"):
        found.append("jev")
    elif os.environ.get("OPENROUTER_API_KEY"):
        found.append("openrouter-jev")
    if os.environ.get("CLOUDFLARE_AUTH_TOKEN") and os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        found.append("clef-flash")
    return found


def make_backend(spec: str) -> SystemOneBackend:
    """Build a backend from a CLI spec.

    jev | jev-<version> | openrouter-jev | clef | clef-flash | ollama:<model> | laya | strands | url:<base>[#model]
    """
    if spec == "openrouter-jev":
        return openrouter_jev()
    if spec == "jev" or spec.startswith("jev-"):
        return jev(spec if spec != "jev" else "jev-1.13.0")
    if spec in ("clef", "clef-flash"):
        return clef(flash=spec == "clef-flash")
    if spec.startswith("ollama:"):
        return local(spec, _ollama_url(), spec.split(":", 1)[1], Limits(max_questions=64, max_options=26, max_score_levels=26))
    if spec == "laya":
        return local("laya", os.environ.get("LAYA_URL", "http://localhost:8000"), None, Limits(max_options=100))
    if spec == "strands":
        return local("strands", os.environ.get("STRANDS_URL", "http://127.0.0.1:8000"), None)
    if spec.startswith("url:"):
        base, _, model = spec[4:].partition("#")
        return local(base, base, model or None)
    raise BackendError(f"Unknown backend '{spec}'")
