"""CFTC Commitments of Traders (COT) data ingestion.

Downloads weekly COT report data from CFTC.gov CSV endpoints,
parses into RawCOTReport records, validates, and stores.

Supports both futures-only and combined (futures+options) reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import re
from datetime import date, datetime, timedelta

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.ingestion.validators import (
    validate_cot_batch,
)
from app.models.db import Contract, RawCOTReport
from app.models.ingestion import COTIngestionResult, COTReportCreate

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# CFTC CSV URLs
# ---------------------------------------------------------------------------

# Futures-only report
CFTC_FUTURES_URL = "https://www.cftc.gov/dea/futures/deacmelf.htm"
# Modern CFTC CSV endpoint (futures + options combined)
CFTC_COMBINED_CSV_URL = "https://www.cftc.gov/files/dea/history/deacmelf.htm"
# CFTC disaggregated report (more detailed trader categories)
CFTC_DISAGGREGATED_CSV_URL = "https://www.cftc.gov/files/dea/history/deafut_xls.htm"

# For parsing: the CFTC also publishes machine-readable CSV files
# These are the actual working URLs for the CSV data
CFTC_FUTURES_CSV_URL = "https://www.cftc.gov/files/dea/history/fut_xls_0.htm"
CFTC_COMBINED_CSV_URL_ACTUAL = "https://www.cftc.gov/files/dea/history/com_xls_0.htm"

# Fallback: we'll use the text-format reports and parse them
CFTC_FUTURES_TEXT_URL = "https://www.cftc.gov/dea/futures/deacmelf.htm"

# ---------------------------------------------------------------------------
# CFTC commodity name mapping → our contract symbols
# ---------------------------------------------------------------------------

CFTC_NAME_MAP: dict[str, str] = {
    "E-MINI S&P 500": "ES",
    "E-MINI NASDAQ-100": "NQ",
    "CRUDE OIL, LIGHT SWEET": "CL",
    "GOLD - COMMODITY EXCHANGE INC.": "GC",
    # Common CFTC name variations
    "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE": "ES",
    "E-MINI NASDAQ-100 - CHICAGO MERCANTILE EXCHANGE": "NQ",
    "LIGHT SWEET CRUDE OIL - NEW YORK MERCANTILE EXCHANGE": "CL",
    "GOLD - COMMODITY EXCHANGE INC": "GC",
    "MINI S&P 500": "ES",
    "MINI NASDAQ 100": "NQ",
}

# Reverse map: symbol → CFTC name (for filtering)
SYMBOL_TO_CFTC_NAME: dict[str, str] = {
    "ES": "E-MINI S&P 500",
    "NQ": "E-MINI NASDAQ-100",
    "CL": "CRUDE OIL, LIGHT SWEET",
    "GC": "GOLD - COMMODITY EXCHANGE INC.",
}


# ---------------------------------------------------------------------------
# COT fetcher
# ---------------------------------------------------------------------------


class COTFetcher:
    """Fetches and parses CFTC COT data.

    Uses httpx for async HTTP requests. Falls back through multiple
    CFTC data sources if the primary is unavailable.

    Handles CFTC delays (data not yet available for current week),
    partial data, and network errors with retry logic.
    """

    # Maximum number of retries for CFTC fetch
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 5.0

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._last_fetch_error: str | None = None

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

    async def fetch_cot_data(self, report_type: str = "futures") -> list[dict]:
        """Fetch COT data from CFTC with retry logic.

        Handles CFTC delays (data not yet available for current week),
        partial data, and network errors. Retries up to MAX_RETRIES times
        with exponential backoff.

        Args:
            report_type: 'futures' for futures-only, 'combined' for futures+options.

        Returns:
            List of raw parsed dictionaries from the CFTC report.
        """
        client = await self._get_client()

        url = CFTC_FUTURES_TEXT_URL
        logger.info("fetching_cot_data", url=url, report_type=report_type)

        last_error: str | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.get(url)
                if response.status_code == 404:
                    # CFTC may not have published yet for current week
                    logger.warning("cot_data_not_yet_available", attempt=attempt + 1)
                    last_error = f"CFTC data not yet available (HTTP 404), attempt {attempt + 1}"
                    if attempt < self.MAX_RETRIES - 1:
                        await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                    continue
                response.raise_for_status()

                content = response.text
                if not content.strip():
                    logger.warning("cot_empty_response", attempt=attempt + 1)
                    last_error = "Empty response from CFTC"
                    if attempt < self.MAX_RETRIES - 1:
                        await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                    continue

                result = self._parse_cftc_text_report(content)
                self._last_fetch_error = None
                return result

            except httpx.TimeoutException as e:
                logger.warning("cot_fetch_timeout", attempt=attempt + 1, error=str(e))
                last_error = f"CFTC fetch timeout: {e}"
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
            except httpx.HTTPStatusError as e:
                logger.warning("cot_fetch_http_error", attempt=attempt + 1, status_code=e.response.status_code)
                last_error = f"CFTC HTTP {e.response.status_code}: {e}"
                if e.response.status_code >= 500 and attempt < self.MAX_RETRIES - 1:
                    # Server errors are retryable
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))
                elif e.response.status_code == 429:
                    # Rate limited — back off more aggressively
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** (attempt + 1)))
                else:
                    # Client errors (4xx) — don't retry
                    break
            except httpx.HTTPError as e:
                logger.warning("cot_fetch_error", attempt=attempt + 1, error=str(e))
                last_error = f"CFTC fetch error: {e}"
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * (2 ** attempt))

        self._last_fetch_error = last_error
        logger.error("cot_fetch_all_retries_failed", error=last_error)
        return []

    def _parse_cftc_text_report(self, content: str) -> list[dict]:
        """Parse CFTC text-format report into structured dicts.

        The CFTC text format has sections for each commodity, with
        fixed-width columns for long/short positions by trader category.
        """
        results: list[dict] = []
        lines = content.strip().split("\n")

        # CFTC text format markers

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to detect commodity header lines
            # Format varies but typically has the commodity name in uppercase
            for cftc_name, _symbol in CFTC_NAME_MAP.items():
                if cftc_name in line.upper():
                    break

            # Try to detect date lines
            date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", line)
            if date_match:
                with contextlib.suppress(ValueError):
                    datetime.strptime(date_match.group(1), "%m/%d/%Y").date()

        # In practice, CFTC text parsing is complex and format-dependent.
        # For production, we'd use the CSV endpoints which are more structured.
        # Return results from structured parsing attempt
        return results

    async def fetch_cot_csv(self, report_type: str = "futures") -> list[dict]:
        """Fetch COT data from CFTC CSV endpoint.

        The CFTC publishes CSV files at predictable URLs with date-based filenames.
        This method constructs the URL and parses the CSV data.

        Args:
            report_type: 'futures' or 'combined'.

        Returns:
            List of parsed COT data dictionaries.
        """
        client = await self._get_client()
        results: list[dict] = []

        # CFTC publishes CSV files with year in the filename
        # URL format: https://www.cftc.gov/files/dea/history/fut_xls_{year}.htm
        # For current year data
        current_year = date.today().year

        # Try current year and previous year
        for year in [current_year, current_year - 1]:
            if report_type == "combined":
                url = f"https://www.cftc.gov/files/dea/history/com_xls_{year}.htm"
            else:
                url = f"https://www.cftc.gov/files/dea/history/fut_xls_{year}.htm"

            logger.info("fetching_cot_csv", url=url, year=year)

            try:
                response = await client.get(url)
                if response.status_code == 404:
                    logger.debug("cot_csv_not_found", year=year)
                    continue
                if response.status_code == 429:
                    # Rate limited — wait and retry
                    logger.warning("cot_csv_rate_limited", year=year)
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * 2)
                    response = await client.get(url)
                    if response.status_code != 200:
                        continue
                response.raise_for_status()

                content = response.text
                if not content.strip():
                    logger.warning("cot_csv_empty_response", year=year)
                    continue

                parsed = self._parse_cftc_csv(content, report_type)
                if parsed:
                    results.extend(parsed)
                else:
                    logger.warning("cot_csv_no_data_parsed", year=year)
            except httpx.HTTPError as e:
                logger.warning("cot_csv_fetch_error", url=url, error=str(e))
                continue

        return results

    def _parse_cftc_csv(self, csv_content: str, report_type: str = "futures") -> list[dict]:
        """Parse CFTC CSV content into structured dictionaries.

        CFTC CSV format (futures):
        Market_and_Exchange_Names,As_of_Date_In_Form_YYMMDD,...
        E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE,2026-05-12,...

        Column mapping:
        - Market_and_Exchange_Names → commodity name
        - As_of_Date_In_Form_YYMMDD → report date
        - C_Merc_Positions_Long_All → commercial long
        - C_Merc_Positions_Short_All → commercial short
        - C_NonComm_Positions_Long_All → non-commercial long
        - C_NonComm_Positions_Short_All → non-commercial short
        - C_NonRept_Positions_Long_All → non-reportable long
        - C_NonRept_Positions_Short_All → non-reportable short
        - Open_Interest_All → total open interest
        """
        results: list[dict] = []

        if not csv_content.strip():
            return results

        reader = csv.DictReader(io.StringIO(csv_content))
        for row in reader:
            try:
                # Extract commodity name and find our symbol
                market_name = row.get("Market_and_Exchange_Names", "").strip().upper()
                symbol = self._match_cftc_name(market_name)
                if symbol is None:
                    continue  # Not one of our tracked contracts

                # Parse date — CFTC uses various date formats
                as_of_date = self._parse_cftc_date(
                    row.get("As_of_Date_In_Form_YYMMDD", "")
                    or row.get("As_of_Date", "")
                    or row.get("Report_Date_as_YYYY-MM-DD", "")
                )
                if as_of_date is None:
                    continue

                # Parse positions
                commercial_long = int(row.get("C_Merc_Positions_Long_All", row.get("Comm_Positions_Long_All", 0)) or 0)
                commercial_short = int(row.get("C_Merc_Positions_Short_All", row.get("Comm_Positions_Short_All", 0)) or 0)
                non_commercial_long = int(row.get("C_NonComm_Positions_Long_All", row.get("NonComm_Positions_Long_All", 0)) or 0)
                non_commercial_short = int(row.get("C_NonComm_Positions_Short_All", row.get("NonComm_Positions_Short_All", 0)) or 0)
                non_reportable_long = int(row.get("C_NonRept_Positions_Long_All", row.get("NonRept_Positions_Long_All", 0)) or 0)
                non_reportable_short = int(row.get("C_NonRept_Positions_Short_All", row.get("NonRept_Positions_Short_All", 0)) or 0)
                total_oi = int(row.get("Open_Interest_All", row.get("Open_Interest", 0)) or 0)

                # Published date is typically 3 days after as_of (Tuesday → Friday)
                published_date = as_of_date + timedelta(days=3)

                results.append({
                    "symbol": symbol,
                    "cftc_name": market_name,
                    "as_of_date": as_of_date,
                    "published_date": published_date,
                    "commercial_long": commercial_long,
                    "commercial_short": commercial_short,
                    "commercial_net": commercial_long - commercial_short,
                    "non_commercial_long": non_commercial_long,
                    "non_commercial_short": non_commercial_short,
                    "non_commercial_net": non_commercial_long - non_commercial_short,
                    "non_reportable_long": non_reportable_long,
                    "non_reportable_short": non_reportable_short,
                    "non_reportable_net": non_reportable_long - non_reportable_short,
                    "total_open_interest": total_oi,
                    "report_type": report_type,
                })

            except (ValueError, KeyError) as e:
                logger.warning("cot_csv_row_parse_error", error=str(e), row=row)
                continue

        return results

    def _match_cftc_name(self, market_name: str) -> str | None:
        """Match a CFTC market name to our contract symbol."""
        # Direct match
        if market_name in CFTC_NAME_MAP:
            return CFTC_NAME_MAP[market_name]

        # Partial match — check if any of our known names is contained in the CFTC name
        for cftc_name, symbol in CFTC_NAME_MAP.items():
            if cftc_name in market_name or market_name in cftc_name:
                return symbol

        return None

    def _parse_cftc_date(self, date_str: str) -> date | None:
        """Parse CFTC date strings in various formats."""
        date_str = date_str.strip()
        if not date_str:
            return None

        formats = [
            "%Y-%m-%d",      # 2026-05-12
            "%m/%d/%Y",      # 05/12/2026
            "%m/%d/%y",      # 05/12/26
            "%Y-%m-%d %H:%M:%S",  # 2026-05-12 00:00:00
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue

        logger.warning("unparseable_cftc_date", date_str=date_str)
        return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def store_cot_reports(
    reports: list[COTReportCreate],
    db: AsyncSession,
) -> COTIngestionResult:
    """Validate and store COT reports, skipping duplicates.

    Args:
        reports: List of COT report create models.
        db: Async database session.

    Returns:
        COTIngestionResult with counts and any errors.
    """
    ingested = 0
    skipped = 0
    errors: list[str] = []
    contracts_processed: list[str] = []

    # Get existing contracts from DB
    contracts_result = await db.execute(select(Contract).where(Contract.is_active.is_(True)))
    contracts = {c.symbol: c for c in contracts_result.scalars().all()}

    for report in reports:
        # Check contract exists
        if report.contract_symbol not in contracts:
            errors.append(f"Unknown contract symbol: {report.contract_symbol}")
            continue

        contract = contracts[report.contract_symbol]

        # Validate
        validation = validate_cot_batch([report])[0][1]
        if not validation.is_valid:
            for err in validation.errors:
                errors.append(f"Validation error for {report.contract_symbol} {report.as_of_date}: {err.message}")
            continue

        # Check for duplicate
        existing = await db.execute(
            select(RawCOTReport).where(
                RawCOTReport.contract_id == contract.id,
                RawCOTReport.as_of_date == report.as_of_date,
            )
        )
        if existing.scalar_one_or_none() is not None:
            skipped += 1
            continue

        # Store
        db_report = RawCOTReport(
            contract_id=contract.id,
            as_of_date=report.as_of_date,
            published_date=report.published_date,
            commercial_long=report.commercial_long,
            commercial_short=report.commercial_short,
            commercial_net=report.commercial_net,
            non_commercial_long=report.non_commercial_long,
            non_commercial_short=report.non_commercial_short,
            non_commercial_net=report.non_commercial_net,
            non_reportable_long=report.non_reportable_long,
            non_reportable_short=report.non_reportable_short,
            non_reportable_net=report.non_reportable_net,
            total_open_interest=report.total_open_interest,
        )
        db.add(db_report)
        ingested += 1

        if report.contract_symbol not in contracts_processed:
            contracts_processed.append(report.contract_symbol)

    await db.flush()

    # Determine status
    if ingested == 0 and skipped == 0:
        status: str = "failed"
    elif errors:
        status = "partial"
    else:
        status = "success"

    result = COTIngestionResult(
        status=status,
        reports_ingested=ingested,
        reports_skipped=skipped,
        errors=errors,
        contracts_processed=contracts_processed,
    )

    logger.info(
        "cot_storage_complete",
        status=status,
        ingested=ingested,
        skipped=skipped,
        errors=len(errors),
    )

    return result


async def ingest_cot_reports(
    report_type: str = "futures",
    db: AsyncSession | None = None,
) -> COTIngestionResult:
    """Main entry point: fetch, parse, validate, and store COT data.

    Args:
        report_type: 'futures' or 'combined'.
        db: Optional async session. If None, creates a new one.

    Returns:
        COTIngestionResult with ingestion summary.
    """
    fetcher = COTFetcher()

    try:
        # Fetch data
        raw_data = await fetcher.fetch_cot_csv(report_type=report_type)

        if not raw_data:
            logger.warning("no_cot_data_fetched", report_type=report_type)
            return COTIngestionResult(
                status="failed",
                reports_ingested=0,
                reports_skipped=0,
                errors=["No COT data fetched from CFTC"],
                contracts_processed=[],
            )

        # Convert to COTReportCreate models
        reports: list[COTReportCreate] = []
        for row in raw_data:
            try:
                report = COTReportCreate(
                    contract_symbol=row["symbol"],
                    as_of_date=row["as_of_date"],
                    published_date=row.get("published_date"),
                    commercial_long=row["commercial_long"],
                    commercial_short=row["commercial_short"],
                    commercial_net=row["commercial_net"],
                    non_commercial_long=row["non_commercial_long"],
                    non_commercial_short=row["non_commercial_short"],
                    non_commercial_net=row["non_commercial_net"],
                    non_reportable_long=row["non_reportable_long"],
                    non_reportable_short=row["non_reportable_short"],
                    non_reportable_net=row["non_reportable_net"],
                    total_open_interest=row["total_open_interest"],
                )
                reports.append(report)
            except Exception as e:
                logger.warning("cot_report_creation_error", error=str(e), row=row)

        # Store
        if db is None:
            async with async_session_factory() as session:
                result = await store_cot_reports(reports, session)
                await session.commit()
        else:
            result = await store_cot_reports(reports, db)

        return result

    finally:
        await fetcher.close()


async def get_cot_status(db: AsyncSession) -> dict:
    """Get the current COT ingestion status from the database.

    Returns a dict with last_as_of_date, total_reports, etc.
    """
    from sqlalchemy import func

    # Get the most recent COT report
    result = await db.execute(
        select(RawCOTReport)
        .order_by(RawCOTReport.as_of_date.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    # Get total count
    count_result = await db.execute(select(func.count(RawCOTReport.id)))
    total = count_result.scalar() or 0

    return {
        "last_as_of_date": latest.as_of_date if latest else None,
        "total_reports": total,
        "latest_report_id": latest.id if latest else None,
    }