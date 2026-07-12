# Private LLM service — runbook

A private AI service over a **local** LLM: the MCP server's chat proxy in front of
an Ollama daemon, reachable from your own devices over a Tailscale private network.
Manually started, authenticated, rate-limited. The knowledge base is **not** part of
this — it serves the model only.

- **Host:** your laptop (Ollama + the proxy run together).
- **Exposure:** Tailscale private mesh (no public exposure).
- **Persistence:** manual (foreground process; Ctrl+C to stop).

---

## 1. Prerequisites (on the host laptop)

```bash
# Ollama daemon + the low-memory model (the launcher creates the model if missing)
ollama serve &                       # or: brew services start ollama

cd ~/PycharmProjects/jarvis-mcp-server
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

## 2. Start the service (one command)

```bash
source .venv/bin/activate
scripts/run_llm_service.sh
```

On first run it **generates and stores an API key** at `~/.jarvis/llm_service.key`
(chmod 600, never committed), ensures the `qwen-android-mini` model exists, and
starts the proxy. It prints the reachable URLs and where the key lives:

```
Starting private LLM service  (model=qwen-android-mini  rpm=30  max_ctx=8192)
  local:    http://localhost:8080
  tailnet:  http://100.x.y.z:8080          # shown once Tailscale is up (step 4)
  auth:     X-API-Key from ~/.jarvis/llm_service.key
```

Tunables (env): `PORT HOST LLM_MODEL LLM_RATE_LIMIT_RPM LLM_MAX_CONTEXT_TOKENS`.

## 3. Verify locally (same machine)

```bash
KEY=$(cat ~/.jarvis/llm_service.key)
curl -s http://localhost:8080/healthz                                   # {"status":"ok"}
curl -s -X POST http://localhost:8080/v1/chat/completions -H "X-API-Key: $KEY" \
  -d '{"messages":[{"role":"user","content":"say ok"}],"max_tokens":10}'
```

## 4. Put it on your private network (Tailscale)

On the **host laptop**:
```bash
brew install tailscale && sudo tailscale up        # or the Tailscale app
tailscale ip -4                                    # note the 100.x.y.z address
```
On the **other device** (phone / second laptop): install Tailscale, log in with the
**same account**. Both are now on one private mesh; nothing is exposed publicly.

Restart the service so it prints the tailnet URL (it binds `0.0.0.0`, so the tailnet
IP already works). For tailnet-only binding, start it with `HOST=$(tailscale ip -4)`.

## 5. Verify network access from another device (the mentor's checks)

Replace `100.x.y.z` with the host's tailnet IP; run these from the **other device**.

**Network access + chat:**
```bash
curl -s http://100.x.y.z:8080/healthz
curl -s -X POST http://100.x.y.z:8080/v1/chat/completions -H "X-API-Key: <KEY>" \
  -d '{"messages":[{"role":"user","content":"name one Kotlin coroutine builder"}],"max_tokens":20}'
```

**From the jarvis-cli client** (on the other device):
```bash
export JARVIS_LLM_PROVIDER=ollama
export JARVIS_OLLAMA_URL=http://100.x.y.z:8080
export JARVIS_OLLAMA_API_KEY=<KEY>
python -m jarvis        # then: config set model qwen-android-mini ; chat
```

**Stability under load** (all should return `200` with valid JSON):
```bash
echo '{"model":"qwen-android-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":20}' > /tmp/r.json
seq 1 8 | xargs -P 8 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://100.x.y.z:8080/v1/chat/completions -H "X-API-Key: <KEY>" --data @/tmp/r.json
```

**Basic limits:**
```bash
# rate limit → 429 once over LLM_RATE_LIMIT_RPM in a 60s window
# max context → 413 for an oversized prompt
python3 -c "import json;print(json.dumps({'messages':[{'role':'user','content':'x'*60000}]}))" \
  | curl -s -o /dev/null -w "%{http_code}\n" -X POST http://100.x.y.z:8080/v1/chat/completions \
      -H "X-API-Key: <KEY>" --data @-       # 413
```

**Auth:** the same requests **without** `X-API-Key` return `401`.

## 6. Stop

`Ctrl+C` in the terminal running the launcher. (Manual persistence — it does not
auto-restart.)

---

## Security notes

- The API key is the only credential; keep `~/.jarvis/llm_service.key` private.
  Rotate by deleting the file and restarting (a new key is generated) — update the
  client's `JARVIS_OLLAMA_API_KEY` to match.
- Tailscale keeps the service off the public internet. Tailscale ACLs can further
  restrict which devices may reach the host.
- The rate limiter is in-memory and **per process** — correct for this single
  instance. A multi-instance deployment would need a shared store (Redis).
- No TLS is configured because Tailscale encrypts the transport. If you ever expose
  this publicly (e.g. Cloudflare Tunnel), terminate TLS there and keep the API key.
