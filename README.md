# OpenInterest Lens

> Real-time futures market structure API — OI + COT + term structure as developer-ready signals.

## Quick Start

### 1. Install

```bash
cd openinterest-lens
pip install -e "./server[dev]"
```

### 2. Run locally (SQLite + no Redis)

```bash
cd server
OIL_DATABASE_URL="sqlite+aiosqlite:///./openinterest_lens.db" \
OIL_MASTER_API_KEY="oil_sk_live_dev_key" \
uvicorn app.main:app --reload --port 8000
```

### 3. Run with Docker Compose (TimescaleDB + Redis)

```bash
docker-compose up -d
```

### 4. Test it

```bash
# Health check (no auth required)
curl http://localhost:8000/v1/health

# List contracts (free tier key)
curl -H "X-API-Key: oil_sk_live_demo_free" http://localhost:8000/v1/contracts

# List contracts (pro tier key — sees all 4)
curl -H "X-API-Key: oil_sk_live_demo_pro" http://localhost:8000/v1/contracts

# Filter by exchange
curl -H "X-API-Key: oil_sk_live_demo_pro" "http://localhost:8000/v1/contracts?exchange=CME"

# Filter by asset class
curl -H "X-API-Key: oil_sk_live_demo_pro" "http://localhost:8000/v1/contracts?asset_class=energy"
```

### 5. Run tests

```bash
cd openinterest-lens
python -m pytest tests/ -v
```

## API Key Tiers

| Tier | Key | Contracts | Rate Limit |
|------|-----|-----------|------------|
| Free | `oil_sk_live_demo_free` | ES, NQ, CL only | 60 req/hr |
| Pro | `oil_sk_live_demo_pro` | All (50 max) | 600 req/hr |
| Enterprise | `oil_sk_live_demo_enterprise` | All (unlimited) | 6000 req/hr |

## Project Structure

```
openinterest-lens/
├── server/                  # FastAPI application
│   ├── app/
│   │   ├── main.py          # App factory + lifespan
│   │   ├── config.py        # Settings (env vars)
│   │   ├── database.py      # Async engine + session
│   │   ├── dependencies.py  # FastAPI deps (auth, DB)
│   │   ├── models/
│   │   │   ├── signal.py    # Pydantic signal models
│   │   │   └── db.py        # SQLAlchemy ORM models
│   │   ├── routers/
│   │   │   ├── health.py    # GET /v1/health
│   │   │   └── contracts.py # GET /v1/contracts
│   │   └── middleware/
│   │       └── auth.py      # API key validation + tier enforcement
│   ├── alembic/             # Database migrations
│   ├── schemas/             # JSON Schema files
│   └── pyproject.toml
├── tests/                   # Integration tests
├── docker-compose.yml       # Local dev stack (API + TimescaleDB + Redis)
├── Dockerfile               # API server container
└── .github/workflows/ci.yml
```

## Tech Stack

- **FastAPI** — async REST API with auto OpenAPI docs
- **SQLAlchemy 2.0** — async ORM with TimescaleDB / SQLite support
- **Alembic** — database migrations
- **Pydantic v2** — request/response validation
- **Redis** — caching + rate limiting (optional in dev)
- **Celery** — scheduled data ingestion (Week 2+)
- **Docker Compose** — local dev environment

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OIL_DATABASE_URL` | `sqlite+aiosqlite:///./openinterest_lens.db` | Database connection (SQLite for dev, PostgreSQL+asyncpg for prod) |
| `OIL_REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `OIL_MASTER_API_KEY` | `oil_sk_live_master_key_change_me` | Master API key (enterprise access) |
| `OIL_DEBUG` | `false` | Enable debug mode |

## License

MIT