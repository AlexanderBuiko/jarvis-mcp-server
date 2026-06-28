"""
Deterministic weather-anomaly detection over a compact daily report.

This is the second tool in the get_weather_readings → detect_weather_anomalies →
send_telegram_alert pipeline. It deliberately consumes **only** the small,
bounded report that ``get_weather_readings`` produces (``aggregate.build_readings_report``)
— never raw measurement rows. That boundary is enforced *in code* by
:func:`validate_report` (not merely requested in a prompt): a raw-readings array,
a payload without the report marker, an over-long ``daily`` list, or an oversized
string are all rejected before any analysis runs. This keeps the data relayed
between tools tiny and tamper-evident, so the LLM never has to echo a large array.

The rules themselves are pure functions over the ≤7 daily buckets, with
env-overridable thresholds, so they are trivially unit-testable with hand-built
reports. No DB, no MCP, no network here.
"""

from __future__ import annotations

import ast
import json
import os

from .aggregate import REPORT_TYPE

# Hard limits used by the input guard. A legitimate report is a few hundred bytes
# with at most 8 daily buckets — get_weather_readings caps the period at 7 days,
# and a 7-day window can straddle 8 UTC calendar dates (a partial first and last
# day). Anything larger is, by construction, not one of our reports.
MAX_DAILY_BUCKETS = 8
MAX_REPORT_BYTES = 4096

# A day whose rainy fraction is at least this counts as a "bad weather" day for the
# prolonged-bad-weather run. Distinct from the window-wide rainfall-frequency rule.
BAD_DAY_RAINY_FRACTION = 0.5


