"""
SQLite persistence for weather measurements.

A thin wrapper over the stdlib ``sqlite3`` module — no ORM, no extra deps. The
store is used from two threads (the hourly scheduler writes; the tool handler
reads), so every operation opens a short-lived connection rather than sharing one
across threads, and the database runs in WAL mode so a read never blocks a write.

Schema (table ``weather_measurements``)::

    id                INTEGER PRIMARY KEY AUTOINCREMENT
    city              TEXT    NOT NULL
    timestamp         TEXT    NOT NULL   -- ISO-8601 UTC
    temperature       REAL    NOT NULL   -- degrees C
    weather_condition TEXT    NOT NULL   -- WMO description
    humidity          REAL                -- percent (nullable)
    precipitation     REAL                -- mm (nullable)

Kept MCP-free and path-injectable so tests can point it at a temp file.
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_measurements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    city              TEXT    NOT NULL,
    timestamp         TEXT    NOT NULL,
    temperature       REAL    NOT NULL,
    weather_condition TEXT    NOT NULL,
    humidity          REAL,
    precipitation     REAL
);
CREATE INDEX IF NOT EXISTS idx_city_ts ON weather_measurements (city, timestamp);

-- The set of cities the scheduler currently collects for. Removing a city drops
-- it here (collection stops) but leaves its measurements intact. COLLATE NOCASE
-- so "tokyo" and "Tokyo" are the same tracked city.
CREATE TABLE IF NOT EXISTS tracked_cities (
    city     TEXT PRIMARY KEY COLLATE NOCASE,
    added_at TEXT NOT NULL
);
"""

# Default DB location: alongside this package, overridable for Docker/tests.
_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "weather.db")


def default_db_path() -> str:
    """Resolve the DB path from the env at call time (WEATHER_DB_PATH)."""
    return os.environ.get("WEATHER_DB_PATH", "").strip() or _DEFAULT_DB


class WeatherStore:
    """Read/write access to the weather_measurements table."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Writes ────────────────────────────────────────────────────────────────

    def insert(self, reading: dict) -> None:
        """Persist one reading dict (as produced by ``fetch_tokyo_weather``)."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO weather_measurements
                   (city, timestamp, temperature, weather_condition, humidity, precipitation)
                   VALUES (:city, :timestamp, :temperature, :weather_condition,
                           :humidity, :precipitation)""",
                {
                    "city": reading["city"],
                    "timestamp": reading["timestamp"],
                    "temperature": reading["temperature"],
                    "weather_condition": reading["weather_condition"],
                    "humidity": reading.get("humidity"),
                    "precipitation": reading.get("precipitation"),
                },
            )

    # ── Tracked cities ────────────────────────────────────────────────────────

    def add_city(self, city: str) -> bool:
        """Track ``city`` for collection. Returns True if newly added.

        Case-insensitive: re-adding an existing city (any case) is a no-op.
        """
        city = (city or "").strip()
        if not city:
            raise ValueError("city must be a non-empty name")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tracked_cities (city, added_at) VALUES (?, ?)",
                (city, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            )
            return cur.rowcount > 0

    def remove_city(self, city: str) -> bool:
        """Stop collecting ``city``. Returns True if it was tracked.

        Only untracks — the city's stored measurements are left untouched.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tracked_cities WHERE city = ?", ((city or "").strip(),))
            return cur.rowcount > 0

    def list_cities(self) -> list[str]:
        """Tracked cities, in the order they were added."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT city FROM tracked_cities ORDER BY rowid ASC"
            ).fetchall()
        return [r["city"] for r in rows]

    # ── Reads ─────────────────────────────────────────────────────────────────

    def count(self, city: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM weather_measurements"
        params: tuple = ()
        if city:
            sql += " WHERE city = ? COLLATE NOCASE"
            params = (city,)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()["n"])

    def cities_with_data(self) -> list[str]:
        """Distinct city keys that actually have stored measurements."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT city FROM weather_measurements ORDER BY city"
            ).fetchall()
        return [r["city"] for r in rows]

    def measurements_since(self, cutoff_iso: str, city: str = "Tokyo") -> list[dict]:
        """Rows for ``city`` with timestamp >= ``cutoff_iso``, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM weather_measurements
                   WHERE city = ? COLLATE NOCASE AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (city, cutoff_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Seeding ───────────────────────────────────────────────────────────────

    def seed_if_empty(self, city: str = "Tokyo") -> int:
        """If no rows exist for ``city``, fill 7 days of hourly mock readings.

        Returns the number of rows inserted (0 if the table already had data).
        Lets the digest be exercised immediately on a fresh database.
        """
        if self.count(city) > 0:
            return 0
        rows = generate_seed_measurements(city)
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO weather_measurements
                   (city, timestamp, temperature, weather_condition, humidity, precipitation)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (r["city"], r["timestamp"], r["temperature"],
                     r["weather_condition"], r["humidity"], r["precipitation"])
                    for r in rows
                ],
            )
        return len(rows)


# A small, realistic set of Tokyo conditions with plausible precipitation.
_SEED_CONDITIONS = [
    ("clear sky", 0.0), ("mainly clear", 0.0), ("partly cloudy", 0.0),
    ("overcast", 0.0), ("light rain", 1.2), ("rain", 3.5),
    ("rain showers", 2.0), ("light drizzle", 0.4),
]


def generate_seed_measurements(city: str = "Tokyo", days: int = 7, seed: int = 42) -> list[dict]:
    """Build ``days`` of hourly mock readings with realistic diurnal variation.

    Deterministic (fixed RNG seed) so tests can assert on it. Temperature follows
    a daily sine curve (cool at night, warm mid-afternoon) plus small noise; a
    fraction of hours are rainy across several conditions.
    """
    import math

    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    rows: list[dict] = []

    total_hours = days * 24
    for h in range(total_hours):
        ts = start + timedelta(hours=h)
        hour = ts.hour
        # Diurnal curve: min ~14C around 05:00, max ~24C around 15:00.
        base = 19.0 + 5.0 * math.sin((hour - 9) / 24 * 2 * math.pi)
        temp = round(base + rng.uniform(-1.5, 1.5), 1)

        # ~25% of hours get a wet condition; the rest are dry.
        if rng.random() < 0.25:
            condition, precip = rng.choice(_SEED_CONDITIONS[4:])
            precip = round(precip * rng.uniform(0.5, 1.5), 1)
        else:
            condition, precip = rng.choice(_SEED_CONDITIONS[:4])

        rows.append({
            "city": city,
            "timestamp": ts.replace(microsecond=0).isoformat(),
            "temperature": temp,
            "weather_condition": condition,
            "humidity": round(rng.uniform(45, 85), 0),
            "precipitation": precip,
        })
    return rows
