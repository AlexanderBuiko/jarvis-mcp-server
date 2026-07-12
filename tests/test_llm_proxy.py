"""Tests for the LLM chat proxy: happy path, validation, and the basic limits."""

import os
from unittest import mock

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from time_server import llm_proxy


def _client() -> TestClient:
    app = Starlette(routes=[
        Route("/v1/chat/completions", llm_proxy.chat_completions, methods=["POST"]),
    ])
    return TestClient(app)


def _ok_upstream(payload, timeout):
    # Echo enough to assert the forwarded payload, in OpenAI response shape.
    return 200, {
        "model": payload.get("model"),
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        "_forwarded": payload,
    }


def setup_function():
    # Fresh, permissive limiter per test unless a test overrides it.
    llm_proxy._rate_limiter = llm_proxy.RateLimiter(rpm=0)


def test_happy_path_forwards_and_returns():
    with mock.patch.object(llm_proxy, "_forward_to_ollama", side_effect=_ok_upstream):
        r = _client().post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "ok"


def test_default_model_filled_when_omitted():
    with mock.patch.dict(os.environ, {"LLM_MODEL": "qwen-android-mini"}), \
         mock.patch.object(llm_proxy, "_forward_to_ollama", side_effect=_ok_upstream):
        r = _client().post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["_forwarded"]["model"] == "qwen-android-mini"


def test_explicit_model_is_preserved():
    with mock.patch.object(llm_proxy, "_forward_to_ollama", side_effect=_ok_upstream):
        r = _client().post("/v1/chat/completions",
                           json={"model": "custom:tag",
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["_forwarded"]["model"] == "custom:tag"


def test_missing_messages_is_400():
    r = _client().post("/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 400


def test_oversized_context_is_413():
    big = "x" * 1000  # ~250 estimated tokens
    with mock.patch.dict(os.environ, {"LLM_MAX_CONTEXT_TOKENS": "10"}), \
         mock.patch.object(llm_proxy, "_forward_to_ollama", side_effect=_ok_upstream) as fwd:
        r = _client().post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": big}]})
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "context_length_exceeded"
    fwd.assert_not_called()  # rejected before reaching the model


def test_rate_limit_is_429():
    llm_proxy._rate_limiter = llm_proxy.RateLimiter(rpm=1)
    with mock.patch.object(llm_proxy, "_forward_to_ollama", side_effect=_ok_upstream):
        c = _client()
        body = {"messages": [{"role": "user", "content": "hi"}]}
        first = c.post("/v1/chat/completions", json=body)
        second = c.post("/v1/chat/completions", json=body)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers.get("Retry-After") == "60"


def test_upstream_unreachable_is_502():
    import urllib.error

    def boom(payload, timeout):
        return llm_proxy._forward_to_ollama(payload, timeout)

    with mock.patch.dict(os.environ, {"OLLAMA_URL": "http://127.0.0.1:1"}):
        r = _client().post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert "unreachable" in r.json()["error"]["message"]
