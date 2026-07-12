#!/usr/bin/env bash
#
# Manually-started private LLM chat service: the MCP chat proxy in front of a
# local Ollama daemon. Runs in the foreground — Ctrl+C to stop (manual persistence).
#
# It handles the fiddly setup so a start is one command:
#   • generates + persists a strong API key on first run (outside the repo),
#   • checks Ollama is up and creates the low-memory model if missing,
#   • starts the proxy with hardened, env-overridable settings,
#   • prints the local + tailnet URLs to reach it.
#
# Usage:   source .venv/bin/activate && scripts/run_llm_service.sh
# Client:  set JARVIS_OLLAMA_URL to a printed URL and JARVIS_OLLAMA_API_KEY to the key.
#
# Overridable env: PORT HOST LLM_MODEL OLLAMA_URL LLM_RATE_LIMIT_RPM
#                  LLM_MAX_CONTEXT_TOKENS LLM_SERVICE_KEY_FILE
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"          # 0.0.0.0 = reachable on the tailnet (and LAN); the
                                 # API key gates access. Set HOST=<tailnet-ip> for
                                 # tailnet-only binding.
MODEL="${LLM_MODEL:-qwen-android-mini}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
RPM="${LLM_RATE_LIMIT_RPM:-30}"
MAXCTX="${LLM_MAX_CONTEXT_TOKENS:-8192}"
KEY_FILE="${LLM_SERVICE_KEY_FILE:-$HOME/.jarvis/llm_service.key}"
MODELFILE="$REPO_ROOT/models/qwen-android-mini.Modelfile"

# 1. API key — generate once and persist (chmod 600, never committed).
mkdir -p "$(dirname "$KEY_FILE")"
if [ ! -s "$KEY_FILE" ]; then
  python3 -c "import secrets; print(secrets.token_hex(24))" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "→ generated a new API key at $KEY_FILE"
fi
API_KEY="$(cat "$KEY_FILE")"

# 2. Ollama must be running (the proxy forwards to it).
if ! curl -sf "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  echo "✗ Ollama not reachable at $OLLAMA_URL — start it first:  ollama serve" >&2
  exit 1
fi

# 3. Ensure the low-memory optimised model exists.
if ! ollama list | grep -q "$MODEL"; then
  echo "→ model '$MODEL' missing; creating it from $MODELFILE"
  ollama create "$MODEL" -f "$MODELFILE"
fi

# 4. Report reachable URLs (tailnet IP if Tailscale is up).
TSIP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
echo "Starting private LLM service  (model=$MODEL  rpm=$RPM  max_ctx=$MAXCTX)"
echo "  local:    http://localhost:$PORT"
[ -n "$TSIP" ] && echo "  tailnet:  http://$TSIP:$PORT"
echo "  auth:     X-API-Key from $KEY_FILE"
echo "  (Ctrl+C to stop)"

# 5. Run the proxy in the foreground.
exec env \
  MCP_API_KEY="$API_KEY" \
  OLLAMA_URL="$OLLAMA_URL" \
  LLM_MODEL="$MODEL" \
  LLM_RATE_LIMIT_RPM="$RPM" \
  LLM_MAX_CONTEXT_TOKENS="$MAXCTX" \
  HOST="$HOST" PORT="$PORT" TRANSPORT=streamable-http \
  "${PYTHON:-python}" -m time_server.server
