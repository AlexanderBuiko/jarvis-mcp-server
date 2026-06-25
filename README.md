# jarvis-mcp-server

Standalone MCP server(s) for Jarvis. It currently hosts a **time** server (in the
`time_server/` package) that reports the current local time for a city, and is
structured to grow more tool domains over time — each added as its own package
alongside `time_server/`.

Unlike a stdio MCP server (which a client launches as a subprocess), it runs as an
**independent network process** over **Streamable HTTP**, so it has its own
lifecycle and can be reached by the MCP Inspector, a CLI client, or a Cloud Run
deployment.

## Tools

| Tool | Args | Returns |
|---|---|---|
| `get_current_time` | `city: str` | Current local time + timezone for the city |
| `get_weather_digest` | `period: str` (e.g. `24h`, `7d`), `city: str` (default Tokyo) | Aggregated weather digest for a city (JSON) |
| `add_city` | `city: str` | Start collecting weather for a city; returns the tracked-city list |
| `remove_city` | `city: str` | Stop collecting a city (its history is kept); returns the tracked-city list |
| `list_cities` | — | The cities currently being collected |
| `echo` | `text: str` | The input unchanged (connectivity smoke-test) |

`get_current_time` is a two-hop pipeline: **Open-Meteo geocoding** (city → lat/lon)
→ **TimeAPI.io** (lat/lon → local time). Both APIs are free and need no key. If the
network is unavailable it falls back to the system UTC clock, so a call never
hard-fails.

### Tokyo weather digest (a scheduled agent)

The [`weather_digest/`](weather_digest) package runs a **continuous agent**: while
the server is up, a background thread collects the current weather **once an hour**
for **every tracked city** (Open-Meteo, with a mock fallback when offline) and
stores each reading in **SQLite** (`weather_measurements` table). The scheduler
starts with the server (via the ASGI lifespan) and stops cleanly on shutdown — it
runs independently of any tool call.

**Tracked cities are managed at runtime.** Tokyo is tracked by default; `add_city`
/ `remove_city` / `list_cities` change the set live (no restart). `add_city` does
one immediate collection so a new city has data right away; `remove_city` only
stops future collection — the city's stored history is preserved (re-adding
resumes with it intact). The set is persisted in a `tracked_cities` table, so it
survives restarts.

`get_weather_digest(period, city)` aggregates the stored readings over a window
(`city` defaults to Tokyo):

```json
{
  "city": "Tokyo", "period": "24h", "sample_count": 36,
  "average_temperature": 19.0, "min_temperature": 12.7, "max_temperature": 24.1,
  "most_common_condition": "overcast", "rainfall_occurrences": 7,
  "temperature_trend": "rising",
  "window_start": "...", "window_end": "..."
}
```

On a **fresh/empty database** the store auto-seeds **7 days of realistic hourly
mock readings** (multiple conditions, rainfall, diurnal temperature swing), so a
digest is demoable immediately — before the hourly scheduler has collected
anything live.

| Env var | Default | Purpose |
|---|---|---|
| `WEATHER_DB_PATH` | `weather_digest/weather.db` | SQLite file location |
| `WEATHER_CITY` | `Tokyo` | Default city (always tracked + seeded); add more via `add_city` |
| `WEATHER_COLLECT_INTERVAL_S` | `3600` | Seconds between collections (lower for demos) |

> **Cloud Run note:** with `--min-instances 0` the service scales to zero when
> idle, so the hourly collection only runs while an instance is alive. The seed
> data keeps the digest meaningful regardless; set `--min-instances 1` for
> uninterrupted hourly collection.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m time_server.server          # serves http://0.0.0.0:8080/mcp
```

### Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8080` | Bind port (Cloud Run injects this) |
| `TRANSPORT` | `streamable-http` | `streamable-http` \| `sse` \| `stdio` |
| `MCP_API_KEY` | _(unset)_ | If set, every request (except `/healthz`) must send a matching `X-API-Key`. Unset → **open** server (local/Inspector only). |
| `LOG_LEVEL` | `INFO` | Server log verbosity |

### Security

- **Auth:** set `MCP_API_KEY`; clients must send it as the `X-API-Key` header.
  Comparison is constant-time and the key is never logged. Leaving it unset logs a
  loud warning and runs the server open — fine for local Inspector testing, never
  for a public deployment.
- **Health:** `GET /healthz` is unauthenticated (for Cloud Run / curl probes).

## Test with the MCP Inspector

In a second terminal (server still running):

```bash
npx @modelcontextprotocol/inspector
```

In the Inspector UI:
1. **Transport Type:** `Streamable HTTP`
2. **URL:** `http://localhost:8080/mcp`
3. **Connect** → open the **Tools** tab → run `get_current_time` with `city = London`.

## Automated checks

```bash
python tests/smoke_http.py      # end-to-end over Streamable HTTP (server must be up)
pytest -q                       # offline unit tests (no network, no server)
```

## Deployment

See [DEPLOY.md](DEPLOY.md) for the Cloud Run build/deploy commands, secret setup,
and how scaling / cold starts work.

## Roadmap

- [x] Standalone Streamable HTTP server + tools (`get_current_time`, `get_weather_digest`)
- [x] Scheduled weather-digest agent (hourly collection → SQLite → aggregation)
- [x] Runtime-managed multi-city collection (`add_city` / `remove_city` / `list_cities`)
- [x] Local test via MCP Inspector
- [x] JarvisCLI client wired (auth-aware, degrades cleanly when down/unauthorized)
- [x] API-key auth middleware (pure-ASGI, constant-time, `/healthz` exempt)
- [x] Dockerfile (slim, non-root, port 8080)
- [x] Cloud Run deploy guide
