"""Database setup for OpenInterest Lens.

Async engine, session factory, and declarative base.
Configured for TimescaleDB in production, SQLite for dev/testing.
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Configuration — switch between TimescaleDB and SQLite via DATABASE_URL
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "OIL_DATABASE_URL",
    "sqlite+aiosqlite:///./openinterest_lens.db",
)

# TimescaleDB example: "postgresql+asyncpg://oil:oil@localhost:5432/openinterest_lens"

_engine_kwargs: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["echo"] = False
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["echo"] = False
    _engine_kwargs["pool_size"] = 20
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables (for dev/SQLite). In production, use Alembic migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    await engine.dispose()