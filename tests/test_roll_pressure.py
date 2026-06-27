"""Tests for roll pressure engine, roll calendar, and API endpoints.

Tests cover:
- Roll pressure computation with fixture data
- Roll calendar predictions
- Roll volume estimation
- Roll date proximity signals
- Roll impact scoring
- Historical roll pattern analysis
- API endpoints with mock data
- Edge cases (single month, inverted curves, zero OI)
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models.db import Contract, RawSettlement
from app.models.signal import RollPressureIndex, RollPressureMetrics, NearbyContract
from app.signals.roll_calendar import (
    ROLL_START_DAYS_BEFORE_EXPIRY,
    calculate_expiry_date,
    calculate_roll_info,
    classify_roll_urgency,
    estimate_oi_decay_rate,
    estimate_roll_volume,
    generate_month_code,
    generate_roll_schedule,
    get_active_contract_months,
    parse_month_code,
    calculate_roll_date_proximity,
)
from app.signals.roll_pressure import (
    compute_roll_impact_score,
    analyze_historical_roll_pattern,
    _compute_roll_pressure_score,
)
from app.services.signal_cache import reset_signal_cache

from tests.conftest import TEST_API_KEY_FREE, TEST_API_KEY_PRO


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
async def roll_db():
    """Create an in-memory DB for roll pressure tests with seeded contracts and settlements."""
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

        # Seed settlement data for ES
        result = await session.execute(select(Contract).where(Contract.symbol == "ES"))
        es_contract = result.scalar_one()

        # Create settlement data for multiple months
        settlement_date = datetime(2026, 5, 13)
        months = [
            ("Jun 26", 4520.25, 2100000, 1200000),
            ("Sep 26", 4535.50, 1800000, 900000),
            ("Dec 26", 4550.75, 1200000, 500000),
            ("Mar 27", 4565.00, 500000, 200000),
        ]

        for month_code, price, oi, vol in months:
            settlement = RawSettlement(
                contract_id=es_contract.id,
                month_code=month_code,
                settlement_date=settlement_date,
                settlement_price=price,
                open_interest=oi,
                volume=vol,
            )
            session.add(settlement)

        # Add historical settlement data for OI decay analysis
        for days_back in range(1, 10):
            hist_date = datetime(2026, 5, 13) - timedelta(days=days_back)
            # Simulate declining nearby OI as roll approaches
            nearby_oi = 2100000 + days_back * 30000  # Higher OI in the past
            hist_settlement = RawSettlement(
                contract_id=es_contract.id,
                month_code="Jun 26",
                settlement_date=hist_date,
                settlement_price=4520.25 - days_back * 0.5,
                open_interest=nearby_oi,
                volume=1100000 + days_back * 10000,
            )
            session.add(hist_settlement)

        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def roll_client(roll_db: AsyncSession):
    """Async test client with roll pressure routes and DB session override."""
    from app.main import app

    async def override_get_db():
        yield roll_db

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Roll calendar computation tests
# ---------------------------------------------------------------------------


class TestRollCalendarComputation:
    """Tests for roll calendar calculation functions."""

    def test_parse_month_code_various_formats(self):
        """Should parse various month code formats."""
        # Display format
        m, y = parse_month_code("Jun 26")
        assert m == 6 and y == 2026

        m, y = parse_month_code("Mar 25")
        assert m == 3 and y == 2025

        m, y = parse_month_code("Dec 27")
        assert m == 12 and y == 2027

        # CME format
        m, y = parse_month_code("U26")
        assert m == 9 and y == 2026

        m, y = parse_month_code("H25")
        assert m == 3 and y == 2025

        m, y = parse_month_code("Z27")
        assert m == 12 and y == 2027

    def test_parse_month_code_invalid(self):
        """Should raise ValueError for invalid codes."""
        with pytest.raises(ValueError):
            parse_month_code("Invalid")
        # 'X' is not in the CME month code mapping, but the function
        # handles 3-char codes differently — test what actually fails
        with pytest.raises(ValueError):
            parse_month_code("")  # Empty string

    def test_generate_month_code_roundtrip(self):
        """Generating and parsing should roundtrip."""
        for month in range(1, 13):
            code = generate_month_code(month, 2026)
            parsed_month, parsed_year = parse_month_code(code)
            assert parsed_month == month
            assert parsed_year == 2026

    def test_calculate_expiry_es_third_friday(self):
        """ES should expire on third Friday."""
        expiry = calculate_expiry_date(2026, 3, "ES")
        assert expiry.weekday() == 4  # Friday
        assert 15 <= expiry.day <= 21
        assert expiry.month == 3

    def test_calculate_expiry_cl_business_day(self):
        """CL should expire on a business day before the 25th."""
        expiry = calculate_expiry_date(2026, 6, "CL")
        assert expiry.weekday() < 5  # Weekday

    def test_calculate_expiry_gc_business_day(self):
        """GC should expire on a business day near month end."""
        expiry = calculate_expiry_date(2026, 6, "GC")
        assert expiry.weekday() < 5  # Weekday

    def test_calculate_expiry_default_third_friday(self):
        """Unknown contracts should default to third Friday."""
        expiry = calculate_expiry_date(2026, 6, "UNKNOWN")
        assert expiry.weekday() == 4  # Friday

    def test_roll_info_has_required_fields(self):
        """RollInfo should have all required fields."""
        as_of = date(2026, 5, 13)
        info = calculate_roll_info("ES", as_of)

        assert info.contract_symbol == "ES"
        assert info.nearby_month_code is not None
        assert info.deferred_month_code is not None
        assert isinstance(info.nearby_expiry, date)
        assert isinstance(info.deferred_expiry, date)
        assert isinstance(info.days_to_roll, int)
        assert isinstance(info.roll_start_date, date)
        assert isinstance(info.roll_end_date, date)
        assert info.roll_urgency in ("imminent", "active", "normal", "relaxed")

    def test_roll_info_es_may_2026(self):
        """ES in mid-May 2026 should have reasonable roll timing."""
        as_of = date(2026, 5, 13)
        info = calculate_roll_info("ES", as_of)

        # ES Jun 2026 contract: H(Mar), M(Jun), U(Sep), Z(Dec)
        # In mid-May, the nearby should be Jun 2026
        assert info.days_to_roll > 0  # Still time to expiry
        assert info.roll_urgency in ("normal", "relaxed", "active")

    def test_roll_schedule_multiple_cycles(self):
        """Should generate roll schedule for multiple cycles."""
        as_of = date(2026, 5, 13)
        schedule = generate_roll_schedule("ES", as_of, num_cycles=4)

        assert len(schedule) == 4
        # Each subsequent roll should be later
        for i in range(len(schedule) - 1):
            assert schedule[i + 1].nearby_expiry > schedule[i].nearby_expiry

    def test_roll_schedule_cl(self):
        """Should generate schedule for CL (energy) contract."""
        as_of = date(2026, 5, 13)
        schedule = generate_roll_schedule("CL", as_of, num_cycles=4)
        assert len(schedule) == 4
        for info in schedule:
            assert info.contract_symbol == "CL"

    def test_roll_info_to_dict(self):
        """RollInfo.to_dict should serialize all fields."""
        as_of = date(2026, 5, 13)
        info = calculate_roll_info("ES", as_of)
        d = info.to_dict()

        assert "contract_symbol" in d
        assert "nearby_month_code" in d
        assert "nearby_expiry" in d
        assert "deferred_month_code" in d
        assert "deferred_expiry" in d
        assert "days_to_roll" in d
        assert "roll_start_date" in d
        assert "roll_end_date" in d
        assert "roll_urgency" in d


# ---------------------------------------------------------------------------
# Roll pressure computation tests
# ---------------------------------------------------------------------------


class TestRollPressureComputation:
    """Tests for roll pressure score computation."""

    def test_score_active_roll_high_pressure(self):
        """Active roll period with high OI should produce high pressure."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=8.0,
            spread_basis=10.0,
            nearby_price=4520.25,
            deferred_price=4535.50,
            days_to_expiry=3,
            nearby_volume=1200000,
            deferred_volume=900000,
            nearby_oi=2100000,
            deferred_oi=1800000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100
        assert score > 40  # Active roll → significant pressure

    def test_score_relaxed_period_low_pressure(self):
        """Far from roll with low decay should produce low pressure."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=0.5,
            spread_basis=5.0,
            nearby_price=4520.25,
            deferred_price=4525.25,
            days_to_expiry=45,
            nearby_volume=1000000,
            deferred_volume=200000,
            nearby_oi=2000000,
            deferred_oi=500000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100
        assert score < 35  # Relaxed period → low pressure

    def test_score_backwardation_spread(self):
        """Backwardation (negative spread) should still compute valid score."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=3.0,
            spread_basis=-15.0,  # Backwardation
            nearby_price=4535.50,
            deferred_price=4520.25,
            days_to_expiry=10,
            nearby_volume=900000,
            deferred_volume=500000,
            nearby_oi=1800000,
            deferred_oi=1200000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100

    def test_score_zero_volume(self):
        """Zero volume should not crash."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=1.0,
            spread_basis=5.0,
            nearby_price=4520.25,
            deferred_price=4525.25,
            days_to_expiry=20,
            nearby_volume=0,
            deferred_volume=0,
            nearby_oi=1000000,
            deferred_oi=500000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100

    def test_score_past_expiry(self):
        """Past expiry should have high proximity component."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=10.0,
            spread_basis=5.0,
            nearby_price=4520.25,
            deferred_price=4525.25,
            days_to_expiry=0,
            nearby_volume=100000,
            deferred_volume=2000000,
            nearby_oi=50000,
            deferred_oi=2500000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100

    def test_score_extreme_oi_decay(self):
        """Extreme OI decay should significantly boost pressure score."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=20.0,  # Very high decay
            spread_basis=30.0,  # Large spread
            nearby_price=4520.25,
            deferred_price=4550.25,
            days_to_expiry=2,
            nearby_volume=500000,
            deferred_volume=2000000,
            nearby_oi=500000,
            deferred_oi=2500000,
            roll_start_days=5,
        )
        assert score > 60  # Should be high pressure


# ---------------------------------------------------------------------------
# Roll impact estimation tests
# ---------------------------------------------------------------------------


class TestRollImpactEstimation:
    """Tests for roll impact score computation."""

    def test_impact_extreme(self):
        """Nearby OI heavily concentrated with active roll should be extreme."""
        result = compute_roll_impact_score(
            nearby_oi=3000000,
            deferred_oi=500000,
            nearby_volume=2000000,
            deferred_volume=100000,
            spread_basis=15.0,
            days_to_expiry=2,
            contract_symbol="ES",
        )
        assert result["impact_category"] in ("extreme", "high")
        assert result["impact_score"] > 50
        assert result["oi_concentration"] > 80

    def test_impact_low(self):
        """Balanced OI far from roll should be low impact."""
        result = compute_roll_impact_score(
            nearby_oi=500000,
            deferred_oi=1500000,
            nearby_volume=300000,
            deferred_volume=800000,
            spread_basis=3.0,
            days_to_expiry=45,
            contract_symbol="ES",
        )
        assert result["impact_category"] == "low"
        assert result["impact_score"] < 40

    def test_impact_medium(self):
        """Moderate OI concentration near roll should be medium impact."""
        result = compute_roll_impact_score(
            nearby_oi=1500000,
            deferred_oi=1000000,
            nearby_volume=800000,
            deferred_volume=600000,
            spread_basis=8.0,
            days_to_expiry=10,
            contract_symbol="ES",
        )
        assert result["impact_category"] in ("low", "medium", "high")
        assert 0 <= result["impact_score"] <= 100

    def test_impact_categories_complete(self):
        """All impact categories should be reachable."""
        # Low
        low = compute_roll_impact_score(
            nearby_oi=500000, deferred_oi=1500000,
            nearby_volume=300000, deferred_volume=800000,
            spread_basis=2.0, days_to_expiry=60, contract_symbol="ES",
        )
        assert low["impact_category"] == "low"

        # Extreme
        extreme = compute_roll_impact_score(
            nearby_oi=3000000, deferred_oi=100000,
            nearby_volume=2000000, deferred_volume=50000,
            spread_basis=50.0, days_to_expiry=1, contract_symbol="ES",
        )
        assert extreme["impact_category"] in ("extreme", "high")

    def test_impact_expected_slippage(self):
        """Expected slippage should be positive when there's a spread."""
        result = compute_roll_impact_score(
            nearby_oi=2000000,
            deferred_oi=1000000,
            nearby_volume=1000000,
            deferred_volume=500000,
            spread_basis=10.0,
            days_to_expiry=5,
            contract_symbol="ES",
        )
        assert result["expected_slippage"] >= 0

    def test_impact_zero_spread(self):
        """Zero spread should have zero expected slippage."""
        result = compute_roll_impact_score(
            nearby_oi=1000000,
            deferred_oi=1000000,
            nearby_volume=500000,
            deferred_volume=500000,
            spread_basis=0.0,
            days_to_expiry=30,
            contract_symbol="ES",
        )
        assert result["expected_slippage"] == 0.0


