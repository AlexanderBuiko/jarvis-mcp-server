"""
API-key authentication as a pure-ASGI middleware.

Why pure ASGI and not Starlette's ``BaseHTTPMiddleware``: the latter buffers the
response body, which breaks streaming transports (SSE / MCP streamable-http). A
thin ASGI wrapper inspects only the request headers and either rejects with 401
*before* the request reaches the MCP app, or passes the untouched stream through.

Behaviour:
  • ``MCP_API_KEY`` unset  → middleware is not installed at all (open server,
    for local Inspector testing). The caller logs a clear warning.
  • ``MCP_API_KEY`` set     → every HTTP request must carry a matching
    ``X-API-Key`` header. Comparison is constant-time. The key is never logged.
  • Exempt paths (e.g. ``/healthz``) skip the check so Cloud Run / curl can probe
    liveness without a key.
  • Non-HTTP scopes (``lifespan``, ``websocket``) pass straight through.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]

API_KEY_HEADER = b"x-api-key"  # ASGI header names are lowercased bytes


class ApiKeyAuthMiddleware:
    """Reject HTTP requests that lack a valid X-API-Key header."""

    def __init__(self, app, api_key: str, exempt_paths: tuple[str, ...] = ("/healthz",)) -> None:
        if not api_key:
            raise ValueError("ApiKeyAuthMiddleware requires a non-empty api_key")
        self._app = app
        self._expected = api_key.encode("utf-8")
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only guard HTTP; let lifespan/websocket through untouched.
        if scope.get("type") != "http" or scope.get("path") in self._exempt:
            await self._app(scope, receive, send)
            return

        provided = b""
        for name, value in scope.get("headers", []):
            if name == API_KEY_HEADER:
                provided = value
                break

        # Constant-time compare; a missing header compares against the key and fails.
        if not hmac.compare_digest(provided, self._expected):
            await self._reject(send)
            return

        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"error":"unauthorized","detail":"missing or invalid X-API-Key"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"X-API-Key"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
