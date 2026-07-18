"""Composite signal API router.

Combines positioning, term structure, and roll pressure signals into one
unified market structure score.

Endpoints:
- GET /v1/signals/composite/{symbol} — compute composite signal for a symbol
"""

from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.models.signal import CompositeSignalResponse
from app.signals.composite import CompositeSignalCalculator

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


# ---------------------------------------------------------------------------
# GET /v1/signals/composite/{symbol} — specific contract
# ---------------------------------------------------------------------------


@router.get("/composite/{symbol}", response_model=CompositeSignalResponse)
async def get_composite_signal(
    symbol: str,
    positioning_weight: float = Query(0.40, ge=0, le=1, description="Weight for positioning signal (0-1)"),
    term_structure_weight: float = Query(0.30, ge=0, le=1, description="Weight for term structure signal (0-1)"),
    roll_pressure_weight: float = Query(0.30, ge=0, le=1, description="Weight for roll pressure signal (0-1)"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Compute the composite market structure signal for a symbol.

    Combines positioning, term structure, and roll pressure signals into
    one unified score (-100 to +100) with alignment, confidence, and
    historical comparison.

    Custom weights can be provided via query parameters; they are
    renormalized if some signals are unavailable.

    Tier enforcement:
    - Free: ES, NQ, CL only
    - Pro/Enterprise: All commodities
    """
    symbol = symbol.upper()

    # Validate symbol format
    if not re.match(r'^[A-Z]{1,10}$', symbol):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_symbol",
                "message": f"Invalid symbol: '{symbol}'. Must be 1-10 uppercase letters.",
            },
        )

    # Tier enforcement
    if not tier_info.can_access_contract(symbol):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{symbol}' is not available on your tier. Upgrade for full access.",
            },
        )

    # Build weight configuration
    weights = {
        "positioning": positioning_weight,
        "term_structure": term_structure_weight,
        "roll_pressure": roll_pressure_weight,
    }

    calculator = CompositeSignalCalculator(weights=weights)

    try:
        response = await calculator.compute(
            contract_symbol=symbol,
            db=db,
        )
        return response
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Contract '{symbol}' is not tracked."},
            ) from None
        elif "No signal data" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "data_unavailable",
                    "message": f"No signal data available for '{symbol}'. Ingest COT and settlement data first.",
                },
            ) from None
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "signal_error", "message": error_msg},
            ) from None
