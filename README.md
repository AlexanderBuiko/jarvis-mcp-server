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
| `echo` | `text: str` | The input unchanged (connectivity smoke-test) |

`get_current_time` is a two-hop pipeline: **Open-Meteo geocoding** (city → lat/lon)
→ **TimeAPI.io** (lat/lon → local time). Both APIs are free and need no key. If the
network is unavailable it falls back to the system UTC clock, so a call never
hard-fails.

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

- [x] Standalone Streamable HTTP server + two tools
- [x] Local test via MCP Inspector
- [x] JarvisCLI client wired (auth-aware, degrades cleanly when down/unauthorized)
- [x] API-key auth middleware (pure-ASGI, constant-time, `/healthz` exempt)
- [x] Dockerfile (slim, non-root, port 8080)
- [x] Cloud Run deploy guide
