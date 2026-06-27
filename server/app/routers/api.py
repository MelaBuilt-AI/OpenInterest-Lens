"""Week 5 canonical API endpoints for OpenInterest Lens.

These endpoints provide the primary API surface:
- GET /v1/signals/{contract} — positioning signals
- GET /v1/term-structure/{contract} — term structure + alerts
- GET /v1/cot/{contract} — raw COT with computed metrics
- GET /v1/roll-pressure/{contract} — roll pressure index
- GET /v1/contracts — contract listing

All endpoints enforce tier-based access control and rate limiting.
Responses include X-Cache and X-RateLimit headers.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo, TIER_LIMITS
from app.models.db import Contract, RawCOTReport, SignalPositioning
from app.services.redis_cache import RedisCacheService, get_cache_service, cache_headers
from app.signals.positioning import compute_positioning_signal
from app.signals.roll_calendar import calculate_roll_info
from app.signals.roll_pressure import compute_roll_pressure, compute_roll_impact_score
from app.signals.term_structure import (
    compute_term_structure,
    compute_contango_backwardation,
    compute_term_structure_slope,
    compute_calendar_spread_ratio,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["api"])


# ---------------------------------------------------------------------------
# Helper: get contract ID from symbol
# ---------------------------------------------------------------------------

async def _get_contract_id(symbol: str, db: AsyncSession) -> int:
    """Look up contract ID from symbol. Raises 404 if not found."""
    result = await db.execute(select(Contract.id).where(Contract.symbol == symbol, Contract.is_active.is_(True)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Contract '{symbol}' is not tracked."},
        )
    return row


# NOTE: /v1/signals/{contract} is NOT added here because it conflicts with
# the existing /v1/signals/positioning path in the signals router.
# The canonical positioning endpoint is /v1/signals/positioning/{commodity}.


# ---------------------------------------------------------------------------
# GET /v1/term-structure/{contract} — term structure + alerts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /v1/term-structure/{contract} — term structure + alerts
# ---------------------------------------------------------------------------


@router.get("/term-structure/{contract}")
async def get_term_structure(
    contract: str,
    response: Response,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get term structure curve with contango/backwardation alerts for a contract.

    Returns the full term structure curve, slope metrics, calendar spreads,
    and any active contango/backwardation alerts.

    Tier enforcement:
    - Free: ES, NQ, CL only (current data only)
    - Pro/Enterprise: All contracts, historical data
    """
    contract = contract.upper()

    # Tier enforcement
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier. Upgrade for full access.",
            },
        )

    # Free tier: no historical queries
    is_historical = start_date is not None or end_date is not None
    if is_historical and tier_info.tier == "free":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": "Historical term structure data requires a Pro or Enterprise plan.",
            },
        )

    # Check contract exists
    await _get_contract_id(contract, db)

    # Parse date
    as_of_date = None
    if end_date:
        try:
            as_of_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "Date must be in YYYY-MM-DD format."},
            )

    # Cache check (latest only)
    cache = get_cache_service()
    if not is_historical:
        cache_key = RedisCacheService.make_key("term_structure", contract)
        cached = await cache.get(cache_key)
        if cached is not None:
            response.headers.update(cache_headers(hit=True, age_seconds=0))
            return cached

    # Compute term structure
    try:
        ts = await compute_term_structure(
            contract_symbol=contract,
            db=db,
            as_of_date=as_of_date,
        )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{contract}' is not tracked."},
            )
        elif "No settlement data" in error_msg or "Insufficient" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "data_unavailable", "message": f"No settlement data available for '{contract}'."},
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

    resp = {
        "contract": contract,
        "term_structure": {
            "structure_type": ts.structure_type,
            "months": [
                {
                    "month": m.month,
                    "expiry_date": m.expiry_date.isoformat() if m.expiry_date else None,
                    "settlement": m.settlement,
                    "open_interest": m.open_interest,
                    "volume": m.volume,
                    "spread_to_front": m.spread_to_front,
                    "annualized_yield": m.annualized_yield,
                }
                for m in ts.months
            ],
            "front_month_oi": ts.curve_metrics.front_month_oi if ts.curve_metrics else 0,
            "total_oi": ts.curve_metrics.total_oi if ts.curve_metrics else 0,
            "oi_concentration_pct": ts.curve_metrics.oi_concentration_pct if ts.curve_metrics else 0.0,
            "steepness": ts.curve_metrics.steepness if ts.curve_metrics else 0.0,
        },
        "contango_backwardation": cb,
        "slope_metrics": slope,
        "calendar_spread_ratios": spreads,
        "metadata": {
            "commodity": contract,
            "as_of_date": ts.as_of_date.isoformat() if ts.as_of_date else None,
            "data_points": len(ts.months),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Cache if latest
    if not is_historical:
        cache_key = RedisCacheService.make_key("term_structure", contract)
        await cache.set(cache_key, resp)
        response.headers.update(cache_headers(hit=False))
    else:
        response.headers.update(cache_headers(hit=False))

    return resp


# ---------------------------------------------------------------------------
# GET /v1/cot/{contract} — raw COT with computed metrics
# ---------------------------------------------------------------------------


@router.get("/cot/{contract}")
async def get_cot(
    contract: str,
    response: Response,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    format: Optional[str] = Query(None, description="Response format: 'full' (default) or 'summary'"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get raw COT data with computed Z-score and percentile metrics.

    Returns COT report data for a contract, enriched with computed
    Z-scores and percentile rankings over the lookback window.

    Tier enforcement:
    - Free: ES, NQ, CL only, 4 weeks history
    - Pro: 50 contracts, 104 weeks history
    - Enterprise: unlimited, 260 weeks history
    """
    contract = contract.upper()

    # Tier enforcement
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
            },
        )

    # Check contract exists
    contract_id = await _get_contract_id(contract, db)

    # Parse dates
    from datetime import datetime as dt

    start = None
    end = None
    if start_date:
        try:
            start = dt.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "start_date must be YYYY-MM-DD format."},
            )
    if end_date:
        try:
            end = dt.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "end_date must be YYYY-MM-DD format."},
            )

    # Enforce history depth by tier
    tier_limits = TIER_LIMITS.get(tier_info.tier, {})
    max_weeks = tier_limits.get("history_weeks", 4)

    is_historical = start is not None or end is not None

    # Cache check (latest only)
    cache = get_cache_service()
    if not is_historical:
        cache_key = RedisCacheService.make_key("cot", contract)
        cached = await cache.get(cache_key)
        if cached is not None:
            response.headers.update(cache_headers(hit=True, age_seconds=0))
            return cached

    # Query COT data
    query = (
        select(RawCOTReport)
        .where(RawCOTReport.contract_id == contract_id)
        .order_by(RawCOTReport.as_of_date.desc())
    )

    if start:
        query = query.where(RawCOTReport.as_of_date >= start)
    if end:
        query = query.where(RawCOTReport.as_of_date <= end)
    else:
        # Limit by tier's history depth
        from datetime import timedelta
        max_date = date.today() - timedelta(weeks=max_weeks)
        if start is None:
            query = query.where(RawCOTReport.as_of_date >= max_date)

    result = await db.execute(query.limit(260))
    reports = result.scalars().all()

    if not reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"No COT data found for '{contract}'. Ingest data first."},
        )

    # Compute Z-scores and percentiles
    from app.signals.historical import rolling_z_score, percentile_rank

    # Build history arrays for computation
    commercial_nets = [r.commercial_net for r in reversed(reports)]
    non_commercial_nets = [r.non_commercial_net for r in reversed(reports)]
    non_reportable_nets = [r.non_reportable_net for r in reversed(reports)]

    resp_reports = []
    for i, r in enumerate(reports):
        # Compute Z-scores relative to all available history
        c_z = rolling_z_score(r.commercial_net, commercial_nets) if len(commercial_nets) >= 2 else 0.0
        c_pct = percentile_rank(r.commercial_net, commercial_nets)
        nc_z = rolling_z_score(r.non_commercial_net, non_commercial_nets) if len(non_commercial_nets) >= 2 else 0.0
        nc_pct = percentile_rank(r.non_commercial_net, non_commercial_nets)
        nr_z = rolling_z_score(r.non_reportable_net, non_reportable_nets) if len(non_reportable_nets) >= 2 else 0.0
        nr_pct = percentile_rank(r.non_reportable_net, non_reportable_nets)

        entry = {
            "as_of_date": r.as_of_date.isoformat() if hasattr(r.as_of_date, 'isoformat') else str(r.as_of_date),
            "published_date": r.published_date.isoformat() if r.published_date and hasattr(r.published_date, 'isoformat') else None,
            "commercial": {
                "long": r.commercial_long,
                "short": r.commercial_short,
                "net": r.commercial_net,
                "z_score_52w": round(c_z, 4),
                "percentile_52w": round(c_pct, 2),
            },
            "non_commercial": {
                "long": r.non_commercial_long,
                "short": r.non_commercial_short,
                "net": r.non_commercial_net,
                "z_score_52w": round(nc_z, 4),
                "percentile_52w": round(nc_pct, 2),
            },
            "non_reportable": {
                "long": r.non_reportable_long,
                "short": r.non_reportable_short,
                "net": r.non_reportable_net,
                "z_score_52w": round(nr_z, 4),
                "percentile_52w": round(nr_pct, 2),
            },
            "total_open_interest": r.total_open_interest,
        }
        resp_reports.append(entry)

    resp = {
        "contract": contract,
        "reports": resp_reports,
        "metadata": {
            "total_reports": len(resp_reports),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Cache if latest
    if not is_historical:
        cache_key = RedisCacheService.make_key("cot", contract)
        await cache.set(cache_key, resp)
        response.headers.update(cache_headers(hit=False))
    else:
        response.headers.update(cache_headers(hit=False))

    return resp


# ---------------------------------------------------------------------------
# GET /v1/roll-pressure/{contract} — roll pressure index
# ---------------------------------------------------------------------------


@router.get("/roll-pressure/{contract}")
async def get_roll_pressure(
    contract: str,
    response: Response,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    days_back: int = Query(30, ge=1, le=365, description="Days of history for OI decay analysis"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get roll pressure index for a contract.

    Returns roll pressure metrics, roll calendar, and impact estimation.

    Tier enforcement:
    - Free: ES, NQ, CL only
    - Pro/Enterprise: All contracts
    """
    contract = contract.upper()

    # Tier enforcement
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
            },
        )

    # Check contract exists
    await _get_contract_id(contract, db)

    is_historical = start_date is not None or end_date is not None

    # Free tier: no historical queries
    if is_historical and tier_info.tier == "free":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": "Historical roll pressure data requires a Pro or Enterprise plan.",
            },
        )

    # Cache check (latest only)
    cache = get_cache_service()
    if not is_historical:
        cache_key = RedisCacheService.make_key("roll_pressure", contract)
        cached = await cache.get(cache_key)
        if cached is not None:
            response.headers.update(cache_headers(hit=True, age_seconds=0))
            return cached

    # Compute roll pressure
    try:
        rp = await compute_roll_pressure(
            contract_symbol=contract,
            db=db,
            days_back=days_back,
        )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{contract}' is not tracked."},
            )
        elif "No settlement data" in error_msg or "Insufficient" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "data_unavailable", "message": f"No settlement data available for '{contract}'."},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "signal_error", "message": error_msg},
            )

    # Get roll calendar info
    as_of_date = date.today()
    roll_info = calculate_roll_info(contract, as_of_date)

    # Compute roll impact
    impact = compute_roll_impact_score(
        nearby_oi=rp.nearby.open_interest,
        deferred_oi=rp.deferred.open_interest,
        nearby_volume=rp.nearby.volume,
        deferred_volume=rp.deferred.volume,
        spread_basis=rp.roll_pressure.spread_basis,
        days_to_expiry=rp.roll_pressure.days_to_expiry,
        contract_symbol=contract,
    )

    resp = {
        "contract": contract,
        "roll_pressure": {
            "index": rp.roll_pressure.index,
            "oi_decay_pct": rp.roll_pressure.oi_decay_pct,
            "spread_basis": rp.roll_pressure.spread_basis,
            "days_to_expiry": rp.roll_pressure.days_to_expiry,
            "roll_window": rp.roll_pressure.roll_window,
        },
        "roll_calendar": {
            "nearby_month": roll_info.nearby_month_code,
            "nearby_expiry": roll_info.nearby_expiry.isoformat(),
            "deferred_month": roll_info.deferred_month_code,
            "deferred_expiry": roll_info.deferred_expiry.isoformat(),
            "days_to_roll": roll_info.days_to_roll,
            "roll_start_date": roll_info.roll_start_date.isoformat(),
            "roll_end_date": roll_info.roll_end_date.isoformat(),
            "roll_urgency": roll_info.roll_urgency,
        },
        "roll_impact": {
            "impact_score": impact["impact_score"],
            "oi_concentration": impact["oi_concentration"],
            "volume_shift": impact["volume_shift"],
            "expected_slippage": impact["expected_slippage"],
            "impact_category": impact["impact_category"],
        },
        "metadata": {
            "commodity": contract,
            "as_of_date": as_of_date.isoformat(),
            "lookback_days": days_back,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Cache if latest
    if not is_historical:
        cache_key = RedisCacheService.make_key("roll_pressure", contract)
        await cache.set(cache_key, resp)
        response.headers.update(cache_headers(hit=False))
    else:
        response.headers.update(cache_headers(hit=False))

    return resp