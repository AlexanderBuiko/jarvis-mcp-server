#!/usr/bin/env python3
"""
End-to-end: start the local MCP server, drive the weather-anomaly pipeline over
the real network transport, and deliver a Telegram alert.

    get_weather_readings → detect_weather_anomalies → send_telegram_alert

Credentials are read from the environment (never hard-coded):

    export TELEGRAM_BOT_TOKEN='123:ABC...'
    export TELEGRAM_CHAT_ID='123456789'
    python3 scripts/send_alert_e2e.py

It seeds a city with a sharp cool-down so anomalies fire deterministically, so a
real message is sent regardless of live weather. Exit code 0 = alert delivered.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

# This repo (server code) and the CLI repo (MCPRegistry client).
SRV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = "/Users/alexanderbuyko/PycharmProjects/jarvis-cli"
sys.path.insert(0, SRV)
sys.path.insert(0, CLI)

PORT = os.environ.get("DEMO_PORT", "8131")
URL = f"http://127.0.0.1:{PORT}/mcp"
DB = f"/tmp/jarvis_alert_demo_{PORT}.db"


def require_creds() -> None:
    missing = [v for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.environ.get(v, "").strip()]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} first — see the script header.")


def start_server() -> subprocess.Popen:
    if os.path.exists(DB):
        os.remove(DB)
    env = {
        **os.environ,  # carries TELEGRAM_* through to the server process
        "PORT": PORT,
        "TRANSPORT": "streamable-http",
        "WEATHER_DB_PATH": DB,
        "WEATHER_COLLECT_INTERVAL_S": "999999",  # no live collection noise
        "LOG_LEVEL": "WARNING",
    }
    proc = subprocess.Popen([sys.executable, "-m", "time_server.server"], cwd=SRV, env=env)
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=1).status_code == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.2)
    proc.terminate()
    sys.exit("server did not become healthy")


def seed_anomalous_city(name: str = "Demoville") -> None:
    """Inject 4 days of data with a sharp drop + wet stretch so anomalies fire."""
    from weather_digest.storage import WeatherStore

    store = WeatherStore(db_path=DB)
    store.add_city(name)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    profile = [(29.0, "clear sky", 0.0), (28.0, "clear sky", 0.0),
               (14.0, "rain", 4.0), (13.0, "rain", 3.0)]
    for d, (temp, cond, precip) in enumerate(profile):
        day = now - timedelta(days=3 - d)
        for h in range(24):
            store.insert({"city": name, "timestamp": day.replace(hour=h).isoformat(),
                          "temperature": temp, "weather_condition": cond,
                          "humidity": 70.0, "precipitation": precip})


async def run_pipeline(city: str = "Demoville") -> dict:
    from jarvis.mcp.config import STREAMABLE_HTTP, MCPServerConfig
    from jarvis.mcp.registry import MCPRegistry

    cfg = [MCPServerConfig(name="jarvis", transport=STREAMABLE_HTTP, url=URL)]
    async with MCPRegistry(cfg) as reg:
        readings = (await reg.call_tool("jarvis.get_weather_readings",
                                        {"city": city, "period": "7d"})).content[0].text
        print("\n[A] get_weather_readings →\n", readings)

        anomalies = (await reg.call_tool("jarvis.detect_weather_anomalies",
                                         {"weather_report": readings})).content[0].text
        print("\n[B] detect_weather_anomalies →\n", anomalies)

        result = (await reg.call_tool("jarvis.send_telegram_alert",
                                      {"anomaly_report": anomalies})).content[0].text
        print("\n[C] send_telegram_alert →\n", result)
        return json.loads(result)


def main() -> int:
    require_creds()
    proc = start_server()
    try:
        seed_anomalous_city()
        result = asyncio.run(run_pipeline())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if os.path.exists(DB):
            os.remove(DB)

    if result.get("sent"):
        print("\n✅ Alert delivered — check your Telegram.")
        return 0
    print(f"\n❌ Not sent: {result.get('reason')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
