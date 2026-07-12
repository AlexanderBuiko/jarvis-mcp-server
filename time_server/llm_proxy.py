"""
LLM chat proxy — turns this MCP server into a private AI service over the local LLM.

Exposes ``POST /v1/chat/completions`` (OpenAI-compatible) that forwards to a local
Ollama daemon and returns its response unchanged. The existing X-API-Key middleware
already authenticates every non-health path, so this route is private by default.

On top of auth it adds the two "basic limits" the task asks for:
  • **rate limit** — a per-client (API key, else IP) fixed-window counter; over the
    limit returns HTTP 429.
  • **max context** — requests whose estimated prompt size exceeds the cap are
    rejected with HTTP 413 before they reach the model (cheap chars≈tokens/4
    estimate — no tokenizer dependency).

The KB is intentionally NOT involved here: this serves the *model* only. Any RAG
retrieval stays on the client, which passes snippets in the request messages.

Config (env):
    OLLAMA_URL              base URL of the Ollama daemon (default http://localhost:11434)
    LLM_MODEL               model to serve if the request omits one (default qwen-android-mini)
    LLM_RATE_LIMIT_RPM      max requests per client per minute (default 30; 0 disables)
    LLM_MAX_CONTEXT_TOKENS  reject prompts estimated larger than this (default 8192; 0 disables)
    LLM_TIMEOUT_S           upstream request timeout (default 120)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from threading import Lock

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _ollama_url() -> str:
    return (os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")


def _default_model() -> str:
    return os.environ.get("LLM_MODEL") or "qwen-android-mini"


# ── Rate limiting ────────────────────────────────────────────────────────────


class RateLimiter:
    """Fixed-window per-client request counter.

    In-memory and per-process — fine for a single instance (the Phase-1/2 target).
    A multi-instance deployment would need a shared store (Redis); noted for later.
    """

    def __init__(self, rpm: int) -> None:
        self.rpm = rpm
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, client: str) -> bool:
        if self.rpm <= 0:
            return True
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            hits = [t for t in self._hits[client] if t >= window_start]
            if len(hits) >= self.rpm:
                self._hits[client] = hits
                return False
            hits.append(now)
            self._hits[client] = hits
            return True


# One limiter per process, sized from the env at import time.
_rate_limiter = RateLimiter(_int_env("LLM_RATE_LIMIT_RPM", 30))


def _client_id(request: Request) -> str:
    """Identify the caller for rate limiting: API key if present, else client IP."""
    key = request.headers.get("x-api-key")
    if key:
        return f"key:{key[:8]}"  # prefix only — the full key is never stored/logged
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


# ── Context estimation ───────────────────────────────────────────────────────


def _estimate_tokens(messages: list[dict]) -> int:
    """Cheap prompt-size estimate: ~4 characters per token across all message text."""
    chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):  # OpenAI content-parts form
            for part in content:
                if isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
    return chars // 4


# ── Upstream call ────────────────────────────────────────────────────────────


def _forward_to_ollama(payload: dict, timeout: float) -> tuple[int, dict]:
    """POST the chat payload to Ollama's OpenAI-compatible endpoint (blocking).

    Runs in a threadpool (see the handler) so it never blocks the event loop.
    Returns (status_code, json_body). Uses stdlib urllib to avoid adding a dep.
    """
    url = f"{_ollama_url()}/v1/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": {"message": exc.reason}}
        return exc.code, body
    except urllib.error.URLError as exc:
        return 502, {"error": {
            "message": f"local LLM unreachable at {_ollama_url()}: {exc.reason}. "
                       f"Is Ollama running and the model pulled?"
        }}


# ── Route handler ────────────────────────────────────────────────────────────


async def chat_completions(request: Request) -> JSONResponse:
    """Authenticated, rate-limited, context-capped proxy to the local LLM."""
    if not _rate_limiter.allow(_client_id(request)):
        return JSONResponse(
            {"error": {"message": "rate limit exceeded", "type": "rate_limit"}},
            status_code=429, headers={"Retry-After": "60"},
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "invalid JSON body"}}, status_code=400
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": {"message": "'messages' must be a non-empty list"}},
            status_code=400,
        )

    max_ctx = _int_env("LLM_MAX_CONTEXT_TOKENS", 8192)
    if max_ctx > 0:
        estimated = _estimate_tokens(messages)
        if estimated > max_ctx:
            return JSONResponse(
                {"error": {
                    "message": f"prompt too large (~{estimated} tokens > {max_ctx} limit)",
                    "type": "context_length_exceeded",
                }},
                status_code=413,
            )

    # Fill in the served model if the caller didn't name one.
    payload.setdefault("model", _default_model())

    timeout = float(_int_env("LLM_TIMEOUT_S", 120))
    status, body = await run_in_threadpool(_forward_to_ollama, payload, timeout)
    return JSONResponse(body, status_code=status)