class InvalidReport(ValueError):
    """Raised when the input is not a valid get_weather_readings report."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def default_thresholds() -> dict:
    """Anomaly thresholds, each overridable via an ``ANOMALY_*`` env var."""
    return {
        # Day-over-day mean-temperature jump (deg C) that counts as "rapid".
        "rapid_temp_delta_c": _env_float("ANOMALY_RAPID_TEMP_DELTA_C", 6.0),
        # Window spread (max daily max − min daily min) that counts as high variability.
        "high_variability_c": _env_float("ANOMALY_HIGH_VARIABILITY_C", 18.0),
        # Mean daily rainy fraction above which rainfall is "unusually frequent".
        "high_rainfall_fraction": _env_float("ANOMALY_HIGH_RAINFALL_FRACTION", 0.40),
        # Mean daily rainy fraction below which the window is "unusually dry".
        "dry_fraction": _env_float("ANOMALY_DRY_FRACTION", 0.05),
        # Consecutive "bad" days that count as prolonged bad weather.
        "prolonged_bad_days": int(_env_float("ANOMALY_PROLONGED_BAD_DAYS", 3)),
        # First-half vs second-half mean-temperature shift that counts as a trend.
        "trend_delta_c": _env_float("ANOMALY_TREND_DELTA_C", 4.0),
    }


# ── Input guard (code-enforced "short report only") ──────────────────────────

def _parse_obj(raw: str):
    """Parse a JSON string, falling back to a Python-repr literal; None on failure.

    The fallback tolerates a model that serialized a dict with str() (single quotes)
    instead of JSON — a common slip when relaying a prior tool's output.
    """
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None


def validate_report(weather_report) -> dict:
    """Parse and validate a get_weather_readings report; raise on anything else.

    Accepts either the JSON string the model relays or an already-parsed dict.
    Rejects (in this order) raw-readings arrays, anything missing our report
    marker, reports with too many daily buckets, and oversized payloads — so a
    large raw array can never reach the detection rules.
    """
    if isinstance(weather_report, (dict, list)):
        # Already parsed; still enforce the size cap against its serialized form.
        raw = json.dumps(weather_report)
        parsed = weather_report
    else:
        raw = str(weather_report or "")
        parsed = _parse_obj(raw)
        if parsed is None:
            raise InvalidReport(
                "weather_report is not valid JSON. Pass the report returned by "
                "get_weather_readings."
            )

    if len(raw) > MAX_REPORT_BYTES:
        raise InvalidReport(
            f"weather_report is too large ({len(raw)} bytes > {MAX_REPORT_BYTES}). "
            "This tool consumes the compact report from get_weather_readings, "
            "not raw readings."
        )
    if isinstance(parsed, list):
        raise InvalidReport(
            "weather_report looks like raw readings (a JSON array). Pass the "
            "report object returned by get_weather_readings instead."
        )
    if not isinstance(parsed, dict):
        raise InvalidReport("weather_report must be a JSON object.")
    if parsed.get("report_type") != REPORT_TYPE:
        raise InvalidReport(
            f"weather_report is missing report_type {REPORT_TYPE!r}. Only the "
            "output of get_weather_readings is accepted."
        )
    daily = parsed.get("daily")
    if not isinstance(daily, list):
        raise InvalidReport("weather_report.daily must be a list of daily buckets.")
    if len(daily) > MAX_DAILY_BUCKETS:
        raise InvalidReport(
            f"weather_report.daily has {len(daily)} buckets (> {MAX_DAILY_BUCKETS}). "
            "get_weather_readings covers at most 7 days."
        )
    return parsed


# ── Detection rules (pure functions over the daily buckets) ──────────────────

def _anomaly(kind: str, severity: str, detail: str, value: float, threshold: float) -> dict:
    return {
        "type": kind,
        "severity": severity,
        "detail": detail,
        "value": round(value, 2),
        "threshold": threshold,
    }


def _temperature_jumps(daily: list[dict], thresholds: dict) -> list[dict]:
    """Largest day-over-day mean-temperature rise and drop, if either is rapid."""
    if len(daily) < 2:
        return []
    limit = thresholds["rapid_temp_delta_c"]
    deltas = [
        (daily[i]["mean_temp"] - daily[i - 1]["mean_temp"], daily[i - 1]["date"], daily[i]["date"])
        for i in range(1, len(daily))
    ]
    found: list[dict] = []
    rise = max(deltas, key=lambda d: d[0])
    if rise[0] > limit:
        found.append(_anomaly(
            "rapid_temperature_rise", "high",
            f"+{rise[0]:.1f}°C between {rise[1]} and {rise[2]}", rise[0], limit,
        ))
    drop = min(deltas, key=lambda d: d[0])
    if drop[0] < -limit:
        found.append(_anomaly(
            "rapid_temperature_drop", "high",
            f"{drop[0]:.1f}°C between {drop[1]} and {drop[2]}", drop[0], -limit,
        ))
    return found


def _variability(daily: list[dict], thresholds: dict) -> list[dict]:
    limit = thresholds["high_variability_c"]
    spread = max(d["max_temp"] for d in daily) - min(d["min_temp"] for d in daily)
    if spread > limit:
        return [_anomaly(
            "high_temperature_variability", "medium",
            f"{spread:.1f}°C spread across the window", spread, limit,
        )]
    return []


def _rainfall(daily: list[dict], thresholds: dict) -> list[dict]:
    mean_fraction = sum(d["rainy_fraction"] for d in daily) / len(daily)
    high = thresholds["high_rainfall_fraction"]
    dry = thresholds["dry_fraction"]
    if mean_fraction > high:
        return [_anomaly(
            "high_rainfall_frequency", "medium",
            f"{mean_fraction * 100:.0f}% of readings were rainy", mean_fraction, high,
        )]
    if len(daily) >= 3 and mean_fraction < dry:
        return [_anomaly(
            "unusually_dry", "low",
            f"only {mean_fraction * 100:.0f}% of readings were rainy", mean_fraction, dry,
        )]
    return []


def _prolonged_bad_weather(daily: list[dict], thresholds: dict) -> list[dict]:
    need = thresholds["prolonged_bad_days"]
    longest = run = 0
    for d in daily:
        run = run + 1 if d["rainy_fraction"] >= BAD_DAY_RAINY_FRACTION else 0
        longest = max(longest, run)
    if longest >= need:
        return [_anomaly(
            "prolonged_bad_weather", "medium",
            f"{longest} consecutive days of mostly-rainy weather", longest, need,
        )]
    return []


def _trend(daily: list[dict], thresholds: dict) -> list[dict]:
    if len(daily) < 2:
        return []
    limit = thresholds["trend_delta_c"]
    mid = len(daily) // 2
    first = [d["mean_temp"] for d in daily[:mid]]
    second = [d["mean_temp"] for d in daily[mid:]]
    delta = sum(second) / len(second) - sum(first) / len(first)
    if delta > limit:
        return [_anomaly("warming_trend", "low",
                         f"mean temperature rose {delta:.1f}°C over the window", delta, limit)]
    if delta < -limit:
        return [_anomaly("cooling_trend", "low",
                         f"mean temperature fell {delta:.1f}°C over the window", delta, -limit)]
    return []


def detect_anomalies(report: dict, *, thresholds: dict | None = None) -> dict:
    """Apply the deterministic rules to a validated report; return an anomaly report.

    ``report`` must already have passed :func:`validate_report`. Returns a small,
    relay-friendly dict listing each anomaly found (possibly none).
    """
    thresholds = thresholds or default_thresholds()
    daily = report["daily"]

    anomalies: list[dict] = []
    if daily:
        anomalies += _temperature_jumps(daily, thresholds)
        anomalies += _variability(daily, thresholds)
        anomalies += _rainfall(daily, thresholds)
        anomalies += _prolonged_bad_weather(daily, thresholds)
        anomalies += _trend(daily, thresholds)

    city = report.get("city", "unknown")
    period = report.get("period", "")
    count = len(anomalies)
    if count:
        summary = f"{count} weather {'anomaly' if count == 1 else 'anomalies'} " \
                  f"detected for {city} over {period}."
    else:
        summary = f"No weather anomalies detected for {city} over {period}."
    return {
        "city": city,
        "period": period,
        "sample_count": report.get("sample_count", 0),
        "days_analyzed": len(daily),
        "anomaly_count": count,
        "anomalies": anomalies,
        "summary": summary,
    }
