"""
Tokyo Weather Digest — a self-contained tool domain alongside ``time_server``.

A long-lived agent: while the MCP server runs, a background scheduler collects
the current Tokyo weather once an hour and stores each reading in SQLite. The
``get_weather_digest(period)`` tool aggregates those stored readings (average /
min / max temperature, dominant condition, rainfall count, temperature trend)
over a window such as ``"24h"`` or ``"7d"``.

Layering mirrors the rest of the repo — plain, MCP-free functions so the storage,
weather client and aggregation are unit-testable on their own; the MCP wiring
(tool + lifespan) lives in ``time_server/server.py``.
"""

from .scheduler import WeatherScheduler
from .storage import WeatherStore
from .weather_client import fetch_tokyo_weather

__all__ = ["WeatherScheduler", "WeatherStore", "fetch_tokyo_weather"]
