"""Pydantic models for COT and settlement data ingestion.

Defines request/response schemas for:
- COT report ingestion (CFTC Commitments of Traders)
- Settlement ingestion (CME daily settlement prices)
- Ingestion status tracking
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# COT (Commitments of Traders) models
# ---------------------------------------------------------------------------


class COTReportCreate(BaseModel):
    """Schema for creating a new COT report record.

    Maps CFTC CSV data to our internal format before validation and storage.
    """

    contract_symbol: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Root symbol, e.g. 'ES'",
    )
    as_of_date: date = Field(..., description="Tuesday reference date for the COT report")
    published_date: date | None = Field(None, description="Friday date when the COT report was published")
    commercial_long: int = Field(..., description="Commercial hedgers long contracts")
    commercial_short: int = Field(..., description="Commercial hedgers short contracts")
    commercial_net: int = Field(..., description="Commercial net position (long - short)")
    non_commercial_long: int = Field(..., description="Non-commercial (managed money) long contracts")
    non_commercial_short: int = Field(..., description="Non-commercial short contracts")
    non_commercial_net: int = Field(..., description="Non-commercial net position")
    non_reportable_long: int = Field(..., description="Non-reportable (retail) long contracts")
    non_reportable_short: int = Field(..., description="Non-reportable short contracts")
    non_reportable_net: int = Field(..., description="Non-reportable net position")
    total_open_interest: int = Field(..., ge=0, description="Total open interest")

    @field_validator("commercial_long", "commercial_short", "non_commercial_long", "non_commercial_short")
    @classmethod
    def validate_positions_non_negative(cls, v: int) -> int:
        """Long and short positions must be non-negative."""
        if v < 0:
            raise ValueError("Position values must be non-negative")
        return v

    @field_validator("total_open_interest")
    @classmethod
    def validate_oi_positive(cls, v: int) -> int:
        """Total open interest must be positive."""
        if v <= 0:
            raise ValueError("Total open interest must be positive")
        return v


class COTReportResponse(BaseModel):
    """Response schema after successfully ingesting a COT report."""

    id: int
    contract_symbol: str
    as_of_date: date
    published_date: date | None
    commercial_long: int
    commercial_short: int
    commercial_net: int
    non_commercial_long: int
    non_commercial_short: int
    non_commercial_net: int
    non_reportable_long: int
    non_reportable_short: int
    non_reportable_net: int
    total_open_interest: int
    ingestion_timestamp: datetime

    model_config = {"from_attributes": True}


class COTIngestionResult(BaseModel):
    """Summary result of a COT data fetch operation."""

    status: Literal["success", "partial", "failed"]
    reports_ingested: int = Field(..., ge=0, description="Number of new COT reports stored")
    reports_skipped: int = Field(..., ge=0, description="Number of reports skipped (duplicates)")
    errors: list[str] = Field(default_factory=list, description="Error messages per failed row")
    contracts_processed: list[str] = Field(default_factory=list, description="Contract symbols processed")
    as_of_date: date | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Settlement models
# ---------------------------------------------------------------------------


class SettlementCreate(BaseModel):
    """Schema for creating a new settlement record.

    Maps CME daily settlement data to our internal format.
    """

    contract_symbol: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Root symbol, e.g. 'ES'",
    )
    month_code: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Contract month code, e.g. 'Jun 26'",
    )
    settlement_date: date = Field(..., description="Trading date of the settlement")
    settlement_price: float = Field(..., gt=0, description="Settlement price (must be positive)")
    open_interest: int = Field(..., ge=0, description="Open interest (non-negative)")
    volume: int = Field(..., ge=0, description="Volume (non-negative)")

    @field_validator("month_code")
    @classmethod
    def validate_month_code(cls, v: str) -> str:
        """Month code should look like 'Jun 26' or 'H26' — at least 2 chars."""
        if len(v) < 2:
            raise ValueError("Month code must be at least 2 characters")
        return v


class SettlementResponse(BaseModel):
    """Response schema after successfully ingesting a settlement record."""

    id: int
    contract_symbol: str
    month_code: str
    settlement_date: date
    settlement_price: float
    open_interest: int
    volume: int
    ingestion_timestamp: datetime

    model_config = {"from_attributes": True}


class SettlementIngestionResult(BaseModel):
    """Summary result of a settlement data fetch operation."""

    status: Literal["success", "partial", "failed"]
    settlements_ingested: int = Field(..., ge=0, description="Number of new settlement records stored")
    settlements_skipped: int = Field(..., ge=0, description="Number of records skipped (duplicates)")
    errors: list[str] = Field(default_factory=list, description="Error messages per failed row")
    contracts_processed: list[str] = Field(default_factory=list, description="Contract symbols processed")
    settlement_date: date | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Ingestion status model
# ---------------------------------------------------------------------------


class IngestionSourceStatus(BaseModel):
    """Status of a single ingestion source."""

    source: Literal["cot", "settlements"]
    last_run: datetime | None = None
    last_status: Literal["success", "partial", "failed", "never_run"] = "never_run"
    last_records_ingested: int = 0
    last_errors: list[str] = Field(default_factory=list)
    next_scheduled: datetime | None = None


class IngestionStatus(BaseModel):
    """Overall ingestion pipeline status."""

    cot: IngestionSourceStatus
    settlements: IngestionSourceStatus
    uptime_seconds: float = Field(..., description="Seconds since the scheduler started")


class IngestionTriggerResponse(BaseModel):
    """Response when manually triggering an ingestion."""

    triggered: bool = True
    source: Literal["cot", "settlements"]
    message: str
    task_id: str | None = None