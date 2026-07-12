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

import ast
import json
import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from weather_digest.aggregate import (
    MAX_READINGS_HOURS,
    InvalidPeriod,
    build_digest,
    build_readings_report,
    cutoff_for,
    parse_period,
)
from weather_digest.anomalies import InvalidReport, detect_anomalies, validate_report
from weather_digest.scheduler import DEFAULT_INTERVAL_S, WeatherScheduler
from weather_digest.storage import WeatherStore
from weather_digest import telegram

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


def _coerce_json_obj(value):
    """Accept a dict/list as-is, or parse a JSON (or Python-repr) string into one.

    Returns the parsed object, or None if it's empty/unparseable. This lets the
    pipeline tools take either a structured object or its text form, so the model
    needn't re-serialize a prior tool's output — and a Python-repr slip (single
    quotes, from str(dict)) still parses via the literal_eval fallback.
    """
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        obj = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return obj if isinstance(obj, (dict, list)) else None


@mcp.tool()
def get_weather_readings(city: str = "", period: str = "7d") -> str:
    """Return a compact per-day weather report for a city over a period (≤ 7 days).

    The first tool in the anomaly pipeline. It reads the stored measurements and
    rolls them up **server-side** into a small report — one bucket per UTC day with
    mean/min/max temperature and the fraction of rainy readings — so the raw rows
    never leave the server. Feed this report straight into detect_weather_anomalies.

    Args:
        period: time window, a number plus a unit (e.g. "24h", "7d", "1w"). Capped
            at 7 days; a longer window returns an error.
        city: city name in English / Latin script (e.g. "Tokyo"). Defaults to Tokyo.

    If the city has no stored data the report's sample_count is 0 and it lists the
    cities that do have data, so you can retry with one of those.
    """
    store = _store or WeatherStore()
    target = (city or "").strip() or WEATHER_CITY
    store.seed_if_empty(WEATHER_CITY)
    try:
        delta = parse_period(period)
    except InvalidPeriod as exc:
        return json.dumps({"error": str(exc)})
    if delta.total_seconds() > MAX_READINGS_HOURS * 3600:
        return json.dumps({
            "error": "period must be 7 days or less for get_weather_readings; "
                     f"got {period!r}."
        })
    rows = store.measurements_since(cutoff_for(period), city=target)
    report = build_readings_report(rows, period=period, city=target)
    if report.get("sample_count", 0) == 0:
        report["default_city"] = WEATHER_CITY
        report["cities_with_data"] = store.cities_with_data()
    return json.dumps(report, indent=2)


@mcp.tool()
def detect_weather_anomalies(weather_report: str | dict = "") -> str:
    """Detect unusual weather from a get_weather_readings report (deterministic rules).

    The second tool in the pipeline. Pass it the **report from get_weather_readings**
    — either the report object directly or its JSON text; both are accepted, so you
    don't need to re-serialize it. Detected anomaly types include rapid day-over-day
    temperature rises/drops, high temperature variability, unusually high or low
    rainfall frequency, prolonged bad weather, and warming/cooling trends. Returns a
    small report with an ``anomaly_count`` and an ``anomalies`` list; feed that to
    send_telegram_alert.

    Raw readings (a JSON array) or any payload that is not a get_weather_readings
    report are rejected with an explanatory error, so retry by calling
    get_weather_readings first.
    """
    try:
        report = validate_report(weather_report)
    except InvalidReport as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(detect_anomalies(report), indent=2)


@mcp.tool()
def send_telegram_alert(
    anomaly_report: str | dict = "", notify_when_clear: bool = False, message: str = ""
) -> str:
    """Send a Telegram notification.

    Two modes:

    * **Composed message (preferred for rich notifications):** pass ``message`` with
      the full text you want delivered — e.g. an anomaly summary plus a translated
      news digest and the local timestamp. It is sent verbatim, regardless of
      whether anomalies were detected. Use this when the notification must contain
      more than the anomaly report alone.
    * **Auto-formatted anomaly alert:** omit ``message`` and pass ``anomaly_report``
      (the detect_weather_anomalies output, object or JSON). A message is formatted
      from it and sent only when anomalies exist — unless ``notify_when_clear`` is
      True, which also sends a reassuring "all clear".

    If Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset) it
    reports that without failing the turn. Returns a small result describing what
    happened.
    """
    # Composed-message mode: send exactly what the caller assembled.
    text = str(message or "").strip()
    if text:
        result = telegram.send_message(text)
        return json.dumps({
            "sent": bool(result.get("ok")),
            "skipped": False,
            "reason": result.get("reason"),
            "message_preview": text[:500],
        })

    report = _coerce_json_obj(anomaly_report)
    if report is None:
        return json.dumps({"sent": False, "skipped": False,
                           "reason": "anomaly_report is not valid JSON; pass the "
                                     "detect_weather_anomalies output."})
    if not isinstance(report, dict):
        return json.dumps({"sent": False, "skipped": False,
                           "reason": "anomaly_report must be a JSON object."})

    count = int(report.get("anomaly_count", 0) or 0)
    if count <= 0 and not notify_when_clear:
        return json.dumps({"sent": False, "skipped": True, "anomaly_count": 0,
                           "reason": "no anomalies detected; nothing to alert"})

    message = telegram.format_alert(report) if count > 0 else telegram.format_all_clear(report)
    result = telegram.send_message(message)
    return json.dumps({
        "sent": bool(result.get("ok")),
        "skipped": False,
        "anomaly_count": count,
        "reason": result.get("reason"),
        "message_preview": message,
    })


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

    # Private AI service: an authenticated, rate-limited, context-capped chat proxy
    # to the local LLM (Ollama). Guarded by the X-API-Key middleware below like any
    # non-health path. Serves the model only — no KB / retrieval here.
    from .llm_proxy import chat_completions
    app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])

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
