"""Data quality API endpoint — /v1/quality.

Exposes data quality monitoring: staleness, gaps, completeness, overall health.
"""

from __future__ import annotations

from datetime import date, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.services.data_quality import get_data_quality_service

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("")
async def get_quality_report(
    contract: str | None = Query(None, description="Specific contract symbol. Omit for all contracts."),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get data quality report.

    Returns staleness, gap detection, and completeness metrics
    for COT and settlement data.

    Tier enforcement:
    - Free: Can only check ES, NQ, CL
    - Pro/Enterprise: All contracts
    """
    # If a specific contract is requested, check tier access
    if contract:
        contract = contract.upper()
        if not tier_info.can_access_contract(contract):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "tier_limit_exceeded",
                    "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
                },
            )

    quality_service = get_data_quality_service()
    report = await quality_service.get_quality_report(db, contract=contract)

    # Filter results by tier for full reports
    if not contract and tier_info.tier == "free":
        # Free tier: only show ES, NQ, CL
        allowed = {"ES", "NQ", "CL"}
        report_dict = report.to_dict()
        for key in ["cot_staleness", "settlement_staleness", "cot_gaps", "settlement_gaps",
                     "cot_completeness", "settlement_completeness"]:
            report_dict[key] = [item for item in report_dict[key] if item["contract"] in allowed]
        report_dict["contracts"] = [c for c in report_dict["contracts"] if c in allowed]
        report_dict["warnings"] = [w for w in report_dict["warnings"]
                                    if any(c in w for c in allowed)]
        return report_dict

    return report.to_dict()


@router.get("/staleness")
async def get_staleness(
    contract: str = Query(..., description="Contract symbol to check"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Check data staleness for a specific contract.

    Returns whether COT and settlement data is considered stale,
    and how many days since the last data point.
    """
    contract = contract.upper()
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
            },
        )

    quality_service = get_data_quality_service()
    result = await quality_service.check_staleness(contract, db)
    return result


@router.get("/gaps")
async def get_gaps(
    contract: str = Query(..., description="Contract symbol to check"),
    start_date: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: str | None = Query(None, description="End date (YYYY-MM-DD)"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Check for data gaps in a specific contract.

    Returns missing dates in the COT and settlement data series.
    """
    contract = contract.upper()
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
            },
        )

    # Parse dates
    start = date.today() - __import__("datetime").timedelta(days=90)
    end = date.today()

    if start_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "start_date must be YYYY-MM-DD format."},
            ) from None
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_date", "message": "end_date must be YYYY-MM-DD format."},
            ) from None

    quality_service = get_data_quality_service()
    result = await quality_service.check_gaps(contract, start, end, db)
    return result


@router.get("/completeness")
async def get_completeness(
    contract: str = Query(..., description="Contract symbol to check"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Check data completeness for a specific contract.

    Returns whether all required fields are present and the percentage
    of complete records.
    """
    contract = contract.upper()
    if not tier_info.can_access_contract(contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": f"Contract '{contract}' is not available on your {tier_info.tier} tier.",
            },
        )

    quality_service = get_data_quality_service()
    result = await quality_service.check_completeness(contract, db)
    return result