"""FastAPI application factory for OpenInterest Lens."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import close_db, init_db
from app.middleware.rate_limit import RateLimitHeadersMiddleware
from app.middleware.request_size import RequestSizeLimitMiddleware
from app.monitoring.middleware import PrometheusMiddleware, add_metrics_endpoint
from app.routers import (
    api,
    composite,
    contracts,
    examples,
    health,
    ingestion,
    quality,
    security,
    signals,
    term_structure,
    ws,
)

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown hooks."""
    settings = get_settings()

    # Startup
    log = logger.bind(app="openinterest-lens", version=settings.app_version)
    log.info("starting_up")

    # Initialize database (creates tables for SQLite dev; in prod use Alembic)
    await init_db()
    log.info("database_initialized")

    # Seed contracts if empty
    from app.routers.contracts import seed_contracts
    await seed_contracts()
    log.info("contracts_seeded")

    # Redis connection (optional in dev)
    app.state.redis = None
    try:
        import redis.asyncio as aioredis

        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await app.state.redis.ping()
        log.info("redis_connected")

        # Initialize Redis-backed cache service
        from app.services.redis_cache import get_cache_service
        get_cache_service(redis=app.state.redis)
        log.info("cache_service_initialized", backend="redis")
    except Exception as exc:
        log.warning("redis_unavailable", error=str(exc))
        app.state.redis = None

        # Initialize in-memory cache service
        from app.services.redis_cache import get_cache_service
        get_cache_service(redis=None)
        log.info("cache_service_initialized", backend="memory")

    # Start WebSocket heartbeat task
    from app.services.ws_manager import get_ws_manager
    ws_manager = get_ws_manager()
    await ws_manager.start_heartbeat()
    log.info("ws_heartbeat_started")

    # Start Redis pub/sub listener
    from app.services.redis_pubsub import get_pubsub_manager
    pubsub = get_pubsub_manager(redis=app.state.redis, ws_manager=ws_manager)
    await pubsub.start()
    log.info("pubsub_started")

    yield

    # Shutdown
    log.info("shutting_down")

    # Stop WebSocket heartbeat
    from app.services.ws_manager import get_ws_manager
    ws_manager = get_ws_manager()
    await ws_manager.stop_heartbeat()
    log.info("ws_heartbeat_stopped")

    # Stop pub/sub
    from app.services.redis_pubsub import get_pubsub_manager
    pubsub = get_pubsub_manager()
    await pubsub.stop()
    log.info("pubsub_stopped")

    if app.state.redis:
        await app.state.redis.close()
        log.info("redis_disconnected")
    await close_db()
    log.info("shutdown_complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Real-time futures market structure API — OI + COT + term structure as developer-ready signals",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # Rate limit response headers middleware
    app.add_middleware(RateLimitHeadersMiddleware)

    # Request size limit middleware (256KB max)
    app.add_middleware(RequestSizeLimitMiddleware)

    # Prometheus monitoring middleware
    app.add_middleware(PrometheusMiddleware)

    # Metrics endpoint
    add_metrics_endpoint(app)

    # Register routers
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(api.router, prefix=settings.api_prefix)
    app.include_router(contracts.router, prefix=settings.api_prefix)
    app.include_router(ingestion.router, prefix=settings.api_prefix)
    app.include_router(signals.router, prefix=settings.api_prefix)
    app.include_router(term_structure.router, prefix=settings.api_prefix)
    app.include_router(quality.router, prefix=settings.api_prefix)
    app.include_router(examples.router, prefix=settings.api_prefix)
    app.include_router(composite.router, prefix=settings.api_prefix)
    app.include_router(security.router, prefix=settings.api_prefix)

    # WebSocket router (no prefix — handles its own /ws/v1/signals path)
    app.include_router(ws.router)

    # Landing page (served at /)
    from pathlib import Path as _Path

    from fastapi.responses import FileResponse

    _landing_dir = _Path(__file__).resolve().parent.parent.parent / "landing"

    if _landing_dir.is_dir() and (_landing_dir / "landing.html").exists():
        from fastapi.staticfiles import StaticFiles

        @app.get("/", include_in_schema=False)
        async def landing_page():
            return FileResponse(_landing_dir / "landing.html")

        app.mount("/static", StaticFiles(directory=str(_landing_dir)), name="landing_static")

    return app


app = create_app()