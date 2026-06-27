"""Signal API router — positioning signal endpoints.

Endpoints:
- GET /v1/signals/positioning — compute positioning signals for all tracked commodities
- GET /v1/signals/positioning/{commodity} — compute for a specific commodity
- GET /v1/signals/positioning/{commodity}/history — historical positioning signals
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.models.db import Contract
from app.models.signal import (
    MultiCommoditySignalResponse,
    PositioningSignalResponse,
)
from app.services.signal_cache import get_signal_cache
from app.signals.positioning import compute_positioning_signal, compute_positioning_signals_multi

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


# ---------------------------------------------------------------------------
# GET /v1/signals/positioning — all commodities
# ---------------------------------------------------------------------------


@router.get("/positioning", response_model=MultiCommoditySignalResponse)
async def get_positioning_signals(
    lookback_weeks: int = Query(52, ge=4, le=260, description="Lookback window in weeks for Z-scores"),
    commodity: Optional[str] = Query(None, description="Filter to a single commodity symbol"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute positioning signals for tracked commodities.

    Returns smart money, retail contrarian, and composite signals
    derived from COT data. Uses Z-scores and percentile rankings
    over the specified lookback window.

    Tier enforcement:
    - Free: Can access ES, NQ, CL positioning only
    - Pro/Enterprise: All commodities
    """
    # Determine which symbols to compute
    if commodity:
        symbols = [commodity.upper()]
    else:
        # Get all active contracts
        from sqlalchemy import select
        result = await db.execute(select(Contract.symbol).where(Contract.is_active.is_(True)))
        symbols = [s for (s,) in result.all()]

    # Filter by tier
    accessible_symbols = [s for s in symbols if tier_info.can_access_contract(s)]
    if not accessible_symbols:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "No accessible contracts found for your tier."},
        )

    # Check cache
    cache = get_signal_cache()
    cached_results: list[PositioningSignalResponse] = []
    uncached_symbols: list[str] = []

    for sym in accessible_symbols:
        cache_key = cache.make_key(sym, "positioning")
        cached = cache.get(cache_key)
        if cached is not None:
            # Mark as cache hit
            cached.metadata.cache_hit = True
            cached_results.append(cached)
        else:
            uncached_symbols.append(sym)

    # Compute uncached
    computed: list[PositioningSignalResponse] = []
    for sym in uncached_symbols:
        try:
            response = await compute_positioning_signal(
                contract_symbol=sym,
                db=db,
                lookback_weeks=lookback_weeks,
            )
            # Cache the result
            cache_key = cache.make_key(sym, "positioning")
            cache.set(cache_key, response)
            computed.append(response)
        except ValueError as e:
            logger.warning("positioning_compute_failed", symbol=sym, error=str(e))
            continue

    # Combine results
    all_results = cached_results + computed

    return MultiCommoditySignalResponse(
        signals=all_results,
        computed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GET /v1/signals/positioning/{commodity} — specific commodity
# ---------------------------------------------------------------------------


@router.get("/positioning/{commodity}", response_model=PositioningSignalResponse)
async def get_positioning_signal_for_commodity(
    commodity: str,
    lookback_weeks: int = Query(52, ge=4, le=260, description="Lookback window in weeks for Z-scores"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute positioning signal for a specific commodity.

    Returns the full PositioningSignalResponse with smart money Z-score,
    retail contrarian signal, composite direction, and detailed breakdown.

    Tier enforcement:
    - Free: ES, NQ, CL only
    - Pro/Enterprise: All commodities
    """
    commodity = commodity.upper()

    # Validate commodity format — only alphanumeric, 1-10 chars
    if not re.match(r'^[A-Z]{1,10}$', commodity):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_symbol", "message": f"Invalid commodity symbol: '{commodity}'. Must be 1-10 uppercase letters."},
        )

    # Tier enforcement
    if not tier_info.can_access_contract(commodity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{commodity}' is not available on your tier. Upgrade for full access.",
            },
        )

    # Check cache first
    cache = get_signal_cache()
    cache_key = cache.make_key(commodity, "positioning")
    cached = cache.get(cache_key)
    if cached is not None:
        cached.metadata.cache_hit = True
        return cached

    # Compute
    try:
        response = await compute_positioning_signal(
            contract_symbol=commodity,
            db=db,
            lookback_weeks=lookback_weeks,
        )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{commodity}' is not tracked."},
            )
        elif "No COT data" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "data_unavailable", "message": f"No COT data available for '{commodity}'. Ingest data first."},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "signal_error", "message": error_msg},
            )

    # Cache the result
    cache.set(cache_key, response)

    return response