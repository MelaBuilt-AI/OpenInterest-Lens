"""Ingestion API router — trigger and monitor data ingestion.

Endpoints:
- POST /v1/ingestion/cot — trigger COT data fetch
- POST /v1/ingestion/settlements — trigger settlement data fetch
- GET /v1/ingestion/status — get ingestion status/last-run info
"""

from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.models.db import Contract, RawCOTReport, RawSettlement
from app.models.ingestion import (
    COTIngestionResult,
    IngestionSourceStatus,
    IngestionStatus,
    IngestionTriggerResponse,
    SettlementIngestionResult,
)
from app.ingestion.cot import ingest_cot_reports, get_cot_status
from app.ingestion.settlements import ingest_settlements, get_settlement_status
from app.ingestion.scheduler import get_scheduler

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/cot", response_model=COTIngestionResult)
async def trigger_cot_ingestion(
    report_type: str = Query("futures", description="Report type: 'futures' or 'combined'"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a COT data fetch.

    Args:
        report_type: 'futures' or 'combined'. Defaults to 'futures'.

    Requires Pro or Enterprise tier.
    """
    # Validate report_type — prevent injection
    if report_type not in ("futures", "combined"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_report_type",
                "message": "report_type must be 'futures' or 'combined'",
            },
        )

    if tier_info.tier == "free":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": "COT ingestion requires a Pro or Enterprise plan.",
            },
        )

    logger.info("cot_ingestion_triggered", tier=tier_info.tier, report_type=report_type)

    try:
        result = await ingest_cot_reports(report_type=report_type, db=db)
        await db.commit()
        return result
    except Exception as e:
        logger.error("cot_ingestion_failed", error=str(e), exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "ingestion_error", "message": f"COT ingestion failed: {str(e)}"},
        )


@router.post("/settlements", response_model=SettlementIngestionResult)
async def trigger_settlement_ingestion(
    symbols: str | None = Query(None, description="Comma-separated contract symbols, e.g. 'ES,NQ'"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a settlement data fetch.

    Args:
        symbols: Comma-separated contract symbols (e.g., 'ES,NQ'). Defaults to all.

    Requires Pro or Enterprise tier.
    """
    if tier_info.tier == "free":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": "Settlement ingestion requires a Pro or Enterprise plan.",
            },
        )

    # Validate symbols — prevent injection, only allow known symbols
    VALID_SYMBOLS = {"ES", "NQ", "CL", "GC"}
    symbol_list = None
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
        # Validate each symbol
        for sym in symbol_list:
            if not re.match(r'^[A-Z]{1,5}$', sym):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_symbol",
                        "message": f"Invalid contract symbol: '{sym}'. Only alphabetic symbols (1-5 chars) are accepted.",
                    },
                )
            if sym not in VALID_SYMBOLS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "unknown_symbol",
                        "message": f"Unknown contract symbol: '{sym}'. Valid symbols: {', '.join(sorted(VALID_SYMBOLS))}",
                    },
                )

    logger.info("settlement_ingestion_triggered", tier=tier_info.tier, symbols=symbol_list)

    try:
        result = await ingest_settlements(symbols=symbol_list, db=db)
        await db.commit()
        return result
    except Exception as e:
        logger.error("settlement_ingestion_failed", error=str(e), exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "ingestion_error", "message": f"Settlement ingestion failed: {str(e)}"},
        )


@router.get("/status", response_model=IngestionStatus)
async def get_ingestion_status(
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get current ingestion pipeline status.

    Returns last run info for COT and settlement ingestion,
    plus scheduler status if the background scheduler is running.
    """
    # Get COT status from DB
    cot_db_status = await get_cot_status(db)
    settlement_db_status = await get_settlement_status(db)

    # Get scheduler status
    scheduler = get_scheduler()
    scheduler_status = scheduler.get_status()

    # Build response
    cot_source = IngestionSourceStatus(
        source="cot",
        last_run=scheduler_status.get("cot", {}).get("last_run"),
        last_status=scheduler_status.get("cot", {}).get("last_status", "never_run"),
        last_records_ingested=scheduler_status.get("cot", {}).get("last_records_ingested", 0),
        last_errors=scheduler_status.get("cot", {}).get("last_errors", []),
        next_scheduled=scheduler_status.get("cot", {}).get("next_scheduled"),
    )

    settlement_source = IngestionSourceStatus(
        source="settlements",
        last_run=scheduler_status.get("settlements", {}).get("last_run"),
        last_status=scheduler_status.get("settlements", {}).get("last_status", "never_run"),
        last_records_ingested=scheduler_status.get("settlements", {}).get("last_records_ingested", 0),
        last_errors=scheduler_status.get("settlements", {}).get("last_errors", []),
        next_scheduled=scheduler_status.get("settlements", {}).get("next_scheduled"),
    )

    # Override with DB status if scheduler hasn't run yet
    if cot_source.last_status == "never_run" and cot_db_status.get("last_as_of_date"):
        cot_source = IngestionSourceStatus(
            source="cot",
            last_status="success",
            last_records_ingested=cot_db_status.get("total_reports", 0),
        )

    if settlement_source.last_status == "never_run" and settlement_db_status.get("last_settlement_date"):
        settlement_source = IngestionSourceStatus(
            source="settlements",
            last_status="success",
            last_records_ingested=settlement_db_status.get("total_settlements", 0),
        )

    return IngestionStatus(
        cot=cot_source,
        settlements=settlement_source,
        uptime_seconds=scheduler_status.get("uptime_seconds", 0),
    )