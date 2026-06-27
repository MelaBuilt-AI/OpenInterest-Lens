"""GET /v1/contracts — list tracked contracts with metadata."""

import json
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.models.db import Contract
from app.models.signal import ContractMetadata, ContractsResponse
from app.config import Settings, get_settings
from app.services.redis_cache import get_cache_service, RedisCacheService, cache_headers

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["contracts"])

# ---------------------------------------------------------------------------
# Seed data — the four MVP contracts
# ---------------------------------------------------------------------------

SEED_CONTRACTS: list[dict] = [
    {
        "symbol": "ES",
        "exchange": "CME",
        "asset_class": "equity_index",
        "full_name": "E-mini S&P 500",
        "tick_size": 0.25,
        "contract_size": 50,
        "months_traded": '["H", "M", "U", "Z"]',
        "data_available_from": "1997-01-01",
        "cftc_name": "E-MINI S&P 500",
    },
    {
        "symbol": "NQ",
        "exchange": "CME",
        "asset_class": "equity_index",
        "full_name": "E-mini Nasdaq-100",
        "tick_size": 0.25,
        "contract_size": 20,
        "months_traded": '["H", "M", "U", "Z"]',
        "data_available_from": "1999-06-01",
        "cftc_name": "E-MINI NASDAQ-100",
    },
    {
        "symbol": "CL",
        "exchange": "NYMEX",
        "asset_class": "energy",
        "full_name": "Crude Oil (Light Sweet)",
        "tick_size": 0.01,
        "contract_size": 1000,
        "months_traded": '["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]',
        "data_available_from": "1983-01-01",
        "cftc_name": "CRUDE OIL, LIGHT SWEET",
    },
    {
        "symbol": "GC",
        "exchange": "COMEX",
        "asset_class": "metal",
        "full_name": "Gold",
        "tick_size": 0.10,
        "contract_size": 100,
        "months_traded": '["G", "J", "M", "Q", "V", "Z"]',
        "data_available_from": "1975-01-01",
        "cftc_name": "GOLD - COMMODITY EXCHANGE INC.",
    },
]


async def seed_contracts() -> None:
    """Insert seed contracts if the table is empty."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        result = await session.execute(select(Contract).limit(1))
        if result.scalar_one_or_none() is not None:
            return

        for data in SEED_CONTRACTS:
            contract = Contract(**data, is_active=True)
            session.add(contract)

        await session.commit()
        logger.info("seeded_contracts", count=len(SEED_CONTRACTS))


def _db_contract_to_response(contract: Contract) -> ContractMetadata:
    """Convert a DB Contract model to the API response model."""
    months = json.loads(contract.months_traded) if isinstance(contract.months_traded, str) else contract.months_traded
    signals = ["positioning", "roll_pressure", "contango_alert", "term_structure"]

    return ContractMetadata(
        symbol=contract.symbol,
        exchange=contract.exchange,
        asset_class=contract.asset_class,
        full_name=contract.full_name,
        tick_size=contract.tick_size,
        contract_size=contract.contract_size,
        months_traded=months,
        data_available_from=contract.data_available_from,
        signals_available=signals,
    )


@router.get("/contracts", response_model=ContractsResponse)
async def list_contracts(
    exchange: str | None = Query(None, description="Filter by exchange (e.g. CME)"),
    asset_class: str | None = Query(None, description="Filter by asset class"),
    tier_info: TierInfo = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List all tracked contracts with metadata.

    Optional query params filter by exchange and asset_class.
    Tier enforcement: free tier can only see ES, NQ, CL.
    """
    # Check cache (only for unfiltered queries)
    cache = get_cache_service()
    cache_key = RedisCacheService.make_key("contracts", "all")
    cached = None
    if not exchange and not asset_class:
        cached = await cache.get(cache_key)

    if cached is not None:
        # Filter cached results by tier (copy to avoid mutating the cached dict)
        filtered = [c for c in cached["contracts"] if tier_info.can_access_contract(c["symbol"])]
        return {**cached, "contracts": filtered}

    query = select(Contract).where(Contract.is_active.is_(True))

    if exchange:
        query = query.where(Contract.exchange == exchange.upper())
    if asset_class:
        query = query.where(Contract.asset_class == asset_class.lower())

    result = await db.execute(query.order_by(Contract.symbol))
    db_contracts = result.scalars().all()

    response_contracts: list[ContractMetadata] = []
    for contract in db_contracts:
        # Tier enforcement: free tier only sees allowed contracts
        if not tier_info.can_access_contract(contract.symbol):
            continue
        response_contracts.append(_db_contract_to_response(contract))

    resp = ContractsResponse(contracts=response_contracts)
    resp_dict = resp.model_dump(mode="json")

    # Cache only unfiltered results
    if not exchange and not asset_class:
        await cache.set(cache_key, resp_dict)

    return resp