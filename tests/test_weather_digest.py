"""
Offline tests for the Tokyo weather-digest domain.

No network and no running server: the weather client is stubbed, storage points at
a temp DB, and the scheduler runs at a millisecond interval. Covers storage +
seeding, aggregation math, period parsing, scheduler start/stop, and the
``get_weather_digest`` tool's output shape.
"""

import json
import time

import pytest

from weather_digest import aggregate
from weather_digest.scheduler import WeatherScheduler
from weather_digest.storage import WeatherStore, generate_seed_measurements


@pytest.fixture
def store(tmp_path):
    return WeatherStore(db_path=str(tmp_path / "weather.db"))


# ── Storage + seeding ────────────────────────────────────────────────────────

def test_insert_and_count(store):
    assert store.count("Tokyo") == 0
    store.insert({
        "city": "Tokyo", "timestamp": "2026-06-25T10:00:00+00:00",
        "temperature": 21.0, "weather_condition": "clear sky",
        "humidity": 55.0, "precipitation": 0.0,
    })
    assert store.count("Tokyo") == 1


def test_seed_if_empty_fills_seven_days(store):
    inserted = store.seed_if_empty("Tokyo")
    assert inserted == 7 * 24
    assert store.count("Tokyo") == 7 * 24
    # Idempotent: a second call adds nothing.
    assert store.seed_if_empty("Tokyo") == 0


def test_seed_has_variety_and_rain(store):
    rows = generate_seed_measurements("Tokyo")
    conditions = {r["weather_condition"] for r in rows}
    assert len(conditions) >= 3  # multiple distinct conditions
    assert any((r["precipitation"] or 0) > 0 for r in rows)  # some rainfall
    temps = [r["temperature"] for r in rows]
    assert max(temps) - min(temps) > 3  # realistic diurnal variation


def test_measurements_since_filters_by_cutoff(store):
    for ts, temp in [("2026-06-20T00:00:00+00:00", 10.0),
                     ("2026-06-25T00:00:00+00:00", 20.0)]:
        store.insert({"city": "Tokyo", "timestamp": ts, "temperature": temp,
                      "weather_condition": "clear sky", "humidity": 50.0,
                      "precipitation": 0.0})
    recent = store.measurements_since("2026-06-23T00:00:00+00:00", "Tokyo")
    assert [r["temperature"] for r in recent] == [20.0]


# ── Aggregation ──────────────────────────────────────────────────────────────

def _rows(*specs):
    """Build rows from (timestamp, temp, condition, precip) tuples."""
    return [
        {"timestamp": ts, "temperature": t, "weather_condition": c,
         "precipitation": p, "humidity": 50.0}
        for ts, t, c, p in specs
    ]


def test_build_digest_math():
    rows = _rows(
        ("2026-06-25T00:00:00+00:00", 10.0, "clear sky", 0.0),
        ("2026-06-25T01:00:00+00:00", 20.0, "rain", 2.0),
        ("2026-06-25T02:00:00+00:00", 30.0, "clear sky", 0.0),
    )
    d = aggregate.build_digest(rows, "24h")
    assert d["sample_count"] == 3
    assert d["average_temperature"] == 20.0
    assert d["min_temperature"] == 10.0
    assert d["max_temperature"] == 30.0
    assert d["most_common_condition"] == "clear sky"
    assert d["rainfall_occurrences"] == 1
    assert d["temperature_trend"] == "rising"


def test_rainfall_counts_condition_without_precip():
    rows = _rows(
        ("t1", 18.0, "rain showers", 0.0),   # precip 0 but condition is wet
        ("t2", 18.0, "clear sky", 0.0),
    )
    assert aggregate.build_digest(rows, "24h")["rainfall_occurrences"] == 1


def test_trend_falling_and_stable():
    falling = _rows(("a", 30.0, "x", 0.0), ("b", 30.0, "x", 0.0),
                    ("c", 10.0, "x", 0.0), ("d", 10.0, "x", 0.0))
    assert aggregate.build_digest(falling, "24h")["temperature_trend"] == "falling"
    stable = _rows(("a", 20.0, "x", 0.0), ("b", 20.1, "x", 0.0),
                   ("c", 20.0, "x", 0.0), ("d", 20.2, "x", 0.0))
    assert aggregate.build_digest(stable, "24h")["temperature_trend"] == "stable"


def test_empty_window_digest():
    d = aggregate.build_digest([], "7d")
    assert d["sample_count"] == 0
    assert "note" in d


@pytest.mark.parametrize("period,hours", [("24h", 24), ("7d", 168), ("1h", 1)])
def test_parse_period_valid(period, hours):
    assert aggregate.parse_period(period).total_seconds() == hours * 3600


@pytest.mark.parametrize("bad", ["", "abc", "10", "0h", "-3d", "5w"])
def test_parse_period_invalid(bad):
    with pytest.raises(aggregate.InvalidPeriod):
        aggregate.parse_period(bad)


