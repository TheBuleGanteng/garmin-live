# garmin-live — plain-HTTP fetch strategy (decision D1), no browser in the image.
# Build context: the repository root.  docker build -f docker/Dockerfile .
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user with a fixed uid so the /data volume permissions are predictable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown -R app:app /app /data

WORKDIR /app

# Install the pinned runtime dependencies first (better layer caching), then the package.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY fixtures/sessions ./fixtures/sessions
RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache

USER app
ENV DB_PATH=/data/garmin.db
VOLUME ["/data"]
EXPOSE 8000

# BASE_PATH is set at runtime; the healthcheck reads it so it works for both "" and "/garmin-mcp".
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; b=os.environ.get('BASE_PATH',''); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:8000{b}/health', timeout=4).status==200 else 1)"

# Exactly one worker: the poller runs in-process.
CMD ["python", "-m", "garmin_live.app"]
