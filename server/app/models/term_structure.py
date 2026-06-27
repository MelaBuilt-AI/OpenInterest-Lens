"""Pydantic models for term structure and roll pressure signals.

Request/response models for the term structure and roll pressure API endpoints.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Term Structure Request Models
# ---------------------------------------------------------------------------


class TermStructureRequest(BaseModel):
    """Request parameters for GET /v1/signals/term-structure."""

    commodity: Optional[str] = Field(
        None,
        min_length=1,
        max_length=10,
        description="Root symbol, e.g. 'ES'. If None, computes for all tracked commodities.",
    )
    date_range_start: Optional[date] = Field(
        None,
        description="Start date for term structure analysis (YYYY-MM-DD). Defaults to 30 days ago.",
    )
    date_range_end: Optional[date] = Field(
        None,
        description="End date for term structure analysis (YYYY-MM-DD). Defaults to today.",
    )
    contract_months: Optional[list[str]] = Field(
        None,
        description="Specific contract months to include (e.g., ['Jun 26', 'Sep 26']). If None, includes all available.",
    )


class TermStructureResponse(BaseModel):
    """Response envelope for term structure endpoint."""

    contract: str = Field(..., description="Root symbol, e.g. 'ES'")
    term_structure: Optional["TermStructureCurveFromSignal"] = Field(
        None, description="Computed term structure curve"
    )
    contango_backwardation: Optional["ContangoBackwardationResult"] = Field(
        None, description="Contango/backwardation indicators"
    )
    slope_metrics: Optional["SlopeMetricsResult"] = Field(
        None, description="Detailed slope metrics"
    )
    calendar_spread_ratios: Optional["CalendarSpreadResult"] = Field(
        None, description="Calendar spread ratios"
    )
    metadata: "TermStructureMetadata" = Field(
        ..., description="Computation metadata"
    )


class TermStructureCurveFromSignal(BaseModel):
    """Term structure curve data in API response format."""

    structure_type: Literal["contango", "backwardation", "flat", "mixed"] = Field(
        ..., description="Overall curve classification"
    )
    months: list["TermStructureMonthData"] = Field(
        ..., description="Per-month term structure data"
    )
    front_month_oi: int = Field(..., description="Front month open interest")
    total_oi: int = Field(..., description="Total open interest across all months")
    oi_concentration_pct: float = Field(..., description="Front month OI as % of total")
    steepness: float = Field(..., description="Curve steepness (slope)")


class TermStructureMonthData(BaseModel):
    """Single month entry in term structure response."""

    month: str = Field(..., description="Month code, e.g. 'Jun 26'")
    expiry_date: Optional[date] = None
    settlement: float = Field(..., description="Settlement price")
    open_interest: int = Field(..., description="Open interest")
    volume: int = Field(..., description="Volume")
    spread_to_front: float = Field(..., description="Price difference from front month")
    annualized_yield: float = Field(..., description="Annualized % vs front month")


class ContangoBackwardationResult(BaseModel):
    """Contango/backwardation indicators."""

    structure_type: Literal["contango", "backwardation", "flat", "mixed"] = Field(
        ..., description="Current term structure state"
    )
    m1_m2_spread: float = Field(..., description="Next - front month spread in price units")
    m1_m2_annualized: float = Field(..., description="Annualized % spread")
    spread_z_score: float = Field(..., description="Z-score of current spread vs historical")
    confidence: float = Field(..., ge=0, le=1, description="Confidence score (0-1)")
    slope: float = Field(..., description="Overall curve slope")


class SlopeMetricsResult(BaseModel):
    """Detailed term structure slope metrics."""

    nearby_deferred_spread: float = Field(..., description="M1 to M_n price spread")
    slope_annualized_pct: float = Field(..., description="Annualized slope percentage")
    linear_slope: float = Field(..., description="Linear fit slope coefficient")
    quadratic_curvature: float = Field(..., description="Quadratic fit curvature")
    r_squared_linear: float = Field(..., description="R² of linear fit")
    r_squared_quadratic: float = Field(..., description="R² of quadratic fit")


class CalendarSpreadResult(BaseModel):
    """Calendar spread ratio metrics."""

    front_to_next_ratio: float = Field(..., description="M1/M2 price ratio")
    front_to_deferred_ratio: float = Field(..., description="M1/M_n price ratio")
    average_monthly_spread_pct: float = Field(..., description="Average monthly spread as % of front")
    max_spread_pct: float = Field(..., description="Maximum single-month spread as % of front")


class TermStructureMetadata(BaseModel):
    """Metadata about term structure computation."""

    commodity: str = Field(..., description="Root symbol")
    as_of_date: date = Field(..., description="Date of the term structure snapshot")
    data_points: int = Field(..., description="Number of contract months used")
    computed_at: datetime = Field(..., description="When this computation was performed")
    cache_hit: bool = Field(False, description="Whether result was served from cache")


# ---------------------------------------------------------------------------
# Roll Pressure Request Models
# ---------------------------------------------------------------------------


class RollPressureRequest(BaseModel):
    """Request parameters for GET /v1/signals/roll-pressure."""

    commodity: Optional[str] = Field(
        None,
        min_length=1,
        max_length=10,
        description="Root symbol, e.g. 'ES'. If None, computes for all tracked commodities.",
    )
    date_range_start: Optional[date] = Field(
        None,
        description="Start date for lookback window (YYYY-MM-DD). Defaults to 30 days ago.",
    )
    date_range_end: Optional[date] = Field(
        None,
        description="End date for lookback window (YYYY-MM-DD). Defaults to today.",
    )
    roll_window_days: int = Field(
        5,
        ge=1,
        le=30,
        description="Days before expiry considered as the active roll window.",
    )


class RollPressureResponse(BaseModel):
    """Response envelope for roll pressure endpoint."""

    contract: str = Field(..., description="Root symbol, e.g. 'ES'")
    roll_pressure: Optional["RollPressureData"] = Field(
        None, description="Computed roll pressure metrics"
    )
    roll_calendar: Optional["RollCalendarData"] = Field(
        None, description="Roll calendar information"
    )
    roll_impact: Optional["RollImpactData"] = Field(
        None, description="Roll impact estimation"
    )
    metadata: "RollPressureMetadata" = Field(
        ..., description="Computation metadata"
    )


class RollPressureData(BaseModel):
    """Roll pressure metrics in API response format."""

    index: float = Field(..., ge=0, le=100, description="Roll pressure score (0-100)")
    oi_decay_pct: float = Field(..., description="Nearby OI decline as % of total OI")
    spread_basis: float = Field(..., description="Deferred - nearby price spread")
    days_to_expiry: int = Field(..., description="Calendar days until nearby expiry")
    roll_window: Literal["pre_roll", "active_roll", "post_roll"] = Field(
        ..., description="Phase of the roll cycle"
    )


class RollCalendarData(BaseModel):
    """Roll calendar information in API response."""

    nearby_month: str = Field(..., description="Nearby contract month code")
    nearby_expiry: date = Field(..., description="Nearby contract expiry date")
    deferred_month: str = Field(..., description="Deferred contract month code")
    deferred_expiry: date = Field(..., description="Deferred contract expiry date")
    days_to_roll: int = Field(..., description="Calendar days until nearby expiry")
    roll_start_date: date = Field(..., description="Date when roll window begins")
    roll_end_date: date = Field(..., description="Date when roll window ends")
    roll_urgency: Literal["imminent", "active", "normal", "relaxed"] = Field(
        ..., description="Roll urgency classification"
    )


class RollImpactData(BaseModel):
    """Roll impact estimation in API response."""

    impact_score: float = Field(..., ge=0, le=100, description="Estimated price impact (0-100)")
    oi_concentration: float = Field(..., description="Nearby OI as % of total")
    volume_shift: float = Field(..., description="Volume shifting to deferred as % of total")
    expected_slippage: float = Field(..., description="Estimated slippage in price units")
    impact_category: Literal["low", "medium", "high", "extreme"] = Field(
        ..., description="Impact category"
    )


class RollPressureMetadata(BaseModel):
    """Metadata about roll pressure computation."""

    commodity: str = Field(..., description="Root symbol")
    as_of_date: date = Field(..., description="Date of the analysis")
    lookback_days: int = Field(..., description="Number of days used for OI decay analysis")
    computed_at: datetime = Field(..., description="When this computation was performed")
    cache_hit: bool = Field(False, description="Whether result was served from cache")


# ---------------------------------------------------------------------------
# Multi-commodity response
# ---------------------------------------------------------------------------


class MultiTermStructureResponse(BaseModel):
    """Response for term structure across multiple commodities."""

    commodities: list[TermStructureResponse] = Field(
        default_factory=list, description="Term structure results per commodity"
    )
    computed_at: datetime = Field(..., description="Timestamp of computation")


class MultiRollPressureResponse(BaseModel):
    """Response for roll pressure across multiple commodities."""

    commodities: list[RollPressureResponse] = Field(
        default_factory=list, description="Roll pressure results per commodity"
    )
    computed_at: datetime = Field(..., description="Timestamp of computation")