# ── Tracked cities ───────────────────────────────────────────────────────────

def test_add_remove_list_cities(store):
    assert store.list_cities() == []
    assert store.add_city("Tokyo") is True
    assert store.add_city("tokyo") is False  # case-insensitive duplicate
    store.add_city("Osaka")
    assert store.list_cities() == ["Tokyo", "Osaka"]  # insertion order
    assert store.remove_city("Tokyo") is True
    assert store.remove_city("Tokyo") is False  # already gone
    assert store.list_cities() == ["Osaka"]


def test_remove_city_keeps_measurements(store):
    store.add_city("Tokyo")
    store.insert({"city": "Tokyo", "timestamp": "2026-06-25T10:00:00+00:00",
                  "temperature": 21.0, "weather_condition": "clear sky",
                  "humidity": 55.0, "precipitation": 0.0})
    store.remove_city("Tokyo")
    assert store.list_cities() == []          # no longer collected
    assert store.count("Tokyo") == 1          # history preserved


# ── Scheduler ────────────────────────────────────────────────────────────────

def _fake_collect(city):
    return {"city": city, "timestamp": "2026-06-25T00:00:00+00:00",
            "temperature": 20.0, "weather_condition": "clear sky",
            "humidity": 50.0, "precipitation": 0.0}


def test_scheduler_collects_and_stops(store):
    store.add_city("Tokyo")
    sched = WeatherScheduler(store, interval_s=0.01, collect=_fake_collect)
    sched.start()
    assert sched.running
    time.sleep(0.1)  # let several cycles run
    sched.stop()
    assert not sched.running
    assert store.count("Tokyo") >= 2  # collected independently of any tool call


def test_scheduler_collects_every_tracked_city(store):
    store.add_city("Tokyo")
    store.add_city("Osaka")
    sched = WeatherScheduler(store, interval_s=999, collect=_fake_collect)
    assert sched.collect_all() == 2
    assert store.count("Tokyo") == 1
    assert store.count("Osaka") == 1


def test_collect_city_persists(store):
    sched = WeatherScheduler(store, interval_s=999, collect=_fake_collect)
    sched.collect_city("Nagoya")
    assert store.count("Nagoya") == 1


# ── Tool handler ─────────────────────────────────────────────────────────────

def test_get_weather_digest_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_DB_PATH", str(tmp_path / "weather.db"))
    # Reload server module state isn't needed: the tool falls back to a fresh
    # WeatherStore() which reads WEATHER_DB_PATH and auto-seeds.
    from time_server import server

    monkeypatch.setattr(server, "_store", None)
    out = server.get_weather_digest("7d")
    digest = json.loads(out)
    assert digest["city"] == "Tokyo"
    assert digest["sample_count"] > 0
    assert {"average_temperature", "min_temperature", "max_temperature",
            "most_common_condition", "rainfall_occurrences",
            "temperature_trend"} <= digest.keys()


def test_get_weather_digest_invalid_period(tmp_path, monkeypatch):
    monkeypatch.setenv("WEATHER_DB_PATH", str(tmp_path / "weather.db"))
    from time_server import server

    monkeypatch.setattr(server, "_store", None)
    digest = json.loads(server.get_weather_digest("nonsense"))
    assert "error" in digest


def _fresh_server(tmp_path, monkeypatch):
    """Point the server tools at a temp DB with no live scheduler."""
    monkeypatch.setenv("WEATHER_DB_PATH", str(tmp_path / "weather.db"))
    from time_server import server

    monkeypatch.setattr(server, "_store", WeatherStore())
    monkeypatch.setattr(server, "_scheduler", None)
    return server


def test_add_remove_list_city_tools(tmp_path, monkeypatch):
    server = _fresh_server(tmp_path, monkeypatch)

    added = json.loads(server.add_city("Osaka"))
    assert added["added"] is True
    assert "Osaka" in added["tracked_cities"]

    listed = json.loads(server.list_cities())
    assert "Osaka" in listed["tracked_cities"]

    removed = json.loads(server.remove_city("Osaka"))
    assert removed["removed"] is True
    assert "Osaka" not in removed["tracked_cities"]


def test_add_city_rejects_empty(tmp_path, monkeypatch):
    server = _fresh_server(tmp_path, monkeypatch)
    assert "error" in json.loads(server.add_city("   "))


def test_digest_is_city_scoped(tmp_path, monkeypatch):
    server = _fresh_server(tmp_path, monkeypatch)
    server._store.insert({"city": "Osaka", "timestamp": "2026-06-25T12:00:00+00:00",
                          "temperature": 27.0, "weather_condition": "clear sky",
                          "humidity": 50.0, "precipitation": 0.0})
    # A wide window so the inserted row is included regardless of "now".
    digest = json.loads(server.get_weather_digest("3650d", city="Osaka"))
    assert digest["city"] == "Osaka"
    assert digest["sample_count"] == 1
    assert digest["average_temperature"] == 27.0
