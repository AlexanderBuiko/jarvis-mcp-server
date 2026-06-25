"""
A weather reading source for the digest scheduler.

Mirrors the repo's *geocode-then-fetch* idiom (see ``time_server/clients.py`` and
the JarvisCLI weather server): resolve the city to coordinates via Open-Meteo
geocoding, then read the current conditions from the Open-Meteo forecast API.
Both endpoints are free and need no key. Any network failure falls back to a
deterministic mock reading so the hourly collection never hard-fails — an empty
DB would otherwise break the demo.

Kept free of MCP imports so it's unit-testable on its own.
"""

from __future__ import annotations

import json
import ssl
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

# Use certifi's CA bundle when present so HTTPS works on Python builds whose
# default trust store isn't configured (the common macOS python.org case).
try:
    import certifi

    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - environment-dependent
    _SSL_CTX = None

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT_S = 6

# Fallback coordinates for Tokyo, used when geocoding is unavailable so we can
# still attempt a real forecast read before dropping to a fully mock reading.
_TOKYO_LAT, _TOKYO_LON = 35.6895, 139.6917

# WMO weather-code → human description (the common cases), matching the wording
# used by the JarvisCLI weather server so conditions are consistent across tools.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
}


def _get_json(url: str, params: dict) -> dict:
    with urlopen(f"{url}?{urlencode(params)}", timeout=_HTTP_TIMEOUT_S, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (the storage timestamp format)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mock_reading(city: str) -> dict:
    """Deterministic offline fallback so a collection cycle never hard-fails."""
    return {
        "city": city,
        "timestamp": _now_iso(),
        "temperature": 18.0,
        "weather_condition": "partly cloudy (mock)",
        "humidity": 60.0,
        "precipitation": 0.0,
        "source": "mock (network unavailable)",
    }


def _coords_for(city: str) -> tuple[float, float]:
    """Resolve a city to (lat, lon), falling back to Tokyo's known coordinates."""
    try:
        geo = _get_json(_GEOCODE_URL, {"name": city, "count": 1})
        results = geo.get("results") or []
        if results:
            return results[0]["latitude"], results[0]["longitude"]
    except Exception:  # noqa: BLE001 — any geocode failure → fixed Tokyo coords
        pass
    return _TOKYO_LAT, _TOKYO_LON


def fetch_tokyo_weather(city: str = "Tokyo") -> dict:
    """Return one current-weather reading for ``city`` as a storage-ready dict.

    Keys: ``city, timestamp, temperature, weather_condition, humidity,
    precipitation, source``. Falls back to a deterministic mock reading when the
    network is unavailable, so the caller always gets a usable row.
    """
    lat, lon = _coords_for(city)
    try:
        forecast = _get_json(_FORECAST_URL, {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code",
        })
        current = forecast.get("current", {})
        return {
            "city": city,
            "timestamp": _now_iso(),
            "temperature": float(current["temperature_2m"]),
            "weather_condition": _WMO.get(current.get("weather_code"), "unknown conditions"),
            "humidity": _maybe_float(current.get("relative_humidity_2m")),
            "precipitation": _maybe_float(current.get("precipitation")) or 0.0,
            "source": "Open-Meteo",
        }
    except Exception:  # noqa: BLE001 — network/shape failure → mock so we never skip a cycle
        return _mock_reading(city)


def _maybe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
