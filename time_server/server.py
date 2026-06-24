"""
A standalone MCP **server** that reports the current time for a city.

Built on the official MCP SDK's FastMCP helper. Unlike the JarvisCLI weather
server (stdio, launched as a subprocess), this one runs as an **independent
network process** over Streamable HTTP — so it has its own lifecycle, can be
started and stopped on its own, and is reachable by any MCP client (the MCP
Inspector, JarvisCLI, or a future Cloud Run deployment).

Run it locally:

    python -m time_server.server          # serves http://0.0.0.0:8080/mcp

Then point the MCP Inspector at  http://localhost:8080/mcp  (Streamable HTTP).

Configuration is via environment variables (12-factor, Cloud Run-friendly):

    HOST       bind address      (default 0.0.0.0)
    PORT       bind port         (default 8080; Cloud Run injects this)
    TRANSPORT  streamable-http | sse | stdio   (default streamable-http)
"""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP

from .auth import ApiKeyAuthMiddleware
from .clients import time_in_city

logger = logging.getLogger("time_server")

# host/port are read at construction time so the same image runs locally and on
# Cloud Run (which injects PORT). log_level WARNING keeps per-request INFO noise
# out of the way, matching the weather server's choice.
mcp = FastMCP(
    "time",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8080")),
    log_level=os.environ.get("LOG_LEVEL", "WARNING"),
)


@mcp.tool()
def get_current_time(city: str) -> str:
    """Return the current local time for a city as a short human-readable line.

    Resolves the city to coordinates (Open-Meteo geocoding), then reads the
    local time for those coordinates (TimeAPI.io). Falls back to the system UTC
    clock when offline.
    """
    city = (city or "").strip()
    if not city:
        return "Please provide a non-empty city name."
    return time_in_city(city)


@mcp.tool()
def echo(text: str) -> str:
    """Return the input unchanged — a connectivity smoke-test tool."""
    return text


async def _healthz(_request):
    """Unauthenticated liveness probe (Cloud Run / curl)."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def build_app(transport: str):
    """Build the ASGI app for a network transport, wiring health + optional auth.

    Auth is applied only when MCP_API_KEY is set, so local Inspector testing stays
    open. With it set, every request (except /healthz) must carry a matching
    X-API-Key header. The key value is never logged.
    """
    if transport == "sse":
        app = mcp.sse_app()
    else:  # streamable-http (default)
        app = mcp.streamable_http_app()

    app.add_route("/healthz", _healthz, methods=["GET"])

    api_key = os.environ.get("MCP_API_KEY", "").strip()
    if api_key:
        logger.info("API-key auth ENABLED (X-API-Key required)")
        return ApiKeyAuthMiddleware(app, api_key)
    logger.warning(
        "MCP_API_KEY not set — server running WITHOUT authentication. "
        "Set MCP_API_KEY before exposing this server publicly."
    )
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    transport = os.environ.get("TRANSPORT", "streamable-http")

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(transport), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
