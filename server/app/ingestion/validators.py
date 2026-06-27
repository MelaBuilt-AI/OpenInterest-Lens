"""Data validation for ingested COT and settlement data.

Validates raw data before storage: null checks, numeric ranges,
date consistency, duplicate detection.
"""

from __future__ import annotations

from datetime import date

import structlog

from app.models.ingestion import COTReportCreate, SettlementCreate

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


class ValidationError:
    """A single validation error for a field."""

    def __init__(self, field: str, message: str, value: object = None) -> None:
        self.field = field
        self.message = message
        self.value = value

    def __repr__(self) -> str:
        return f"ValidationError(field={self.field!r}, message={self.message!r})"

    def to_dict(self) -> dict:
        return {"field": self.field, "message": self.message, "value": str(self.value) if self.value is not None else None}


class ValidationResult:
    """Aggregated validation result — valid flag plus list of errors."""

    def __init__(self, errors: list[ValidationError] | None = None) -> None:
        self.errors: list[ValidationError] = errors or []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, field: str, message: str, value: object = None) -> None:
        self.errors.append(ValidationError(field=field, message=message, value=value))

    def to_dict(self) -> dict:
        return {"valid": self.is_valid, "errors": [e.to_dict() for e in self.errors]}

    def __repr__(self) -> str:
        return f"ValidationResult(valid={self.is_valid}, errors={len(self.errors)})"


# ---------------------------------------------------------------------------
# COT validators
# ---------------------------------------------------------------------------


def validate_cot_report(report: COTReportCreate) -> ValidationResult:
    """Validate a COT report before storage.

    Checks:
    - No nulls in required fields (handled by Pydantic, but we double-check)
    - Open interest is non-negative
    - Net positions are consistent (net ≈ long - short)
    - Date is not in the future
    - Date is a Tuesday (COT reference date)
    - Total OI is reasonable vs sum of positions
    """
    result = ValidationResult()

    # Date checks
    if report.as_of_date > date.today():
        result.add_error("as_of_date", "COT reference date is in the future", report.as_of_date)

    # COT as_of_date should be a Tuesday (CFTC reports are as-of Tuesday)
    weekday = report.as_of_date.weekday()  # 0=Monday, 1=Tuesday...
    if weekday != 1:
        result.add_error(
            "as_of_date",
            f"COT as_of_date should be a Tuesday, got {report.as_of_date.strftime('%A')}",
            report.as_of_date,
        )

    # Published date should be after as_of_date (usually Friday)
    if report.published_date is not None and report.published_date < report.as_of_date:
        result.add_error(
            "published_date",
            "Published date should be on or after the as_of_date",
            report.published_date,
        )

    # Net position consistency checks
    commercial_net_computed = report.commercial_long - report.commercial_short
    if report.commercial_net != commercial_net_computed:
        result.add_error(
            "commercial_net",
            f"commercial_net ({report.commercial_net}) != commercial_long - commercial_short ({commercial_net_computed})",
            report.commercial_net,
        )

    non_commercial_net_computed = report.non_commercial_long - report.non_commercial_short
    if report.non_commercial_net != non_commercial_net_computed:
        result.add_error(
            "non_commercial_net",
            f"non_commercial_net ({report.non_commercial_net}) != non_commercial_long - non_commercial_short ({non_commercial_net_computed})",
            report.non_commercial_net,
        )

    non_reportable_net_computed = report.non_reportable_long - report.non_reportable_short
    if report.non_reportable_net != non_reportable_net_computed:
        result.add_error(
            "non_reportable_net",
            f"non_reportable_net ({report.non_reportable_net}) != non_reportable_long - non_reportable_short ({non_reportable_net_computed})",
            report.non_reportable_net,
        )

    # Total OI reasonableness: should be close to max of (long, short) totals
    total_long = report.commercial_long + report.non_commercial_long + report.non_reportable_long
    total_short = report.commercial_short + report.non_commercial_short + report.non_reportable_short
    max_side = max(total_long, total_short)

    # OI tolerance: allow 20% deviation (some positions may not be categorized)
    if max_side > 0 and report.total_open_interest > 0:
        ratio = report.total_open_interest / max_side
        if ratio < 0.8 or ratio > 1.5:
            result.add_error(
                "total_open_interest",
                f"Total OI ({report.total_open_interest}) is inconsistent with position totals (max_side={max_side}, ratio={ratio:.2f})",
                report.total_open_interest,
            )

    if not result.is_valid:
        logger.warning("cot_validation_failed", errors=[e.to_dict() for e in result.errors])

    return result


