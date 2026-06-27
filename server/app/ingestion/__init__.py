"""Ingestion package — COT and settlement data pipelines."""

from app.ingestion.cot import COTFetcher, ingest_cot_reports, store_cot_reports
from app.ingestion.settlements import SettlementFetcher, ingest_settlements, store_settlements
from app.ingestion.validators import (
    ValidationResult,
    ValidationError,
    validate_cot_report,
    validate_cot_batch,
    validate_settlement,
    validate_settlement_batch,
    detect_duplicate_cot,
    detect_duplicate_settlement,
    check_cot_staleness,
    check_settlement_staleness,
)

__all__ = [
    "COTFetcher",
    "ingest_cot_reports",
    "store_cot_reports",
    "SettlementFetcher",
    "ingest_settlements",
    "store_settlements",
    "ValidationResult",
    "ValidationError",
    "validate_cot_report",
    "validate_cot_batch",
    "validate_settlement",
    "validate_settlement_batch",
    "detect_duplicate_cot",
    "detect_duplicate_settlement",
    "check_cot_staleness",
    "check_settlement_staleness",
]