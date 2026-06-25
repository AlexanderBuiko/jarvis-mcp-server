"""
The hourly weather-collection agent.

A single background daemon thread: each cycle it collects one reading for **every
tracked city** (read fresh from the store, so cities added/removed via the MCP
tools are picked up without a restart), stores them, then waits for the interval
(default one hour) — repeating until asked to stop. Implemented with the stdlib
``threading`` only (no APScheduler), keeping the dependency surface as lean as the
rest of the server.

The wait is an ``Event.wait(interval)`` rather than ``sleep``, so ``stop()`` wakes
the thread immediately for a clean, prompt shutdown instead of blocking up to a
full interval. The collection runs independently of any tool call — the digest
tool only reads what this thread has written.
"""

from __future__ import annotations

import logging
import threading

from .storage import WeatherStore
from .weather_client import fetch_tokyo_weather

logger = logging.getLogger("weather_digest.scheduler")

# One hour between collections, overridable (mainly so tests run in milliseconds).
DEFAULT_INTERVAL_S = 3600.0


class WeatherScheduler:
    """Periodically collect and store weather for every tracked city."""

    def __init__(
        self,
        store: WeatherStore,
        interval_s: float = DEFAULT_INTERVAL_S,
        collect=fetch_tokyo_weather,
    ) -> None:
        self._store = store
        self._interval_s = interval_s
        self._collect = collect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def collect_city(self, city: str) -> dict:
        """Fetch one reading for ``city`` and persist it. Returns the reading."""
        reading = self._collect(city)
        self._store.insert(reading)
        logger.info(
            "collected %s: %.1fC, %s",
            reading["city"], reading["temperature"], reading["weather_condition"],
        )
        return reading

    def collect_all(self) -> int:
        """Collect once for each tracked city. Returns how many were collected.

        A failure on one city is logged and never blocks the others.
        """
        collected = 0
        for city in self._store.list_cities():
            try:
                self.collect_city(city)
                collected += 1
            except Exception:  # noqa: BLE001 — one bad city must not skip the rest
                logger.exception("weather collection failed for %s", city)
        return collected

    def _run(self) -> None:
        # Collect once immediately so a freshly-started server has live data,
        # then settle into the hourly cadence until stopped.
        while not self._stop.is_set():
            self.collect_all()
            self._stop.wait(self._interval_s)

    def start(self) -> None:
        """Start the background collection thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="weather-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("weather scheduler started (interval=%.0fs)", self._interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("weather scheduler stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
