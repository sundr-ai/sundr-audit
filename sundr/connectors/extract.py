"""Find the verdict inside logged OpenAI and Anthropic calls (docs/research/log-formats.md §5–6)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import urlparse

CHAT = "openai.chat"
RESPONSES = "openai.responses"
ANTHROPIC = "anthropic.messages"
UNKNOWN = "unknown"

TEXT_PARTS = {"text", "input_text", "output_text"}
OPENAI_ONLY_PARTS = {"input_text", "output_text", "refusal"}
ANTHROPIC_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use", "server_tool_use"}
RESPONSE_ITEMS = {"message", "reasoning", "function_call"}
SKIPPED_PARTS = {"thinking", "redacted_thinking", "reasoning"}
ROLE_ALIASES = {"developer": "system", "human": "user", "ai": "assistant", "model": "assistant", "function": "tool"}
ROLES = {"system", "user", "assistant", "tool"}
FINISH_STATUS = {
    "length": "truncated",
    "max_tokens": "truncated",
    "model_context_window_exceeded": "truncated",
    "content_filter": "content_filtered",
    "refusal": "refusal",
}
LC_CLASSES = {
    "SystemMessage": "system",
    "HumanMessage": "user",
    "AIMessage": "assistant",
    "ToolMessage": "tool",
    "FunctionMessage": "tool",
}

VERDICT_NAME = re.compile(
    r"^(verdict|label|decision|classification|class|category|route|routing|intent|outcome|result|"
    r"judg(e)?ment|grade|score|rating|answer|pass(ed)?|approved|valid|is_.+|should_.+|has_.+)$",
    re.I,
)
RATIONALE_NAME = re.compile(r"reason|rationale|explanation|justification|thought|analysis|evidence|comment|notes", re.I)
CONFIDENCE_NAME = re.compile(r"confidence|probability|certainty|likelihood", re.I)
RATIONALE_ORDER = (
    "rationale", "explanation", "reasoning", "justification", "reason",
    "thought", "analysis", "evidence", "comment", "notes",
)
GENERIC_NAMES = frozenset(
    name.lower()
    for name in (
        "OpenAI-generation", "ChatOpenAI", "ChatAnthropic", "Chat Completion", "openai.chat",
        "AzureChatOpenAI", "ChatBedrock", "ChatBedrockConverse", "ChatVertexAI", "ChatGoogleGenerativeAI",
        "RunnableSequence", "RunnableLambda", "RunnableParallel", "LangGraph",
        "openai.responses", "anthropic.messages", "chat.completions.create", "openai.chat.completions.create",
        "responses.create", "anthropic.messages.create", "messages.create",
        "response", "output", "schema", "json_schema",
    )
)

_FENCE = re.compile(r"```[ \t]*(?:json5?|JSON)?[ \t]*\n?(.*?)```", re.S)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)'[^'\n]*'(?!\w)")
_DIGITS = re.compile(r"\d+")
_MISS = object()


class Extracted(NamedTuple):
    """What one logged response says: §6.3's (payload, channel, schema, schema_source, status) plus extras."""

    payload: Any
    channel: str
    schema: dict | None
    schema_source: str | None
    status: str
    schema_name: str | None = None
    model: str | None = None


@dataclass
class Tokens:
    input: int | None = None
    output: int | None = None
    cached: int | None = None
    reasoning: int | None = None


@dataclass
class FieldChoice:
    """A verdict pointer, or the tied candidates when none could be chosen."""

    pointer: str | None = None
    candidates: list[str] = field(default_factory=list)
    ambiguous: bool = False
    score: int = 0


@dataclass
class Verdict:
    value: Any
    type: str | None
    options: list | None
    ok: bool = True


# ---------------------------------------------------------------- coercion


def to_float(value: Any) -> float | None:
    """A finite float from a number or numeric string, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(round(number))


def loads(value: Any) -> Any:
    """Parse a JSON string; anything else, or text that is not JSON, comes back unchanged."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def as_dict(value: Any) -> dict:
    value = loads(value)
    return value if isinstance(value, dict) else {}


def _first(d: dict, *keys: str) -> Any:
    return next((d[k] for k in keys if d.get(k) is not None), None)


def parse_json(payload: Any) -> tuple[bool, Any]:
    """Parse a judge payload: a dict as-is, else JSON, fenced JSON, then the first balanced {...}."""
    if isinstance(payload, (dict, list)):
        return True, payload
    if not isinstance(payload, str) or not payload.strip():
        return False, None
    for candidate in _json_candidates(payload.strip()):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, str) and value.strip().startswith("{"):
            return parse_json(value)
        return True, value
    return False, None


