# Deploying the MCP time server to Cloud Run

The server is a container that listens on `$PORT` (8080) over Streamable HTTP and
enforces its own `X-API-Key` auth. Cloud Run runs the container, autoscales it,
and terminates TLS — you get an `https://…run.app` URL the JarvisCLI client dials.

## 0. Prerequisites (once)

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
                       artifactregistry.googleapis.com secretmanager.googleapis.com
export REGION=europe-west1          # pick one near you
export SERVICE=jarvis-mcp-server
```

## 1. Create the API key as a secret (never an env literal)

```bash
# Generate a strong key and store it in Secret Manager.
python -c "import secrets; print(secrets.token_urlsafe(32))" | \
  gcloud secrets create mcp-api-key --data-file=-

# Let the Cloud Run runtime service account read it.
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding mcp-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## 2. Build + deploy from source

`--source .` makes Cloud Build build the Dockerfile and push the image for you.

```bash
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --port 8080 \
  --allow-unauthenticated \
  --set-env-vars TRANSPORT=streamable-http,LOG_LEVEL=INFO \
  --set-secrets MCP_API_KEY=mcp-api-key:latest \
  --min-instances 1 \
  --max-instances 5 \
  --timeout 3600 \
  --cpu 1 --memory 256Mi
```

What the flags do, and why:

| Flag | Why |
|---|---|
| `--allow-unauthenticated` | Lets requests *reach* the container. This is **not** "no auth" — our `X-API-Key` middleware still guards every request. It just means we use **app-level** auth (the CLI sends an API key) instead of **Cloud Run IAM** auth (which would need GCP identity tokens the CLI doesn't have). |
| `--set-secrets MCP_API_KEY=…` | Injects the key from Secret Manager as an env var at runtime. It never appears in the image, the build logs, or `gcloud` history. |
| `--min-instances 1` | Keeps one instance warm so a Streamable-HTTP/SSE stream isn't dropped by a cold start. Set to `0` to scale to zero (cheaper, but first request pays a cold start). |
| `--timeout 3600` | Streamable HTTP / SSE hold a long-lived request open; the default 300s would cut streams. |
| `--max-instances 5` | Caps cost/blast radius. |

## 3. Verify the deployment

```bash
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "$URL"

# Health is unauthenticated:
curl -s -w '\n[%{http_code}]\n' "$URL/healthz"            # → {"status":"ok"} [200]

# /mcp without a key is rejected:
curl -s -w '\n[%{http_code}]\n' -X POST "$URL/mcp" \
     -H 'content-type: application/json' -d '{}'           # → 401
```

## 4. Point JarvisCLI at it

Read the deployed key once, then put both values in `~/.jarvis/.env`:

```bash
KEY=$(gcloud secrets versions access latest --secret=mcp-api-key)
cat >> ~/.jarvis/.env <<EOF
JARVIS_TIME_MCP_URL=${URL}/mcp
MCP_API_KEY=${KEY}
EOF
```

Now `jarvis` (or `python -m jarvis.mcp list`) connects to the cloud server with no
inline vars. Wrong/absent key → the CLI degrades cleanly to weather-only with
`✗ time: unauthorized`.

## How scaling & cold starts work (briefly)

- **Scaling:** Cloud Run routes requests to container instances and adds instances
  as concurrent load rises (up to `--max-instances`), removing them when idle.
  Each Streamable-HTTP connection is one in-flight request.
- **Cold start:** when scaled to zero, the first request waits for a container to
  start (image pull + `python -m time_server.server` boot — typically a couple of
  seconds for this slim image). `--min-instances 1` keeps one always warm so
  interactive use and long-lived streams never pay that penalty.

## Updating

Re-run the `gcloud run deploy --source .` command; it builds a new revision and
shifts traffic to it. Rotate the key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" | \
  gcloud secrets versions add mcp-api-key --data-file=-
gcloud run services update "$SERVICE" --region "$REGION" \
  --set-secrets MCP_API_KEY=mcp-api-key:latest   # picks up the new version
```
