"""Data quality monitoring service for OpenInterest Lens.

Tracks staleness, gaps, completeness, and overall data health.
Stores quality metrics in Redis with TTL for production, or
in-memory for dev/testing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Contract, RawCOTReport, RawSettlement

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Quality thresholds
# ---------------------------------------------------------------------------

# COT data is published weekly (Tuesday as-of, Friday publication)
# Considered stale if no data in the last 14 days
COT_STALE_THRESHOLD_DAYS = 14

# Settlement data is published daily
# Considered stale if no data in the last 3 business days
SETTLEMENT_STALE_THRESHOLD_DAYS = 3

# COT data should be weekly — gap if missing >2 consecutive Tuesdays
COT_GAP_THRESHOLD_WEEKS = 2

# Settlement data should be daily — gap if missing >3 consecutive days
SETTLEMENT_GAP_THRESHOLD_DAYS = 3

# Minimum expected fields for completeness
COT_REQUIRED_FIELDS = [
    "commercial_long", "commercial_short", "commercial_net",
    "non_commercial_long", "non_commercial_short", "non_commercial_net",
    "non_reportable_long", "non_reportable_short", "non_reportable_net",
    "total_open_interest",
]

SETTLEMENT_REQUIRED_FIELDS = [
    "settlement_price", "open_interest", "volume",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class StalenessCheck:
    """Result of a data staleness check."""
    source: str  # "cot" or "settlements"
    contract: str
    is_stale: bool
    last_data_date: Optional[str]
    days_since_last: Optional[int]
    threshold_days: int
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GapCheck:
    """Result of a date gap check."""
    source: str
    contract: str
    has_gaps: bool
    gap_count: int
    gap_dates: list[str]  # Missing dates
    expected_frequency: str  # "weekly" or "daily"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompletenessCheck:
    """Result of a field completeness check."""
    source: str
    contract: str
    is_complete: bool
    total_records: int
    complete_records: int
    missing_fields: list[str]
    completeness_pct: float  # 0.0 - 100.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DataQualityReport:
    """Overall data quality report."""
    generated_at: str
    contracts: list[str]
    cot_staleness: list[dict]
    settlement_staleness: list[dict]
    cot_gaps: list[dict]
    settlement_gaps: list[dict]
    cot_completeness: list[dict]
    settlement_completeness: list[dict]
    overall_health: str  # "healthy", "degraded", "critical"
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# In-memory quality metrics cache
# ---------------------------------------------------------------------------

_quality_cache: dict[str, dict] = {}
_QUALITY_CACHE_TTL_SECONDS = 300  # 5 minutes


class DataQualityService:
    """Service for monitoring and reporting on data quality.

    Checks staleness, gaps, and completeness of COT and settlement data.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[datetime, dict]] = {}

    # -------------------------------------------------------------------
    # Staleness checks
    # -------------------------------------------------------------------

    async def check_staleness(
        self, contract: str, db: AsyncSession
    ) -> dict:
        """Check if data for a contract is stale.

        Returns staleness info for both COT and settlement data.
        """
        results = {}

        # COT staleness
        cot_staleness = await self._check_cot_staleness(contract, db)
        results["cot"] = cot_staleness.to_dict()

        # Settlement staleness
        settlement_staleness = await self._check_settlement_staleness(contract, db)
        results["settlements"] = settlement_staleness.to_dict()

        return results

    async def _check_cot_staleness(
        self, contract: str, db: AsyncSession
    ) -> StalenessCheck:
        """Check COT data staleness for a contract."""
        # Get contract ID
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return StalenessCheck(
                source="cot",
                contract=contract,
                is_stale=True,
                last_data_date=None,
                days_since_last=None,
                threshold_days=COT_STALE_THRESHOLD_DAYS,
                warning=f"Contract '{contract}' not found or inactive",
            )

        # Get most recent COT date
        result = await db.execute(
            select(func.max(RawCOTReport.as_of_date))
            .where(RawCOTReport.contract_id == contract_id)
        )
        last_date = result.scalar_one_or_none()

        if last_date is None:
            return StalenessCheck(
                source="cot",
                contract=contract,
                is_stale=True,
                last_data_date=None,
                days_since_last=None,
                threshold_days=COT_STALE_THRESHOLD_DAYS,
                warning="No COT data available",
            )

        # Handle datetime vs date
        if isinstance(last_date, datetime):
            last_date = last_date.date()

        days_since = (date.today() - last_date).days
        is_stale = days_since > COT_STALE_THRESHOLD_DAYS
        warning = None
        if is_stale:
            warning = f"COT data is {days_since} days old (threshold: {COT_STALE_THRESHOLD_DAYS} days)"
        elif days_since > COT_STALE_THRESHOLD_DAYS // 2:
            warning = f"COT data is {days_since} days old, approaching staleness threshold"

        return StalenessCheck(
            source="cot",
            contract=contract,
            is_stale=is_stale,
            last_data_date=last_date.isoformat(),
            days_since_last=days_since,
            threshold_days=COT_STALE_THRESHOLD_DAYS,
            warning=warning,
        )

    async def _check_settlement_staleness(
        self, contract: str, db: AsyncSession
    ) -> StalenessCheck:
        """Check settlement data staleness for a contract."""
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return StalenessCheck(
                source="settlements",
                contract=contract,
                is_stale=True,
                last_data_date=None,
                days_since_last=None,
                threshold_days=SETTLEMENT_STALE_THRESHOLD_DAYS,
                warning=f"Contract '{contract}' not found or inactive",
            )

        result = await db.execute(
            select(func.max(RawSettlement.settlement_date))
            .where(RawSettlement.contract_id == contract_id)
        )
        last_date = result.scalar_one_or_none()

        if last_date is None:
            return StalenessCheck(
                source="settlements",
                contract=contract,
                is_stale=True,
                last_data_date=None,
                days_since_last=None,
                threshold_days=SETTLEMENT_STALE_THRESHOLD_DAYS,
                warning="No settlement data available",
            )

        if isinstance(last_date, datetime):
            last_date = last_date.date()

        days_since = (date.today() - last_date).days
        is_stale = days_since > SETTLEMENT_STALE_THRESHOLD_DAYS
        warning = None
        if is_stale:
            warning = f"Settlement data is {days_since} days old (threshold: {SETTLEMENT_STALE_THRESHOLD_DAYS} days)"
        elif days_since > 1:
            warning = f"Settlement data is {days_since} days old"

        return StalenessCheck(
            source="settlements",
            contract=contract,
            is_stale=is_stale,
            last_data_date=last_date.isoformat(),
            days_since_last=days_since,
            threshold_days=SETTLEMENT_STALE_THRESHOLD_DAYS,
            warning=warning,
        )

    # -------------------------------------------------------------------
    # Gap detection
    # -------------------------------------------------------------------

    async def check_gaps(
        self, contract: str, start: date, end: date, db: AsyncSession
    ) -> dict:
        """Check for data gaps in the specified date range.

        Returns gap info for both COT and settlement data.
        """
        results = {}

        # COT gaps (weekly — Tuesdays)
        cot_gaps = await self._check_cot_gaps(contract, start, end, db)
        results["cot"] = cot_gaps.to_dict()

        # Settlement gaps (daily — business days)
        settlement_gaps = await self._check_settlement_gaps(contract, start, end, db)
        results["settlements"] = settlement_gaps.to_dict()

        return results

    async def _check_cot_gaps(
        self, contract: str, start: date, end: date, db: AsyncSession
    ) -> GapCheck:
        """Check for missing Tuesdays in COT data."""
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return GapCheck(
                source="cot",
                contract=contract,
                has_gaps=True,
                gap_count=0,
                gap_dates=[],
                expected_frequency="weekly",
            )

        # Get all COT dates for this contract
        result = await db.execute(
            select(RawCOTReport.as_of_date)
            .where(
                RawCOTReport.contract_id == contract_id,
                RawCOTReport.as_of_date >= start,
                RawCOTReport.as_of_date <= end,
            )
            .order_by(RawCOTReport.as_of_date)
        )
        existing_dates = {r.date() if isinstance(r, datetime) else r for (r,) in result.all()}

        # Generate expected Tuesdays
        expected_tuesdays = []
        current = start
        while current <= end:
            if current.weekday() == 1:  # Tuesday
                expected_tuesdays.append(current)
            current += timedelta(days=1)

        # Find gaps (excluding future Tuesdays)
        today = date.today()
        gap_dates = [t.isoformat() for t in expected_tuesdays if t not in existing_dates and t <= today]

        return GapCheck(
            source="cot",
            contract=contract,
            has_gaps=len(gap_dates) > 0,
            gap_count=len(gap_dates),
            gap_dates=gap_dates,
            expected_frequency="weekly",
        )

    async def _check_settlement_gaps(
        self, contract: str, start: date, end: date, db: AsyncSession
    ) -> GapCheck:
        """Check for missing business days in settlement data."""
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return GapCheck(
                source="settlements",
                contract=contract,
                has_gaps=True,
                gap_count=0,
                gap_dates=[],
                expected_frequency="daily",
            )

        # Get all settlement dates
        result = await db.execute(
            select(RawSettlement.settlement_date)
            .where(
                RawSettlement.contract_id == contract_id,
                RawSettlement.settlement_date >= start,
                RawSettlement.settlement_date <= end,
            )
            .distinct()
        )
        existing_dates = {r.date() if isinstance(r, datetime) else r for (r,) in result.all()}

        # Generate expected business days (Mon-Fri)
        expected_days = []
        current = start
        while current <= end:
            if current.weekday() < 5:  # Mon-Fri
                expected_days.append(current)
            current += timedelta(days=1)

        # Find gaps (excluding future days and weekends)
        today = date.today()
        gap_dates = [d.isoformat() for d in expected_days if d not in existing_dates and d <= today]

        return GapCheck(
            source="settlements",
            contract=contract,
            has_gaps=len(gap_dates) > 0,
            gap_count=len(gap_dates),
            gap_dates=gap_dates,
            expected_frequency="daily",
        )

    # -------------------------------------------------------------------
    # Completeness checks
    # -------------------------------------------------------------------

    async def check_completeness(
        self, contract: str, db: AsyncSession
    ) -> dict:
        """Check data completeness for a contract.

        Verifies all expected fields are present and non-null.
        """
        results = {}

        # COT completeness
        cot_completeness = await self._check_cot_completeness(contract, db)
        results["cot"] = cot_completeness.to_dict()

        # Settlement completeness
        settlement_completeness = await self._check_settlement_completeness(contract, db)
        results["settlements"] = settlement_completeness.to_dict()

        return results

    async def _check_cot_completeness(
        self, contract: str, db: AsyncSession
    ) -> CompletenessCheck:
        """Check COT data completeness for a contract."""
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return CompletenessCheck(
                source="cot",
                contract=contract,
                is_complete=False,
                total_records=0,
                complete_records=0,
                missing_fields=["contract_id"],
                completeness_pct=0.0,
            )

        result = await db.execute(
            select(RawCOTReport)
            .where(RawCOTReport.contract_id == contract_id)
            .order_by(RawCOTReport.as_of_date.desc())
            .limit(52)  # Check last 52 weeks (1 year)
        )
        reports = result.scalars().all()

        if not reports:
            return CompletenessCheck(
                source="cot",
                contract=contract,
                is_complete=False,
                total_records=0,
                complete_records=0,
                missing_fields=COT_REQUIRED_FIELDS,
                completeness_pct=0.0,
            )

        complete = 0
        all_missing: set[str] = set()
        for report in reports:
            missing = []
            for f in COT_REQUIRED_FIELDS:
                val = getattr(report, f, None)
                if val is None or val == 0:
                    missing.append(f)
            if not missing:
                complete += 1
            all_missing.update(missing)

        pct = (complete / len(reports) * 100) if reports else 0.0

        return CompletenessCheck(
            source="cot",
            contract=contract,
            is_complete=len(all_missing) == 0,
            total_records=len(reports),
            complete_records=complete,
            missing_fields=sorted(all_missing),
            completeness_pct=round(pct, 1),
        )

    async def _check_settlement_completeness(
        self, contract: str, db: AsyncSession
    ) -> CompletenessCheck:
        """Check settlement data completeness for a contract."""
        contract_result = await db.execute(
            select(Contract.id).where(
                Contract.symbol == contract, Contract.is_active.is_(True)
            )
        )
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            return CompletenessCheck(
                source="settlements",
                contract=contract,
                is_complete=False,
                total_records=0,
                complete_records=0,
                missing_fields=["contract_id"],
                completeness_pct=0.0,
            )

        result = await db.execute(
            select(RawSettlement)
            .where(RawSettlement.contract_id == contract_id)
            .order_by(RawSettlement.settlement_date.desc())
            .limit(30)  # Check last 30 days
        )
        settlements = result.scalars().all()

        if not settlements:
            return CompletenessCheck(
                source="settlements",
                contract=contract,
                is_complete=False,
                total_records=0,
                complete_records=0,
                missing_fields=SETTLEMENT_REQUIRED_FIELDS,
                completeness_pct=0.0,
            )

        complete = 0
        all_missing: set[str] = set()
        for settlement in settlements:
            missing = []
            for f in SETTLEMENT_REQUIRED_FIELDS:
                val = getattr(settlement, f, None)
                if val is None or (isinstance(val, (int, float)) and val == 0):
                    missing.append(f)
            if not missing:
                complete += 1
            all_missing.update(missing)

        pct = (complete / len(settlements) * 100) if settlements else 0.0

        return CompletenessCheck(
            source="settlements",
            contract=contract,
            is_complete=len(all_missing) == 0,
            total_records=len(settlements),
            complete_records=complete,
            missing_fields=sorted(all_missing),
            completeness_pct=round(pct, 1),
        )

    # -------------------------------------------------------------------
    # Full quality report
    # -------------------------------------------------------------------

    async def get_quality_report(
        self, db: AsyncSession, contract: Optional[str] = None
    ) -> DataQualityReport:
        """Generate a full data quality report.

        Args:
            db: Database session.
            contract: Optional specific contract. If None, checks all active contracts.

        Returns:
            DataQualityReport with staleness, gaps, and completeness info.
        """
        # Get contracts to check
        if contract:
            result = await db.execute(
                select(Contract).where(
                    Contract.symbol == contract, Contract.is_active.is_(True)
                )
            )
        else:
            result = await db.execute(
                select(Contract).where(Contract.is_active.is_(True))
            )
        contracts = result.scalars().all()

        if not contracts:
            return DataQualityReport(
                generated_at=datetime.now(timezone.utc).isoformat(),
                contracts=[],
                cot_staleness=[],
                settlement_staleness=[],
                cot_gaps=[],
                settlement_gaps=[],
                cot_completeness=[],
                settlement_completeness=[],
                overall_health="critical",
                warnings=["No active contracts found"],
            )

        cot_staleness = []
        settlement_staleness = []
        cot_gaps = []
        settlement_gaps = []
        cot_completeness = []
        settlement_completeness = []
        warnings = []

        end = date.today()
        start = end - timedelta(days=90)  # Check last 90 days

        for c in contracts:
            sym = c.symbol

            # Staleness
            staleness = await self.check_staleness(sym, db)
            cot_staleness.append(staleness["cot"])
            settlement_staleness.append(staleness["settlements"])

            if staleness["cot"]["is_stale"]:
                warnings.append(f"COT data for {sym} is stale: {staleness['cot'].get('warning', 'unknown')}")
            if staleness["settlements"]["is_stale"]:
                warnings.append(f"Settlement data for {sym} is stale: {staleness['settlements'].get('warning', 'unknown')}")

            # Gaps
            gaps = await self.check_gaps(sym, start, end, db)
            cot_gaps.append(gaps["cot"])
            settlement_gaps.append(gaps["settlements"])

            # Completeness
            completeness = await self.check_completeness(sym, db)
            cot_completeness.append(completeness["cot"])
            settlement_completeness.append(completeness["settlements"])

        # Determine overall health
        stale_count = sum(1 for s in cot_staleness + settlement_staleness if s.get("is_stale", False))
        gap_count = sum(g.get("gap_count", 0) for g in cot_gaps + settlement_gaps)
        incomplete_count = sum(
            1 for c in cot_completeness + settlement_completeness if not c.get("is_complete", False)
        )

        total_checks = len(contracts) * 4  # 2 sources × 2 checks
        health_score = max(0, total_checks - stale_count - (gap_count > 0) * len(contracts) - incomplete_count)
        health_ratio = health_score / max(1, total_checks)

        if health_ratio >= 0.8:
            overall_health = "healthy"
        elif health_ratio >= 0.5:
            overall_health = "degraded"
        else:
            overall_health = "critical"

        return DataQualityReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            contracts=[c.symbol for c in contracts],
            cot_staleness=cot_staleness,
            settlement_staleness=settlement_staleness,
            cot_gaps=cot_gaps,
            settlement_gaps=settlement_gaps,
            cot_completeness=cot_completeness,
            settlement_completeness=settlement_completeness,
            overall_health=overall_health,
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_data_quality_service: Optional[DataQualityService] = None


def get_data_quality_service() -> DataQualityService:
    """Get or create the global DataQualityService instance."""
    global _data_quality_service
    if _data_quality_service is None:
        _data_quality_service = DataQualityService()
    return _data_quality_service