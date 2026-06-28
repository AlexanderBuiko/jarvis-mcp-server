"""
Offline tests for the weather-anomaly pipeline: the compact readings report,
the code-enforced input guard, and the deterministic detection rules.
"""

import json

import pytest

from weather_digest import aggregate
from weather_digest.aggregate import REPORT_TYPE, build_readings_report
from weather_digest.anomalies import (
    InvalidReport,
    detect_anomalies,
    validate_report,
)


def _rows(*specs):
    """Build rows from (timestamp, temp, condition, precip) tuples."""
    return [
        {"timestamp": ts, "temperature": t, "weather_condition": c,
         "precipitation": p, "humidity": 50.0}
        for ts, t, c, p in specs
    ]


def _report(daily, *, city="Tokyo", period="7d", sample_count=None):
    """A minimal valid report with hand-built daily buckets."""
    return {
        "report_type": REPORT_TYPE,
        "city": city,
        "period": period,
        "sample_count": sample_count if sample_count is not None else len(daily) * 24,
        "daily": daily,
    }


def _day(date, mean, lo, hi, rainy):
    return {"date": date, "mean_temp": mean, "min_temp": lo,
            "max_temp": hi, "rainy_fraction": rainy}


# ── build_readings_report ────────────────────────────────────────────────────

def test_build_readings_report_buckets_by_day():
    rows = _rows(
        ("2026-06-19T00:00:00+00:00", 10.0, "clear sky", 0.0),
        ("2026-06-19T12:00:00+00:00", 20.0, "rain", 2.0),
        ("2026-06-20T06:00:00+00:00", 30.0, "clear sky", 0.0),
    )
    report = build_readings_report(rows, "7d", "Tokyo")
    assert report["report_type"] == REPORT_TYPE
    assert report["sample_count"] == 3
    assert [d["date"] for d in report["daily"]] == ["2026-06-19", "2026-06-20"]
    day0 = report["daily"][0]
    assert day0["mean_temp"] == 15.0
    assert day0["min_temp"] == 10.0
    assert day0["max_temp"] == 20.0
    assert day0["rainy_fraction"] == 0.5  # one of two readings was rainy


def test_build_readings_report_empty():
    report = build_readings_report([], "7d", "Tokyo")
    assert report["sample_count"] == 0
    assert report["daily"] == []


def test_readings_report_at_most_seven_buckets():
    rows = _rows(*[
        (f"2026-06-{19 + i:02d}T00:00:00+00:00", 20.0, "clear sky", 0.0)
        for i in range(7)
    ])
    assert len(build_readings_report(rows, "7d", "Tokyo")["daily"]) == 7


# ── validate_report (the code-enforced "short report only" guard) ────────────

def test_validate_accepts_dict_and_json_string():
    report = _report([_day("2026-06-19", 20.0, 16.0, 24.0, 0.1)])
    assert validate_report(report)["city"] == "Tokyo"
    assert validate_report(json.dumps(report))["city"] == "Tokyo"


def test_validate_accepts_python_repr_string():
    # A model that serialized the report with str() (single quotes) still works.
    report = _report([_day("2026-06-19", 20.0, 16.0, 24.0, 0.1)])
    assert validate_report(str(report))["city"] == "Tokyo"


def test_validate_rejects_raw_array():
    raw = json.dumps([
        {"timestamp": "2026-06-19T00:00:00+00:00", "temperature": 20.0,
         "condition": "clear", "precipitation": 0.0},
    ])
    with pytest.raises(InvalidReport, match="raw readings"):
        validate_report(raw)


def test_validate_rejects_missing_marker():
    with pytest.raises(InvalidReport, match="report_type"):
        validate_report(json.dumps({"city": "Tokyo", "daily": []}))


def test_validate_rejects_too_many_days():
    daily = [_day(f"2026-06-{10 + i:02d}", 20.0, 16.0, 24.0, 0.1) for i in range(9)]
    with pytest.raises(InvalidReport, match="buckets"):
        validate_report(_report(daily))


def test_validate_rejects_oversized_payload():
    # A report whose serialized form exceeds the 4 KB cap (padded marker field).
    fat = _report([_day("2026-06-19", 20.0, 16.0, 24.0, 0.1)])
    fat["junk"] = "x" * 5000
    with pytest.raises(InvalidReport, match="too large"):
        validate_report(fat)


def test_validate_rejects_bad_json():
    with pytest.raises(InvalidReport, match="valid JSON"):
        validate_report("{not json")


# ── Detection rules ──────────────────────────────────────────────────────────

def test_detect_no_anomalies_on_calm_week():
    daily = [_day(f"2026-06-{19 + i:02d}", 20.0 + (i % 2) * 0.3, 18.0, 22.0, 0.1)
             for i in range(7)]
    result = detect_anomalies(_report(daily))
    assert result["anomaly_count"] == 0
    assert result["anomalies"] == []
    assert "No weather anomalies" in result["summary"]


def test_detect_rapid_temperature_drop():
    daily = [_day("2026-06-19", 25.0, 22.0, 28.0, 0.1),
             _day("2026-06-20", 16.0, 13.0, 19.0, 0.1)]  # −9°C day-over-day
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "rapid_temperature_drop" in types


def test_detect_rapid_temperature_rise():
    daily = [_day("2026-06-19", 12.0, 9.0, 15.0, 0.1),
             _day("2026-06-20", 22.0, 19.0, 25.0, 0.1)]  # +10°C
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "rapid_temperature_rise" in types


def test_detect_high_variability():
    daily = [_day("2026-06-19", 20.0, 5.0, 35.0, 0.1),
             _day("2026-06-20", 20.0, 18.0, 22.0, 0.1)]  # 30°C spread
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "high_temperature_variability" in types


def test_detect_high_rainfall_frequency():
    daily = [_day(f"2026-06-{19 + i:02d}", 20.0, 18.0, 22.0, 0.8) for i in range(4)]
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "high_rainfall_frequency" in types


def test_detect_unusually_dry():
    daily = [_day(f"2026-06-{19 + i:02d}", 20.0, 18.0, 22.0, 0.0) for i in range(4)]
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "unusually_dry" in types


def test_detect_prolonged_bad_weather():
    daily = [_day(f"2026-06-{19 + i:02d}", 20.0, 18.0, 22.0, 0.9) for i in range(3)]
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "prolonged_bad_weather" in types


def test_detect_warming_trend():
    daily = [_day("2026-06-19", 12.0, 10.0, 14.0, 0.1),
             _day("2026-06-20", 13.0, 11.0, 15.0, 0.1),
             _day("2026-06-21", 22.0, 20.0, 24.0, 0.1),
             _day("2026-06-22", 23.0, 21.0, 25.0, 0.1)]
    types = {a["type"] for a in detect_anomalies(_report(daily))["anomalies"]}
    assert "warming_trend" in types


def test_thresholds_override_suppresses_anomaly():
    daily = [_day("2026-06-19", 25.0, 22.0, 28.0, 0.1),
             _day("2026-06-20", 16.0, 13.0, 19.0, 0.1)]
    loose = {"rapid_temp_delta_c": 50.0, "high_variability_c": 99.0,
             "high_rainfall_fraction": 1.1, "dry_fraction": -1.0,
             "prolonged_bad_days": 99, "trend_delta_c": 99.0}
    assert detect_anomalies(_report(daily), thresholds=loose)["anomaly_count"] == 0
