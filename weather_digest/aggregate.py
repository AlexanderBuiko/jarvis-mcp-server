"""
Aggregation for the weather digest.

Pure functions over a list of measurement dicts (as returned by
``WeatherStore.measurements_since``): period parsing, then the summary the
``get_weather_digest`` tool returns. No DB, no MCP, no network — trivially
unit-testable with hand-built rows.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

# Conditions that count as rainfall for the "rainfall occurrence" metric, in
# addition to any reading whose precipitation value is > 0.
_RAIN_WORDS = ("rain", "drizzle", "shower", "thunderstorm")

# Trend deadband: average-temperature changes smaller than this (deg C) between
# the window's first and second half are reported as "stable".
_TREND_DEADBAND_C = 0.5

# A count followed by a unit. The unit is forgiving — short or spelled-out,
# singular or plural — because the value usually comes straight from an LLM that
# may emit "24h", "7 days" or "1 week". Hours in each unit:
_UNIT_HOURS = {
    "h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1,
    "d": 24, "day": 24, "days": 24,
    "w": 168, "wk": 168, "wks": 168, "week": 168, "weeks": 168,
}
_PERIOD_RE = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$", re.IGNORECASE)


# Marker stamped on the get_weather_readings report. detect_weather_anomalies
# requires it (see anomalies.validate_report), so only this output is accepted as
# that tool's input — the code-enforced boundary that keeps relayed data tiny.
REPORT_TYPE = "weather_readings.v1"

# get_weather_readings covers at most this many days, so the per-day report has at
# most 7 buckets — small enough for the model to relay verbatim without truncation.
MAX_READINGS_HOURS = 7 * 24


class InvalidPeriod(ValueError):
    """Raised when a period string can't be parsed (e.g. not ``24h`` / ``7d``)."""


def parse_period(period: str) -> timedelta:
    """Parse a period like ``"24h"``, ``"7d"`` or ``"1w"`` into a ``timedelta``.

    Accepts a positive integer count followed by a unit: hours (``h``/``hour``/
    ``hours``), days (``d``/``day``/``days``) or weeks (``w``/``week``/``weeks``),
    with optional spaces — so ``"24h"``, ``"7 days"`` and ``"1 week"`` all work.
    """
    match = _PERIOD_RE.match(period or "")
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        if amount > 0 and unit in _UNIT_HOURS:
            return timedelta(hours=amount * _UNIT_HOURS[unit])
    raise InvalidPeriod(
        f"Invalid period {period!r}. Use a positive number followed by a unit, "
        f"e.g. '24h', '7d' or '1w'."
    )


def cutoff_for(period: str, now: datetime | None = None) -> str:
    """Return the ISO-8601 UTC cutoff timestamp for a period window."""
    now = now or datetime.now(timezone.utc)
    return (now - parse_period(period)).replace(microsecond=0).isoformat()


def _is_rainy(row: dict) -> bool:
    precip = row.get("precipitation") or 0.0
    if precip and precip > 0:
        return True
    condition = (row.get("weather_condition") or "").lower()
    return any(word in condition for word in _RAIN_WORDS)


def _temperature_trend(rows: list[dict]) -> str:
    """Compare the mean temperature of the window's first vs second half."""
    if len(rows) < 2:
        return "insufficient data"
    mid = len(rows) // 2
    first = [r["temperature"] for r in rows[:mid]]
    second = [r["temperature"] for r in rows[mid:]]
    delta = (sum(second) / len(second)) - (sum(first) / len(first))
    if delta > _TREND_DEADBAND_C:
        return "rising"
    if delta < -_TREND_DEADBAND_C:
        return "falling"
    return "stable"


def build_readings_report(rows: list[dict], period: str, city: str = "Tokyo") -> dict:
    """Roll raw rows up into the compact, bounded report get_weather_readings returns.

    Groups the (time-ordered) measurements by UTC calendar day and summarises each
    day as mean/min/max temperature and the fraction of readings that were rainy.
    The result carries the ``report_type`` marker and at most 7 daily buckets, so
    detect_weather_anomalies can consume it without ever seeing raw rows.
    """
    base = {
        "report_type": REPORT_TYPE,
        "city": city,
        "period": period,
        "sample_count": len(rows),
    }
    if not rows:
        return {**base, "daily": [], "note": "No measurements in this window yet."}

    # Preserve day order via a dict keyed on the UTC date (rows arrive oldest-first).
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row["timestamp"])[:10]
        by_day.setdefault(day, []).append(row)

    daily = []
    for day, day_rows in by_day.items():
        temps = [r["temperature"] for r in day_rows]
        rainy = sum(1 for r in day_rows if _is_rainy(r))
        daily.append({
            "date": day,
            "mean_temp": round(sum(temps) / len(temps), 1),
            "min_temp": round(min(temps), 1),
            "max_temp": round(max(temps), 1),
            "rainy_fraction": round(rainy / len(day_rows), 2),
        })

    return {
        **base,
        "window": {"start": rows[0]["timestamp"], "end": rows[-1]["timestamp"]},
        "daily": daily,
    }


def build_digest(rows: list[dict], period: str, city: str = "Tokyo") -> dict:
    """Aggregate measurement rows (assumed time-ordered) into a digest dict."""
    if not rows:
        return {
            "city": city,
            "period": period,
            "sample_count": 0,
            "note": "No measurements in this window yet.",
        }

    temps = [r["temperature"] for r in rows]
    conditions = [r["weather_condition"] for r in rows if r.get("weather_condition")]
    most_common = Counter(conditions).most_common(1)[0][0] if conditions else None
    rainfall_count = sum(1 for r in rows if _is_rainy(r))

    return {
        "city": city,
        "period": period,
        "sample_count": len(rows),
        "average_temperature": round(sum(temps) / len(temps), 1),
        "min_temperature": round(min(temps), 1),
        "max_temperature": round(max(temps), 1),
        "most_common_condition": most_common,
        "rainfall_occurrences": rainfall_count,
        "temperature_trend": _temperature_trend(rows),
        "window_start": rows[0]["timestamp"],
        "window_end": rows[-1]["timestamp"],
    }
