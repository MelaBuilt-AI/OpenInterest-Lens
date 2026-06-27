# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install server dependencies first (layer cache)
COPY server/pyproject.toml server/
RUN pip install --no-cache-dir --prefix=/install "./server[dev]"

# Copy server source and install
COPY server/ server/
RUN pip install --no-cache-dir --prefix=/install "./server"

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="OpenInterest Lens"
LABEL org.opencontainers.image.description="Real-time futures market structure API"
LABEL org.opencontainers.image.source="https://github.com/openinterest-lens/openinterest-lens"

# Security: non-root user
RUN groupadd --gid 1000 oil && \
    useradd --uid 1000 --gid oil --shell /bin/bash --create-home oil

# Minimal runtime deps (libpq for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Create app directory and set ownership
WORKDIR /app
RUN chown oil:oil /app

# Copy application code
COPY --chown=oil:oil server/ /app/

# Environment defaults (override at runtime)
ENV OIL_DATABASE_URL="sqlite+aiosqlite:///./openinterest_lens.db" \
    OIL_REDIS_URL="redis://redis:6379/0" \
    OIL_MASTER_API_KEY="" \
    OIL_DEBUG="false" \
    OIL_LOG_LEVEL="info" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/v1/health'); raise SystemExit(0) if r.status_code==200 else SystemExit(1)" || exit 1

USER oil

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]