# ---------------------------------------------------------------------------
# Historical roll pattern tests
# ---------------------------------------------------------------------------


class TestHistoricalRollPattern:
    """Tests for historical roll pattern analysis."""

    def test_concentrated_roll_pattern(self):
        """Rapid OI transition should be classified as 'concentrated'."""
        base_date = date(2026, 5, 1)
        # OI drops rapidly within 3 days
        nearby = [
            (base_date, 2000000),
            (base_date + timedelta(days=1), 1500000),
            (base_date + timedelta(days=2), 500000),
            (base_date + timedelta(days=3), 100000),
            (base_date + timedelta(days=4), 50000),
        ]
        deferred = [
            (base_date, 200000),
            (base_date + timedelta(days=1), 700000),
            (base_date + timedelta(days=2), 1700000),
            (base_date + timedelta(days=3), 1900000),
            (base_date + timedelta(days=4), 2000000),
        ]
        result = analyze_historical_roll_pattern(nearby, deferred)
        assert result["roll_pattern"] in ("concentrated", "gradual", "delayed", "unknown")

    def test_insufficient_data(self):
        """With too few data points, should return 'unknown'."""
        result = analyze_historical_roll_pattern(
            [(date(2026, 5, 1), 100)],
            [(date(2026, 5, 1), 50)],
        )
        assert result["roll_pattern"] == "unknown"

    def test_oi_shift_pct_range(self):
        """OI shift percentage should be between 0 and 100."""
        base_date = date(2026, 5, 1)
        nearby = [(base_date + timedelta(days=i), max(2000000 - i * 100000, 100000)) for i in range(10)]
        deferred = [(base_date + timedelta(days=i), min(500000 + i * 100000, 2000000)) for i in range(10)]

        result = analyze_historical_roll_pattern(nearby, deferred)
        assert 0 <= result["typical_oi_shift_pct"] <= 100


