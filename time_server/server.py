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

import json
import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from weather_digest.aggregate import InvalidPeriod, build_digest, cutoff_for
from weather_digest.scheduler import DEFAULT_INTERVAL_S, WeatherScheduler
from weather_digest.storage import WeatherStore

from .auth import ApiKeyAuthMiddleware
from .clients import time_in_city

logger = logging.getLogger("time_server")

# The city the digest agent tracks, and how often it collects (env-overridable so
# tests/demos can speed the cadence up without touching code).
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Tokyo").strip() or "Tokyo"
_COLLECT_INTERVAL_S = float(os.environ.get("WEATHER_COLLECT_INTERVAL_S", DEFAULT_INTERVAL_S))

# Created on server startup (see _start_weather_agent); the digest tool reads it.
_store: WeatherStore | None = None
_scheduler: WeatherScheduler | None = None


def _start_weather_agent() -> None:
    """Open the DB (seeding it if empty) and start the hourly collection thread.

    Idempotent — safe to call from whichever lifecycle hook fires for the active
    transport. The default city (WEATHER_CITY) is always tracked; on a fresh DB it
    is seeded with 7 days of mock readings so the digest is demoable immediately.
    The scheduler then collects live data hourly for every tracked city. More
    cities can be added/removed at runtime via the add_city / remove_city tools.
    """
    global _store, _scheduler
    if _scheduler and _scheduler.running:
        return
    _store = WeatherStore()
    _store.add_city(WEATHER_CITY)  # the default city is always tracked
    seeded = _store.seed_if_empty(WEATHER_CITY)
    if seeded:
        logger.info("seeded empty DB with %d mock %s readings", seeded, WEATHER_CITY)
    _scheduler = WeatherScheduler(_store, interval_s=_COLLECT_INTERVAL_S)
    _scheduler.start()


def _stop_weather_agent() -> None:
    """Signal the collection thread to stop and wait for it to finish."""
    if _scheduler:
        _scheduler.stop()


# Lifecycle note: the agent is tied to the **process**, not to an MCP session.
# Deliberately NOT a FastMCP constructor lifespan — the lowlevel server runs that
# once *per client session*, which would restart the scheduler on every
# connection. For the network transports we wrap the Starlette app's lifespan
# (fires once per process, and its shutdown runs cleanly on SIGTERM); for stdio
# main() brackets the run. Both routes funnel through the idempotent helpers.

# host/port are read at construction time so the same image runs locally and on
# Cloud Run (which injects PORT). log_level WARNING keeps per-request INFO noise
# out of the way, matching the weather server's choice.
mcp = FastMCP(
    "jarvis",
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
def get_weather_digest(period: str = "24h", city: str = "") -> str:
    """Return an aggregated weather digest for a city over a period.

    Args:
        period: time window — a number plus a unit, e.g. "24h", "7d" or "1w".
        city: city name **in English / Latin script** (e.g. "Tokyo", not "Токио") —
            measurements are stored under the English name the data source uses, so
            a localized name will match nothing. Defaults to Tokyo.

    Reads the measurements the background scheduler has stored and aggregates
    them: average / min / max temperature, the most common weather condition,
    how many readings had rainfall, and a simple temperature trend, as a JSON
    object. The default city is auto-seeded with mock data so a digest is always
    available; other cities accumulate data once added via add_city. If the city
    has no data, the result lists the cities that do, so you can retry.
    """
    store = _store or WeatherStore()
    target = (city or "").strip() or WEATHER_CITY
    store.seed_if_empty(WEATHER_CITY)  # default city demoable even before any live cycle
    try:
        cutoff = cutoff_for(period)
    except InvalidPeriod as exc:
        return json.dumps({"error": str(exc)})
    rows = store.measurements_since(cutoff, city=target)
    digest = build_digest(rows, period=period, city=target)
    if digest.get("sample_count", 0) == 0:
        # Make the empty case self-correcting: tell the caller which English city
        # keys actually have data (the most common cause is a localized name).
        digest["default_city"] = WEATHER_CITY
        digest["cities_with_data"] = store.cities_with_data()
    return json.dumps(digest, indent=2)


@mcp.tool()
def add_city(city: str) -> str:
    """Start collecting weather for a city. Returns the updated tracked-city list.

    Case-insensitive; re-adding an existing city is a no-op. If the scheduler is
    running, one reading is collected immediately so the city has data right away
    (otherwise it is picked up on the next hourly cycle).
    """
    name = (city or "").strip()
    if not name:
        return json.dumps({"error": "city must be a non-empty name"})
    store = _store or WeatherStore()
    added = store.add_city(name)
    collected_now = False
    if added and _scheduler is not None:
        try:
            _scheduler.collect_city(name)
            collected_now = True
        except Exception:  # noqa: BLE001 — collection is best-effort; the city is still tracked
            logger.exception("immediate collection failed for %s", name)
    return json.dumps({
        "city": name, "added": added, "collected_now": collected_now,
        "tracked_cities": store.list_cities(),
    })


@mcp.tool()
def remove_city(city: str) -> str:
    """Stop collecting weather for a city. Its past measurements are kept.

    Returns whether the city was tracked, plus the updated tracked-city list.
    """
    store = _store or WeatherStore()
    removed = store.remove_city(city)
    return json.dumps({
        "city": (city or "").strip(), "removed": removed,
        "tracked_cities": store.list_cities(),
    })


@mcp.tool()
def list_cities() -> str:
    """Return the cities the digest agent is currently collecting weather for."""
    store = _store or WeatherStore()
    return json.dumps({"tracked_cities": store.list_cities()})


@mcp.tool()
def echo(text: str) -> str:
    """Return the input unchanged — a connectivity smoke-test tool."""
    return text


async def _healthz(_request):
    """Unauthenticated liveness probe (Cloud Run / curl)."""
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


def _wrap_lifespan_with_weather_agent(app) -> None:
    """Run the weather agent's start/stop around a Starlette app's own lifespan.

    The SDK's network app already sets a lifespan (the MCP session manager); we
    compose ours around it. This fires once per process — uvicorn drives the ASGI
    lifespan a single time — and its shutdown half runs on SIGTERM, so the
    scheduler is stopped and joined cleanly on server shutdown.
    """
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def combined(scope_app):
        _start_weather_agent()
        try:
            async with inner(scope_app):
                yield
        finally:
            _stop_weather_agent()

    app.router.lifespan_context = combined


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

    _wrap_lifespan_with_weather_agent(app)
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
        # stdio has no ASGI lifespan, so bracket the run here (one session = the
        # whole process lifetime, so this fires exactly once).
        _start_weather_agent()
        try:
            mcp.run(transport="stdio")
        finally:
            _stop_weather_agent()
        return

    # Network transports: the agent lifecycle rides the ASGI app lifespan wired in
    # build_app (starts on startup, stops cleanly on shutdown).
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(transport), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
