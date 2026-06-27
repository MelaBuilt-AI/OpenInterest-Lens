# Deployment Guide — OpenInterest Lens

Production deployment guide for the OpenInterest Lens API server.

## Quick Deploy (Docker)

```bash
# Build and run
docker build -t openinterest-lens .
docker run -d \
  -p 8000:8000 \
  -e OIL_MASTER_API_KEY=your_secret_key \
  -e OIL_DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/oil \
  -e OIL_REDIS_URL=redis://redis:6379/0 \
  --name oil-api \
  openinterest-lens
```

## Docker Compose

```yaml
version: "3.8"
services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - OIL_MASTER_API_KEY=${OIL_MASTER_API_KEY}
      - OIL_DATABASE_URL=postgresql+asyncpg://oil:oil@db:5432/oil
      - OIL_REDIS_URL=redis://redis:6379/0
      - OIL_CORS_ORIGINS=["https://openinterestlens.com","https://app.openinterestlens.com"]
      - OIL_RATE_LIMIT_FREE=60
      - OIL_RATE_LIMIT_PRO=600
      - OIL_RATE_LIMIT_ENTERPRISE=6000
    depends_on:
      - db
      - redis
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/v1/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: oil
      POSTGRES_USER: oil
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    volumes:
      - redisdata:/data

  celery:
    build: .
    command: celery -A app.celery worker -l info
    environment:
      - OIL_DATABASE_URL=postgresql+asyncpg://oil:oil@db:5432/oil
      - OIL_REDIS_URL=redis://redis:6379/0
    depends_on:
      - db
      - redis

volumes:
  pgdata:
  redisdata:
```

## Environment Variables

| Variable | Default | Required | Description |
|---|---|---|---|
| `OIL_MASTER_API_KEY` | `oil_sk_live_master_key_change_me` | ⚠️ Yes | Master API key — **change in production** |
| `OIL_DATABASE_URL` | `sqlite+aiosqlite:///./openinterest_lens.db` | No | Database connection URL |
| `OIL_REDIS_URL` | `redis://localhost:6379/0` | No | Redis URL for caching and pub/sub |
| `OIL_API_PREFIX` | `/v1` | No | API URL prefix |
| `OIL_CORS_ORIGINS` | `["http://localhost:3000","http://localhost:8000"]` | No | Allowed CORS origins (JSON array) |
| `OIL_CORS_ALLOW_METHODS` | `["GET","POST","OPTIONS"]` | No | Allowed CORS methods |
| `OIL_CORS_ALLOW_HEADERS` | `["X-API-Key","Authorization","Content-Type","Accept"]` | No | Allowed CORS headers |
| `OIL_RATE_LIMIT_FREE` | `60` | No | Free tier rate limit (req/hr) |
| `OIL_RATE_LIMIT_PRO` | `600` | No | Pro tier rate limit (req/hr) |
| `OIL_RATE_LIMIT_ENTERPRISE` | `6000` | No | Enterprise tier rate limit (req/hr) |
| `OIL_DEBUG` | `false` | No | Enable debug mode |
| `OIL_LOG_LEVEL` | `info` | No | Log level: debug, info, warning, error |

## Database Setup

### SQLite (Development)

Default — no configuration needed. Tables are created on startup.

### PostgreSQL (Production)

```bash
# Create database
createdb openinterest_lens

# Run migrations
alembic upgrade head

# Or let the app create tables (dev only)
# Set OIL_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/openinterest_lens
```

### Alembic Migrations

```bash
# Generate migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

## Redis Setup

Redis is used for:
- **Rate limiting** — sliding window counters
- **Response caching** — TTL-based cache for computed signals
- **WebSocket pub/sub** — real-time signal broadcasts
- **Celery broker** — task queue for data ingestion

```bash
# Install Redis
apt-get install redis-server  # Debian/Ubuntu
brew install redis             # macOS

# Configure
redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
```

**Redis is optional in development** — the app falls back to in-memory caching.

## Production Checklist

### Security
- [ ] Change `OIL_MASTER_API_KEY` to a strong random key
- [ ] Set `OIL_CORS_ORIGINS` to your actual domains (no wildcards)
- [ ] Enable HTTPS (use a reverse proxy like nginx or Caddy)
- [ ] Rotate API keys periodically via `/v1/keys/rotate`
- [ ] Set `OIL_DEBUG=false`
- [ ] Use PostgreSQL instead of SQLite
- [ ] Configure firewall rules (only expose ports 80/443)
- [ ] Enable rate limiting (enabled by default)

### Performance
- [ ] Use Redis for caching (not in-memory)
- [ ] Run with `uvicorn` workers: `uvicorn app.main:app --workers 4`
- [ ] Set up connection pooling for PostgreSQL
- [ ] Configure CDN for static assets (landing page)
- [ ] Monitor `/metrics` endpoint (Prometheus)

### Reliability
- [ ] Set up health checks (`/v1/health`)
- [ ] Configure log aggregation
- [ ] Set up Redis persistence (`appendonly yes`)
- [ ] Configure PostgreSQL backups
- [ ] Set up Celery workers for data ingestion
- [ ] Monitor data quality endpoint (`/v1/quality`)

### Monitoring
- [ ] Scrape `/metrics` with Prometheus
- [ ] Set up alerts for:
  - `http_request_duration_seconds` p99 > 2s
  - `http_requests_total{status=~"5.."}` > 0
  - `data_quality_score` < 0.8
  - `active_websocket_connections` > 1000
  - `data_ingestion_events{status="failed"}` increasing

## Running with Uvicorn

```bash
# Development (auto-reload)
uvicorn app.main:app --reload --port 8000

# Production (multiple workers)
uvicorn app.main:app --workers 4 --port 8000 --access-log

# With gunicorn + uvicorn workers
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

## Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name api.openinterestlens.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.openinterestlens.com;

    ssl_certificate /etc/ssl/certs/openinterestlens.pem;
    ssl_certificate_key /etc/ssl/private/openinterestlens.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_read_timeout 86400s;  # 24h for WebSocket
        proxy_send_timeout 86400s;
    }

    # Landing page (static)
    location = / {
        proxy_pass http://127.0.0.1:8000;
    }

    # Metrics (internal only)
    location /metrics {
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        deny all;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

## Data Ingestion

CFTC COT data is ingested weekly (Fridays). CME settlement data is ingested daily.

```bash
# Manual COT ingestion
celery -A app.celery call ingest_cot

# Manual settlement ingestion
celery -A app.celery call ingest_settlements

# Schedule with Celery Beat (celeryconfig.py)
# COT: Every Friday at 8 PM ET
# Settlements: Every weekday at 7 PM ET
```

## Troubleshooting

**Redis connection failed:** App falls back to in-memory caching. Check Redis is running: `redis-cli ping`

**Database migration error:** Run `alembic upgrade head` manually.

**WebSocket disconnections:** Check nginx timeout settings (`proxy_read_timeout`).

**Rate limit headers missing:** Ensure `RateLimitHeadersMiddleware` is registered in `create_app()`.

**CORS errors:** Check `OIL_CORS_ORIGINS` includes your frontend domain.

**Prometheus metrics not updating:** Ensure `PrometheusMiddleware` is registered and `/metrics` is accessible.