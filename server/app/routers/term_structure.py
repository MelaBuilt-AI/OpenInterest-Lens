"""Term structure and roll pressure API router.

Endpoints:
- GET /v1/signals/term-structure — compute term structure for all tracked commodities
- GET /v1/signals/term-structure/{commodity} — compute for specific commodity
- GET /v1/signals/roll-pressure — compute roll pressure for all tracked commodities
- GET /v1/signals/roll-pressure/{commodity} — compute for specific commodity
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.models.db import Contract
from app.models.term_structure import (
    CalendarSpreadResult,
    ContangoBackwardationResult,
    MultiRollPressureResponse,
    MultiTermStructureResponse,
    RollCalendarData,
    RollImpactData,
    RollPressureData,
    RollPressureMetadata,
    RollPressureResponse,
    SlopeMetricsResult,
    TermStructureCurveFromSignal,
    TermStructureMetadata,
    TermStructureMonthData,
    TermStructureResponse,
)
from app.services.signal_cache import get_signal_cache
from app.signals.roll_calendar import calculate_roll_info
from app.signals.roll_pressure import (
    compute_roll_impact_score,
    compute_roll_pressure,
)
from app.signals.term_structure import (
    compute_calendar_spread_ratio,
    compute_contango_backwardation,
    compute_term_structure,
    compute_term_structure_slope,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


# ---------------------------------------------------------------------------
# GET /v1/signals/term-structure — all commodities
# ---------------------------------------------------------------------------


@router.get("/term-structure", response_model=MultiTermStructureResponse)
async def get_term_structure_all(
    date: Optional[str] = Query(None, description="As-of date (YYYY-MM-DD)"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute term structure for all tracked commodities.

    Returns contango/backwardation indicators, slope metrics,
    and calendar spread ratios for each commodity with data.

    Tier enforcement:
    - Free: ES, NQ, CL term structure (current only)
    - Pro: All commodities, historical data
    - Enterprise: All commodities, full history
    """
    # Parse date
    as_of_date = None
    if date:
        try:
            as_of_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "Date must be in YYYY-MM-DD format."},
            )

    # Get all active contracts
    result = await db.execute(select(Contract.symbol).where(Contract.is_active.is_(True)))
    symbols = [s for (s,) in result.all()]

    # Filter by tier
    accessible_symbols = [s for s in symbols if tier_info.can_access_contract(s)]
    if not accessible_symbols:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "No accessible contracts found for your tier."},
        )

    # Compute term structure for each accessible commodity
    results: list[TermStructureResponse] = []
    cache = get_signal_cache()

    for sym in accessible_symbols:
        # Check cache
        cache_key = cache.make_key(sym, "term_structure")
        cached = cache.get(cache_key)
        if cached is not None:
            cached.metadata.cache_hit = True
            results.append(cached)
            continue

        try:
            ts = await compute_term_structure(
                contract_symbol=sym,
                db=db,
                as_of_date=as_of_date,
            )

            # Compute contango/backwardation indicators
            cb = compute_contango_backwardation(ts.months)

            # Compute slope metrics
            slope = compute_term_structure_slope(ts.months)

            # Compute calendar spread ratios
            spreads = compute_calendar_spread_ratio(ts.months)

            # Build response
            months_data = [
                TermStructureMonthData(
                    month=m.month,
                    expiry_date=m.expiry_date,
                    settlement=m.settlement,
                    open_interest=m.open_interest,
                    volume=m.volume,
                    spread_to_front=m.spread_to_front,
                    annualized_yield=m.annualized_yield,
                )
                for m in ts.months
            ]

            response = TermStructureResponse(
                contract=sym,
                term_structure=TermStructureCurveFromSignal(
                    structure_type=ts.structure_type,
                    months=months_data,
                    front_month_oi=ts.curve_metrics.front_month_oi if ts.curve_metrics else 0,
                    total_oi=ts.curve_metrics.total_oi if ts.curve_metrics else 0,
                    oi_concentration_pct=ts.curve_metrics.oi_concentration_pct if ts.curve_metrics else 0.0,
                    steepness=ts.curve_metrics.steepness if ts.curve_metrics else 0.0,
                ),
                contango_backwardation=ContangoBackwardationResult(
                    structure_type=cb["structure_type"],
                    m1_m2_spread=cb["m1_m2_spread"],
                    m1_m2_annualized=cb["m1_m2_annualized"],
                    spread_z_score=cb["spread_z_score"],
                    confidence=cb["confidence"],
                    slope=cb["slope"],
                ),
                slope_metrics=SlopeMetricsResult(
                    nearby_deferred_spread=slope["nearby_deferred_spread"],
                    slope_annualized_pct=slope["slope_annualized_pct"],
                    linear_slope=slope["linear_slope"],
                    quadratic_curvature=slope["quadratic_curvature"],
                    r_squared_linear=slope["r_squared_linear"],
                    r_squared_quadratic=slope["r_squared_quadratic"],
                ),
                calendar_spread_ratios=CalendarSpreadResult(
                    front_to_next_ratio=spreads["front_to_next_ratio"],
                    front_to_deferred_ratio=spreads["front_to_deferred_ratio"],
                    average_monthly_spread_pct=spreads["average_monthly_spread_pct"],
                    max_spread_pct=spreads["max_spread_pct"],
                ),
                metadata=TermStructureMetadata(
                    commodity=sym,
                    as_of_date=ts.as_of_date,
                    data_points=len(ts.months),
                    computed_at=datetime.now(timezone.utc),
                    cache_hit=False,
                ),
            )

            # Cache the result
            cache.set(cache_key, response)
            results.append(response)

        except ValueError as e:
            logger.warning("term_structure_compute_failed", symbol=sym, error=str(e))
            continue

    return MultiTermStructureResponse(
        commodities=results,
        computed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /v1/signals/term-structure/{commodity} — specific commodity
# ---------------------------------------------------------------------------


@router.get("/term-structure/{commodity}", response_model=TermStructureResponse)
async def get_term_structure_for_commodity(
    commodity: str,
    date: Optional[str] = Query(None, description="As-of date (YYYY-MM-DD)"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute term structure for a specific commodity.

    Returns the full term structure curve with contango/backwardation
    indicators, slope metrics, and calendar spread ratios.

    Tier enforcement:
    - Free: ES, NQ, CL only
    - Pro/Enterprise: All commodities
    """
    commodity = commodity.upper()

    # Tier enforcement
    if not tier_info.can_access_contract(commodity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{commodity}' is not available on your tier. Upgrade for full access.",
            },
        )

    # Parse date
    as_of_date = None
    if date:
        try:
            as_of_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "Date must be in YYYY-MM-DD format."},
            )

    # Check cache
    cache = get_signal_cache()
    cache_key = cache.make_key(commodity, "term_structure")
    cached = cache.get(cache_key)
    if cached is not None:
        cached.metadata.cache_hit = True
        return cached

    # Compute term structure
    try:
        ts = await compute_term_structure(
            contract_symbol=commodity,
            db=db,
            as_of_date=as_of_date,
        )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{commodity}' is not tracked."},
            )
        elif "No settlement data" in error_msg or "Insufficient" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "data_unavailable", "message": f"No settlement data available for '{commodity}'. Ingest data first."},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "signal_error", "message": error_msg},
            )

    # Compute sub-metrics
    cb = compute_contango_backwardation(ts.months)
    slope = compute_term_structure_slope(ts.months)
    spreads = compute_calendar_spread_ratio(ts.months)

    # Build response
    months_data = [
        TermStructureMonthData(
            month=m.month,
            expiry_date=m.expiry_date,
            settlement=m.settlement,
            open_interest=m.open_interest,
            volume=m.volume,
            spread_to_front=m.spread_to_front,
            annualized_yield=m.annualized_yield,
        )
        for m in ts.months
    ]

    response = TermStructureResponse(
        contract=commodity,
        term_structure=TermStructureCurveFromSignal(
            structure_type=ts.structure_type,
            months=months_data,
            front_month_oi=ts.curve_metrics.front_month_oi if ts.curve_metrics else 0,
            total_oi=ts.curve_metrics.total_oi if ts.curve_metrics else 0,
            oi_concentration_pct=ts.curve_metrics.oi_concentration_pct if ts.curve_metrics else 0.0,
            steepness=ts.curve_metrics.steepness if ts.curve_metrics else 0.0,
        ),
        contango_backwardation=ContangoBackwardationResult(
            structure_type=cb["structure_type"],
            m1_m2_spread=cb["m1_m2_spread"],
            m1_m2_annualized=cb["m1_m2_annualized"],
            spread_z_score=cb["spread_z_score"],
            confidence=cb["confidence"],
            slope=cb["slope"],
        ),
        slope_metrics=SlopeMetricsResult(
            nearby_deferred_spread=slope["nearby_deferred_spread"],
            slope_annualized_pct=slope["slope_annualized_pct"],
            linear_slope=slope["linear_slope"],
            quadratic_curvature=slope["quadratic_curvature"],
            r_squared_linear=slope["r_squared_linear"],
            r_squared_quadratic=slope["r_squared_quadratic"],
        ),
        calendar_spread_ratios=CalendarSpreadResult(
            front_to_next_ratio=spreads["front_to_next_ratio"],
            front_to_deferred_ratio=spreads["front_to_deferred_ratio"],
            average_monthly_spread_pct=spreads["average_monthly_spread_pct"],
            max_spread_pct=spreads["max_spread_pct"],
        ),
        metadata=TermStructureMetadata(
            commodity=commodity,
            as_of_date=ts.as_of_date,
            data_points=len(ts.months),
            computed_at=datetime.now(timezone.utc),
            cache_hit=False,
        ),
    )

    # Cache the result
    cache.set(cache_key, response)

    return response


# ---------------------------------------------------------------------------
# GET /v1/signals/roll-pressure — all commodities
# ---------------------------------------------------------------------------


@router.get("/roll-pressure", response_model=MultiRollPressureResponse)
async def get_roll_pressure_all(
    days_back: int = Query(30, ge=1, le=365, description="Days of history for OI decay analysis"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute roll pressure for all tracked commodities.

    Returns roll pressure index, roll calendar info, and impact
    estimation for each commodity with data.

    Tier enforcement:
    - Free: ES, NQ, CL roll pressure only
    - Pro/Enterprise: All commodities
    """
    # Get all active contracts
    result = await db.execute(select(Contract.symbol).where(Contract.is_active.is_(True)))
    symbols = [s for (s,) in result.all()]

    # Filter by tier
    accessible_symbols = [s for s in symbols if tier_info.can_access_contract(s)]
    if not accessible_symbols:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "No accessible contracts found for your tier."},
        )

    # Compute roll pressure for each accessible commodity
    results: list[RollPressureResponse] = []
    cache = get_signal_cache()
    as_of_date = date.today()

    for sym in accessible_symbols:
        # Check cache
        cache_key = cache.make_key(sym, "roll_pressure")
        cached = cache.get(cache_key)
        if cached is not None:
            cached.metadata.cache_hit = True
            results.append(cached)
            continue

        try:
            rp = await compute_roll_pressure(
                contract_symbol=sym,
                db=db,
                days_back=days_back,
            )

            # Get roll calendar info
            roll_info = calculate_roll_info(sym, as_of_date)

            # Compute roll impact
            impact = compute_roll_impact_score(
                nearby_oi=rp.nearby.open_interest,
                deferred_oi=rp.deferred.open_interest,
                nearby_volume=rp.nearby.volume,
                deferred_volume=rp.deferred.volume,
                spread_basis=rp.roll_pressure.spread_basis,
                days_to_expiry=rp.roll_pressure.days_to_expiry,
                contract_symbol=sym,
            )

            # Build roll calendar data
            roll_calendar = RollCalendarData(
                nearby_month=roll_info.nearby_month_code,
                nearby_expiry=roll_info.nearby_expiry,
                deferred_month=roll_info.deferred_month_code,
                deferred_expiry=roll_info.deferred_expiry,
                days_to_roll=roll_info.days_to_roll,
                roll_start_date=roll_info.roll_start_date,
                roll_end_date=roll_info.roll_end_date,
                roll_urgency=roll_info.roll_urgency,  # type: ignore
            )

            # Build roll impact data
            roll_impact = RollImpactData(
                impact_score=impact["impact_score"],
                oi_concentration=impact["oi_concentration"],
                volume_shift=impact["volume_shift"],
                expected_slippage=impact["expected_slippage"],
                impact_category=impact["impact_category"],  # type: ignore
            )

            response = RollPressureResponse(
                contract=sym,
                roll_pressure=RollPressureData(
                    index=rp.roll_pressure.index,
                    oi_decay_pct=rp.roll_pressure.oi_decay_pct,
                    spread_basis=rp.roll_pressure.spread_basis,
                    days_to_expiry=rp.roll_pressure.days_to_expiry,
                    roll_window=rp.roll_pressure.roll_window,
                ),
                roll_calendar=roll_calendar,
                roll_impact=roll_impact,
                metadata=RollPressureMetadata(
                    commodity=sym,
                    as_of_date=as_of_date,
                    lookback_days=days_back,
                    computed_at=datetime.now(timezone.utc),
                    cache_hit=False,
                ),
            )

            # Cache the result
            cache.set(cache_key, response)
            results.append(response)

        except ValueError as e:
            logger.warning("roll_pressure_compute_failed", symbol=sym, error=str(e))
            continue

    return MultiRollPressureResponse(
        commodities=results,
        computed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /v1/signals/roll-pressure/{commodity} — specific commodity
# ---------------------------------------------------------------------------


@router.get("/roll-pressure/{commodity}", response_model=RollPressureResponse)
async def get_roll_pressure_for_commodity(
    commodity: str,
    days_back: int = Query(30, ge=1, le=365, description="Days of history for OI decay analysis"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute roll pressure for a specific commodity.

    Returns the roll pressure index, roll calendar, and impact
    estimation for the specified commodity.

    Tier enforcement:
    - Free: ES, NQ, CL only
    - Pro/Enterprise: All commodities
    """
    commodity = commodity.upper()

    # Tier enforcement
    if not tier_info.can_access_contract(commodity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{commodity}' is not available on your tier. Upgrade for full access.",
            },
        )

    # Check cache
    cache = get_signal_cache()
    cache_key = cache.make_key(commodity, "roll_pressure")
    cached = cache.get(cache_key)
    if cached is not None:
        cached.metadata.cache_hit = True
        return cached

    # Compute roll pressure
    try:
        rp = await compute_roll_pressure(
            contract_symbol=commodity,
            db=db,
            days_back=days_back,
        )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{commodity}' is not tracked."},
            )
        elif "No settlement data" in error_msg or "Insufficient" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "data_unavailable", "message": f"No settlement data available for '{commodity}'. Ingest data first."},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "signal_error", "message": error_msg},
            )

    # Get roll calendar info
    as_of_date = date.today()
    roll_info = calculate_roll_info(commodity, as_of_date)

    # Compute roll impact
    impact = compute_roll_impact_score(
        nearby_oi=rp.nearby.open_interest,
        deferred_oi=rp.deferred.open_interest,
        nearby_volume=rp.nearby.volume,
        deferred_volume=rp.deferred.volume,
        spread_basis=rp.roll_pressure.spread_basis,
        days_to_expiry=rp.roll_pressure.days_to_expiry,
        contract_symbol=commodity,
    )

    # Build response
    roll_calendar = RollCalendarData(
        nearby_month=roll_info.nearby_month_code,
        nearby_expiry=roll_info.nearby_expiry,
        deferred_month=roll_info.deferred_month_code,
        deferred_expiry=roll_info.deferred_expiry,
        days_to_roll=roll_info.days_to_roll,
        roll_start_date=roll_info.roll_start_date,
        roll_end_date=roll_info.roll_end_date,
        roll_urgency=roll_info.roll_urgency,  # type: ignore
    )

    roll_impact = RollImpactData(
        impact_score=impact["impact_score"],
        oi_concentration=impact["oi_concentration"],
        volume_shift=impact["volume_shift"],
        expected_slippage=impact["expected_slippage"],
        impact_category=impact["impact_category"],  # type: ignore
    )

    response = RollPressureResponse(
        contract=commodity,
        roll_pressure=RollPressureData(
            index=rp.roll_pressure.index,
            oi_decay_pct=rp.roll_pressure.oi_decay_pct,
            spread_basis=rp.roll_pressure.spread_basis,
            days_to_expiry=rp.roll_pressure.days_to_expiry,
            roll_window=rp.roll_pressure.roll_window,
        ),
        roll_calendar=roll_calendar,
        roll_impact=roll_impact,
        metadata=RollPressureMetadata(
            commodity=commodity,
            as_of_date=as_of_date,
            lookback_days=days_back,
            computed_at=datetime.now(timezone.utc),
            cache_hit=False,
        ),
    )

    # Cache the result
    cache.set(cache_key, response)

    return response