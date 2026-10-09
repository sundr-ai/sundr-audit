import copy
import gzip
import json

import pytest

from sundr.compiler import TaskSpec
from sundr.connectors import (
    compute_cost,
    fingerprint,
    group_tasks,
    load_export,
    map_row,
    map_rows,
    parse_json,
    select_verdict_field,
    task_spec,
    to_records,
    write_records_jsonl,
    write_task_yaml,
)
from sundr.connectors.extract import normalize_verdict
from sundr.connectors.prices import price_for
from sundr.ingest import read_records

TICKET_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["billing", "bug", "how_to", "other"]},
        "rationale": {"type": "string"},
    },
    "required": ["label", "rationale"],
    "additionalProperties": False,
}
SPAM_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["spam", "ham"]}, "reasoning": {"type": "string"}},
    "required": ["label", "reasoning"],
    "additionalProperties": False,
}
SPAM_SCHEMA_ANTHROPIC = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["spam", "ham"]}, "explanation": {"type": "string"}},
    "required": ["label", "explanation"],
    "additionalProperties": False,
}
LABEL_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["billing", "bug", "other"]}},
    "required": ["label"],
    "additionalProperties": False,
}

# §1.6 Langfuse v2 GENERATION observation.
LANGFUSE_V2 = {
    "id": "a1b2c3d4e5f60718",
    "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
    "projectId": "clx9proj0000",
    "parentObservationId": "f0e1d2c3b4a59687",
    "type": "GENERATION",
    "isRootObservation": False,
    "name": "ticket-classifier",
    "level": "DEFAULT",
    "statusMessage": None,
    "version": None,
    "environment": "production",
    "userId": "user-123",
    "sessionId": None,
    "startTime": "2026-10-07T14:03:21.512Z",
    "endTime": "2026-10-07T14:03:22.424Z",
    "completionStartTime": None,
    "input": json.dumps(
        [
            {"role": "system", "content": "Classify the ticket. Reply in JSON."},
            {"role": "user", "content": "I was charged twice."},
        ]
    ),
    "output": json.dumps(
        {"role": "assistant", "content": json.dumps({"label": "billing", "rationale": "Duplicate charge."})}
    ),
    "metadata": {
        "response_format": json.dumps(
            {"type": "json_schema", "json_schema": {"name": "ticket_label", "strict": True, "schema": TICKET_SCHEMA}}
        )
    },
    "model": "gpt-4o-2024-08-06",
    "modelParameters": {"temperature": 0, "top_p": 1, "frequency_penalty": 0, "presence_penalty": 0},
    "usageDetails": {"input": 412, "output": 18, "total": 430, "input_cached_tokens": 0, "output_reasoning_tokens": 0},
    "costDetails": {"input": 0.00103, "output": 0.00018, "total": 0.00121},
    "totalCost": 0.00121,
    "inputPrice": "0.0000025",
    "outputPrice": "0.00001",
    "totalPrice": None,
    "promptId": None,
    "promptName": None,
    "promptVersion": None,
    "latency": 0.912,
    "timeToFirstToken": None,
    "traceName": "classify-ticket",
    "tags": ["support"],
    "release": None,
}

# §2.6 Helicone query-clickhouse record.
HELICONE = {
    "request_id": "5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33",
    "request_created_at": "2026-10-08T14:03:20.870Z",
    "request_body": {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Classify the support ticket. Return JSON only."},
            {"role": "user", "content": "I was charged twice for my subscription, please refund one."},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "ticket_label",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "confidence", "rationale"],
                    "properties": {
                        "label": {"type": "string", "enum": ["billing", "bug", "how_to", "other"]},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                },
            },
        },
    },
    "response_id": "8f0c2a7e-3d1b-4c55-9a8e-2b6f1d0e4a11",
    "response_created_at": "2026-10-08T14:03:21.512Z",
    "response_status": 200,
    "response_body": {
        "id": "chatcmpl-AbC123",
        "object": "chat.completion",
        "created": 1759932201,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "refusal": None,
                    "content": '{"label":"billing","confidence":0.93,"rationale":"Mentions refund for duplicate charge."}',
                },
            }
        ],
        "usage": {
            "prompt_tokens": 412,
            "completion_tokens": 38,
            "total_tokens": 450,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
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
        "Helicone-Prompt-Id": "ticket_classifier",
    },
    "scores": {"helicone-score-feedback": 1},
    "prompt_id": "",
    "prompt_version": "",
    "cache_reference_id": "00000000-0000-0000-0000-000000000000",
    "cache_enabled": False,
    "country_code": "US",
    "asset_ids": [],
    "storage_location": "clickhouse",
    "updated_at": "2026-10-08T14:03:22.004Z",
}