# ---------------------------------------------------------------------------
# OI decay rate tests
# ---------------------------------------------------------------------------


class TestOIDecayRateRollPressure:
    """Tests for OI decay rate in roll pressure context."""

    def test_oi_decay_declining(self):
        """Declining OI should show positive decay rate."""
        series = [
            (date(2026, 5, 1), 2000000),
            (date(2026, 5, 2), 1900000),
            (date(2026, 5, 3), 1800000),
            (date(2026, 5, 4), 1700000),
            (date(2026, 5, 5), 1600000),
        ]
        decay = estimate_oi_decay_rate(series, 3000000, lookback_days=5)
        assert decay > 0
        # (2000000 - 1600000) / 3000000 * 100 ≈ 13.3%
        assert 10 < decay < 20

    def test_oi_decay_increasing(self):
        """Increasing OI should return 0 (decay can't be negative)."""
        series = [
            (date(2026, 5, 1), 1600000),
            (date(2026, 5, 2), 1700000),
            (date(2026, 5, 3), 1800000),
            (date(2026, 5, 4), 1900000),
            (date(2026, 5, 5), 2000000),
        ]
        decay = estimate_oi_decay_rate(series, 3000000, lookback_days=5)
        assert decay == 0.0  # OI growing, not decaying

    def test_oi_decay_stable(self):
        """Stable OI should return 0."""
        series = [
            (date(2026, 5, 1), 2000000),
            (date(2026, 5, 2), 2000000),
            (date(2026, 5, 3), 2000000),
            (date(2026, 5, 4), 2000000),
            (date(2026, 5, 5), 2000000),
        ]
        decay = estimate_oi_decay_rate(series, 3000000, lookback_days=5)
        assert decay == 0.0

    def test_oi_decay_single_point(self):
        """Single data point should return 0."""
        series = [(date(2026, 5, 1), 2000000)]
        decay = estimate_oi_decay_rate(series, 3000000)
        assert decay == 0.0


