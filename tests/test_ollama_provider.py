"""Unit tests for the native Ollama chat adapter.

Exercised with httpx.MockTransport — no network, no Ollama daemon. The point of
this adapter is that every /api/chat call carries options.num_ctx (the compat
/v1 endpoint can't, so Ollama truncates at its 4096 default).
"""
from __future__ import annotations

import asyncio
import json

import httpx

from services.providers.ollama_provider import (
    MIN_NUM_CTX,
    OllamaChatProvider,
    _native_base,
    _to_ollama_messages,
)


def _client_capturing(captured: dict, response_body: dict | None = None):
    body = response_body or {
        "message": {"role": "assistant", "content": "hoi"},
        "prompt_eval_count": 100,
        "eval_count": 10,
        "created_at": "2026-07-04T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_native_base_strips_v1():
    assert _native_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert _native_base("http://localhost:11434") == "http://localhost:11434"
    assert _native_base(None) == "http://localhost:11434"


def test_chat_sends_num_ctx_from_model_context_window():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434/v1",
        default_model="m1",
        model_ctx={"m1": 32000},
        client=_client_capturing(captured),
    )
    res = asyncio.run(p.chat("hallo", instructions="sys"))
    assert captured["url"].endswith("/api/chat")
    body = captured["body"]
    # capped by OLLAMA_NUM_CTX (default 16384), not the raw 32000
    assert body["options"]["num_ctx"] == 16384
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hallo"}
    assert res.text == "hoi"
    assert res.usage["prompt_tokens"] == 100
    assert res.usage["completion_tokens"] == 10


def test_chat_num_ctx_uses_smaller_model_window_but_never_below_min():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434",
        default_model="tiny",
        model_ctx={"tiny": 2048},  # below Ollama's own default → clamp up
        client=_client_capturing(captured),
    )
    asyncio.run(p.chat("x"))
    assert captured["body"]["options"]["num_ctx"] == MIN_NUM_CTX


def test_chat_num_ctx_defaults_to_setting_when_model_unknown():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434",
        default_model="unknown",
        client=_client_capturing(captured),
    )
    asyncio.run(p.chat("x"))
    assert captured["body"]["options"]["num_ctx"] == 16384


def test_chat_json_schema_becomes_native_format():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434", default_model="m", client=_client_capturing(captured)
    )
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    asyncio.run(p.chat("x", response_format={
        "type": "json_schema", "json_schema": {"name": "plan", "schema": schema},
    }))
    assert captured["body"]["format"] == schema


def test_chat_json_object_becomes_json_mode():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434", default_model="m", client=_client_capturing(captured)
    )
    asyncio.run(p.chat("x", response_format={"type": "json_object"}))
    assert captured["body"]["format"] == "json"


def test_chat_error_body_raises_with_ollama_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'nope' not found"})

    p = OllamaChatProvider(
        base_url="http://localhost:11434", default_model="nope",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        asyncio.run(p.chat("x"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "model 'nope' not found" in str(exc)


def test_messages_fold_blocks_and_data_url_images():
    msgs = _to_ollama_messages(
        [
            {"role": "user", "content": [
                {"type": "text", "text": "kijk"},
                {"type": "image", "image_url": "data:image/png;base64,QUJD"},
            ]},
        ],
        "sys",
    )
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"] == "kijk"
    assert msgs[1]["images"] == ["QUJD"]


def test_max_output_tokens_maps_to_num_predict():
    captured: dict = {}
    p = OllamaChatProvider(
        base_url="http://localhost:11434", default_model="m", client=_client_capturing(captured)
    )
    asyncio.run(p.chat("x", max_output_tokens=222, temperature=0.1))
    assert captured["body"]["options"]["num_predict"] == 222
    assert captured["body"]["options"]["temperature"] == 0.1