def lc(cls, kind, content, **extra):
    return {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", cls], "kwargs": {"content": content, "type": kind, **extra}}


# §3.5 LangSmith v1 / bulk-export run (LangChain ChatOpenAI + with_structured_output).
LANGSMITH_V1 = {
    "id": "0ec6b845-18b9-4aa1-8f1b-6ba3f9fdefd6",
    "name": "ChatOpenAI",
    "run_type": "llm",
    "start_time": "2026-09-14T17:02:11.120000",
    "end_time": "2026-09-14T17:02:11.904000",
    "status": "success",
    "error": None,
    "tags": [],
    "trace_id": "0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c11",
    "parent_run_id": "a8024e23-7a4f-4bb1-8f0e-2b7f3c1d9e22",
    "session_id": "1ffd059c-3e8a-4f2b-9c6d-5a4b3c2d1e33",
    "inputs": {
        "messages": [
            [
                lc("SystemMessage", "system", "Classify the ticket. Return JSON {label}."),
                lc("HumanMessage", "human", "My card was charged twice"),
            ]
        ]
    },
    "outputs": {
        "generations": [
            [
                {
                    "text": '{"label":"billing"}',
                    "generation_info": {"finish_reason": "stop", "logprobs": None},
                    "type": "ChatGeneration",
                    "message": lc(
                        "AIMessage",
                        "ai",
                        '{"label":"billing"}',
                        additional_kwargs={"refusal": None},
                        response_metadata={"finish_reason": "stop", "model_name": "gpt-4o-mini-2024-07-18"},
                        tool_calls=[],
                        invalid_tool_calls=[],
                        usage_metadata={"input_tokens": 52, "output_tokens": 6, "total_tokens": 58},
                    ),
                }
            ]
        ],
        "llm_output": {
            "token_usage": {"prompt_tokens": 52, "completion_tokens": 6, "total_tokens": 58},
            "model_name": "gpt-4o-mini-2024-07-18",
        },
        "run": None,
        "type": "LLMResult",
    },
    "extra": {
        "invocation_params": {"model": "gpt-4o-mini", "temperature": 0.0},
        "options": {"ls_structured_output_format": {"kwargs": {"method": "json_schema"}, "schema": LABEL_SCHEMA}},
        "batch_size": 1,
        "metadata": {
            "ls_provider": "openai",
            "ls_model_name": "gpt-4o-mini",
            "ls_model_type": "chat",
            "ls_temperature": 0.0,
            "usage_metadata": {"input_tokens": 52, "output_tokens": 6, "total_tokens": 58},
        },
    },
    "prompt_tokens": 52,
    "completion_tokens": 6,
    "total_tokens": 58,
    "prompt_cost": "0.0000078",
    "completion_cost": "0.0000036",
    "total_cost": "0.0000114",
    "first_token_time": None,
    "feedback_stats": {},
    "reference_example_id": None,
}

WRAP_OPENAI_IO = {
    "inputs": {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Classify the ticket. Return JSON {label}."},
            {"role": "user", "content": "The export button crashes the app"},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "Label",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                    "additionalProperties": False,
                },
            },
        },
    },
    "outputs": {
        "id": "chatcmpl-xyz",
        "object": "chat.completion",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"label":"bug"}', "refusal": None, "tool_calls": None},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage_metadata": {"input_tokens": 52, "output_tokens": 6, "total_tokens": 58},
    },
}

# §4.4 Braintrust span.
BRAINTRUST = {
    "id": "385052b6-50a2-43b4-b52d-9afaa34f0bff",
    "span_id": "70b04fd2-0177-47a9-a70b-e32ca43db131",
    "root_span_id": "70b04fd2-0177-47a9-a70b-e32ca43db131",
    "span_parents": [],
    "is_root": True,
    "_xact_id": "1000196123456789",
    "_pagination_key": "p07384512345678901",
    "created": "2026-09-14T17:02:11.120Z",
    "org_id": "5d2c1a0b-0000-4000-8000-000000000000",
    "project_id": "d709efc0-ac9f-410d-8387-345e1e5074dc",
    "log_id": "g",
    "input": [
        {"role": "system", "content": "Classify the ticket. Return JSON {label}."},
        {"role": "user", "content": "My card was charged twice"},
    ],
    "output": [
        {
            "index": 0,
            "finish_reason": "stop",
            "logprobs": None,
            "message": {"role": "assistant", "content": '{"label":"billing"}', "refusal": None, "tool_calls": None},
        }
    ],
    "expected": None,
    "error": None,
    "scores": None,
    "tags": None,
    "metadata": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "Label", "strict": True, "schema": LABEL_SCHEMA},
        },
    },
    "metrics": {
        "start": 1757869331.120,
        "end": 1757869331.904,
        "prompt_tokens": 52,
        "completion_tokens": 6,
        "tokens": 58,
        "prompt_cached_tokens": 0,
        "completion_reasoning_tokens": 0,
        "time_to_first_token": 0.78,
    },
    "context": {"caller_functionname": "classify", "caller_filename": "/app/classify.py", "caller_lineno": 42},
    "span_attributes": {"name": "Chat Completion", "type": "llm"},
    "origin": None,
}

# §5.1–5.3 raw provider logs.
RAW_CHAT = {
    "request": {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "developer", "content": "Classify the email as spam or ham."},
            {"role": "user", "content": [{"type": "text", "text": "WIN A FREE CRUISE!!! Click now"}]},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "spam_classification", "description": "Spam verdict", "strict": True, "schema": SPAM_SCHEMA},
        },
    },
    "response": {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1760000000,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"label":"spam","reasoning":"Unsolicited prize offer with urgent call to action."}',
                    "refusal": None,
                    "annotations": [],
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 61,
            "completion_tokens": 18,
            "total_tokens": 79,
            "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0},
        },
        "service_tier": "default",
    },
}

RESPONSES_FORMAT = {
    "type": "json_schema",
    "name": "spam_classification",
    "description": "Spam verdict",
    "strict": True,
    "schema": SPAM_SCHEMA,
}
RAW_RESPONSES = {
    "request": {
        "model": "gpt-4o-mini",
        "instructions": "Classify the email as spam or ham.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "WIN A FREE CRUISE!!! Click now"}]}],
        "text": {"format": RESPONSES_FORMAT},
    },
    "response": {
        "id": "resp_abc",
        "object": "response",
        "created_at": 1760000000,
        "completed_at": 1760000001,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": "Classify the email as spam or ham.",
        "model": "o4-mini-2025-04-16",
        "output": [
            {"type": "reasoning", "id": "rs_abc", "summary": []},
            {
                "type": "message",
                "id": "msg_abc",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"label":"spam","reasoning":"Unsolicited prize offer with urgent call to action."}',
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            },
        ],
        "text": {"format": RESPONSES_FORMAT, "verbosity": "medium"},
        "tools": [],
        "tool_choice": "auto",
        "usage": {
            "input_tokens": 58,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 120,
            "output_tokens_details": {"reasoning_tokens": 102},
            "total_tokens": 178,
        },
    },
}