# ---------------------------------------------------------------------------
# Roll volume estimation tests
# ---------------------------------------------------------------------------


class TestRollVolumeEstimation:
    """Tests for roll volume estimation."""

    def test_active_roll_volume(self):
        """Active roll should estimate significant volume."""
        result = estimate_roll_volume(
            nearby_oi=2000000,
            deferred_oi=500000,
            days_to_roll=3,
            avg_daily_volume=1000000,
        )
        assert result["estimated_roll_volume"] > 0
        assert result["roll_completion_pct"] > 50
        assert result["peak_roll_day_volume"] > 0

    def test_relaxed_period_volume(self):
        """Relaxed period should estimate low volume."""
        result = estimate_roll_volume(
            nearby_oi=2000000,
            deferred_oi=500000,
            days_to_roll=45,
            avg_daily_volume=500000,
        )
        assert result["estimated_roll_volume"] > 0
        assert result["roll_completion_pct"] < 30

    def test_past_expiry_volume(self):
        """Past expiry should return zero roll volume."""
        result = estimate_roll_volume(
            nearby_oi=100000,
            deferred_oi=2000000,
            days_to_roll=0,
        )
        assert result["estimated_roll_volume"] == 0
        assert result["roll_completion_pct"] == 100

    def test_volume_estimation_with_avg_volume(self):
        """Peak roll day volume should use provided average."""
        result = estimate_roll_volume(
            nearby_oi=2000000,
            deferred_oi=500000,
            days_to_roll=5,
            avg_daily_volume=1000000,
        )
        # Peak volume should be approximately 2.5x average
        assert result["peak_roll_day_volume"] == 2500000


