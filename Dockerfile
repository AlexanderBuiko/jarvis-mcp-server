# Production image for the MCP time server.
# Slim base, non-root user, dependency-layer caching, listens on $PORT (8080).

FROM python:3.12-slim

# Fail fast, no .pyc clutter, unbuffered logs (so Cloud Logging sees them live).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies FIRST, as their own layer. This layer is cached and only
# rebuilt when requirements.txt changes — code edits don't re-run pip.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy the application code (changes often → kept after the deps layer).
COPY time_server ./time_server
COPY weather_digest ./weather_digest

# The hourly weather scheduler writes its SQLite DB here. The container FS is
# ephemeral (the DB resets on each new revision and is re-seeded on startup),
# which is fine for this digest demo.
ENV WEATHER_DB_PATH=/tmp/weather.db

# Run as a non-root user (defence in depth; Cloud Run also enforces this).
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Cloud Run sends traffic to $PORT (default 8080) and bind address must be 0.0.0.0.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    TRANSPORT=streamable-http \
    LOG_LEVEL=INFO
EXPOSE 8080

# server.main() reads HOST/PORT/TRANSPORT from the env and serves via uvicorn.
CMD ["python", "-m", "time_server.server"]