RAW_ANTHROPIC = {
    "timestamp": "2026-10-08T09:00:00Z",
    "request": {
        "model": "claude-haiku-5-5",
        "max_tokens": 1024,
        "system": "Classify the email as spam or ham.",
        "messages": [{"role": "user", "content": "WIN A FREE CRUISE!!! Click now"}],
        "output_config": {"format": {"type": "json_schema", "schema": SPAM_SCHEMA_ANTHROPIC}},
    },
    "response": {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "EqQBCgIYAhIM"},
            {"type": "text", "text": '{"label":"spam","explanation":"Unsolicited prize offer with urgent call to action."}'},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {
            "input_tokens": 74,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 41,
            "output_tokens_details": {"thinking_tokens": 12},
            "service_tier": "standard",
        },
    },
}

RAW_ANTHROPIC_TOOL = {
    "timestamp": 1760000000,
    "request": {
        "model": "claude-opus-5",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "Classify the email as spam or ham."}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Lunch tomorrow? - Sam"}]}],
        "tools": [{"name": "record_verdict", "description": "Record the verdict", "input_schema": SPAM_SCHEMA_ANTHROPIC}],
        "tool_choice": {"type": "tool", "name": "record_verdict"},
    },
    "response": {
        "id": "msg_01Aq9w938a90dw8q",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01A09q90qw90lq917835lq9",
                "name": "record_verdict",
                "input": {"label": "ham", "explanation": "Personal message from a known contact."},
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 512, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 38},
    },
}


