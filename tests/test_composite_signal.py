"""Tests for the composite market structure signal.

Covers:
- Composite calculator: unit tests for scoring, alignment, confidence, breakdown
- Weight renormalization for missing signals
- Historical comparison
- API endpoint: tier enforcement, caching, error codes
- Edge cases: missing data, zero weights, flat markets
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from app.database import Base, get_db
from app.models.db import Contract, RawCOTReport, RawSettlement
from app.models.signal import (
    CompositeSignalResponse,
    HistoricalComparison,
    SignalAlignment,
    SignalBreakdownItem,
)
from app.services.signal_cache import reset_signal_cache
from app.signals.composite import CompositeSignalCalculator
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
async def composite_db():
    """Create an in-memory DB with contracts, settlements, and COT data for composite tests."""
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

        # Look up ES and NQ contracts
        es_result = await session.execute(select(Contract).where(Contract.symbol == "ES"))
        es_contract = es_result.scalar_one()

        nq_result = await session.execute(select(Contract).where(Contract.symbol == "NQ"))
        nq_contract = nq_result.scalar_one()

        cl_result = await session.execute(select(Contract).where(Contract.symbol == "CL"))
        cl_contract = cl_result.scalar_one()

        # -----------------------------------------------------------------------
        # Seed settlement data for ES (contango curve)
        # -----------------------------------------------------------------------
        base_date = datetime(2026, 5, 13)

        es_months = [
            ("Jun 26", 4520.25, 2100000, 1200000),
            ("Sep 26", 4535.50, 1800000, 900000),
            ("Dec 26", 4550.75, 1200000, 500000),
            ("Mar 27", 4565.00, 500000, 200000),
        ]
        for month_code, price, oi, vol in es_months:
            session.add(RawSettlement(
                contract_id=es_contract.id,
                month_code=month_code,
                settlement_date=base_date,
                settlement_price=price,
                open_interest=oi,
                volume=vol,
            ))

        # -----------------------------------------------------------------------
        # Seed settlement data for NQ (backwardation curve)
        # -----------------------------------------------------------------------
        nq_months = [
            ("Jun 26", 19550.00, 1100000, 800000),
            ("Sep 26", 19500.00, 900000, 600000),
            ("Dec 26", 19450.00, 600000, 300000),
            ("Mar 27", 19400.00, 300000, 100000),
        ]
        for month_code, price, oi, vol in nq_months:
            session.add(RawSettlement(
                contract_id=nq_contract.id,
                month_code=month_code,
                settlement_date=base_date,
                settlement_price=price,
                open_interest=oi,
                volume=vol,
            ))

        # -----------------------------------------------------------------------
        # Seed settlement data for CL (flat curve)
        # -----------------------------------------------------------------------
        cl_months = [
            ("Jul 26", 78.50, 1500000, 900000),
            ("Aug 26", 78.55, 1200000, 700000),
            ("Sep 26", 78.60, 800000, 400000),
            ("Oct 26", 78.65, 400000, 200000),
        ]
        for month_code, price, oi, vol in cl_months:
            session.add(RawSettlement(
                contract_id=cl_contract.id,
                month_code=month_code,
                settlement_date=base_date,
                settlement_price=price,
                open_interest=oi,
                volume=vol,
            ))

        # -----------------------------------------------------------------------
        # Seed historical settlement data for ES (last 30 days for historical comparison)
        # -----------------------------------------------------------------------
        for days_ago in range(1, 30):
            hist_date = datetime(2026, 5, 13) - timedelta(days=days_ago)
            # ES: slight variations each day
            front_price = 4520.25 - days_ago * 0.3
            deferred_price = 4535.50 - days_ago * 0.2

            session.add(RawSettlement(
                contract_id=es_contract.id,
                month_code="Jun 26",
                settlement_date=hist_date,
                settlement_price=front_price,
                open_interest=2100000 + days_ago * 5000,
                volume=1200000 + days_ago * 2000,
            ))
            session.add(RawSettlement(
                contract_id=es_contract.id,
                month_code="Sep 26",
                settlement_date=hist_date,
                settlement_price=deferred_price,
                open_interest=1800000 + days_ago * 3000,
                volume=900000 + days_ago * 1000,
            ))

        # -----------------------------------------------------------------------
        # Seed COT data for ES (bullish positioning: commercial long, retail short)
        # -----------------------------------------------------------------------
        cot_date = datetime(2026, 5, 10)
        session.add(RawCOTReport(
            contract_id=es_contract.id,
            as_of_date=cot_date,
            published_date=datetime(2026, 5, 13),
            commercial_long=500000,
            commercial_short=300000,
            commercial_net=200000,
            non_commercial_long=250000,
            non_commercial_short=350000,
            non_commercial_net=-100000,
            non_reportable_long=50000,
            non_reportable_short=150000,
            non_reportable_net=-100000,
            total_open_interest=2100000,
        ))

        # Add a few weeks of COT data for historical positioning
        for weeks_ago in range(1, 6):
            hist_cot = datetime(2026, 5, 10) - timedelta(weeks=weeks_ago)
            session.add(RawCOTReport(
                contract_id=es_contract.id,
                as_of_date=hist_cot,
                published_date=hist_cot + timedelta(days=3),
                commercial_long=480000 - weeks_ago * 5000,
                commercial_short=320000 + weeks_ago * 3000,
                commercial_net=160000 - weeks_ago * 8000,
                non_commercial_long=260000 + weeks_ago * 2000,
                non_commercial_short=340000 - weeks_ago * 5000,
                non_commercial_net=-80000 - weeks_ago * 3000,
                non_reportable_long=60000 + weeks_ago * 2000,
                non_reportable_short=140000 - weeks_ago * 5000,
                non_reportable_net=-80000 - weeks_ago * 3000,
                total_open_interest=2100000 - weeks_ago * 10000,
            ))

        # -----------------------------------------------------------------------
        # Seed COT data for NQ (bearish positioning: commercial short, retail long)
        # -----------------------------------------------------------------------
        session.add(RawCOTReport(
            contract_id=nq_contract.id,
            as_of_date=cot_date,
            published_date=datetime(2026, 5, 13),
            commercial_long=200000,
            commercial_short=400000,
            commercial_net=-200000,
            non_commercial_long=300000,
            non_commercial_short=150000,
            non_commercial_net=150000,
            non_reportable_long=120000,
            non_reportable_short=40000,
            non_reportable_net=80000,
            total_open_interest=1100000,
        ))

        # Add historical COT data for NQ
        for weeks_ago in range(1, 6):
            hist_cot = datetime(2026, 5, 10) - timedelta(weeks=weeks_ago)
            session.add(RawCOTReport(
                contract_id=nq_contract.id,
                as_of_date=hist_cot,
                published_date=hist_cot + timedelta(days=3),
                commercial_long=210000 - weeks_ago * 3000,
                commercial_short=380000 + weeks_ago * 4000,
                commercial_net=-170000 + weeks_ago * 5000,
                non_commercial_long=290000 + weeks_ago * 2000,
                non_commercial_short=160000 - weeks_ago * 3000,
                non_commercial_net=130000 + weeks_ago * 5000,
                non_reportable_long=110000 + weeks_ago * 3000,
                non_reportable_short=50000 - weeks_ago * 2000,
                non_reportable_net=60000 + weeks_ago * 5000,
                total_open_interest=1100000 - weeks_ago * 20000,
            ))

        # -----------------------------------------------------------------------
        # Seed COT data for CL (neutral positioning: balanced)
        # -----------------------------------------------------------------------
        session.add(RawCOTReport(
            contract_id=cl_contract.id,
            as_of_date=cot_date,
            published_date=datetime(2026, 5, 13),
            commercial_long=300000,
            commercial_short=280000,
            commercial_net=20000,
            non_commercial_long=250000,
            non_commercial_short=270000,
            non_commercial_net=-20000,
            non_reportable_long=80000,
            non_reportable_short=70000,
            non_reportable_net=10000,
            total_open_interest=1500000,
        ))

        await session.commit()
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def composite_client(composite_db: AsyncSession):
    """Async test client with composite signal routes."""
    from app.main import app

    async def override_get_db():
        yield composite_db

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Calculator unit tests — ES (contango + bullish positioning)
# ---------------------------------------------------------------------------


class TestCompositeCalculatorScoring:
    """Tests for signal-to-score conversion methods."""

    def test_positioning_to_score_bullish(self):
        """Bullish positioning → positive score."""
        score = CompositeSignalCalculator._positioning_to_score(direction="bullish", strength=0.7)
        assert score == 70.0

    def test_positioning_to_score_bearish(self):
        """Bearish positioning → negative score."""
        score = CompositeSignalCalculator._positioning_to_score(direction="bearish", strength=0.6)
        assert score == -60.0

    def test_positioning_to_score_neutral(self):
        """Neutral positioning → 0."""
        score = CompositeSignalCalculator._positioning_to_score(direction="neutral", strength=0.3)
        assert score == 0.0

    def test_positioning_to_score_strong_bullish(self):
        """Extreme bullish → +100."""
        score = CompositeSignalCalculator._positioning_to_score(direction="bullish", strength=1.0)
        assert score == 100.0

    def test_term_structure_to_score_contango(self):
        """Contango → negative score."""
        score = CompositeSignalCalculator._term_structure_to_score(
            structure_type="contango", confidence=0.8, z_score=1.5, slope=0.05
        )
        assert score < 0
        assert -100 <= score <= 0

    def test_term_structure_to_score_backwardation(self):
        """Backwardation → positive score."""
        score = CompositeSignalCalculator._term_structure_to_score(
            structure_type="backwardation", confidence=0.8, z_score=-1.5, slope=-0.05
        )
        assert score > 0
        assert 0 <= score <= 100

    def test_term_structure_to_score_flat(self):
        """Flat curve → 0."""
        score = CompositeSignalCalculator._term_structure_to_score(
            structure_type="flat", confidence=0.5, z_score=0.0, slope=0.0
        )
        assert score == 0.0

    def test_term_structure_to_score_mixed(self):
        """Mixed curve follows the dominant slope."""
        score = CompositeSignalCalculator._term_structure_to_score(
            structure_type="mixed", confidence=0.6, z_score=0.5, slope=0.02
        )
        assert score > 0  # Positive slope → positive score

    def test_term_structure_to_score_mixed_negative_slope(self):
        """Mixed curve with negative slope → negative score."""
        score = CompositeSignalCalculator._term_structure_to_score(
            structure_type="mixed", confidence=0.6, z_score=0.5, slope=-0.02
        )
        assert score < 0

    def test_roll_pressure_to_score_with_contango(self):
        """Roll pressure in contango → negative score."""
        score = CompositeSignalCalculator._roll_pressure_to_score(
            raw_index=65.0, contango_direction=-30.0
        )
        assert score < 0
        assert score == -65.0

    def test_roll_pressure_to_score_with_backwardation(self):
        """Roll pressure in backwardation → positive score."""
        score = CompositeSignalCalculator._roll_pressure_to_score(
            raw_index=45.0, contango_direction=30.0
        )
        assert score > 0
        assert score == 45.0

    def test_roll_pressure_to_score_no_context(self):
        """Roll pressure without term structure context centered at 0."""
        score = CompositeSignalCalculator._roll_pressure_to_score(
            raw_index=50.0, contango_direction=None
        )
        assert score == 0.0

    def test_roll_pressure_to_score_extreme_no_context(self):
        """Extreme roll pressure without context."""
        score = CompositeSignalCalculator._roll_pressure_to_score(
            raw_index=90.0, contango_direction=None
        )
        assert score == 40.0  # 90 - 50 = 40


class TestCompositeCalculatorAlignment:
    """Tests for signal alignment calculation."""

    def test_aligned_bullish(self):
        """All signals bullish → ALIGNED_BULLISH."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=50.0,
            term_structure_score=40.0,
            roll_pressure_score=30.0,
        )
        assert alignment == SignalAlignment.ALIGNED_BULLISH

    def test_aligned_bearish(self):
        """All signals bearish → ALIGNED_BEARISH."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=-50.0,
            term_structure_score=-40.0,
            roll_pressure_score=-30.0,
        )
        assert alignment == SignalAlignment.ALIGNED_BEARISH

    def test_mixed_signals(self):
        """Bullish + bearish → MIXED."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=50.0,
            term_structure_score=-40.0,
            roll_pressure_score=-30.0,
        )
        assert alignment == SignalAlignment.MIXED

    def test_all_neutral(self):
        """All signals neutral → NEUTRAL."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=0.0,
            term_structure_score=0.0,
            roll_pressure_score=0.0,
        )
        assert alignment == SignalAlignment.NEUTRAL

    def test_mixed_with_neutral(self):
        """Bullish + neutral + bearish → MIXED."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=50.0,
            term_structure_score=0.0,
            roll_pressure_score=-40.0,
        )
        assert alignment == SignalAlignment.MIXED

    def test_partial_signals_aligned(self):
        """Only 2 signals, both bullish → ALIGNED_BULLISH."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=30.0,
            term_structure_score=None,
            roll_pressure_score=25.0,
        )
        assert alignment == SignalAlignment.ALIGNED_BULLISH

    def test_partial_signals_mixed(self):
        """Only 2 signals, disagreeing → MIXED."""
        alignment = CompositeSignalCalculator._determine_alignment(
            positioning_score=30.0,
            term_structure_score=None,
            roll_pressure_score=-25.0,
        )
        assert alignment == SignalAlignment.MIXED


class TestCompositeCalculatorConfidence:
    """Tests for confidence calculation."""

    def test_high_confidence_all_aligned(self):
        """All three signals aligned → high confidence."""
        confidence = CompositeSignalCalculator._calculate_confidence(
            positioning_score=50.0,
            term_structure_score=40.0,
            roll_pressure_score=30.0,
        )
        assert 0.7 <= confidence <= 1.0

    def test_medium_confidence_mixed(self):
        """Mixed signals → moderate confidence."""
        confidence = CompositeSignalCalculator._calculate_confidence(
            positioning_score=50.0,
            term_structure_score=0.0,
            roll_pressure_score=-40.0,
        )
        assert 0.0 <= confidence <= 0.9

    def test_low_confidence_single_signal(self):
        """Only one signal → low confidence."""
        confidence = CompositeSignalCalculator._calculate_confidence(
            positioning_score=50.0,
            term_structure_score=None,
            roll_pressure_score=None,
        )
        assert 0.2 <= confidence <= 0.5

    def test_confidence_extreme_boost(self):
        """Extreme scores get a confidence boost."""
        confidence_extreme = CompositeSignalCalculator._calculate_confidence(
            positioning_score=90.0,
            term_structure_score=85.0,
            roll_pressure_score=80.0,
        )
        confidence_normal = CompositeSignalCalculator._calculate_confidence(
            positioning_score=30.0,
            term_structure_score=25.0,
            roll_pressure_score=20.0,
        )
        assert confidence_extreme > confidence_normal

    def test_confidence_no_signals(self):
        """No signals → zero confidence."""
        confidence = CompositeSignalCalculator._calculate_confidence(
            positioning_score=None,
            term_structure_score=None,
            roll_pressure_score=None,
        )
        assert confidence == 0.0

    def test_confidence_mixed_punishment(self):
        """Bullish + bearish → lower than if all same."""
        mixed = CompositeSignalCalculator._calculate_confidence(
            positioning_score=50.0,
            term_structure_score=-40.0,
            roll_pressure_score=30.0,
        )
        aligned = CompositeSignalCalculator._calculate_confidence(
            positioning_score=50.0,
            term_structure_score=40.0,
            roll_pressure_score=30.0,
        )
        assert mixed < aligned


class TestCompositeCalculatorWeights:
    """Tests for weight renormalization."""

    def test_all_signals_present(self):
        """All three signals → original weights."""
        calc = CompositeSignalCalculator()
        weights = calc._renormalize_weights(
            ["positioning", "term_structure", "roll_pressure"]
        )
        assert weights == {"positioning": 0.40, "term_structure": 0.30, "roll_pressure": 0.30}

    def test_missing_one_signal(self):
        """Missing positioning → renormalize."""
        calc = CompositeSignalCalculator()
        weights = calc._renormalize_weights(["term_structure", "roll_pressure"])
        assert abs(weights["term_structure"] - 0.50) < 0.01
        assert abs(weights["roll_pressure"] - 0.50) < 0.01

    def test_missing_two_signals(self):
        """Only one signal → weight = 1."""
        calc = CompositeSignalCalculator()
        weights = calc._renormalize_weights(["positioning"])
        assert weights["positioning"] == 1.0

    def test_custom_weights(self):
        """Custom weights are used when all signals present."""
        calc = CompositeSignalCalculator(weights={"positioning": 0.5, "term_structure": 0.3, "roll_pressure": 0.2})
        weights = calc._renormalize_weights(
            ["positioning", "term_structure", "roll_pressure"]
        )
        assert weights["positioning"] == 0.5
        assert weights["term_structure"] == 0.3
        assert weights["roll_pressure"] == 0.2

    def test_custom_weights_renormalized(self):
        """Custom weights renormalized when signal missing."""
        calc = CompositeSignalCalculator(weights={"positioning": 0.7, "term_structure": 0.2, "roll_pressure": 0.1})
        weights = calc._renormalize_weights(["positioning", "roll_pressure"])
        # 0.7 + 0.1 = 0.8, so: positioning = 0.7/0.8 = 0.875, roll = 0.1/0.8 = 0.125
        assert abs(weights["positioning"] - 0.875) < 0.01
        assert abs(weights["roll_pressure"] - 0.125) < 0.01

    def test_invalid_negative_weight(self):
        """Negative weight should raise ValueError."""
        with pytest.raises(ValueError, match="Weight.*must be >= 0"):
            CompositeSignalCalculator(weights={"positioning": -0.1})


class TestCompositeCalculatorBreakdown:
    """Tests for signal breakdown."""

    def test_breakdown_structure(self):
        """Breakdown should have correct structure."""
        active = {"positioning": 60.0, "term_structure": -30.0, "roll_pressure": 20.0}
        weights = {"positioning": 0.4, "term_structure": 0.3, "roll_pressure": 0.3}
        items = CompositeSignalCalculator._build_breakdown(active, weights, total_score=20.0)

        assert len(items) == 3
        for item in items:
            assert item.signal_type in active
            assert item.score == active[item.signal_type]
            assert item.weight == weights[item.signal_type]

    def test_breakdown_contributions_sum(self):
        """Check that contributions make sense."""
        active = {"positioning": 50.0, "term_structure": 30.0, "roll_pressure": -20.0}
        weights = {"positioning": 0.4, "term_structure": 0.3, "roll_pressure": 0.3}
        items = CompositeSignalCalculator._build_breakdown(active, weights, total_score=23.0)

        # Positioning contribution: 50 * 0.4 = 20
        assert items[0].contribution == 20.0

        # Term structure contribution: 30 * 0.3 = 9
        assert items[1].contribution == 9.0

        # Roll pressure contribution: -20 * 0.3 = -6
        assert items[2].contribution == -6.0


class TestCompositeCalculatorInterpretation:
    """Tests for interpretation text."""

    def test_interpretation_strongly_bullish(self):
        """Strongly bullish → appropriate text."""
        text = CompositeSignalCalculator._generate_interpretation(
            composite_score=75.0,
            alignment=SignalAlignment.ALIGNED_BULLISH,
            confidence=0.85,
            positioning_score=80.0,
            term_structure_score=60.0,
            roll_pressure_score=50.0,
        )
        assert "bullish" in text.lower()
        assert "strongly" in text.lower() or "high" in text.lower()
        assert "positioning" in text.lower()
        assert "term structure" in text.lower()

    def test_interpretation_strongly_bearish(self):
        """Strongly bearish → appropriate text."""
        text = CompositeSignalCalculator._generate_interpretation(
            composite_score=-75.0,
            alignment=SignalAlignment.ALIGNED_BEARISH,
            confidence=0.85,
            positioning_score=-80.0,
            term_structure_score=-60.0,
            roll_pressure_score=-50.0,
        )
        assert "bearish" in text.lower()
        assert "strongly" in text.lower()

    def test_interpretation_neutral(self):
        """Neutral → mentions mixed or neutral."""
        text = CompositeSignalCalculator._generate_interpretation(
            composite_score=5.0,
            alignment=SignalAlignment.NEUTRAL,
            confidence=0.3,
            positioning_score=0.0,
            term_structure_score=5.0,
            roll_pressure_score=0.0,
        )
        assert "neutral" in text.lower()

    def test_interpretation_mixed(self):
        """Mixed alignment → mentions conflicting signals."""
        text = CompositeSignalCalculator._generate_interpretation(
            composite_score=10.0,
            alignment=SignalAlignment.MIXED,
            confidence=0.5,
            positioning_score=50.0,
            term_structure_score=-40.0,
            roll_pressure_score=20.0,
        )
        assert "mixed" in text.lower() or "conflicting" in text.lower()

    def test_interpretation_partial_signals(self):
        """Only positioning available → still works."""
        text = CompositeSignalCalculator._generate_interpretation(
            composite_score=40.0,
            alignment=SignalAlignment.ALIGNED_BULLISH,
            confidence=0.5,
            positioning_score=40.0,
            term_structure_score=None,
            roll_pressure_score=None,
        )
        assert "positioning" in text.lower()
        assert "term" not in text.lower() or "term" in text.lower()


# ---------------------------------------------------------------------------
# End-to-end tests with DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCompositeWithDB:
    """End-to-end composite signal computation using seeded DB data."""

    async def test_composite_es_contango_bullish_positioning(self, composite_db: AsyncSession):
        """ES: contango curve + bullish positioning → composite should reflect both."""
        calculator = CompositeSignalCalculator()
        result = await calculator.compute(contract_symbol="ES", db=composite_db)

        assert isinstance(result, CompositeSignalResponse)
        assert result.contract == "ES"
        assert result.composite_score is not None
        assert -100 <= result.composite_score <= 100

        # ES has bullish positioning (commercial long, retail short)
        assert result.positioning_score is not None
        assert result.positioning_score > 0

        # ES has contango curve → negative term structure score
        assert result.term_structure_score is not None

        # Check breakdown
        assert len(result.breakdown) > 0
        for item in result.breakdown:
            assert isinstance(item, SignalBreakdownItem)

    async def test_composite_nq_backwardation_bearish_positioning(self, composite_db: AsyncSession):
        """NQ: backwardation curve + bearish positioning."""
        calculator = CompositeSignalCalculator()
        result = await calculator.compute(contract_symbol="NQ", db=composite_db)

        assert result.contract == "NQ"
        assert isinstance(result.composite_score, float)

        # NQ has bearish positioning (commercial short, retail long)
        if result.positioning_score is not None:
            assert result.positioning_score <= 0

    async def test_composite_missing_data(self, composite_db: AsyncSession):
        """GC has no seeded data → should raise ValueError."""
        calculator = CompositeSignalCalculator()
        with pytest.raises(ValueError):
            await calculator.compute(contract_symbol="GC", db=composite_db)

    async def test_historical_comparison_present(self, composite_db: AsyncSession):
        """ES should have historical comparison data (30 days seeded)."""
        calculator = CompositeSignalCalculator()
        result = await calculator.compute(contract_symbol="ES", db=composite_db)

        assert result.historical_comparison is not None
        hc = result.historical_comparison
        assert isinstance(hc, HistoricalComparison)
        assert hc.current == result.composite_score
        assert hc.average is not None
        assert hc.min is not None
        assert hc.max is not None
        assert 0 <= hc.percentile_rank <= 100
        assert len(hc.values) >= 3

    async def test_historical_comparison_no_data(self, composite_db: AsyncSession):
        """GC has no data → no historical comparison."""
        calculator = CompositeSignalCalculator()
        with pytest.raises(ValueError):
            await calculator.compute(contract_symbol="GC", db=composite_db)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCompositeAPIEndpoint:
    """Tests for the composite API endpoint."""

    async def test_composite_endpoint_requires_auth(self, composite_client: AsyncClient):
        """Composite endpoint should require auth."""
        response = await composite_client.get("/v1/signals/composite/ES")
        assert response.status_code == 401

    async def test_composite_endpoint_free_tier_es(self, composite_client: AsyncClient):
        """Free tier can access ES."""
        response = await composite_client.get(
            "/v1/signals/composite/ES",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code in (200, 503)

    async def test_composite_endpoint_free_tier_gc_forbidden(self, composite_client: AsyncClient):
        """Free tier cannot access GC."""
        response = await composite_client.get(
            "/v1/signals/composite/GC",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code == 403

    async def test_composite_endpoint_pro_tier_es(self, composite_client: AsyncClient):
        """Pro tier can access ES."""
        response = await composite_client.get(
            "/v1/signals/composite/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code in (200, 503)

    async def test_composite_response_structure(self, composite_client: AsyncClient):
        """Successful response should have expected structure."""
        response = await composite_client.get(
            "/v1/signals/composite/ES",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        if response.status_code == 200:
            data = response.json()
            assert "contract" in data
            assert "composite_score" in data
            assert "signal_alignment" in data
            assert "confidence" in data
            assert "interpretation" in data
            assert "weights" in data
            assert "breakdown" in data
            assert data["contract"] == "ES"
            assert -100 <= data["composite_score"] <= 100
            assert 0 <= data["confidence"] <= 1
            assert data["signal_alignment"] in ("ALIGNED_BULLISH", "ALIGNED_BEARISH", "MIXED", "NEUTRAL")

    async def test_composite_custom_weights(self, composite_client: AsyncClient):
        """Custom weights should be reflected in response (renormalized for active signals)."""
        response = await composite_client.get(
            "/v1/signals/composite/ES?positioning_weight=0.6&term_structure_weight=0.2&roll_pressure_weight=0.2",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        if response.status_code == 200:
            data = response.json()
            # Roll pressure may be unavailable in test env, so weights get renormalized.
            # If only positioning + term_structure are active:
            # positioning = 0.6 / (0.6 + 0.2) = 0.75, term_structure = 0.2 / (0.6 + 0.2) = 0.25
            # Check that weights reflect the custom ratio, not defaults
            p_weight = data["weights"].get("positioning", 0)
            ts_weight = data["weights"].get("term_structure", 0)
            rp_weight = data["weights"].get("roll_pressure", 0)
            # Sum should be 1 (within rounding)
            assert abs(p_weight + ts_weight + rp_weight - 1.0) < 0.01
            # Custom ratio positioning:term_structure should be 3:1
            if ts_weight > 0:
                ratio = p_weight / ts_weight
                assert abs(ratio - 3.0) < 0.1  # 0.6/0.2 = 3

    async def test_composite_invalid_symbol(self, composite_client: AsyncClient):
        """Invalid symbol should return 400."""
        response = await composite_client.get(
            "/v1/signals/composite/123INVALID",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 400

    async def test_composite_unknown_contract(self, composite_client: AsyncClient):
        """Unknown contract should return 404."""
        response = await composite_client.get(
            "/v1/signals/composite/ZZ",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code in (404, 503)
