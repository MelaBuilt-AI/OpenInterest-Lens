"""Pytest configuration and shared fixtures for OpenInterest Lens tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

# Server-side fixtures (database, HTTP client) — only loaded when server deps are available
try:
    import pytest_asyncio
    from app.database import Base
    from app.models.db import Contract
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    _HAS_SERVER_DEPS = True
except ImportError:
    _HAS_SERVER_DEPS = False


if _HAS_SERVER_DEPS:
    # ---------------------------------------------------------------------------
    # Test database setup (SQLite in-memory)
    # ---------------------------------------------------------------------------

    TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


    @pytest_asyncio.fixture(scope="function")
    async def test_engine():
        """Create an in-memory SQLite engine for tests (function-scoped for isolation)."""
        engine = create_async_engine(TEST_DATABASE_URL, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


    @pytest_asyncio.fixture
    async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
        """Provide a clean database session for each test, rolled back after."""
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session:
            # Seed contracts
            await _seed_test_contracts(session)
            yield session
            await session.rollback()


    @pytest_asyncio.fixture
    async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
        """Provide an async HTTP test client with DB session override."""
        from app.database import get_db
        from app.main import app

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

        app.dependency_overrides.clear()


    # ---------------------------------------------------------------------------
    # Singleton reset fixture — runs before every test to prevent state leakage
    # ---------------------------------------------------------------------------


    @pytest.fixture(autouse=True)
    def _reset_singletons():
        """Reset all module-level singletons before each test to prevent state leakage."""
        from app.services.redis_cache import reset_cache_service
        from app.services.redis_pubsub import reset_pubsub_manager
        from app.services.signal_cache import reset_signal_cache
        from app.services.ws_manager import reset_ws_manager

        reset_signal_cache()
        reset_cache_service()
        reset_ws_manager()
        reset_pubsub_manager()

        # Clear the APIKeyAuth singleton's key cache
        from app.dependencies import _auth
        _auth._key_cache.clear()
        _auth._revoked_keys.clear()

        # Also clear the rotated keys store in security module
        from app.middleware.security import _rotated_keys
        _rotated_keys.clear()

        yield

        # Clean up after test too
        reset_signal_cache()
        reset_cache_service()
        reset_ws_manager()
        reset_pubsub_manager()
        _auth._key_cache.clear()
        _auth._revoked_keys.clear()
        _rotated_keys.clear()


    # ---------------------------------------------------------------------------
    # Test API keys
    # ---------------------------------------------------------------------------

    TEST_API_KEY_FREE = "oil_sk_live_demo_free"
    TEST_API_KEY_PRO = "oil_sk_live_demo_pro"
    TEST_API_KEY_ENTERPRISE = "oil_sk_live_demo_enterprise"


    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    async def _seed_test_contracts(session: AsyncSession) -> None:
        """Insert the four MVP contracts for testing."""
        from app.routers.contracts import SEED_CONTRACTS

        for data in SEED_CONTRACTS:
            existing = await session.execute(
                Contract.__table__.select().where(Contract.symbol == data["symbol"])
            )
            if existing.fetchone() is None:
                session.add(Contract(**data, is_active=True))
        await session.flush()

else:
    # SDK-only test runs — provide minimal fixtures so imports don't break
    TEST_API_KEY_FREE = "oil_sk_live_demo_free"
    TEST_API_KEY_PRO = "oil_sk_live_demo_pro"
    TEST_API_KEY_ENTERPRISE = "oil_sk_live_demo_enterprise"
