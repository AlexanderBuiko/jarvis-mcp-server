"""
Offline tests for the Telegram sender and the three pipeline MCP tools
(get_weather_readings → detect_weather_anomalies → send_telegram_alert),
including the end-to-end chain. No network: telegram.send_message is stubbed.
"""

import json

import pytest

from weather_digest import telegram
from weather_digest.storage import WeatherStore


# ── telegram module ──────────────────────────────────────────────────────────

def test_send_message_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram.is_configured() is False
    assert telegram.send_message("hi") == {"ok": False, "reason": "not configured"}


def test_is_configured_true(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert telegram.is_configured() is True


def test_format_alert_lists_anomalies():
    report = {
        "city": "Tokyo", "period": "7d",
        "anomalies": [
            {"type": "rapid_temperature_drop", "severity": "high",
             "detail": "-9.0°C between 2026-06-19 and 2026-06-20"},
        ],
        "summary": "1 weather anomaly detected for Tokyo over 7d.",
    }
    text = telegram.format_alert(report)
    assert "Tokyo" in text
    assert "rapid_temperature_drop" in text
    assert "HIGH" in text
    assert "1 weather anomaly" in text


# ── server tool fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    """Point the server tools at a temp DB with no live scheduler."""
    monkeypatch.setenv("WEATHER_DB_PATH", str(tmp_path / "weather.db"))
    from time_server import server as srv

    monkeypatch.setattr(srv, "_store", WeatherStore())
    monkeypatch.setattr(srv, "_scheduler", None)
    return srv


# ── get_weather_readings ─────────────────────────────────────────────────────

def test_get_weather_readings_shape(server):
    report = json.loads(server.get_weather_readings(period="7d"))
    assert report["report_type"] == "weather_readings.v1"
    assert report["city"] == "Tokyo"
    assert report["sample_count"] > 0
    assert 1 <= len(report["daily"]) <= 8
    assert {"date", "mean_temp", "min_temp", "max_temp", "rainy_fraction"} <= report["daily"][0].keys()


def test_get_weather_readings_rejects_period_over_a_week(server):
    out = json.loads(server.get_weather_readings(period="14d"))
    assert "error" in out
    assert "7 days or less" in out["error"]


def test_get_weather_readings_invalid_period(server):
    assert "error" in json.loads(server.get_weather_readings(period="nonsense"))


def test_get_weather_readings_unknown_city_lists_available(server):
    out = json.loads(server.get_weather_readings(period="7d", city="Атлантида"))
    assert out["sample_count"] == 0
    assert "Tokyo" in out["cities_with_data"]


# ── detect_weather_anomalies ─────────────────────────────────────────────────

def test_detect_tool_rejects_raw_array(server):
    raw = json.dumps([{"timestamp": "t", "temperature": 20.0, "condition": "clear"}])
    assert "error" in json.loads(server.detect_weather_anomalies(raw))


def test_detect_tool_on_real_report(server):
    report = server.get_weather_readings(period="7d")
    result = json.loads(server.detect_weather_anomalies(report))
    assert "anomaly_count" in result
    assert "anomalies" in result


# ── send_telegram_alert ──────────────────────────────────────────────────────

def test_send_alert_skips_when_no_anomalies(server):
    report = json.dumps({"city": "Tokyo", "period": "7d", "anomaly_count": 0,
                         "anomalies": [], "summary": "calm"})
    out = json.loads(server.send_telegram_alert(report))
    assert out["sent"] is False
    assert out["skipped"] is True


def test_send_alert_clear_skips_by_default(server, monkeypatch):
    # notify_when_clear defaults to False: a clean report must not send.
    calls = []
    monkeypatch.setattr(telegram, "send_message", lambda text: calls.append(text) or {"ok": True})
    report = json.dumps({"city": "Tokyo", "period": "7d", "anomaly_count": 0,
                         "anomalies": [], "summary": "calm"})
    out = json.loads(server.send_telegram_alert(report))
    assert out["skipped"] is True
    assert calls == []  # nothing sent


def test_send_alert_clear_notifies_when_requested(server, monkeypatch):
    sent = {}
    monkeypatch.setattr(telegram, "send_message",
                        lambda text: sent.update(text=text) or {"ok": True})
    report = json.dumps({"city": "Tokyo", "period": "7d", "anomaly_count": 0,
                         "anomalies": [], "summary": "calm"})
    out = json.loads(server.send_telegram_alert(report, notify_when_clear=True))
    assert out["sent"] is True
    assert out["skipped"] is False
    assert out["anomaly_count"] == 0
    assert "All clear" in sent["text"]
    assert "Tokyo" in sent["text"]


def test_format_all_clear():
    text = telegram.format_all_clear({"city": "Osaka", "period": "24h"})
    assert "All clear" in text
    assert "Osaka" in text


def test_send_alert_sends_when_anomalies(server, monkeypatch):
    sent = {}

    def fake_send(text):
        sent["text"] = text
        return {"ok": True}

    monkeypatch.setattr(telegram, "send_message", fake_send)
    report = json.dumps({
        "city": "Tokyo", "period": "7d", "anomaly_count": 1,
        "anomalies": [{"type": "rapid_temperature_drop", "severity": "high",
                       "detail": "-9°C"}],
        "summary": "1 weather anomaly detected for Tokyo over 7d.",
    })
    out = json.loads(server.send_telegram_alert(report))
    assert out["sent"] is True
    assert out["anomaly_count"] == 1
    assert "Tokyo" in sent["text"]


def test_send_alert_reports_telegram_failure(server, monkeypatch):
    monkeypatch.setattr(telegram, "send_message",
                        lambda text: {"ok": False, "reason": "not configured"})
    report = json.dumps({"city": "Tokyo", "period": "7d", "anomaly_count": 1,
                         "anomalies": [{"type": "x", "severity": "high", "detail": "y"}],
                         "summary": "s"})
    out = json.loads(server.send_telegram_alert(report))
    assert out["sent"] is False
    assert out["skipped"] is False
    assert out["reason"] == "not configured"


def test_send_alert_rejects_bad_json(server):
    assert "not valid JSON" in json.loads(server.send_telegram_alert("{bad"))["reason"]


def test_send_alert_accepts_dict_and_repr(server, monkeypatch):
    # The model may pass the report object directly, or a str()-ified (repr) form;
    # both must work, not only a JSON string.
    sent = []
    monkeypatch.setattr(telegram, "send_message",
                        lambda text: sent.append(text) or {"ok": True})
    report = {"city": "Tokyo", "period": "7d", "anomaly_count": 1,
              "anomalies": [{"type": "x", "severity": "high", "detail": "y"}],
              "summary": "s"}
    assert json.loads(server.send_telegram_alert(report))["sent"] is True          # dict
    assert json.loads(server.send_telegram_alert(str(report)))["sent"] is True      # repr
    assert len(sent) == 2


def test_detect_tool_accepts_dict_report(server):
    # detect_weather_anomalies must accept the report object, not only its JSON text.
    import json as _json
    readings = _json.loads(server.get_weather_readings(period="7d"))  # a dict
    result = _json.loads(server.detect_weather_anomalies(readings))
    assert "anomaly_count" in result


# ── End-to-end chain (in-process) ────────────────────────────────────────────

def test_full_pipeline_in_process(server, monkeypatch):
    """get_weather_readings → detect_weather_anomalies → send_telegram_alert."""
    from datetime import datetime, timedelta, timezone

    sent = {}
    monkeypatch.setattr(telegram, "send_message",
                        lambda text: sent.update(text=text) or {"ok": True})

    # Seed a deterministic anomaly: a sharp day-over-day drop the rules must catch.
    # Timestamps are relative to *now* so they fall inside the 7-day read window.
    store = server._store
    store.add_city("Testville")
    today = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    for day, temp in ((yesterday, 28.0), (today, 15.0)):  # −13°C day-over-day
        for hour in range(24):
            ts = day.replace(hour=hour)
            store.insert({"city": "Testville", "timestamp": ts.isoformat(),
                          "temperature": temp, "weather_condition": "clear sky",
                          "humidity": 50.0, "precipitation": 0.0})

    readings = server.get_weather_readings(period="7d", city="Testville")
    anomalies = server.detect_weather_anomalies(readings)
    anomaly_report = json.loads(anomalies)
    assert anomaly_report["anomaly_count"] >= 1

    alert = json.loads(server.send_telegram_alert(anomalies))
    assert alert["sent"] is True
    assert "Testville" in sent["text"]
