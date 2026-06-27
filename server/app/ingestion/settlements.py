"""CME Group daily settlement price ingestion.

Fetches settlement prices for ES, NQ, CL, GC futures contracts
from CME Group. Parses contract month codes, settlement prices,
open interest, and volume data.

CME provides settlement data via:
1. Daily settlement CSV files (public)
2. CME Group API (requires registration)
3. Scraped settlement pages (fallback)

For MVP, we support the CSV download approach with mock data for testing.
"""

from __future__ import annotations

import asyncio

import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.db import Contract, RawSettlement
from app.models.ingestion import SettlementCreate, SettlementIngestionResult
from app.ingestion.validators import (
    detect_duplicate_settlement,
    validate_settlement_batch,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# CME settlement URLs and configuration
# ---------------------------------------------------------------------------

# CME Group settlement data URLs
# The actual CME settlement page: https://www.cmegroup.com/markets/settlements.html
# Direct CSV downloads for specific products
CME_SETTLEMENT_BASE_URL = "https://www.cmegroup.com/CmeWS/mds/settlements"

# Product-specific settlement URLs
CME_PRODUCT_URLS: dict[str, str] = {
    "ES": "https://www.cmegroup.com/CmeWS/mds/settlements/V1/Settlements?exchange=XCBT&foi=O&prodCode=ES",
    "NQ": "https://www.cmegroup.com/CmeWS/mds/settlements/V1/Settlements?exchange=XCBT&foi=O&prodCode=NQ",
    "CL": "https://www.cmegroup.com/CmeWS/mds/settlements/V1/Settlements?exchange=NYMEX&foi=O&prodCode=CL",
    "GC": "https://www.cmegroup.com/CmeWS/mds/settlements/V1/Settlements?exchange=XCEC&foi=O&prodCode=GC",
}

# Month code mapping for CME futures
# CME uses single-letter codes for contract months
CME_MONTH_CODES: dict[str, int] = {
    "F": 1,   # January
    "G": 2,   # February
    "H": 3,   # March
    "J": 4,   # April
    "K": 5,   # May
    "M": 6,   # June
    "N": 7,   # July
    "Q": 8,   # August
    "U": 9,   # September
    "V": 10,  # October
    "X": 11,  # November
    "Z": 12,  # December
}

MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# Contract month patterns for each product
# ES/NQ: H(Mar), M(Jun), U(Sep), Z(Dec) — quarterly
# CL: All 12 months (monthly for nearby, then quarterly)
# GC: G(Feb), J(Apr), M(Jun), Q(Aug), V(Oct), Z(Dec)
PRODUCT_MONTH_CODES: dict[str, list[str]] = {
    "ES": ["H", "M", "U", "Z"],
    "NQ": ["H", "M", "U", "Z"],
    "CL": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
    "GC": ["G", "J", "M", "Q", "V", "Z"],
}


def parse_month_code(code: str) -> tuple[int, int]:
    """Parse a CME month code like 'Jun 26' or 'H26' or 'U26' into (month, year).

    Returns:
        Tuple of (month_number, full_year).
        E.g., parse_month_code('Jun 26') → (6, 2026)
    """
    code = code.strip()

    # Format: "Jun 26" or "June 2026"
    full_month_match = re.match(r"^(\w{3,9})\s+(\d{2,4})$", code)
    if full_month_match:
        month_str = full_month_match.group(1)
        year_str = full_month_match.group(2)

        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4,
            "may": 5, "jun": 6, "jul": 7, "aug": 8,
            "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            "january": 1, "february": 2, "march": 3, "april": 4,
            "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }

        month = month_map.get(month_str.lower())
        if month is None:
            raise ValueError(f"Unknown month name: {month_str}")

        year = int(year_str)
        if year < 100:
            year += 2000  # "26" → 2026

        return (month, year)

    # Format: "H26" or "U26" (single letter + 2-digit year)
    letter_match = re.match(r"^([FGHJKMNQUVXZ])(\d{1,2})$", code)
    if letter_match:
        letter = letter_match.group(1)
        year_str = letter_match.group(2)

        month = CME_MONTH_CODES.get(letter)
        if month is None:
            raise ValueError(f"Unknown month code: {letter}")

        year = int(year_str)
        if year < 100:
            year += 2000

        return (month, year)

    raise ValueError(f"Cannot parse month code: {code}")


def format_month_code(month: int, year: int) -> str:
    """Format a month and year into CME month code format like 'Jun 26'."""
    month_name = MONTH_NAMES.get(month, str(month))
    short_year = year % 100
    return f"{month_name} {short_year:02d}"


def get_contract_expiry(symbol: str, month_code: str) -> date:
    """Calculate the approximate expiry date for a futures contract.

    Rules:
    - ES/NQ: Third Friday of the contract month
    - CL: Third business day before the 25th of the month prior to contract month
    - GC: Third-to-last business day of the contract month

    For simplicity in MVP, we approximate: last business day of the contract month
    for ES/NQ, and specific rules for CL/GC.
    """
    month, year = parse_month_code(month_code)

    if symbol in ("ES", "NQ"):
        # Third Friday of the contract month
        # Find first day of month, then find third Friday
        first_of_month = date(year, month, 1)
        # Day of week: 0=Monday, 4=Friday
        first_friday = 1 + (4 - first_of_month.weekday()) % 7
        if first_friday < 1:
            first_friday += 7
        third_friday = first_friday + 14
        return date(year, month, third_friday)

    elif symbol == "CL":
        # Third business day before the 25th of the prior month
        # Simplified: 22nd of the prior month
        if month == 1:
            return date(year - 1, 12, 22)
        return date(year, month - 1, 22)

    elif symbol == "GC":
        # Third-to-last business day of the contract month
        # Simplified: 27th of the contract month
        last_day = 28  # Safe for all months
        return date(year, month, last_day)

    # Default: last day of the contract month
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


# ---------------------------------------------------------------------------
# CME settlement fetcher
# ---------------------------------------------------------------------------


class SettlementFetcher:
    """Fetches and parses CME Group daily settlement data.

    Uses httpx for async HTTP requests. Handles CME's settlement
    data format and contract month code parsing.

    Includes retry logic for CME outages and timeouts.
    """

    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 5.0

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._last_fetch_error: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx async client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "User-Agent": "OpenInterest-Lens/1.0",
                    "Accept": "text/html,text/csv,application/json",
                },
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def fetch_settlements(
        self,
        symbol: str,
        settlement_date: Optional[date] = None,
    ) -> list[dict]:
        """Fetch settlement data for a specific contract symbol.

        Args:
            symbol: Root symbol, e.g. 'ES'
            settlement_date: Date to fetch. Defaults to most recent trading day.

        Returns:
            List of parsed settlement dictionaries.
        """
        if symbol not in CME_PRODUCT_URLS:
            logger.warning("unknown_contract_symbol", symbol=symbol)
            return []

        client = await self._get_client()
        url = CME_PRODUCT_URLS[symbol]

        if settlement_date:
            # CME API supports date parameter
            url = f"{url}&tradeDate={settlement_date.strftime('%m/%d/%Y')}"

        logger.info("fetching_settlements", symbol=symbol, url=url)

        response = None
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.get(url)
                if response.status_code == 404:
                    logger.warning("settlement_data_not_available", symbol=symbol, attempt=attempt + 1)
                    # CME may not have data yet for today
                    break
                if response.status_code == 429:
                    logger.warning("settlement_rate_limited", symbol=symbol, attempt=attempt + 1)
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** (attempt + 1)))
                    continue
                response.raise_for_status()
                break  # Success
            except httpx.TimeoutException as e:
                logger.warning("settlement_fetch_timeout", symbol=symbol, attempt=attempt + 1, error=str(e))
                self._last_fetch_error = f"CME timeout for {symbol}: {e}"
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                else:
                    return []
            except httpx.HTTPStatusError as e:
                logger.warning("settlement_http_error", symbol=symbol, status=e.response.status_code, attempt=attempt + 1)
                self._last_fetch_error = f"CME HTTP {e.response.status_code} for {symbol}: {e}"
                if e.response.status_code >= 500 and attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                else:
                    return []
            except httpx.HTTPError as e:
                logger.warning("settlement_fetch_error", symbol=symbol, attempt=attempt + 1, error=str(e))
                self._last_fetch_error = f"CME fetch error for {symbol}: {e}"
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                else:
                    return []
        else:
            logger.error("settlement_all_retries_failed", symbol=symbol)
            return []

        if response is None:
            return []

        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return self._parse_cme_json(response.json(), symbol, settlement_date)
        elif "csv" in content_type or "text" in content_type:
            return self._parse_cme_csv(response.text, symbol, settlement_date)

        return self._parse_cme_csv(response.text, symbol, settlement_date)

    async def fetch_all_settlements(
        self,
        symbols: Optional[list[str]] = None,
        settlement_date: Optional[date] = None,
    ) -> list[dict]:
        """Fetch settlements for all tracked contracts.

        Args:
            symbols: List of contract symbols to fetch. Defaults to all tracked.
            settlement_date: Date to fetch. Defaults to most recent.

        Returns:
            Combined list of settlement dictionaries.
        """
        if symbols is None:
            symbols = list(CME_PRODUCT_URLS.keys())

        all_results: list[dict] = []
        for symbol in symbols:
            results = await self.fetch_settlements(symbol, settlement_date)
            all_results.extend(results)

        return all_results

    def _parse_cme_json(self, data: dict, symbol: str, settlement_date: Optional[date]) -> list[dict]:
        """Parse CME JSON settlement data.

        CME API returns JSON with 'settlements' array containing contract months.
        """
        results: list[dict] = []

        settlements = data.get("settlements", [])
        if isinstance(settlements, dict):
            settlements = settlements.get("settlement", [])

        for entry in settlements:
            try:
                month_code = entry.get("month", entry.get("instrumentName", ""))
                price = float(entry.get("price", entry.get("settlementPrice", 0)))
                oi = int(entry.get("openInterest", entry.get("oi", 0)) or 0)
                volume = int(entry.get("volume", entry.get("clearedVolume", 0)) or 0)
                trade_date = settlement_date or date.today()

                # Normalize month code
                month_str = self._normalize_month_code(month_code, symbol)

                results.append({
                    "symbol": symbol,
                    "month_code": month_str,
                    "settlement_date": trade_date,
                    "settlement_price": price,
                    "open_interest": oi,
                    "volume": volume,
                })

            except (ValueError, KeyError) as e:
                logger.warning("settlement_json_parse_error", symbol=symbol, error=str(e))
                continue

        return results

    def _parse_cme_csv(self, csv_content: str, symbol: str, settlement_date: Optional[date]) -> list[dict]:
        """Parse CME CSV settlement data.

        CME settlement CSV typically has columns like:
        Month,Open,High,Low,Settle,Change,OpenInterest,Volume
        """
        results: list[dict] = []

        if not csv_content.strip():
            return results

        trade_date = settlement_date or date.today()

        reader = csv.DictReader(io.StringIO(csv_content))
        for row in reader:
            try:
                month_raw = row.get("Month", row.get("month", row.get("Instrument", "")))
                price_str = row.get("Settle", row.get("Settlement", row.get("settlementPrice", "0")))
                oi_str = row.get("OpenInterest", row.get("OI", row.get("openInterest", "0")))
                vol_str = row.get("Volume", row.get("ClearedVolume", row.get("volume", "0")))

                if not month_raw or not price_str:
                    continue

                # Skip totals/header rows
                if month_raw.lower() in ("total", "month", "", "symbol"):
                    continue

                # Clean price — handle commas, dashes for zero, and whitespace
                clean_price = str(price_str).replace(",", "").replace("-", "0").strip()
                clean_oi = str(oi_str).replace(",", "").replace("-", "0").strip()
                clean_vol = str(vol_str).replace(",", "").replace("-", "0").strip()

                price = float(clean_price) if clean_price else 0.0
                oi = int(clean_oi) if clean_oi else 0
                volume = int(clean_vol) if clean_vol else 0

                month_code = self._normalize_month_code(month_raw.strip(), symbol)

                results.append({
                    "symbol": symbol,
                    "month_code": month_code,
                    "settlement_date": trade_date,
                    "settlement_price": price,
                    "open_interest": oi,
                    "volume": volume,
                })

            except (ValueError, KeyError) as e:
                logger.warning("settlement_csv_parse_error", symbol=symbol, error=str(e))
                continue

        return results

    def _normalize_month_code(self, raw_code: str, symbol: str) -> str:
        """Normalize various month code formats to our standard 'Mon YY' format.

        Examples:
            'Jun 26' → 'Jun 26'
            'H26' → 'Mar 26'
            'M26' → 'Jun 26'
            'June 2026' → 'Jun 26'
        """
        raw_code = raw_code.strip()

        # Already in our format: "Jun 26"
        if re.match(r"^[A-Z][a-z]{2}\s+\d{2}$", raw_code):
            return raw_code

        # Full month name: "June 2026"
        if re.match(r"^[A-Z][a-z]+\s+\d{4}$", raw_code):
            month, year = parse_month_code(raw_code)
            return format_month_code(month, year)

        # CME single letter + year: "H26" or "U26"
        if re.match(r"^[FGHJKMNQUVXZ]\d{1,2}$", raw_code):
            month, year = parse_month_code(raw_code)
            return format_month_code(month, year)

        # Try to parse and reformat
        try:
            month, year = parse_month_code(raw_code)
            return format_month_code(month, year)
        except ValueError:
            logger.warning("cannot_normalize_month_code", raw=raw_code, symbol=symbol)
            return raw_code


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def store_settlements(
    settlements: list[SettlementCreate],
    db: AsyncSession,
) -> SettlementIngestionResult:
    """Validate and store settlement records, skipping duplicates.

    Args:
        settlements: List of settlement create models.
        db: Async database session.

    Returns:
        SettlementIngestionResult with counts and any errors.
    """
    ingested = 0
    skipped = 0
    errors: list[str] = []
    contracts_processed: list[str] = []

    # Get existing contracts
    contracts_result = await db.execute(select(Contract).where(Contract.is_active.is_(True)))
    contracts = {c.symbol: c for c in contracts_result.scalars().all()}

    # Get existing settlement keys for duplicate detection
    existing_settlements = await db.execute(select(RawSettlement))
    existing_keys: set[tuple[date, str]] = set()
    for s in existing_settlements.scalars().all():
        key = (s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date, s.month_code)
        # Need to also match by contract, but for simplicity we'll check per-contract below

    for settlement in settlements:
        # Check contract exists
        if settlement.contract_symbol not in contracts:
            errors.append(f"Unknown contract symbol: {settlement.contract_symbol}")
            continue

        contract = contracts[settlement.contract_symbol]

        # Validate
        validation_results = validate_settlement_batch([settlement])
        _, validation = validation_results[0]
        if not validation.is_valid:
            for err in validation.errors:
                errors.append(
                    f"Validation error for {settlement.contract_symbol} "
                    f"{settlement.month_code} {settlement.settlement_date}: {err.message}"
                )
            continue

        # Check for duplicate
        existing = await db.execute(
            select(RawSettlement).where(
                RawSettlement.contract_id == contract.id,
                RawSettlement.month_code == settlement.month_code,
                RawSettlement.settlement_date == settlement.settlement_date,
            )
        )
        if existing.scalar_one_or_none() is not None:
            skipped += 1
            continue

        # Store
        db_settlement = RawSettlement(
            contract_id=contract.id,
            month_code=settlement.month_code,
            settlement_date=settlement.settlement_date,
            settlement_price=settlement.settlement_price,
            open_interest=settlement.open_interest,
            volume=settlement.volume,
        )
        db.add(db_settlement)
        ingested += 1

        if settlement.contract_symbol not in contracts_processed:
            contracts_processed.append(settlement.contract_symbol)

    await db.flush()

    # Determine status
    if ingested == 0 and skipped == 0:
        status: str = "failed"
    elif errors:
        status = "partial"
    else:
        status = "success"

    result = SettlementIngestionResult(
        status=status,
        settlements_ingested=ingested,
        settlements_skipped=skipped,
        errors=errors,
        contracts_processed=contracts_processed,
    )

    logger.info(
        "settlement_storage_complete",
        status=status,
        ingested=ingested,
        skipped=skipped,
        errors=len(errors),
    )

    return result