def chat_line(content, *, system="Classify the ticket.", user="hello", response_format=None, finish="stop", **message):
    request = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if response_format is not None:
        request["response_format"] = response_format
    return {
        "request": request,
        "response": {
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{"message": {"role": "assistant", "content": content, **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    }


def json_schema(schema, name="verdict"):
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


# ---------------------------------------------------------------- Langfuse


def test_langfuse_v2_generation():
    call = map_row(LANGFUSE_V2, "langfuse")
    assert call.id == "langfuse:a1b2c3d4e5f60718"
    assert call.tool == "langfuse"
    assert call.source["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert call.source["api"] == "openai.chat"
    assert call.task == "ticket-classifier"
    assert call.timestamp == "2026-10-07T14:03:21.512Z"
    assert call.model == "gpt-4o-2024-08-06"
    assert call.system_prompt == "Classify the ticket. Reply in JSON."
    assert call.input_messages == [{"role": "user", "content": "I was charged twice."}]
    assert call.response_schema == TICKET_SCHEMA
    assert call.schema_source == "openai.response_format"
    assert call.schema_name == "ticket_label"
    assert call.verdict_field == "/label"
    assert call.verdict == "billing"
    assert call.verdict_type == "enum"
    assert call.verdict_options == ["billing", "bug", "how_to", "other"]
    assert call.rationale == "Duplicate charge."
    assert call.parse_status == "ok"
    assert call.output_channel == "content"
    assert (call.input_tokens, call.output_tokens, call.cached_input_tokens, call.reasoning_tokens) == (412, 18, 0, 0)
    assert call.cost_usd == pytest.approx(0.00121)
    assert call.cost_source == "tool"
    assert call.latency_ms == pytest.approx(912)
    assert call.attributes["user_id"] == "user-123"
    assert call.attributes["tags"] == ["support"]


def test_langfuse_v1_parsed_io_and_usage():
    row = copy.deepcopy(LANGFUSE_V2)
    row["input"] = json.loads(row["input"])
    row["output"] = json.loads(row["output"])
    row["metadata"]["response_format"] = json.loads(row["metadata"]["response_format"])
    for key in ("usageDetails", "costDetails", "totalCost", "endTime"):
        row.pop(key)
    row.update(inputPrice=0.0000025, outputPrice=0.00001, calculatedTotalCost=0.00121)
    row["usage"] = {"unit": "TOKENS", "input": 412, "output": 18, "total": 430}
    call = map_row(row, "langfuse")
    assert call.verdict == "billing"
    assert call.system_prompt == "Classify the ticket. Reply in JSON."
    assert (call.input_tokens, call.output_tokens) == (412, 18)
    assert call.cost_usd == pytest.approx(0.00121)
    assert call.latency_ms == pytest.approx(912)


def test_langfuse_sums_usage_detail_buckets():
    row = copy.deepcopy(LANGFUSE_V2)
    row["usageDetails"] = {"input": 100, "input_cached_tokens": 300, "output": 10, "output_reasoning_tokens": 40, "total": 450}
    call = map_row(row, "langfuse")
    assert (call.input_tokens, call.output_tokens, call.cached_input_tokens, call.reasoning_tokens) == (400, 50, 300, 40)


def test_langfuse_skips_non_generation_and_uses_parent_for_generic_name():
    span = {"id": "f0e1d2c3b4a59687", "type": "SPAN", "name": "triage-ticket"}
    generic = copy.deepcopy(LANGFUSE_V2)
    generic["name"] = "OpenAI-generation"
    calls = map_rows([span, generic], "langfuse")
    assert len(calls) == 1
    assert calls[0].task == "triage-ticket"

    orphan = dict(generic, parentObservationId=None)
    assert map_row(orphan, "langfuse").task == "classify-ticket"
    no_trace = dict(orphan, traceName=None)
    assert map_row(no_trace, "langfuse").task == "ticket_label"


def test_langfuse_prompt_name_wins_and_metadata_task_key():
    row = dict(LANGFUSE_V2, promptName="ticket-prompt", promptVersion=3)
    call = map_row(row, "langfuse")
    assert call.task == "ticket-prompt"
    assert call.attributes["prompt_version"] == 3
    row["metadata"] = {**row["metadata"], "task": "explicit-task"}
    assert map_row(row, "langfuse").task == "explicit-task"


# ---------------------------------------------------------------- Helicone


def test_helicone_matches_canonical_example():
    call = map_row(HELICONE, "helicone")
    assert call.id == "helicone:5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33"
    assert call.timestamp == "2026-10-08T14:03:20.870Z"
    assert call.task == "ticket-classifier"
    assert call.model == "gpt-4o-mini-2024-07-18"
    assert call.system_prompt == "Classify the support ticket. Return JSON only."
    assert call.input_messages == [
        {"role": "user", "content": "I was charged twice for my subscription, please refund one."}
    ]
    assert call.response_schema == HELICONE["request_body"]["response_format"]["json_schema"]["schema"]
    assert call.verdict_field == "/label"
    assert call.verdict == "billing"
    assert call.rationale == "Mentions refund for duplicate charge."
    assert (call.input_tokens, call.output_tokens) == (412, 38)
    assert call.cost_usd == pytest.approx(0.0000846)
    assert call.source == {
        "tool": "helicone",
        "native_id": "5b1e9c3a-7f2d-4e8b-b1a0-6c9d2e7f8a33",
        "trace_id": "3c0e5a1f-0000-4000-8000-000000000001",
        "project": None,
        "api": "openai.chat",
    }
    assert call.verdict_type == "enum"
    assert call.verdict_options == ["billing", "bug", "how_to", "other"]
    assert call.parsed_output == {"label": "billing", "confidence": 0.93, "rationale": "Mentions refund for duplicate charge."}
    assert call.raw_output == HELICONE["response_body"]["choices"][0]["message"]["content"]
    assert call.output_channel == "content"
    assert call.schema_source == "openai.response_format"
    assert call.parse_status == "ok"
    assert (call.cached_input_tokens, call.reasoning_tokens) == (0, 0)
    assert call.cost_source == "tool"
    assert call.latency_ms == 642
    assert call.labels == {"helicone-score-feedback": 1}
    assert {"environment": "production", "user_id": "user-12345", "session_path": "/triage/classify"}.items() <= call.attributes.items()
    assert "task" not in call.attributes


def test_helicone_s3_preview_is_body_unavailable_unless_signed_body_supplied():
    row = dict(HELICONE, storage_location="s3", request_body="{\"model\":\"gpt-4o-mini\",\"messa", response_body="{\"id\":\"chatc")
    call = map_row(row, "helicone")
    assert call.parse_status == "body_unavailable"
    assert call.verdict is None
    assert (call.input_tokens, call.output_tokens) == (412, 38)
    assert call.task == "ticket-classifier"

    supplied = dict(row, signed_body={"request": HELICONE["request_body"], "response": HELICONE["response_body"]})
    call = map_row(supplied, "helicone")
    assert call.parse_status == "ok"
    assert call.verdict == "billing"


def test_helicone_placeholder_cache_hit_and_numeric_strings():
    placeholder = dict(HELICONE, response_body={"helicone_message": "fetching body from signed_url..."})
    assert map_row(placeholder, "helicone").parse_status == "body_unavailable"
    assert map_row(dict(HELICONE, cache_reference_id="9b0c1d2e-0000-4000-8000-000000000000"), "helicone") is None

    row = copy.deepcopy(HELICONE)
    del row["response_body"]["usage"]
    row.update(prompt_tokens="412", completion_tokens="38", cost="0.0000846")
    call = map_row(row, "helicone")
    assert (call.input_tokens, call.output_tokens) == (412, 38)
    assert call.cost_usd == pytest.approx(0.0000846)


def test_helicone_task_falls_back_to_prompt_id_then_session_path():
    row = copy.deepcopy(HELICONE)
    del row["request_properties"]["task"]
    assert map_row(row, "helicone").task == "ticket_classifier"
    del row["request_properties"]["Helicone-Prompt-Id"]
    assert map_row(row, "helicone").task == "classify"


def test_helicone_anthropic_body_by_target_url():
    row = dict(
        HELICONE,
        request_body=RAW_ANTHROPIC["request"],
        response_body=RAW_ANTHROPIC["response"],
        target_url="https://api.anthropic.com/v1/messages",
        cost=0,
    )
    call = map_row(row, "helicone")
    assert call.source["api"] == "anthropic.messages"
    assert call.verdict == "spam"
    assert call.rationale == "Unsolicited prize offer with urgent call to action."
    assert call.cost_source == "computed"


# ---------------------------------------------------------------- LangSmith


def test_langsmith_langchain_run():
    call = map_row(LANGSMITH_V1, "langsmith")
    assert call.id == "langsmith:0ec6b845-18b9-4aa1-8f1b-6ba3f9fdefd6"
    assert call.timestamp == "2026-09-14T17:02:11.120Z"
    assert call.system_prompt == "Classify the ticket. Return JSON {label}."
    assert call.input_messages == [{"role": "user", "content": "My card was charged twice"}]
    assert call.verdict == "billing"
    assert call.verdict_field == "/label"
    assert call.verdict_options == ["billing", "bug", "other"]
    assert call.schema_source == "langchain.ls_structured_output_format"
    assert call.model == "gpt-4o-mini-2024-07-18"
    assert (call.input_tokens, call.output_tokens) == (52, 6)
    assert call.cost_usd == pytest.approx(0.0000114)
    assert call.cost_source == "tool"
    assert call.latency_ms == pytest.approx(784)
    assert call.source["project"] == "1ffd059c-3e8a-4f2b-9c6d-5a4b3c2d1e33"
    assert call.task.startswith("task_") and len(call.task) == 13


def test_langsmith_generic_name_uses_parent_run():
    parent = {"id": "a8024e23-7a4f-4bb1-8f0e-2b7f3c1d9e22", "name": "triage_ticket", "run_type": "chain"}
    calls = map_rows([parent, LANGSMITH_V1], "langsmith")
    assert [c.task for c in calls] == ["triage_ticket"]


def test_langsmith_wrap_openai_run():
    call = map_row({**LANGSMITH_V1, **WRAP_OPENAI_IO, "extra": {"metadata": {"ls_model_name": "gpt-4o-mini"}}}, "langsmith")
    assert call.verdict == "bug"
    assert call.verdict_type == "string"
    assert call.schema_source == "openai.response_format"
    assert call.input_messages == [{"role": "user", "content": "The export button crashes the app"}]
    assert call.model == "gpt-4o-mini-2024-07-18"


def test_langsmith_v2_shape_and_function_calling():
    row = copy.deepcopy(LANGSMITH_V1)
    row.update(run_type="LLM", status="SUCCESS", project_id=row.pop("session_id"), total_cost=0.0000114)
    row["parent_run_ids"] = ["root-run", row.pop("parent_run_id")]
    row["metadata"] = row["extra"].pop("metadata")
    row["prompt_token_details"] = {"raw": {"cache_read": 20}}
    message = row["outputs"]["generations"][0][0]["message"]["kwargs"]
    message["content"] = ""
    message["tool_calls"] = [{"name": "Label", "args": {"label": "bug"}, "id": "call_1"}]
    row["extra"]["options"]["ls_structured_output_format"]["kwargs"]["method"] = "function_calling"
    root = {"id": "root-run", "name": "support_pipeline", "run_type": "CHAIN"}
    middle = {"id": "a8024e23-7a4f-4bb1-8f0e-2b7f3c1d9e22", "name": "RunnableSequence", "run_type": "CHAIN"}
    calls = map_rows([root, middle, row], "langsmith")
    assert len(calls) == 1
    call = calls[0]
    assert call.task == "support_pipeline"
    assert call.verdict == "bug"
    assert call.output_channel == "tool_call"
    assert call.schema_source == "langchain.ls_structured_output_format"
    assert call.cached_input_tokens == 20
    assert call.source["project"] == "1ffd059c-3e8a-4f2b-9c6d-5a4b3c2d1e33"


def test_langsmith_refusal_and_truncation():
    refused = copy.deepcopy(LANGSMITH_V1)
    refused["outputs"]["generations"][0][0]["message"]["kwargs"]["additional_kwargs"]["refusal"] = "I can't help."
    call = map_row(refused, "langsmith")
    assert (call.parse_status, call.verdict) == ("refusal", None)
    assert call.verdict_options == ["billing", "bug", "other"]

    cut = copy.deepcopy(LANGSMITH_V1)
    cut["outputs"]["generations"][0][0]["generation_info"]["finish_reason"] = "length"
    assert map_row(cut, "langsmith").parse_status == "truncated"


# ---------------------------------------------------------------- Braintrust


def test_braintrust_span():
    call = map_row(BRAINTRUST, "braintrust")
    assert call.id == "braintrust:385052b6-50a2-43b4-b52d-9afaa34f0bff"
    assert call.timestamp == "2025-09-14T17:02:11.120Z"
    assert call.verdict == "billing"
    assert call.verdict_options == ["billing", "bug", "other"]
    assert call.system_prompt == "Classify the ticket. Return JSON {label}."
    assert call.model == "gpt-4o-mini"
    assert (call.input_tokens, call.output_tokens, call.cached_input_tokens) == (52, 6, 0)
    assert call.cost_source == "computed"
    assert call.cost_usd == pytest.approx((52 * 0.15 + 6 * 0.60) / 1e6)
    assert call.latency_ms == pytest.approx(784)
    assert call.task == "Label"
    assert call.source["trace_id"] == "70b04fd2-0177-47a9-a70b-e32ca43db131"
    assert call.attributes["context"]["caller_functionname"] == "classify"


def test_braintrust_dedupe_cached_and_parent_span():
    parent = {"id": "p", "span_id": "parent-span", "span_attributes": {"name": "classify_ticket", "type": "task"}}
    older = dict(BRAINTRUST, _xact_id="1000196123456700", output=None, span_parents=["parent-span"])
    newer = dict(BRAINTRUST, _xact_id="1000196123456789", span_parents=["parent-span"])
    cached = dict(BRAINTRUST, id="cached-row", metrics={**BRAINTRUST["metrics"], "cached": 1})
    calls = map_rows([parent, newer, older, cached], "braintrust")
    assert len(calls) == 1
    assert calls[0].verdict == "billing"
    assert calls[0].task == "classify_ticket"


# ---------------------------------------------------------------- raw provider logs


def test_raw_openai_chat():
    call = map_row(RAW_CHAT, "raw")
    assert call.id == "openai_raw:chatcmpl-abc123"
    assert call.source["api"] == "openai.chat"
    assert call.system_prompt == "Classify the email as spam or ham."
    assert call.input_messages == [{"role": "user", "content": "WIN A FREE CRUISE!!! Click now"}]
    assert call.task == "spam_classification"
    assert call.verdict == "spam"
    assert call.rationale == "Unsolicited prize offer with urgent call to action."
    assert (call.input_tokens, call.output_tokens) == (61, 18)
    assert call.cost_source == "computed"
    assert call.cost_usd == pytest.approx((61 * 0.15 + 18 * 0.60) / 1e6)
    assert call.timestamp == "2025-10-09T08:53:20.000Z"


def test_raw_openai_responses_with_reasoning_item():
    call = map_row(RAW_RESPONSES, "raw")
    assert call.source["api"] == "openai.responses"
    assert call.system_prompt == "Classify the email as spam or ham."
    assert call.input_messages == [{"role": "user", "content": "WIN A FREE CRUISE!!! Click now"}]
    assert call.verdict == "spam"
    assert call.schema_source == "openai.text.format"
    assert (call.input_tokens, call.output_tokens, call.reasoning_tokens) == (58, 120, 102)
    assert call.latency_ms == pytest.approx(1000)
    assert call.cost_usd is None


def test_raw_responses_refusal_and_truncation_keep_null_verdict():
    refused = copy.deepcopy(RAW_RESPONSES)
    refused["response"]["output"][1]["content"] = [{"type": "refusal", "refusal": "I'm sorry, I cannot assist with that request."}]
    call = map_row(refused, "raw")
    assert (call.parse_status, call.verdict, call.verdict_field) == ("refusal", None, "/label")
    assert call.verdict_options == ["spam", "ham"]

    truncated = copy.deepcopy(RAW_RESPONSES)
    truncated["response"].update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    truncated["response"]["output"][1]["content"][0]["text"] = '{"label":"sp'
    call = map_row(truncated, "raw")
    assert (call.parse_status, call.verdict) == ("truncated", None)


def test_raw_anthropic_json_outputs_skip_thinking_and_sum_cache():
    call = map_row(RAW_ANTHROPIC, "raw")
    assert call.id == "anthropic_raw:msg_01XFDUDYJgAACzvnptvVoYEL"
    assert call.source["api"] == "anthropic.messages"
    assert call.timestamp == "2026-10-08T09:00:00.000Z"
    assert call.system_prompt == "Classify the email as spam or ham."
    assert call.verdict == "spam"
    assert call.rationale == "Unsolicited prize offer with urgent call to action."
    assert call.schema_source == "anthropic.output_config.format"
    assert (call.input_tokens, call.output_tokens, call.reasoning_tokens) == (74, 41, 12)
    assert call.cost_usd == pytest.approx((74 * 0.10 + 41 * 0.50) / 1e6)
    assert call.task.startswith("task_")

    cached = copy.deepcopy(RAW_ANTHROPIC)
    cached["response"]["usage"].update(cache_creation_input_tokens=100, cache_read_input_tokens=2000)
    call = map_row(cached, "raw")
    assert (call.input_tokens, call.cached_input_tokens) == (2174, 2000)

    legacy = copy.deepcopy(RAW_ANTHROPIC)
    legacy["request"]["output_format"] = legacy["request"].pop("output_config")["format"]
    assert map_row(legacy, "raw").schema_source == "anthropic.output_format"


def test_raw_anthropic_forced_tool_use():
    call = map_row(RAW_ANTHROPIC_TOOL, "raw")
    assert call.output_channel == "tool_call"
    assert call.schema_source == "tool:record_verdict"
    assert call.task == "record_verdict"
    assert call.verdict == "ham"
    assert call.rationale == "Personal message from a known contact."
    assert call.input_messages == [{"role": "user", "content": "Lunch tomorrow? - Sam"}]
    assert call.system_prompt == "Classify the email as spam or ham."
    assert json.loads(call.raw_output) == {"label": "ham", "explanation": "Personal message from a known contact."}
    assert call.timestamp == "2025-10-09T08:53:20.000Z"


def test_raw_anthropic_refusal_and_max_tokens():
    refused = copy.deepcopy(RAW_ANTHROPIC)
    refused["response"]["stop_reason"] = "refusal"
    assert map_row(refused, "raw").parse_status == "refusal"
    assert map_row(refused, "raw").verdict is None
    cut = copy.deepcopy(RAW_ANTHROPIC)
    cut["response"]["stop_reason"] = "max_tokens"
    assert map_row(cut, "raw").parse_status == "truncated"


def test_chat_payload_parsing_statuses():
    schema = json_schema(SPAM_SCHEMA)
    assert map_row(chat_line("not json at all", response_format=schema), "raw").parse_status == "invalid_json"
    fenced = map_row(chat_line('```json\n{"label": "ham", "reasoning": "ok"}\n```', response_format=schema), "raw")
    assert (fenced.parse_status, fenced.verdict) == ("ok", "ham")
    prose = map_row(chat_line('Sure! Here it is: {"label": "spam", "reasoning": "a {brace} inside"} Thanks.', response_format=schema), "raw")
    assert (prose.parse_status, prose.verdict) == ("ok", "spam")
    refused = map_row(chat_line(None, response_format=schema, refusal="I can't help with that."), "raw")
    assert (refused.parse_status, refused.verdict, refused.output_channel) == ("refusal", None, "none")
    truncated = map_row(chat_line('{"label": "sp', response_format=schema, finish="length"), "raw")
    assert (truncated.parse_status, truncated.verdict) == ("truncated", None)
    missing = map_row(chat_line('{"label": "spam"}', response_format=schema), "raw")
    assert (missing.parse_status, missing.verdict) == ("schema_mismatch", "spam")


def test_chat_tool_call_uses_tool_schema():
    line = chat_line(
        None,
        tool_calls=[{"id": "call_abc", "type": "function", "function": {"name": "record_verdict", "arguments": '{"label":"ham","reasoning":"fine"}'}}],
    )
    line["request"]["tools"] = [{"type": "function", "function": {"name": "record_verdict", "parameters": SPAM_SCHEMA, "strict": True}}]
    call = map_row(line, "raw")
    assert (call.output_channel, call.schema_source, call.verdict) == ("tool_call", "tool:record_verdict", "ham")
    assert call.verdict_options == ["spam", "ham"]


def test_parse_json_variants():
    assert parse_json({"a": 1}) == (True, {"a": 1})
    assert parse_json('{"a": 1}') == (True, {"a": 1})
    assert parse_json('"{\\"a\\": 1}"') == (True, {"a": 1})
    assert parse_json("```\n{\"a\": 1}\n```") == (True, {"a": 1})
    assert parse_json('noise {"a": "}"} tail') == (True, {"a": "}"})
    assert parse_json("{broken") == (False, None)
    assert parse_json("") == (False, None)


# ---------------------------------------------------------------- verdict field selection


def test_verdict_field_scoring_and_ambiguity():
    reasoning_first = {
        "type": "object",
        "properties": {"reasoning": {"type": "string"}, "confidence": {"type": "number"}, "decision": {"type": "string", "enum": ["approve", "reject"]}},
        "required": ["reasoning", "confidence", "decision"],
    }
    assert select_verdict_field(reasoning_first).pointer == "/decision"
    rubric = {
        "type": "object",
        "properties": {"is_relevant": {"type": "boolean"}, "is_safe": {"type": "boolean"}, "notes": {"type": "string"}},
        "required": ["is_relevant", "is_safe", "notes"],
    }
    choice = select_verdict_field(rubric)
    assert choice.pointer is None and choice.ambiguous and set(choice.candidates) == {"/is_relevant", "/is_safe"}
    nested = {
        "type": "object",
        "properties": {"decision": {"type": "object", "properties": {"route": {"type": "string", "enum": ["a", "b"]}, "why": {"type": "string"}}}},
    }
    assert select_verdict_field(nested).pointer == "/decision/route"
    scale = {"type": "object", "properties": {"helpfulness": {"type": "integer", "minimum": 1, "maximum": 5}, "summary": {"type": "string"}}}
    assert select_verdict_field(scale).pointer == "/helpfulness"
    refs = {
        "type": "object",
        "$defs": {"Label": {"type": "string", "enum": ["pos", "neg"]}},
        "properties": {"sentiment": {"$ref": "#/$defs/Label"}, "explanation": {"type": "string"}},
    }
    assert select_verdict_field(refs).pointer == "/sentiment"


def test_ambiguous_task_waits_for_explicit_field():
    rubric = {
        "type": "object",
        "properties": {"is_relevant": {"type": "boolean"}, "is_safe": {"type": "boolean"}},
        "required": ["is_relevant", "is_safe"],
        "additionalProperties": False,
    }
    lines = [chat_line(json.dumps({"is_relevant": i % 2 == 0, "is_safe": True}), user=f"doc {i}", response_format=json_schema(rubric, "rubric")) for i in range(4)]
    calls = map_rows(lines, "raw")
    groups = group_tasks(calls)
    assert list(groups) == ["rubric"]
    assert all(c.verdict is None and c.verdict_field is None for c in calls)
    assert set(calls[0].verdict_candidates) == {"/is_relevant", "/is_safe"}
    assert to_records(calls) == []

    group_tasks(calls, verdict_field={"rubric": "/is_relevant"})
    assert [c.verdict for c in calls] == [True, False, True, False]
    assert calls[0].verdict_options == [True, False]
    assert not calls[0].verdict_candidates
    assert [r.reference for r in to_records(calls)] == ["true", "false", "true", "false"]

    explicit = map_rows(lines, "raw", verdict_field="is_safe")
    assert {c.verdict_field for c in explicit} == {"/is_safe"}


def test_schema_less_task_infers_verdict_from_data():
    sentiments = ["positive"] * 12 + ["negative"] * 6 + ["neutral"] * 2
    lines = []
    for i, label in enumerate(sentiments):
        shown = label.upper() if i == 0 else label
        body = {"sentiment": shown, "summary": f"Review number {i} says something unique {i * 7}", "confidence": 0.5 + i / 100}
        lines.append(chat_line(json.dumps(body), system="Rate the review. Reply in JSON.", user=f"review {i}", response_format={"type": "json_object"}))
    calls = map_rows(lines, "raw")
    assert all(c.verdict_field is None and c.schema_source == "openai.json_object" for c in calls)
    groups = group_tasks(calls)
    assert len(groups) == 1
    (task, members), = groups.items()
    assert task.startswith("task_")
    assert all(c.verdict_field == "/sentiment" and c.schema_source == "inferred" for c in members)
    assert members[0].verdict == "positive"
    assert members[0].verdict_options == ["positive", "negative", "neutral"]
    assert members[0].rationale is None
    assert task_spec(task, members).labels == ["positive", "negative", "neutral"]


def test_verdict_normalization_rules():
    enum = {"type": "string", "enum": ["Spam", "Ham"]}
    assert normalize_verdict(" spam ", enum).value == "Spam"
    assert normalize_verdict("junk", enum).ok is False
    boolean = {"type": "boolean"}
    assert (normalize_verdict("yes", boolean).value, normalize_verdict("yes", boolean).ok) == (True, False)
    integer = {"type": "integer", "minimum": 1, "maximum": 5}
    assert normalize_verdict(4.0, integer).value == 4 and normalize_verdict(4.0, integer).ok
    assert normalize_verdict(4.5, integer).ok is False
    assert normalize_verdict(7, integer).ok is False
    assert normalize_verdict(3, integer).options == [1, 2, 3, 4, 5]
    consts = {"anyOf": [{"const": "a"}, {"const": "b"}]}
    assert normalize_verdict("b", consts).type == "enum"
    nullable = {
        "type": "object",
        "properties": {"label": {"type": ["string", "null"], "enum": ["x", "y", None]}},
        "required": ["label"],
    }
    call = map_row(chat_line('{"label": null}', response_format=json_schema(nullable)), "raw")
    assert (call.parse_status, call.verdict, call.verdict_options) == ("ok", None, ["x", "y"])


# ---------------------------------------------------------------- task derivation


def test_fingerprint_is_stable_across_templated_prompts():
    a = 'Today is 2026-10-07. Ticket 1234 from "Acme Corp" (id 4bf92f35-77b3-4da6-a3ce-929d0e0e4736). Classify it.'
    b = 'Today is 2026-10-08. Ticket 98 from "Globex" (id 0e01bf50-5d7c-4c1a-9a43-1f4b0a9e2c11). Classify it.'
    assert fingerprint(a, SPAM_SCHEMA) == fingerprint(b, SPAM_SCHEMA)
    assert fingerprint(a, SPAM_SCHEMA) != fingerprint("Summarise the ticket.", SPAM_SCHEMA)
    assert fingerprint(a, SPAM_SCHEMA) != fingerprint(a, LABEL_SCHEMA)

    one, two = copy.deepcopy(RAW_ANTHROPIC), copy.deepcopy(RAW_ANTHROPIC)
    one["request"]["system"], two["request"]["system"] = a, b
    two["response"]["id"] = "msg_other"
    calls = map_rows([one, two], "raw")
    assert len(calls) == 2 and calls[0].task == calls[1].task and calls[0].task.startswith("task_")


# ---------------------------------------------------------------- loading and round trip


def test_load_export_envelopes_and_jsonl(tmp_path):
    (tmp_path / "lf.json").write_text(json.dumps({"data": [LANGFUSE_V2, LANGFUSE_V2], "meta": {"cursor": "x"}}))
    assert [c.id for c in load_export(tmp_path / "lf.json", "langfuse")] == ["langfuse:a1b2c3d4e5f60718"]
    (tmp_path / "ls.json").write_text(json.dumps({"items": [LANGSMITH_V1], "next_cursor": None}))
    assert len(load_export(tmp_path / "ls.json", "langsmith")) == 1
    (tmp_path / "bt.json").write_text(json.dumps({"events": [BRAINTRUST], "cursor": None}))
    assert len(load_export(tmp_path / "bt.json", "braintrust")) == 1
    (tmp_path / "hc.json").write_text(json.dumps([HELICONE]))
    assert len(load_export(tmp_path / "hc.json", "helicone")) == 1
    (tmp_path / "raw.jsonl").write_text("\n".join(json.dumps(x) for x in (RAW_CHAT, RAW_RESPONSES, RAW_ANTHROPIC)) + "\n")
    assert len(load_export(tmp_path / "raw.jsonl", "raw")) == 3
    with gzip.open(tmp_path / "raw.jsonl.gz", "wt") as f:
        f.write(json.dumps(RAW_CHAT) + "\n")
    assert len(load_export(tmp_path / "raw.jsonl.gz", "raw")) == 1
    with pytest.raises(ValueError):
        load_export(tmp_path / "raw.jsonl", "nope")


def test_round_trip_into_ingest_and_taskspec(tmp_path):
    rows = []
    for i, label in enumerate(["billing", "bug", "billing", "other", "billing"]):
        row = copy.deepcopy(HELICONE)
        row["request_id"] = f"req-{i}"
        row["request_body"]["messages"][1]["content"] = f"Ticket text {i}"
        row["response_body"]["choices"][0]["message"]["content"] = json.dumps({"label": label, "confidence": 0.9, "rationale": "r"})
        rows.append(row)
    multi = copy.deepcopy(rows[0])
    multi["request_id"] = "req-multi"
    multi["request_body"]["messages"][1:] = [
        {"role": "user", "content": [{"type": "text", "text": "See attached"}, {"type": "image_url", "image_url": {"url": "data:..."}}]},
        {"role": "assistant", "content": "Which charge?"},
        {"role": "user", "content": "The second one"},
    ]
    refused = copy.deepcopy(rows[0])
    refused["request_id"] = "req-refused"
    refused["response_body"]["choices"][0]["message"].update(content=None, refusal="No.")
    rows += [multi, refused]

    calls = map_rows(rows, "helicone")
    groups = group_tasks(calls)
    members = groups["ticket-classifier"]
    records = to_records(members)
    assert len(records) == 6
    assert records[0].input == "Ticket text 0"
    assert records[-1].input == "user: See attached\n[image]\nassistant: Which charge?\nuser: The second one"
    assert records[0].meta["parse_status"] == "ok" and records[0].meta["tool"] == "helicone"

    write_records_jsonl(records, tmp_path / "records.jsonl")
    loaded = read_records(tmp_path / "records.jsonl")
    assert [(r.id, r.input, r.reference, r.cost_usd) for r in loaded] == [
        (r.id, r.input, r.reference, r.cost_usd) for r in records
    ]

    spec = task_spec("ticket-classifier", members)
    assert spec.description == "Classify the support ticket. Return JSON only."
    assert spec.labels == ["billing", "bug", "how_to", "other"]
    write_task_yaml(spec, tmp_path / "task.yaml")
    assert TaskSpec.load(tmp_path / "task.yaml") == spec


def test_task_spec_boolean_labels_descriptions_and_trimming(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "route": {"anyOf": [{"const": "sales", "description": "Buying intent"}, {"const": "support", "description": "Needs help"}]},
        },
        "required": ["route"],
    }
    long_prompt = "Route the message. " * 200
    calls = map_rows([chat_line('{"route": "sales"}', system=long_prompt, response_format=json_schema(schema, "router"))], "raw")
    spec = task_spec("router", calls)
    assert spec.labels == ["sales", "support"]
    assert spec.label_descriptions == {"sales": "Buying intent", "support": "Needs help"}
    assert len(spec.description) <= 1500 and spec.description.endswith("…")

    flags = {"type": "object", "properties": {"is_spam": {"type": "boolean"}}, "required": ["is_spam"]}
    calls = map_rows([chat_line('{"is_spam": false}', system="", response_format=json_schema(flags, "spam_flag"))], "raw")
    spec = task_spec("spam_flag", calls)
    assert spec.labels == ["true", "false"]
    assert "spam_flag" in spec.description
    write_task_yaml(spec, tmp_path / "flags.yaml")
    assert TaskSpec.load(tmp_path / "flags.yaml").labels == ["true", "false"]
    assert to_records(calls)[0].reference == "false"


def test_price_table_longest_prefix():
    assert price_for("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert price_for("gpt-4o-2024-08-06") == (2.50, 10.00)
    assert price_for("us.anthropic.claude-sonnet-4-6-v1:0") == (3.00, 15.00)
    assert price_for("anthropic/claude-opus-5-5") == (4.00, 20.00)
    assert price_for("o4-mini") is None
    assert compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    assert compute_cost("unknown-model", 10, 10) is None
