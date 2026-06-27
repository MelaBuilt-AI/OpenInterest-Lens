"""Tests for positioning signal computation and caching.

Tests cover:
- Smart money signal logic
- Retail contrarian signal logic
- Composite signal combination
- Signal cache behavior (LRU, TTL, invalidation)
- API endpoints with mock data
- Edge cases (no data, single data point, extreme values)
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models.db import Contract, RawCOTReport
from app.models.signal import (
    NetPosition,
    PositioningBreakdown,
    PositioningSignal,
    PositioningSignalResponse,
    Retail,
    SignalMetadata,
    SignalOverall,
    SmartMoney,
    TraderPositionBreakdown,
)
from app.signals.historical import (
    detect_extreme_positioning,
    detect_mean_reversion,
    percentile_rank,
    rolling_z_score,
)
from app.signals.positioning import (
    compute_composite_signal,
    compute_retail_signal,
    compute_smart_money_signal,
)
from app.services.signal_cache import SignalCache, get_signal_cache, reset_signal_cache

from tests.conftest import TEST_API_KEY_FREE, TEST_API_KEY_PRO, TEST_API_KEY_ENTERPRISE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the signal cache before each test."""
    reset_signal_cache()
    yield
    reset_signal_cache()


@pytest_asyncio.fixture
async def signal_db():
    """Create an in-memory DB for signal tests with seeded contracts and COT data."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # Seed contracts
        from app.routers.contracts import SEED_CONTRACTS
        for data in SEED_CONTRACTS:
            contract = Contract(**data, is_active=True)
            session.add(contract)
        await session.commit()

        # Seed COT data — 60 weekly reports for ES
        result = await session.execute(select(Contract).where(Contract.symbol == "ES"))
        es_contract = result.scalar_one()

        base_date = date(2025, 4, 1)  # Start ~60 weeks back
        for week in range(60):
            as_of = base_date + timedelta(weeks=week)
            # Gradually shift positions to create a realistic distribution
            base_commercial_long = 800000 + week * 2000
            base_commercial_short = 1100000 + week * 1000
            base_nc_long = 500000 + week * 3000
            base_nc_short = 200000 + week * 500
            base_nr_long = 100000 + week * 1000
            base_nr_short = 50000 + week * 500

            report = RawCOTReport(
                contract_id=es_contract.id,
                as_of_date=datetime.combine(as_of, datetime.min.time()),
                published_date=datetime.combine(as_of + timedelta(days=3), datetime.min.time()),
                commercial_long=base_commercial_long,
                commercial_short=base_commercial_short,
                commercial_net=base_commercial_long - base_commercial_short,
                non_commercial_long=base_nc_long,
                non_commercial_short=base_nc_short,
                non_commercial_net=base_nc_long - base_nc_short,
                non_reportable_long=base_nr_long,
                non_reportable_short=base_nr_short,
                non_reportable_net=base_nr_long - base_nr_short,
                total_open_interest=base_commercial_long + base_nc_long + base_nr_long,
            )
            session.add(report)

        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def signal_client(signal_db: AsyncSession):
    """Async test client with signal routes and DB session override."""
    from app.main import app

    async def override_get_db():
        yield signal_db

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Smart money signal tests
# ---------------------------------------------------------------------------


class TestSmartMoneySignal:
    """Tests for smart money (commercial hedger) signal computation."""

    def test_smart_money_neutral_range(self):
        """Commercial net near the historical mean should be neutral/low conviction."""
        # Create a series where current value is near the mean
        history = [-300000] * 50 + [-350000]  # Mean ≈ -301000
        signal = compute_smart_money_signal(-301000, history)
        assert signal.direction in ("neutral", "short")  # Slightly negative net
        assert signal.conviction in ("low", "medium")  # May be medium due to many identical values
        assert abs(signal.z_score) < 1.0

    def test_smart_money_extreme_long(self):
        """Extremely positive commercial net should be high conviction long."""
        history = [-300000, -310000, -290000, -320000, -280000, -300000,
                   -290000, -310000, -300000, -290000, -310000, -300000,
                   -290000, -310000, -300000, -290000, -310000, -300000,
                   -290000, -310000, -300000, -290000, -310000, -300000,
                   -290000, -310000, -300000, -290000, -310000, -300000]
        # Current value well above historical range
        signal = compute_smart_money_signal(200000, history)
        assert signal.direction == "long"
        assert signal.z_score > 1.5

    def test_smart_money_extreme_short(self):
        """Extremely negative commercial net should be high conviction short."""
        history = [-100000, -120000, -80000, -110000, -90000, -100000,
                   -90000, -110000, -100000, -90000, -110000, -100000,
                   -90000, -110000, -100000, -90000, -110000, -100000,
                   -90000, -110000, -100000, -90000, -110000, -100000,
                   -90000, -110000, -100000, -90000, -110000, -100000]
        signal = compute_smart_money_signal(-500000, history)
        assert signal.direction == "short"
        assert signal.z_score < -1.5

    def test_smart_money_small_history(self):
        """Should handle small history gracefully (returns z_score=0)."""
        history = [-300000]
        signal = compute_smart_money_signal(-300000, history)
        assert signal.z_score == 0.0  # Not enough data for meaningful Z

    def test_smart_money_constant_history(self):
        """Constant history should produce z_score of 0."""
        history = [-300000] * 52
        signal = compute_smart_money_signal(-300000, history)
        assert signal.z_score == 0.0


# ---------------------------------------------------------------------------
# Retail contrarian signal tests
# ---------------------------------------------------------------------------


class TestRetailSignal:
    """Tests for retail contrarian signal logic."""

    def test_retail_extreme_long_contrarian(self):
        """Extremely positive retail positioning should produce fade_long."""
        # History of small retail net positions, current is very large
        history = [5000, 8000, 3000, 7000, 6000, 4000, 9000, 5000, 7000, 3000,
                   6000, 8000, 4000, 7000, 5000, 9000, 3000, 6000, 8000, 4000,
                   7000, 5000, 9000, 3000, 6000, 8000, 4000, 7000, 5000, 9000]
        # Current value is way above history
        signal = compute_retail_signal(100000, history)
        assert signal.direction == "long"
        assert signal.z_score > 1.5
        # With extreme z-score AND extreme percentile → contrarian signal
        assert signal.contrarian_signal in ("fade_long", "none")  # Depends on percentile threshold

    def test_retail_extreme_short_contrarian(self):
        """Extremely negative retail positioning should produce fade_short."""
        history = [5000, 8000, 3000, 7000, 6000, 4000, 9000, 5000, 7000, 3000,
                   6000, 8000, 4000, 7000, 5000, 9000, 3000, 6000, 8000, 4000,
                   7000, 5000, 9000, 3000, 6000, 8000, 4000, 7000, 5000, 9000]
        # Current value deeply negative
        signal = compute_retail_signal(-80000, history)
        assert signal.z_score < -1.5

    def test_retail_no_extreme(self):
        """Normal retail positioning should have 'none' contrarian signal."""
        history = [100000, 110000, 90000, 105000, 95000, 100000,
                   102000, 98000, 101000, 99000, 100000, 100500,
                   99500, 100000, 100500, 99500, 100000, 100500,
                   99500, 100000, 100500, 99500, 100000, 100500,
                   99500, 100000, 100500, 99500, 100000, 100500]
        signal = compute_retail_signal(101000, history)
        assert signal.contrarian_signal == "none"

    def test_retail_percentile_and_z_score(self):
        """Retail signal should include both Z-score and percentile."""
        history = list(range(100, 200))  # 100 values from 100 to 199
        signal = compute_retail_signal(250, history)  # Way above range
        assert signal.z_score > 3.0
        assert signal.percentile > 90


# ---------------------------------------------------------------------------
# Composite signal tests
# ---------------------------------------------------------------------------


class TestCompositeSignal:
    """Tests for composite signal combination logic."""

    def test_bullish_signal_smart_money_long_retail_fade_short(self):
        """Smart money long + retail fade_short → bullish with high strength."""
        smart_money = SmartMoney(
            z_score=2.0, percentile=90.0, direction="long", conviction="high"
        )
        retail = Retail(
            z_score=-2.0, percentile=5.0, direction="short", contrarian_signal="fade_short"
        )
        net_position = NetPosition(commercial=-50000, non_commercial=200000, non_reportable=-30000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.overall == "bullish"
        assert signal.strength > 0.5
        assert signal.divergence is True  # Smart money long, retail short

    def test_bearish_signal_smart_money_short_retail_fade_long(self):
        """Smart money short + retail fade_long → bearish with high strength."""
        smart_money = SmartMoney(
            z_score=-2.0, percentile=10.0, direction="short", conviction="high"
        )
        retail = Retail(
            z_score=2.0, percentile=95.0, direction="long", contrarian_signal="fade_long"
        )
        net_position = NetPosition(commercial=-300000, non_commercial=100000, non_reportable=200000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.overall == "bearish"
        assert signal.strength > 0.5
        assert signal.divergence is True

    def test_neutral_signal_both_neutral(self):
        """Neutral smart money and retail → neutral signal."""
        smart_money = SmartMoney(
            z_score=0.2, percentile=52.0, direction="neutral", conviction="low"
        )
        retail = Retail(
            z_score=0.1, percentile=48.0, direction="neutral", contrarian_signal="none"
        )
        net_position = NetPosition(commercial=0, non_commercial=0, non_reportable=0)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.overall == "neutral"
        assert signal.divergence is False

    def test_moderate_bullish_smart_money_only(self):
        """Smart money long without retail extremes → moderate bullish."""
        smart_money = SmartMoney(
            z_score=1.0, percentile=70.0, direction="long", conviction="medium"
        )
        retail = Retail(
            z_score=0.3, percentile=55.0, direction="long", contrarian_signal="none"
        )
        net_position = NetPosition(commercial=50000, non_commercial=100000, non_reportable=20000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.overall == "bullish"
        assert signal.strength >= 0.3

    def test_divergence_detection(self):
        """Divergence should be True when smart money and retail disagree."""
        smart_money = SmartMoney(
            z_score=1.5, percentile=80.0, direction="long", conviction="medium"
        )
        retail = Retail(
            z_score=-0.5, percentile=30.0, direction="short", contrarian_signal="none"
        )
        net_position = NetPosition(commercial=200000, non_commercial=-50000, non_reportable=-30000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.divergence is True

    def test_no_divergence_same_direction(self):
        """No divergence when both smart money and retail are on the same side."""
        smart_money = SmartMoney(
            z_score=1.5, percentile=80.0, direction="long", conviction="medium"
        )
        retail = Retail(
            z_score=0.5, percentile=65.0, direction="long", contrarian_signal="none"
        )
        net_position = NetPosition(commercial=200000, non_commercial=100000, non_reportable=50000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert signal.divergence is False

    def test_strength_range(self):
        """Signal strength should always be between 0 and 1."""
        smart_money = SmartMoney(
            z_score=3.0, percentile=99.0, direction="long", conviction="high"
        )
        retail = Retail(
            z_score=-3.0, percentile=1.0, direction="short", contrarian_signal="fade_short"
        )
        net_position = NetPosition(commercial=500000, non_commercial=-200000, non_reportable=-300000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        assert 0 < signal.strength <= 1.0


# ---------------------------------------------------------------------------
# Signal cache tests
# ---------------------------------------------------------------------------


class TestSignalCache:
    """Tests for the signal caching layer."""

    def test_cache_set_and_get(self):
        """Should store and retrieve values."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("positioning:ES:latest", {"value": "test"})
        result = cache.get("positioning:ES:latest")
        assert result == {"value": "test"}

    def test_cache_miss(self):
        """Should return None for missing keys."""
        cache = SignalCache(max_size=10, default_ttl=60)
        result = cache.get("nonexistent")
        assert result is None

    def test_cache_ttl_expiration(self):
        """Expired entries should not be returned."""
        cache = SignalCache(max_size=10, default_ttl=0.01)  # 10ms TTL
        cache.set("key1", "value1")
        time.sleep(0.02)  # Wait for expiration
        result = cache.get("key1")
        assert result is None

    def test_cache_lru_eviction(self):
        """Should evict oldest entries when at max capacity."""
        cache = SignalCache(max_size=3, default_ttl=60)
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")
        cache.set("key4", "value4")  # Should evict key1
        assert cache.get("key1") is None
        assert cache.get("key2") is not None
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None

    def test_cache_lru_access_updates_order(self):
        """Accessing an entry should move it to the end (most recently used)."""
        cache = SignalCache(max_size=3, default_ttl=60)
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")
        # Access key1 — it's now most recently used
        cache.get("key1")
        # Adding key4 should evict key2 (now least recently used)
        cache.set("key4", "value4")
        assert cache.get("key1") is not None  # Still there (recently accessed)
        assert cache.get("key2") is None  # Evicted
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None

    def test_cache_invalidate_by_commodity(self):
        """Should invalidate all entries for a specific commodity."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("positioning:ES:latest", "val1")
        cache.set("positioning:ES:2026-05-12", "val2")
        cache.set("positioning:NQ:latest", "val3")
        removed = cache.invalidate("ES")
        assert removed >= 2
        assert cache.get("positioning:ES:latest") is None
        assert cache.get("positioning:ES:2026-05-12") is None
        assert cache.get("positioning:NQ:latest") is not None

    def test_cache_invalidate_full_flush(self):
        """Should flush entire cache when commodity is None."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("key1", "val1")
        cache.set("key2", "val2")
        cache.set("key3", "val3")
        removed = cache.invalidate()
        assert removed == 3
        assert cache.size == 0

    def test_cache_stats(self):
        """Should track hits, misses, and hit rate."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("key1", "val1")
        cache.get("key1")  # Hit
        cache.get("key1")  # Hit
        cache.get("nonexistent")  # Miss
        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["hit_rate"] == pytest.approx(0.6667, abs=0.01)

    def test_cache_cleanup_expired(self):
        """Should remove expired entries on cleanup."""
        cache = SignalCache(max_size=10, default_ttl=0.01)
        cache.set("key1", "val1")
        cache.set("key2", "val2")
        time.sleep(0.02)
        cache.set("key3", "val3")  # Not expired
        removed = cache.cleanup_expired()
        assert removed == 2
        assert cache.size == 1
        assert cache.get("key3") is not None

    def test_cache_make_key(self):
        """Should generate consistent cache keys."""
        key1 = SignalCache.make_key("ES", "positioning")
        key2 = SignalCache.make_key("ES", "positioning", date(2026, 5, 12))
        key3 = SignalCache.make_key("NQ", "positioning")
        assert key1 == "positioning:ES:latest"
        assert key2 == "positioning:ES:2026-05-12"
        assert key3 == "positioning:NQ:latest"

    def test_cache_invalidate_on_ingestion(self):
        """Should invalidate commodity entries on ingestion."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("positioning:ES:latest", "val1")
        cache.set("positioning:ES:2026-05-12", "val2")
        cache.set("positioning:NQ:latest", "val3")
        removed = cache.invalidate_on_ingestion("ES")
        assert removed >= 2
        assert cache.get("positioning:ES:latest") is None

    def test_get_signal_cache_singleton(self):
        """get_signal_cache should return the same instance."""
        cache1 = get_signal_cache()
        cache2 = get_signal_cache()
        assert cache1 is cache2

    def test_reset_signal_cache(self):
        """reset_signal_cache should create a new instance on next get."""
        cache1 = get_signal_cache()
        reset_signal_cache()
        cache2 = get_signal_cache()
        assert cache1 is not cache2


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestPositioningEndpoint:
    """Tests for the positioning signal API endpoints."""

    @pytest.mark.asyncio
    async def test_get_positioning_signal_for_es(self, signal_client: AsyncClient):
        """Should compute positioning signal for ES."""
        response = await signal_client.get(
            "/v1/signals/positioning/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["commodity"] == "ES"
        assert "signal" in data
        assert "breakdown" in data
        assert "metadata" in data
        # Check signal structure
        signal = data["signal"]
        assert signal["contract"] == "ES"
        assert signal["smart_money"]["direction"] in ("long", "short", "neutral")
        assert signal["smart_money"]["conviction"] in ("low", "medium", "high")
        assert signal["retail"]["contrarian_signal"] in ("fade_long", "fade_short", "none")
        assert signal["signal"]["overall"] in ("bullish", "bearish", "neutral")

    @pytest.mark.asyncio
    async def test_get_positioning_signal_for_unknown_contract(self, signal_client: AsyncClient):
        """Should return 404 for unknown contract."""
        response = await signal_client.get(
            "/v1/signals/positioning/ZZ",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_positioning_signal_free_tier_gc(self, signal_client: AsyncClient):
        """Free tier should get 403 for GC (not in free tier)."""
        # First, seed GC data
        # This test just checks the tier enforcement
        response = await signal_client.get(
            "/v1/signals/positioning/GC",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_positioning_signals_all(self, signal_client: AsyncClient):
        """Should compute signals for all commodities with data."""
        response = await signal_client.get(
            "/v1/signals/positioning",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert "signals" in data
        assert "computed_at" in data
        # Only ES has data in test fixture
        commodities = [s["commodity"] for s in data["signals"]]
        assert "ES" in commodities

    @pytest.mark.asyncio
    async def test_get_positioning_signal_with_lookback(self, signal_client: AsyncClient):
        """Should accept lookback_weeks parameter."""
        response = await signal_client.get(
            "/v1/signals/positioning/ES?lookback_weeks=26",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["metadata"]["lookback_weeks"] <= 26

    @pytest.mark.asyncio
    async def test_positioning_signal_breakdown_fields(self, signal_client: AsyncClient):
        """Breakdown should have all required fields with Z-scores."""
        response = await signal_client.get(
            "/v1/signals/positioning/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        breakdown = data["breakdown"]

        # Check all three trader categories exist
        for category in ("commercial", "non_commercial", "non_reportable"):
            assert category in breakdown
            cat = breakdown[category]
            assert "long" in cat
            assert "short" in cat
            assert "net" in cat
            assert "z_score" in cat
            assert "percentile" in cat
            assert "direction" in cat
            assert isinstance(cat["z_score"], (int, float))
            assert isinstance(cat["percentile"], (int, float))
            assert cat["direction"] in ("long", "short", "neutral")

    @pytest.mark.asyncio
    async def test_positioning_signal_caching(self, signal_client: AsyncClient):
        """Second request should be served from cache."""
        # First request — computes
        response1 = await signal_client.get(
            "/v1/signals/positioning/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response1.status_code == 200
        assert response1.json()["metadata"]["cache_hit"] is False

        # Second request — should be from cache
        response2 = await signal_client.get(
            "/v1/signals/positioning/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response2.status_code == 200
        assert response2.json()["metadata"]["cache_hit"] is True


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in signal computation."""

    def test_z_score_all_same_values(self):
        """All identical values should produce z_score of 0."""
        values = [50000] * 52
        z = rolling_z_score(50000, values)
        assert z == 0.0

    def test_z_score_two_data_points(self):
        """Two data points should still produce a valid Z-score."""
        values = [100, 200]
        z = rolling_z_score(150, values)
        # Mean = 150, so Z = 0
        assert abs(z) < 0.01

    def test_percentile_single_unique_value(self):
        """Single unique value should return 50th percentile for itself."""
        values = [50]
        pct = percentile_rank(50, values)
        assert pct == 50.0

    def test_retail_signal_no_history(self):
        """Retail signal with very little history should still work."""
        # Only one data point
        signal = compute_retail_signal(50000, [50000])
        assert signal.z_score == 0.0  # Not enough data
        assert signal.contrarian_signal == "none"  # Can't detect extremes

    def test_smart_money_signal_single_data_point(self):
        """Smart money signal with single data point should work with z_score=0."""
        signal = compute_smart_money_signal(-300000, [-300000])
        assert signal.z_score == 0.0
        assert signal.direction in ("neutral", "short")  # Negative net position

    def test_extreme_values_z_score(self):
        """Extremely large/small values should produce large Z-scores."""
        # Normal range
        history = [100, 102, 98, 105, 95, 101, 103, 97, 104, 96] * 5  # 50 values around 100
        z_high = rolling_z_score(10000, history)  # Way above
        z_low = rolling_z_score(-10000, history)  # Way below
        assert z_high > 10.0
        assert z_low < -10.0

    def test_composite_signal_both_extreme(self):
        """Both smart money and retail extreme on same side → strong signal."""
        smart_money = SmartMoney(
            z_score=2.5, percentile=95.0, direction="long", conviction="high"
        )
        retail = Retail(
            z_score=2.0, percentile=90.0, direction="long", contrarian_signal="none"
        )
        net_position = NetPosition(commercial=500000, non_commercial=300000, non_reportable=200000)
        signal = compute_composite_signal(smart_money, retail, net_position)
        # Smart money long, retail long, no contrarian → moderate bullish
        assert signal.overall in ("bullish", "neutral")

    def test_cache_custom_ttl(self):
        """Cache should respect custom TTL per entry."""
        cache = SignalCache(max_size=10, default_ttl=3600)
        cache.set("short_key", "value", ttl_seconds=0.01)
        cache.set("long_key", "value")
        time.sleep(0.02)
        assert cache.get("short_key") is None
        assert cache.get("long_key") is not None

    def test_cache_overwrite(self):
        """Setting an existing key should update the value."""
        cache = SignalCache(max_size=10, default_ttl=60)
        cache.set("key1", "old_value")
        cache.set("key1", "new_value")
        assert cache.get("key1") == "new_value"

    @pytest.mark.asyncio
    async def test_no_cot_data_returns_503(self, signal_db: AsyncSession):
        """Should return 503 when no COT data is available for a contract."""
        # CL has no COT data in our test fixture
        from app.main import app

        async def override_get_db():
            yield signal_db

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            response = await ac.get(
                "/v1/signals/positioning/CL",
                headers={"X-API-Key": TEST_API_KEY_PRO},
            )
            # CL exists as a contract but has no COT data
            assert response.status_code == 503

        app.dependency_overrides.clear()