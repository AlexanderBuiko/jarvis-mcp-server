"""
HTTP clients for the time server's two public, key-less data sources.

The flow mirrors the JarvisCLI weather server's *geocode-then-fetch* pattern:

  1. Open-Meteo geocoding  — city name  → latitude / longitude
  2. TimeAPI.io            — coordinates → current local time + timezone

Both APIs are free and need no key. As with the weather server, every network
path falls back to deterministic local data (here, the system UTC clock) so a
tool call never hard-fails — handy for offline demos and Inspector smoke-tests.

Kept as plain functions with no MCP imports so they're unit-testable on their own.
"""

from __future__ import annotations

import json
import ssl
from datetime import datetime, timezone
from urllib.error import URLError
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
_TIME_URL = "https://timeapi.io/api/Time/current/coordinate"
_HTTP_TIMEOUT_S = 6


class GeocodeError(RuntimeError):
    """Raised when a city name can't be resolved to coordinates."""


def _get_json(url: str, params: dict) -> dict:
    with urlopen(f"{url}?{urlencode(params)}", timeout=_HTTP_TIMEOUT_S, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def geocode_city(city: str) -> dict:
    """Resolve a city name to a place dict: name, country, latitude, longitude.

    Raises GeocodeError if the name is unknown. Network errors propagate as
    URLError so the caller can decide whether to fall back.
    """
    geo = _get_json(_GEOCODE_URL, {"name": city, "count": 1})
    results = geo.get("results") or []
    if not results:
        raise GeocodeError(f"Unknown city: {city!r}. Try a different spelling.")
    place = results[0]
    return {
        "name": place.get("name", city),
        "country": place.get("country", ""),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
    }


def _fetch_time(latitude: float, longitude: float) -> dict:
    """Call TimeAPI.io for the current local time at the given coordinates."""
    return _get_json(_TIME_URL, {"latitude": latitude, "longitude": longitude})


def _mock_time(place: dict) -> str:
    """Offline fallback: report system UTC so the tool never hard-fails."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    label = ", ".join(p for p in (place.get("name"), place.get("country")) if p)
    return f"{label or 'Unknown location'}: {now} UTC (source: system clock — network unavailable)"


def time_in_city(city: str) -> str:
    """Return a short human-readable line with the current local time in `city`.

    Two hops (geocode → time API); falls back to the system UTC clock on any
    network failure. A genuinely unknown city is reported as such (not faked).
    """
    try:
        place = geocode_city(city)
    except GeocodeError as exc:
        return str(exc)
    except (URLError, OSError, ValueError):
        # Can't even geocode — nothing to anchor a real answer to.
        return _mock_time({"name": city})

    try:
        data = _fetch_time(place["latitude"], place["longitude"])
        label = ", ".join(p for p in (place["name"], place["country"]) if p)
        local_time = data.get("dateTime", "")[:16].replace("T", " ")
        tz = data.get("timeZone", "")
        day = data.get("dayOfWeek", "")
        suffix = f" ({tz})" if tz else ""
        weekday = f"{day}, " if day else ""
        return f"{label}: {weekday}{local_time}{suffix} (source: TimeAPI.io)"
    except (URLError, OSError, ValueError, KeyError):
        return _mock_time(place)