def _json_candidates(text: str) -> Iterator[str]:
    yield text
    for match in _FENCE.finditer(text):
        yield match.group(1).strip()
    yield from _balanced_objects(text)


def _balanced_objects(text: str, limit: int = 64) -> Iterator[str]:
    start = text.find("{")
    while start != -1 and limit > 0:
        limit -= 1
        end = _closing_brace(text, start)
        if end is not None:
            yield text[start : end + 1]
        start = text.find("{", start + 1)


def _closing_brace(text: str, start: int) -> int | None:
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


# ---------------------------------------------------------------- tokens


def usage_tokens(usage: Any, dialect: str = UNKNOWN) -> Tokens:
    """Normalise a provider usage block (§5.4): input includes cache, output includes reasoning."""
    if not isinstance(usage, dict):
        return Tokens()
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        prompt = as_dict(usage.get("prompt_tokens_details"))
        completion = as_dict(usage.get("completion_tokens_details"))
        return Tokens(
            to_int(usage.get("prompt_tokens")),
            to_int(usage.get("completion_tokens")),
            to_int(prompt.get("cached_tokens")),
            to_int(completion.get("reasoning_tokens")),
        )
    anthropic_keys = "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    if anthropic_keys or (dialect == ANTHROPIC and "input_token_details" not in usage):
        base = to_int(usage.get("input_tokens"))
        read = to_int(usage.get("cache_read_input_tokens"))
        write = to_int(usage.get("cache_creation_input_tokens"))
        total = None if base is None else base + (read or 0) + (write or 0)
        thinking = as_dict(usage.get("output_tokens_details")).get("thinking_tokens")
        return Tokens(total, to_int(usage.get("output_tokens")), read, to_int(thinking))
    inputs = as_dict(usage.get("input_tokens_details") or usage.get("input_token_details"))
    outputs = as_dict(usage.get("output_tokens_details") or usage.get("output_token_details"))
    return Tokens(
        to_int(usage.get("input_tokens")),
        to_int(usage.get("output_tokens")),
        to_int(_first(inputs, "cached_tokens", "cache_read")),
        to_int(_first(outputs, "reasoning_tokens", "reasoning")),
    )


def langfuse_tokens(details: dict) -> Tokens:
    """Langfuse usageDetails: detail buckets are subtracted from input/output, so sum them back."""

    def total(word: str) -> int | None:
        values = [to_int(v) for k, v in details.items() if word in k.lower()]
        values = [v for v in values if v is not None]
        return sum(values) if values else None

    cached = _first(details, "input_cached_tokens", "input_cache_read", "cache_read_input_tokens", "input_cached")
    reasoning = _first(details, "output_reasoning_tokens", "output_reasoning")
    return Tokens(total("input"), total("output"), to_int(cached), to_int(reasoning))


# ---------------------------------------------------------------- messages


def text_of(value: Any, sep: str = "\n\n") -> str:
    """The text parts of a string, content block, or list of blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        return sep.join(t for t in (_part_text(p) for p in value) if t)
    return str(value)


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and (part.get("type") in TEXT_PARTS or ("type" not in part and "text" in part)):
        return str(part.get("text") or "")
    return ""


def normalize_messages(raw: Any) -> list[dict]:
    """Canonical {role, content} turns from Chat messages, Responses input items or Anthropic messages."""
    if isinstance(raw, str):
        raw = [{"role": "user", "content": raw}]
    elif isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [m for m in (_message(item) for item in raw) if m is not None]


def _message(item: Any) -> dict | None:
    if isinstance(item, str):
        return {"role": "user", "content": item}
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "")
    if kind == "function_call_output":
        return {"role": "tool", "content": text_of(item.get("output"), "\n")}
    if kind in ("reasoning", "item_reference"):
        return None
    if kind.endswith("_call"):
        return {"role": "assistant", "content": [{"type": "other", "kind": kind}]}
    role = str(item.get("role") or "user").lower()
    role = ROLE_ALIASES.get(role, role)
    content = _content(item.get("content"))
    if not content and item.get("tool_calls"):
        content = [{"type": "other", "kind": "tool_call"}]
    return {"role": role if role in ROLES else "user", "content": content}


def _content(content: Any) -> str | list[dict]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return str(content)
    parts = [p for p in (_part(x) for x in content) if p is not None]
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def _part(part: Any) -> dict | None:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    kind = str(part.get("type") or "")
    if kind in SKIPPED_PARTS:
        return None
    if kind in TEXT_PARTS or (not kind and "text" in part):
        return {"type": "text", "text": str(part.get("text") or "")}
    if kind == "refusal":
        return {"type": "text", "text": str(part.get("refusal") or "")}
    if "image" in kind:
        return {"type": "image"}
    if kind in ("file", "input_file", "document"):
        return {"type": "file"}
    return {"type": "other", "kind": kind or "unknown"}


def split_system(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Leading system/developer turns as text, and the conversation after them."""
    lead, i = [], 0
    while i < len(messages) and messages[i]["role"] == "system":
        lead.append(text_of(messages[i]["content"], "\n"))
        i += 1
    return lead, messages[i:]