async def ingest_settlements(
    symbols: Optional[list[str]] = None,
    settlement_date: Optional[date] = None,
    db: AsyncSession | None = None,
) -> SettlementIngestionResult:
    """Main entry point: fetch, parse, validate, and store settlement data.

    Args:
        symbols: List of contract symbols. Defaults to all tracked.
        settlement_date: Date to fetch. Defaults to most recent.
        db: Optional async session. If None, creates a new one.

    Returns:
        SettlementIngestionResult with ingestion summary.
    """
    fetcher = SettlementFetcher()

    try:
        # Fetch data
        raw_data = await fetcher.fetch_all_settlements(symbols, settlement_date)

        if not raw_data:
            logger.warning("no_settlement_data_fetched")
            return SettlementIngestionResult(
                status="failed",
                settlements_ingested=0,
                settlements_skipped=0,
                errors=["No settlement data fetched from CME"],
                contracts_processed=[],
            )

        # Convert to SettlementCreate models
        settlements: list[SettlementCreate] = []
        for row in raw_data:
            try:
                settlement = SettlementCreate(
                    contract_symbol=row["symbol"],
                    month_code=row["month_code"],
                    settlement_date=row["settlement_date"],
                    settlement_price=row["settlement_price"],
                    open_interest=row["open_interest"],
                    volume=row["volume"],
                )
                settlements.append(settlement)
            except Exception as e:
                logger.warning("settlement_creation_error", error=str(e), row=row)

        # Store
        if db is None:
            async with async_session_factory() as session:
                result = await store_settlements(settlements, session)
                await session.commit()
        else:
            result = await store_settlements(settlements, db)

        return result

    finally:
        await fetcher.close()


async def get_settlement_status(db: AsyncSession) -> dict:
    """Get the current settlement ingestion status from the database."""
    from sqlalchemy import func

    # Get the most recent settlement
    result = await db.execute(
        select(RawSettlement)
        .order_by(RawSettlement.settlement_date.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    # Get total count
    count_result = await db.execute(select(func.count(RawSettlement.id)))
    total = count_result.scalar() or 0

    return {
        "last_settlement_date": latest.settlement_date.date() if latest and isinstance(latest.settlement_date, datetime) else (latest.settlement_date if latest else None),
        "total_settlements": total,
        "latest_settlement_id": latest.id if latest else None,
    }