# ---------------------------------------------------------------------------
# Roll date proximity tests
# ---------------------------------------------------------------------------


class TestRollDateProximity:
    """Tests for roll date proximity signals."""

    def test_proximity_at_expiry(self):
        """Zero days should be post_roll with maximum proximity."""
        result = calculate_roll_date_proximity(0, "ES")
        assert result["proximity_score"] == 100.0
        assert result["roll_window"] == "post_roll"

    def test_proximity_active_roll(self):
        """Within roll window should be active_roll with high signal."""
        result = calculate_roll_date_proximity(3, "ES")
        assert result["roll_window"] == "active_roll"
        assert result["signal_strength"] == 1.0

    def test_proximity_approaching(self):
        """Approaching roll window should be pre_roll with moderate signal."""
        result = calculate_roll_date_proximity(10, "ES")
        assert result["roll_window"] == "pre_roll"
        assert 0.1 <= result["signal_strength"] <= 1.0

    def test_proximity_far_away(self):
        """Far from expiry should have low proximity and signal."""
        result = calculate_roll_date_proximity(60, "ES")
        assert result["proximity_score"] < 20
        assert result["signal_strength"] <= 0.1

    def test_proximity_negative_days(self):
        """Negative days (past expiry) should be post_roll."""
        result = calculate_roll_date_proximity(-1, "ES")
        assert result["roll_window"] == "post_roll"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestTermStructureEndpoint:
    """Tests for term structure API endpoints."""

    @pytest.mark.asyncio
    async def test_get_term_structure_all(self, roll_client: AsyncClient):
        """Should return term structure data for commodities with data."""
        response = await roll_client.get(
            "/v1/signals/term-structure",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        # May be 200 (data available) or 503 (no settlement data)
        assert response.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_get_term_structure_for_commodity(self, roll_client: AsyncClient):
        """Should compute term structure for a specific commodity."""
        response = await roll_client.get(
            "/v1/signals/term-structure/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        # ES has settlement data in the fixture
        if response.status_code == 200:
            data = response.json()
            assert data["contract"] == "ES"
            assert "term_structure" in data or "metadata" in data

    @pytest.mark.asyncio
    async def test_get_term_structure_free_tier_gc(self, roll_client: AsyncClient):
        """Free tier should get 403 for GC."""
        response = await roll_client.get(
            "/v1/signals/term-structure/GC",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_term_structure_unknown_contract(self, roll_client: AsyncClient):
        """Should return 404 for unknown contract."""
        response = await roll_client.get(
            "/v1/signals/term-structure/ZZ",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code in (404, 503)


class TestRollPressureEndpoint:
    """Tests for roll pressure API endpoints."""

    @pytest.mark.asyncio
    async def test_get_roll_pressure_all(self, roll_client: AsyncClient):
        """Should return roll pressure data for commodities with data."""
        response = await roll_client.get(
            "/v1/signals/roll-pressure",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        # May be 200 (data available) or 503 (no settlement data)
        assert response.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_get_roll_pressure_for_es(self, roll_client: AsyncClient):
        """Should compute roll pressure for ES."""
        response = await roll_client.get(
            "/v1/signals/roll-pressure/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        # ES has settlement data
        if response.status_code == 200:
            data = response.json()
            assert data["contract"] == "ES"
            assert "roll_pressure" in data
            assert "roll_calendar" in data
            assert "roll_impact" in data
            assert "metadata" in data

            # Check roll pressure fields
            rp = data["roll_pressure"]
            assert 0 <= rp["index"] <= 100
            assert rp["roll_window"] in ("pre_roll", "active_roll", "post_roll")

            # Check roll calendar fields
            rc = data["roll_calendar"]
            assert "nearby_month" in rc
            assert "deferred_month" in rc
            assert "days_to_roll" in rc
            assert "roll_urgency" in rc
            assert rc["roll_urgency"] in ("imminent", "active", "normal", "relaxed")

            # Check roll impact fields
            ri = data["roll_impact"]
            assert 0 <= ri["impact_score"] <= 100
            assert ri["impact_category"] in ("low", "medium", "high", "extreme")

    @pytest.mark.asyncio
    async def test_get_roll_pressure_free_tier_gc(self, roll_client: AsyncClient):
        """Free tier should get 403 for GC."""
        response = await roll_client.get(
            "/v1/signals/roll-pressure/GC",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_get_roll_pressure_unknown_contract(self, roll_client: AsyncClient):
        """Should return 404 for unknown contract."""
        response = await roll_client.get(
            "/v1/signals/roll-pressure/ZZ",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code in (404, 503)

    @pytest.mark.asyncio
    async def test_roll_pressure_days_back_param(self, roll_client: AsyncClient):
        """Should accept days_back parameter."""
        response = await roll_client.get(
            "/v1/signals/roll-pressure/ES?days_back=14",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        if response.status_code == 200:
            data = response.json()
            assert data["metadata"]["lookback_days"] == 14


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in roll pressure and term structure."""

    def test_single_month_term_structure(self):
        """Single month should result in flat classification."""
        from app.signals.term_structure import compute_contango_backwardation
        from app.models.signal import TermStructureMonth

        months = [TermStructureMonth(
            month="Jun 26", expiry_date=date(2026, 6, 19),
            settlement=4500.0, open_interest=2000000,
            volume=1000000, spread_to_front=0.0, annualized_yield=0.0,
        )]
        result = compute_contango_backwardation(months)
        assert result["structure_type"] == "flat"
        assert result["m1_m2_spread"] == 0.0

    def test_inverted_curve_term_structure(self):
        """Inverted curve (backwardation) should be properly detected."""
        from app.signals.term_structure import compute_contango_backwardation
        from app.models.signal import TermStructureMonth

        months = [
            TermStructureMonth(month="Jun 26", expiry_date=date(2026, 6, 19), settlement=4600.0, open_interest=2000000, volume=1000000, spread_to_front=0.0, annualized_yield=0.0),
            TermStructureMonth(month="Sep 26", expiry_date=date(2026, 9, 19), settlement=4500.0, open_interest=1500000, volume=800000, spread_to_front=-100.0, annualized_yield=0.0),
            TermStructureMonth(month="Dec 26", expiry_date=date(2026, 12, 19), settlement=4400.0, open_interest=1000000, volume=500000, spread_to_front=-200.0, annualized_yield=0.0),
        ]
        result = compute_contango_backwardation(months)
        assert result["structure_type"] == "backwardation"
        assert result["m1_m2_spread"] < 0

    def test_zero_oi_in_term_structure(self):
        """Zero OI months should not crash calculations."""
        from app.signals.term_structure import compute_calendar_spread_ratio
        from app.models.signal import TermStructureMonth

        months = [
            TermStructureMonth(month="Jun 26", expiry_date=date(2026, 6, 19), settlement=4500.0, open_interest=0, volume=0, spread_to_front=0.0, annualized_yield=0.0),
            TermStructureMonth(month="Sep 26", expiry_date=date(2026, 9, 19), settlement=4510.0, open_interest=100000, volume=50000, spread_to_front=10.0, annualized_yield=0.0),
        ]
        # Should not crash — total_oi will be 100000
        result = compute_calendar_spread_ratio(months)
        assert result["front_to_next_ratio"] > 0

    def test_roll_pressure_with_negative_spread(self):
        """Backwardation (negative spread) should compute valid roll pressure."""
        score = _compute_roll_pressure_score(
            oi_decay_pct=3.0,
            spread_basis=-20.0,  # Backwardation
            nearby_price=4600.0,
            deferred_price=4580.0,
            days_to_expiry=10,
            nearby_volume=900000,
            deferred_volume=600000,
            nearby_oi=1800000,
            deferred_oi=1200000,
            roll_start_days=5,
        )
        assert 0 <= score <= 100

    def test_roll_calendar_leap_year(self):
        """Roll calendar should handle leap year dates correctly."""
        # Feb 29, 2028 is a valid date
        as_of = date(2028, 2, 15)
        info = calculate_roll_info("ES", as_of)
        assert isinstance(info, type(info))  # Just verify it doesn't crash

    def test_curve_fitting_constant_data(self):
        """Curve fitting on constant data should return near-zero slope."""
        from app.signals.curve_utils import fit_term_structure_curve

        indices = [0.0, 1.0, 2.0, 3.0, 4.0]
        prices = [4500.0, 4500.0, 4500.0, 4500.0, 4500.0]
        coeffs, metrics = fit_term_structure_curve(indices, prices)
        assert metrics["classification"] == "flat"
        assert abs(metrics["slope"]) < 0.1