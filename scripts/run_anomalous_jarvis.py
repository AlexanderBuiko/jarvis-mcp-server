"""
Run a LOCAL jarvis MCP server pre-seeded with a guaranteed weather anomaly, so the
full pipeline (readings → anomalies → news → wiki → time → Telegram) actually
fires during a demo — without redeploying the remote server or tweaking thresholds.

It seeds Tokyo with a sharp multi-day temperature drop + a rainy stretch, which
trips the default rules (rapid_temperature_drop, cooling_trend, high_rainfall),
then starts the normal server over streamable-http.

Usage:
    cd jarvis-mcp-server
    TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… python3 scripts/run_anomalous_jarvis.py

Then, in another terminal, point the CLI at it (overrides servers.json's jarvis url):
    cd jarvis-cli
    JARVIS_TIME_MCP_URL=http://localhost:8080/mcp jarvis

and ask:
    "Analyze weather conditions in Tokyo. If anomalies are detected, gather recent
     weather-related news near Tokyo, collect contextual information about the city,
     determine the current local time, and send me a Telegram summary."
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# Use a dedicated DB so we don't disturb the real one, and freeze live collection
# so the seeded anomaly isn't diluted by calm live readings.
DB = os.environ.setdefault("WEATHER_DB_PATH", "/tmp/jarvis_anomalous.db")
os.environ.setdefault("WEATHER_COLLECT_INTERVAL_S", "999999")
os.environ.setdefault("PORT", "8080")
os.environ.setdefault("TRANSPORT", "streamable-http")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from weather_digest.storage import WeatherStore


def seed_anomaly(city: str = "Tokyo") -> None:
    """Fill the DB with a sharp cool-down so the anomaly rules fire for `city`."""
    if os.path.exists(DB):
        os.remove(DB)
    store = WeatherStore(db_path=DB)
    store.add_city(city)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # 4 days: warm & dry → sharp drop to cold & rainy.
    profile = [(29.0, "clear sky", 0.0), (28.0, "clear sky", 0.0),
               (14.0, "rain", 4.0), (13.0, "rain", 3.0)]
    for d, (temp, cond, precip) in enumerate(profile):
        day = now - timedelta(days=3 - d)
        for h in range(24):
            store.insert({"city": city, "timestamp": day.replace(hour=h).isoformat(),
                          "temperature": temp, "weather_condition": cond,
                          "humidity": 70.0, "precipitation": precip})
    print(f"Seeded anomalous {city} data into {DB} "
          f"(sharp drop {profile[1][0]}°C → {profile[2][0]}°C + rain).")


def main() -> None:
    seed_anomaly("Tokyo")
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — the alert step will "
              "report 'not configured' (the rest of the flow still runs).")
    # Import after env is set so the server reads our PORT/DB/etc.
    from time_server.server import main as serve
    print(f"Starting local jarvis server on http://localhost:{os.environ['PORT']}/mcp …")
    serve()


if __name__ == "__main__":
    main()
