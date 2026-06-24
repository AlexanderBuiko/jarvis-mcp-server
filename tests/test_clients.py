"""Offline unit tests for the client layer — no network required.

Network paths are exercised by tests/smoke_http.py against the live server;
here we pin the deterministic behaviour: unknown city, and the system-clock
fallback when the network is unavailable.
"""

from urllib.error import URLError

from time_server import clients


def test_unknown_city_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(clients, "_get_json", lambda url, params: {"results": []})
    out = clients.time_in_city("NotARealPlace")
    assert "Unknown city" in out


def test_falls_back_to_system_clock_when_offline(monkeypatch):
    def boom(url, params):
        raise URLError("offline")

    monkeypatch.setattr(clients, "_get_json", boom)
    out = clients.time_in_city("London")
    assert "system clock" in out
    assert "UTC" in out


def test_real_geocode_then_time_fallback(monkeypatch):
    # Geocode succeeds, time API fails -> labelled fallback that still names the place.
    monkeypatch.setattr(
        clients,
        "geocode_city",
        lambda city: {"name": "Paris", "country": "France", "latitude": 48.85, "longitude": 2.35},
    )

    def boom(lat, lon):
        raise URLError("time api down")

    monkeypatch.setattr(clients, "_fetch_time", boom)
    out = clients.time_in_city("Paris")
    assert "Paris, France" in out
    assert "system clock" in out