def request_messages(req: Any) -> tuple[str | None, list[dict]]:
    """(system_prompt, input_messages) from a Chat, Responses or Anthropic request body."""
    if not isinstance(req, dict):
        return None, []
    head = [text_of(req.get(key)) for key in ("instructions", "system")]
    raw = req["messages"] if "messages" in req else req.get("input")
    lead, rest = split_system(normalize_messages(raw))
    parts = [p.strip() for p in head + lead if p and p.strip()]
    return ("\n\n".join(parts) or None), rest


def lc_message(message: Any) -> dict | None:
    """A LangChain-serialised message ({lc, id, kwargs}, {type, data} or (role, text)) as {role, content}."""
    if isinstance(message, (list, tuple)) and len(message) == 2:
        return {"role": str(message[0]), "content": message[1]}
    if isinstance(message, str):
        return {"role": "user", "content": message}
    if not isinstance(message, dict):
        return None
    if "role" in message and "kwargs" not in message:
        return message
    kwargs = _lc_kwargs(message)
    ids = message.get("id")
    cls = ids[-1].removesuffix("Chunk") if isinstance(ids, list) and ids and isinstance(ids[-1], str) else ""
    outer = message.get("type") if message.get("type") != "constructor" else None
    role = kwargs.get("role") or kwargs.get("type") or LC_CLASSES.get(cls) or outer or "user"
    return {"role": role, "content": kwargs.get("content")}


def _lc_kwargs(message: Any) -> dict:
    if not isinstance(message, dict):
        return {}
    for key in ("kwargs", "data"):
        if isinstance(message.get(key), dict):
            return message[key]
    return message


# ---------------------------------------------------------------- schemas


def unwrap_schema(fmt: Any) -> tuple[dict | None, str | None]:
    """The JSON Schema and its name inside a response_format, text.format, output_config.format or tool."""
    fmt = loads(fmt)
    if not isinstance(fmt, dict):
        return None, None
    if _looks_like_schema(fmt):
        return fmt, fmt.get("title")
    for key in ("json_schema", "format", "function"):
        if isinstance(loads(fmt.get(key)), dict):
            schema, name = unwrap_schema(fmt[key])
            return schema, name or fmt.get("name")
    for key in ("schema", "parameters", "input_schema"):
        schema = loads(fmt.get(key))
        if isinstance(schema, dict):
            return schema, fmt.get("name") or schema.get("title")
    return None, None


def _looks_like_schema(d: dict) -> bool:
    return d.get("type") == "object" or "properties" in d


def tool_schema(tools: Any, name: str | None) -> dict | None:
    """The parameter schema of the named tool (OpenAI nested, Responses flat, or Anthropic input_schema)."""
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        fn = tool["function"] if isinstance(tool.get("function"), dict) else tool
        if fn.get("name") == name:
            schema = loads(fn.get("parameters") or fn.get("input_schema"))
            return schema if isinstance(schema, dict) else None
    return None