def validate_cot_batch(reports: list[COTReportCreate]) -> list[tuple[COTReportCreate, ValidationResult]]:
    """Validate a batch of COT reports. Returns list of (report, result) tuples."""
    return [(report, validate_cot_report(report)) for report in reports]


# ---------------------------------------------------------------------------
# Settlement validators
# ---------------------------------------------------------------------------


# CME month codes: H=Mar, M=Jun, U=Sep, Z=Dec (quarterly), plus monthly codes
MONTH_CODE_MAP = {
    "F": "January", "G": "February", "H": "March", "J": "April",
    "K": "May", "M": "June", "N": "July", "Q": "August",
    "U": "September", "V": "October", "X": "November", "Z": "December",
}


def validate_settlement(settlement: SettlementCreate) -> ValidationResult:
    """Validate a settlement record before storage.

    Checks:
    - Settlement price is positive
    - Open interest is non-negative
    - Volume is non-negative
    - Settlement date is not in the future
    - Month code format is valid
    """
    result = ValidationResult()

    # Price must be positive
    if settlement.settlement_price <= 0:
        result.add_error(
            "settlement_price",
            "Settlement price must be positive",
            settlement.settlement_price,
        )

    # OI and volume non-negative (also in Pydantic, but double-check)
    if settlement.open_interest < 0:
        result.add_error("open_interest", "Open interest must be non-negative", settlement.open_interest)

    if settlement.volume < 0:
        result.add_error("volume", "Volume must be non-negative", settlement.volume)

    # Date checks
    if settlement.settlement_date > date.today():
        result.add_error(
            "settlement_date",
            "Settlement date is in the future",
            settlement.settlement_date,
        )

    # Month code validation
    mc = settlement.month_code.strip()
    # Accept formats: "Jun 26", "H26", "Jun26", "June 2026"
    valid = False

    # Format 1: "Jun 26" or "Jun26" (3-letter month + space? + 2-digit year)
    import re
    if re.match(r"^[A-Z][a-z]{2}\s*\d{2,4}$", mc) or re.match(r"^[FGHJKMNQUVXZ]\d{1,2}$", mc) or re.match(r"^[A-Z][a-z]+\s+\d{4}$", mc):
        valid = True

    if not valid:
        result.add_error(
            "month_code",
            f"Month code '{mc}' is not a recognized format (expected 'Jun 26', 'H26', or 'June 2026')",
            mc,
        )

    if not result.is_valid:
        logger.warning("settlement_validation_failed", errors=[e.to_dict() for e in result.errors])

    return result


def validate_settlement_batch(settlements: list[SettlementCreate]) -> list[tuple[SettlementCreate, ValidationResult]]:
    """Validate a batch of settlement records."""
    return [(s, validate_settlement(s)) for s in settlements]


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def detect_duplicate_cot(
    existing_dates: set[date],
    report: COTReportCreate,
    contract_symbol: str,
) -> bool:
    """Check if a COT report already exists for this contract + date.

    Args:
        existing_dates: Set of as_of_dates already in the DB for this contract.
        report: The new COT report to check.
        contract_symbol: Contract root symbol.

    Returns:
        True if this report is a duplicate.
    """
    # Normalize: compare just the date
    return report.as_of_date in existing_dates


def detect_duplicate_settlement(
    existing_keys: set[tuple[date, str]],
    settlement: SettlementCreate,
    contract_symbol: str,
) -> bool:
    """Check if a settlement record already exists for this contract + month + date.

    Args:
        existing_keys: Set of (settlement_date, month_code) already in the DB.
        settlement: The new settlement record to check.
        contract_symbol: Contract root symbol.

    Returns:
        True if this record is a duplicate.
    """
    return (settlement.settlement_date, settlement.month_code) in existing_keys


# ---------------------------------------------------------------------------
# Date consistency checks
# ---------------------------------------------------------------------------


def check_cot_staleness(last_as_of_date: date | None, max_stale_days: int = 14) -> str | None:
    """Check if the most recent COT data is too stale.

    Returns a warning message if stale, None if fresh.
    """
    if last_as_of_date is None:
        return "No COT data available"

    days_since = (date.today() - last_as_of_date).days
    if days_since > max_stale_days:
        return f"COT data is {days_since} days old (threshold: {max_stale_days} days)"

    return None


def check_settlement_staleness(last_settlement_date: date | None, max_stale_days: int = 3) -> str | None:
    """Check if the most recent settlement data is too stale.

    Returns a warning message if stale, None if fresh.
    Settlement data should be updated daily, so 3 days is already concerning.
    """
    if last_settlement_date is None:
        return "No settlement data available"

    days_since = (date.today() - last_settlement_date).days
    if days_since > max_stale_days:
        return f"Settlement data is {days_since} days old (threshold: {max_stale_days} days)"

    return None