def escape_pointer(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def split_pointer(pointer: str) -> list[str]:
    if not pointer:
        return []
    pointer = pointer if pointer.startswith("/") else "/" + pointer
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def normalize_pointer(pointer: str | None) -> str | None:
    if pointer is None or pointer == "" or pointer.startswith("/"):
        return pointer
    return "/" + pointer


def get_pointer(obj: Any, pointer: str) -> tuple[bool, Any]:
    """RFC 6901 lookup: (found, value)."""
    for part in split_pointer(pointer):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return False, None
    return True, obj


def _resolve(node: Any, root: dict) -> dict:
    """Follow local $refs and single-member allOf wrappers."""
    for _ in range(16):
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#"):
            found, target = get_pointer(root, ref[1:])
            rest = {k: v for k, v in node.items() if k != "$ref"}
            node = {**(target if found and isinstance(target, dict) else {}), **rest}
            continue
        all_of = node.get("allOf")
        if isinstance(all_of, list) and len(all_of) == 1:
            rest = {k: v for k, v in node.items() if k != "allOf"}
            node = {**_resolve(all_of[0], root), **rest}
            continue
        return node
    return node if isinstance(node, dict) else {}


def _is_null(node: dict) -> bool:
    return node.get("type") == "null" or ("const" in node and node["const"] is None) or node.get("enum") == [None]


def _unwrap_nullable(node: Any, root: dict) -> tuple[dict, bool]:
    """Strip the null member of ["T","null"] or anyOf [T, null]; report whether null is allowed."""
    node, nullable = _resolve(node, root), False
    kind = node.get("type")
    if isinstance(kind, list) and "null" in kind:
        rest = [k for k in kind if k != "null"]
        node, nullable = {**node, "type": rest[0] if len(rest) == 1 else rest}, True
    for key in ("anyOf", "oneOf"):
        members = node.get(key)
        if not isinstance(members, list):
            continue
        resolved = [_resolve(m, root) for m in members]
        concrete = [m for m in resolved if not _is_null(m)]
        nullable = nullable or len(concrete) < len(resolved)
        if len(concrete) == 1 and "const" not in concrete[0]:
            inner, inner_null = _unwrap_nullable({**{k: v for k, v in node.items() if k != key}, **concrete[0]}, root)
            return inner, nullable or inner_null
        node = {**node, key: concrete}
    if isinstance(node.get("enum"), list) and None in node["enum"]:
        nullable = True
    return node, nullable


def _options(node: dict) -> list | None:
    enum = node.get("enum")
    if isinstance(enum, list):
        return [v for v in enum if v is not None]
    if "const" in node and node["const"] is not None:
        return [node["const"]]
    for key in ("anyOf", "oneOf"):
        members = node.get(key)
        if isinstance(members, list) and members and all(
            isinstance(m, dict) and ("const" in m or isinstance(m.get("enum"), list)) for m in members
        ):
            values = [v for m in members for v in ([m["const"]] if "const" in m else m["enum"])]
            return [v for v in values if v is not None]
    return None


def _type(node: dict) -> str | None:
    kind = node.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind is None:
        options = _options(node)
        if options:
            kind = value_type(options[0])
    return kind if isinstance(kind, str) else None


def value_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _is_object(node: dict) -> bool:
    return node.get("type") == "object" or ("properties" in node and "type" not in node)


def _is_scalar(node: dict) -> bool:
    return _options(node) is not None or _type(node) in ("string", "integer", "number", "boolean")


def schema_fields(schema: dict | None) -> Iterator[tuple[str, str, dict, bool]]:
    """Scalar properties as (pointer, name, node, required): the top level and one nested level down."""
    if not isinstance(schema, dict):
        return

    def walk(node: Any, prefix: str, depth: int) -> Iterator[tuple[str, str, dict, bool]]:
        node = _resolve(node, schema)
        required = set(node.get("required") or [])
        for name, sub in (node.get("properties") or {}).items():
            sub, _ = _unwrap_nullable(sub, schema)
            pointer = f"{prefix}/{escape_pointer(name)}"
            if _is_object(sub):
                if depth < 2:
                    yield from walk(sub, pointer, depth + 1)
            elif _is_scalar(sub):
                yield pointer, name, sub, name in required

    yield from walk(schema, "", 1)


def schema_node(schema: dict | None, pointer: str | None) -> tuple[dict | None, bool]:
    """The (resolved) property schema at a pointer into the output, and whether it is nullable."""
    if not isinstance(schema, dict) or pointer is None:
        return None, False
    node, nullable = _resolve(schema, schema), False
    for part in split_pointer(pointer):
        props = node.get("properties") or {}
        if part not in props:
            return None, False
        node, nullable = _unwrap_nullable(props[part], schema)
    return node, nullable


def missing_required(parsed: dict, schema: dict | None) -> list[str]:
    if not isinstance(schema, dict):
        return []
    root = _resolve(schema, schema)
    return [key for key in root.get("required") or [] if key not in parsed]


def option_descriptions(schema: dict | None, pointer: str | None) -> dict:
    """Per-option descriptions from anyOf/oneOf consts or x-enum-descriptions style arrays."""
    node, _ = schema_node(schema, pointer)
    if not node:
        return {}
    out = {}
    for key in ("anyOf", "oneOf"):
        for member in node.get(key) or []:
            if isinstance(member, dict) and member.get("const") is not None:
                text = member.get("description") or member.get("title")
                if text:
                    out[member["const"]] = text
    values = node.get("enum") or []
    for key in ("x-enum-descriptions", "x-enumDescriptions", "enumDescriptions", "enum_descriptions"):
        texts = node.get(key)
        if isinstance(texts, list):
            out.update({v: t for v, t in zip(values, texts) if t})
        elif isinstance(texts, dict):
            out.update({v: t for v, t in texts.items() if t})
    return out


# ---------------------------------------------------------------- verdicts


def _int_values(node: dict) -> list[int] | None:
    low, high = _bounds(node)
    if low is None or high is None or not 0 <= high - low <= 10:
        return None
    return list(range(int(low), int(high) + 1))


def _bounds(node: dict) -> tuple[float | None, float | None]:
    low, high = to_float(node.get("minimum")), to_float(node.get("maximum"))
    if low is None and to_float(node.get("exclusiveMinimum")) is not None:
        low = math.floor(to_float(node["exclusiveMinimum"])) + 1
    if high is None and to_float(node.get("exclusiveMaximum")) is not None:
        high = math.ceil(to_float(node["exclusiveMaximum"])) - 1
    return low, high


def field_score(name: str, node: dict, required: bool) -> int | None:
    """§6.5 step 2 score for one scalar property; None when it is not a verdict candidate."""
    options, kind = _options(node), _type(node)
    if options is None and kind != "boolean" and RATIONALE_NAME.search(name):
        return None
    if options is None and kind == "number" and CONFIDENCE_NAME.search(name):
        return None
    if options and all(isinstance(o, (str, int, bool)) for o in options):
        signal = 4
    elif kind == "boolean":
        signal = 3
    elif kind == "integer":
        signal = 3 if _int_values(node) else 1
    else:
        signal = 0
    named = bool(VERDICT_NAME.match(name))
    if not signal and not named:
        return None
    return signal + 2 * named + int(required)


def select_verdict_field(schema: dict | None) -> FieldChoice:
    """§6.5 step 2: the best-scoring scalar property. Ties at score >= 4 are ambiguous."""
    scored = []
    for pointer, name, node, required in schema_fields(schema):
        score = field_score(name, node, required)
        if score is not None:
            scored.append((score, pointer))
    if not scored:
        return FieldChoice()
    best = max(score for score, _ in scored)
    tied = [pointer for score, pointer in scored if score == best]
    if len(tied) == 1:
        return FieldChoice(tied[0], tied, False, best)
    return FieldChoice(None, tied, best >= 4, best)


def _is_scalar_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _fold(value: Any) -> Any:
    return value.strip().casefold() if isinstance(value, str) else (type(value).__name__, value)


def infer_verdict_field(outputs: list[dict], candidates: list[str] | None = None) -> FieldChoice:
    """§6.5 step 3: the low-cardinality key present in >= 95% of a task's parsed outputs."""
    outputs = [o for o in outputs if isinstance(o, dict)]
    if not outputs:
        return FieldChoice()
    if candidates is None:
        keys = sorted({k for o in outputs for k, v in o.items() if _is_scalar_value(v)})
        candidates = ["/" + escape_pointer(k) for k in keys]
    stats = []
    for pointer in candidates:
        values = [v for found, v in (get_pointer(o, pointer) for o in outputs) if found and _is_scalar_value(v)]
        if len(values) < 0.95 * len(outputs):
            continue
        name = (split_pointer(pointer) or [""])[-1]
        present = [v for v in values if v is not None]
        if RATIONALE_NAME.search(name) and not all(isinstance(v, bool) for v in present):
            continue
        if CONFIDENCE_NAME.search(name) and all(value_type(v) in ("integer", "number") for v in present):
            continue
        distinct = len({_fold(v) for v in present})
        stats.append((pointer, bool(VERDICT_NAME.match(name)), distinct, len(values)))

    def fits(ratio: float) -> list[tuple]:
        return [s for s in stats if 0 < s[2] <= 20 and s[2] / s[3] <= ratio]

    pool = fits(0.05) or (fits(0.5) if len(outputs) < 400 else [])
    pool = [s for s in pool if s[2] > 1] or pool
    if not pool:
        return FieldChoice()
    rank = {s[0]: (not s[1], s[2]) for s in pool}
    best = min(rank.values())
    tied = [pointer for pointer, r in rank.items() if r == best]
    if len(tied) == 1:
        return FieldChoice(tied[0], tied)
    return FieldChoice(None, tied, True)


def normalize_verdict(value: Any, node: dict | None, nullable: bool = False) -> Verdict:
    """§6.5 step 4: coerce the raw value to the property's type. ok=False means schema_mismatch."""
    if node is None:
        return Verdict(value.strip() if isinstance(value, str) else value, value_type(value), None, True)
    options, kind = _options(node), _type(node)
    if options is not None:
        vtype, opts = "enum", options
    elif kind == "boolean":
        vtype, opts = "boolean", [True, False]
    elif kind == "integer":
        vtype, opts = "integer", _int_values(node)
    else:
        vtype, opts = kind, None
    if value is None:
        return Verdict(None, vtype, opts, nullable)
    if vtype == "enum":
        hit = _match_option(value, opts)
        return Verdict(value if hit is _MISS else hit, vtype, opts, hit is not _MISS)
    if vtype == "boolean":
        return _as_bool(value, opts)
    if vtype == "integer":
        return _as_int(value, node, opts)
    if vtype == "number":
        return Verdict(value, vtype, None, value_type(value) in ("integer", "number"))
    if vtype == "string":
        return Verdict(value.strip() if isinstance(value, str) else value, vtype, None, isinstance(value, str))
    return Verdict(value, value_type(value), None, _is_scalar_value(value))


def _match_option(value: Any, options: list) -> Any:
    for option in options:
        same_kind = isinstance(option, bool) == isinstance(value, bool) and (
            type(option) is type(value) or (value_type(option) in ("integer", "number") and value_type(value) in ("integer", "number"))
        )
        if same_kind and option == value:
            return option
    if isinstance(value, str):
        folded = value.strip().casefold()
        for option in options:
            if isinstance(option, str) and option.strip().casefold() == folded:
                return option
    return _MISS


def _as_bool(value: Any, options: list) -> Verdict:
    if isinstance(value, bool):
        return Verdict(value, "boolean", options, True)
    word = value.strip().lower() if isinstance(value, str) else None
    if word in ("true", "yes"):
        return Verdict(True, "boolean", options, False)
    if word in ("false", "no"):
        return Verdict(False, "boolean", options, False)
    return Verdict(value, "boolean", options, False)


def _as_int(value: Any, node: dict, options: list | None) -> Verdict:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Verdict(value, "integer", options, False)
    if isinstance(value, float):
        if not value.is_integer():
            return Verdict(value, "integer", options, False)
        value = int(value)
    low, high = _bounds(node)
    in_range = (low is None or value >= low) and (high is None or value <= high)
    return Verdict(value, "integer", options, in_range)


def _rationale_rank(name: str) -> tuple[int, int] | None:
    low = name.lower()
    for i, word in enumerate(RATIONALE_ORDER):
        if low == word:
            return i, 0
    for i, word in enumerate(RATIONALE_ORDER):
        if word in low:
            return i, 1
    return None


def rationale_of(schema: dict | None, parsed: dict) -> str | None:
    """§6.5 step 5: the stated rationale string (rationale > explanation > reasoning > justification)."""
    if schema:
        fields = [(p, n) for p, n, node, _ in schema_fields(schema) if _type(node) == "string" and _options(node) is None]
    else:
        fields = [("/" + escape_pointer(k), k) for k, v in parsed.items() if isinstance(v, str)]
    ranked = sorted((_rationale_rank(n), p.count("/"), p) for p, n in fields if _rationale_rank(n))
    for _, _, pointer in ranked:
        found, value = get_pointer(parsed, pointer)
        if found and isinstance(value, str):
            return value
    return None


# ---------------------------------------------------------------- dialects


def _response_items(items: Any) -> bool:
    return (
        isinstance(items, list)
        and bool(items)
        and all(isinstance(i, dict) and (i.get("type") in RESPONSE_ITEMS or str(i.get("type", "")).endswith("_call")) for i in items)
    )


def _anthropic_blocks(content: Any) -> bool:
    if not isinstance(content, list) or not content or not all(isinstance(b, dict) for b in content):
        return False
    kinds = {b.get("type") for b in content}
    return bool(kinds & ANTHROPIC_BLOCKS) and not kinds & OPENAI_ONLY_PARTS


def _is_anthropic_message(resp: dict) -> bool:
    content = resp.get("content")
    return resp.get("type") == "message" and isinstance(content, list) and ("stop_reason" in resp or _anthropic_blocks(content))


def detect_dialect(resp: Any, req: Any = None, url: str | None = None) -> str:
    """Which wire format a logged response is in (§6.3): by shape, then by URL path, then by request shape."""
    if isinstance(resp, dict):
        if resp.get("object") == "chat.completion" or "choices" in resp:
            return CHAT
        if resp.get("object") == "response" or _response_items(resp.get("output")):
            return RESPONSES
        if _is_anthropic_message(resp):
            return ANTHROPIC
    path = urlparse(url).path.rstrip("/") if isinstance(url, str) else ""
    for suffix, dialect in (("/chat/completions", CHAT), ("/responses", RESPONSES), ("/messages", ANTHROPIC)):
        if path.endswith(suffix):
            return dialect
    if isinstance(req, dict):
        if "input" in req or "instructions" in req:
            return RESPONSES
        if "output_config" in req or "output_format" in req or isinstance(req.get("system"), (str, list)):
            return ANTHROPIC
        if "messages" in req:
            return CHAT
    return UNKNOWN


def coerce_response(output: Any) -> tuple[str, Any]:
    """Wrap a bare logged output (a message, choices, output items or content blocks) as a provider response."""
    if isinstance(output, dict):
        dialect = detect_dialect(output)
        if dialect != UNKNOWN:
            return dialect, output
        if _anthropic_blocks(output.get("content")):
            return ANTHROPIC, {"type": "message", **output}
        if output.get("type") in RESPONSE_ITEMS:
            return RESPONSES, {"output": [output]}
        if isinstance(output.get("message"), dict):
            return CHAT, {"choices": [output]}
        if any(k in output for k in ("role", "content", "tool_calls", "refusal", "function_call")):
            return CHAT, {"choices": [{"message": output, "finish_reason": output.get("finish_reason")}]}
        if isinstance(output.get("messages"), list) and output["messages"]:
            return coerce_response(output["messages"][-1])
        return UNKNOWN, output
    if isinstance(output, list) and output and all(isinstance(o, dict) for o in output):
        if all(isinstance(o.get("message"), dict) for o in output):
            return CHAT, {"choices": output}
        if _response_items(output):
            return RESPONSES, {"output": output}
        if _anthropic_blocks(output):
            return ANTHROPIC, {"type": "message", "content": output}
        return coerce_response(output[0])
    return UNKNOWN, output


def _forced_tool(req: dict) -> str | None:
    choice = req.get("tool_choice")
    if isinstance(choice, dict):
        return choice.get("name") or as_dict(choice.get("function")).get("name")
    return as_dict(req.get("function_call")).get("name")


def _pick_named(items: list[dict], name: str | None) -> dict:
    return next((i for i in items if name and i.get("name") == name), items[0])


def chat(req: dict, resp: dict) -> Extracted:
    """OpenAI Chat Completions: the first choice's refusal, tool call, or JSON text."""
    req, resp = as_dict(req), as_dict(resp)
    fmt = loads(req.get("response_format"))
    schema, name = unwrap_schema(fmt)
    src = "openai.response_format" if schema else ("openai.json_object" if as_dict(fmt).get("type") == "json_object" else None)
    model = resp.get("model") or req.get("model")
    choices = [c for c in resp.get("choices") or [] if isinstance(c, dict)]
    if not choices:
        return Extracted(None, "none", schema, src, "no_structured_output", name, model)
    choice = choices[0]
    msg = as_dict(choice.get("message"))
    status = FINISH_STATUS.get(choice.get("finish_reason"), "ok")
    if msg.get("refusal"):
        return Extracted(None, "none", schema, src, "refusal", name, model)
    calls = [as_dict(c.get("function")) for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
    if not calls and isinstance(msg.get("function_call"), dict):
        calls = [msg["function_call"]]
    if calls:
        fn = _pick_named(calls, _forced_tool(req))
        tool = fn.get("name")
        tools = req.get("tools") or req.get("functions")
        return Extracted(fn.get("arguments"), "tool_call", tool_schema(tools, tool), f"tool:{tool}", status, tool, model)
    payload = text_of(msg.get("content"), "") or (msg.get("parsed") if isinstance(msg.get("parsed"), dict) else None)
    return Extracted(payload or None, "content", schema, src, status, name, model)


def responses(req: dict, resp: dict) -> Extracted:
    """OpenAI Responses: skip reasoning and commentary items; never index output[0]."""
    req, resp = as_dict(req), as_dict(resp)
    fmt = as_dict(as_dict(resp.get("text")).get("format")) or as_dict(as_dict(req.get("text")).get("format"))
    schema, name = unwrap_schema(fmt)
    src = "openai.text.format" if schema else ("openai.json_object" if fmt.get("type") == "json_object" else None)
    model = resp.get("model") or req.get("model")
    state = resp.get("status")
    if state == "incomplete":
        reason = as_dict(resp.get("incomplete_details")).get("reason")
        status = "content_filtered" if reason == "content_filter" else "truncated"
    else:
        status = "no_structured_output" if state == "failed" else "ok"
    texts = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            tool = item.get("name")
            tools = [*(req.get("tools") or []), *(resp.get("tools") or [])]
            return Extracted(item.get("arguments"), "tool_call", tool_schema(tools, tool), f"tool:{tool}", status, tool, model)
        if item.get("type") == "message" and item.get("phase") != "commentary":
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            for part in content or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    return Extracted(None, "none", schema, src, "refusal", name, model)
                if part.get("type") in TEXT_PARTS:
                    texts.append(str(part.get("text") or ""))
    text = "".join(texts) or (resp["output_text"] if isinstance(resp.get("output_text"), str) else "")
    return Extracted(text or None, "content", schema, src, status, name, model)


def anthropic(req: dict, resp: dict) -> Extracted:
    """Anthropic Messages: JSON in a text block or a tool_use input. Blocks are chosen by type, never content[0]."""
    req, resp = as_dict(req), as_dict(resp)
    fmt, src = as_dict(as_dict(req.get("output_config")).get("format")), "anthropic.output_config.format"
    if not fmt:
        fmt, src = as_dict(req.get("output_format")), "anthropic.output_format"
    schema, _ = unwrap_schema(fmt)
    src = src if schema else None
    model = resp.get("model") or req.get("model")
    status = FINISH_STATUS.get(resp.get("stop_reason"), "ok")
    if as_dict(resp.get("stop_details")).get("type") == "refusal":
        status = "refusal"
    content = resp.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    if uses:
        block = _pick_named(uses, _forced_tool(req))
        tool = block.get("name")
        return Extracted(block.get("input"), "tool_call", tool_schema(req.get("tools"), tool), f"tool:{tool}", status, tool, model)
    text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
    return Extracted(text or None, "content", schema, src, status, None, model)


def langchain_output(outputs: dict) -> Extracted:
    """A LangChain LLMResult (`outputs.generations`) from a LangSmith run. The schema comes from the run's extra."""
    outputs = as_dict(outputs)
    gens = outputs.get("generations") or []
    if gens and isinstance(gens[0], list):
        gens = gens[0]
    gen = gens[0] if gens and isinstance(gens[0], dict) else {}
    message = _lc_kwargs(gen.get("message"))
    extra = as_dict(message.get("additional_kwargs"))
    meta = as_dict(message.get("response_metadata"))
    finish = as_dict(gen.get("generation_info")).get("finish_reason") or meta.get("finish_reason") or meta.get("stop_reason")
    status = FINISH_STATUS.get(finish, "ok")
    model = as_dict(outputs.get("llm_output")).get("model_name") or meta.get("model_name") or meta.get("model")
    if extra.get("refusal"):
        return Extracted(None, "none", None, None, "refusal", None, model)
    if isinstance(extra.get("parsed"), dict):
        return Extracted(extra["parsed"], "content", None, None, status, None, model)
    calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    if calls:
        tool = calls[0].get("name")
        return Extracted(calls[0].get("args"), "tool_call", None, f"tool:{tool}", status, tool, model)
    raw_calls = [as_dict(c.get("function")) for c in extra.get("tool_calls") or [] if isinstance(c, dict)]
    if raw_calls:
        tool = raw_calls[0].get("name")
        return Extracted(raw_calls[0].get("arguments"), "tool_call", None, f"tool:{tool}", status, tool, model)
    content = message.get("content", gen.get("text"))
    if isinstance(content, list):
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if uses:
            tool = uses[0].get("name")
            return Extracted(uses[0].get("input"), "tool_call", None, f"tool:{tool}", status, tool, model)
        content = text_of(content, "")
    return Extracted(content or gen.get("text") or None, "content", None, None, status, None, model)


def extract_output(req: Any, resp: Any, dialect: str | None = None) -> Extracted:
    """Dispatch to chat(), responses() or anthropic(); an unknown output is taken as the payload itself."""
    req = as_dict(req)
    dialect = dialect or detect_dialect(resp, req)
    if dialect == CHAT:
        return chat(req, resp)
    if dialect == RESPONSES:
        return responses(req, resp)
    if dialect == ANTHROPIC:
        return anthropic(req, resp)
    schema, name = unwrap_schema(req.get("response_format"))
    src = "openai.response_format" if schema else None
    if resp in (None, "", {}, []):
        return Extracted(None, "none", schema, src, "no_structured_output", name, req.get("model"))
    return Extracted(resp, "content", schema, src, "ok", name, req.get("model"))


# ---------------------------------------------------------------- tasks


def is_generic(name: Any) -> bool:
    return not isinstance(name, str) or not name.strip() or name.strip().lower() in GENERIC_NAMES


def normalize_prompt(text: str | None) -> str:
    """Mask UUIDs, quoted literals and digits so templated prompts share one fingerprint."""
    text = _UUID.sub("<uuid>", text or "")
    text = _QUOTED.sub("<str>", text)
    text = _DIGITS.sub("#", text)
    return " ".join(text.split())


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(system_prompt: str | None, schema: dict | None) -> str:
    blob = normalize_prompt(system_prompt) + canonical_json(schema)
    return "task_" + hashlib.sha256(blob.encode()).hexdigest()[:8]


def derive_task(names: list[Any], schema_name: str | None, system_prompt: str | None, schema: dict | None) -> str:
    """§6.4: the first non-generic name (rules 1–4, in the order given), else the fingerprint (rule 5)."""
    for name in [*names, schema_name]:
        if not is_generic(name):
            return name.strip()
    return fingerprint(system_prompt, schema)
