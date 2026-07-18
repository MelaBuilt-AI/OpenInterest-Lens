"""Pydantic signal models for OpenInterest Lens.

Defines all signal schemas: PositioningSignal, TermStructureCurve,
RollPressureIndex, ContangoAlert, plus request/response wrappers.
"""

from datetime import date as date_type
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Sub-models shared across signals
# ---------------------------------------------------------------------------


class NetPosition(BaseModel):
    """Net long contracts by trader category."""

    commercial: int = Field(..., description="Net long contracts (commercial hedgers)")
    non_commercial: int = Field(..., description="Net long contracts (managed money / specs)")
    non_reportable: int = Field(..., description="Net long contracts (small traders / retail)")


class SmartMoney(BaseModel):
    """Smart money positioning metrics derived from commercial hedgers."""

    z_score: float = Field(..., description="Z-score of commercial net position vs 52-week range")
    percentile: float = Field(..., ge=0, le=100, description="Percentile rank of current position (0-100)")
    direction: Literal["long", "short", "neutral"] = Field(..., description="Net direction of commercial positioning")
    conviction: Literal["low", "medium", "high"] = Field(..., description="Position extremity classification")


class Retail(BaseModel):
    """Retail positioning metrics derived from non-reportable traders."""

    z_score: float = Field(..., description="Z-score of non-reportable net position")
    percentile: float = Field(..., ge=0, le=100, description="Percentile rank (0-100)")
    direction: Literal["long", "short", "neutral"] = Field(...)
    contrarian_signal: Literal["fade_long", "fade_short", "none"] = Field(
        ..., description="Contrarian fade signal based on extreme retail positioning"
    )


class SignalOverall(BaseModel):
    """Composite signal from smart money vs retail divergence."""

    overall: Literal["bullish", "bearish", "neutral"] = Field(
        ..., description="Composite signal from smart money vs retail divergence"
    )
    strength: float = Field(..., ge=0, le=1, description="Signal confidence (0=weak, 1=strong)")
    divergence: bool = Field(..., description="True when smart money and retail are on opposite sides")


# ---------------------------------------------------------------------------
# Core signal models
# ---------------------------------------------------------------------------


class PositioningSignal(BaseModel):
    """Smart money vs retail positioning derived from COT data."""

    contract: str = Field(..., description="Root symbol, e.g. 'ES'", examples=["ES", "NQ", "CL", "GC"])
    timestamp: datetime = Field(..., description="COT report reference date (Tuesday as-of)")
    as_of_friday: date_type | None = Field(None, description="Date the COT report was published (Friday)")
    net_position: NetPosition
    smart_money: SmartMoney
    retail: Retail
    signal: SignalOverall
    week_over_week_change: NetPosition | None = Field(None, description="Change in net positions from prior week")


class TermStructureMonth(BaseModel):
    """Single month entry in a term structure curve."""

    month: str = Field(..., description="e.g. 'Jun 26'")
    expiry_date: date_type | None = None
    settlement: float
    open_interest: int
    volume: int
    spread_to_front: float = Field(..., description="Price difference from front month")
    annualized_yield: float = Field(..., description="Annualized % vs front month")


class CurveMetrics(BaseModel):
    """Aggregate curve metrics."""

    front_month_oi: int
    total_oi: int
    oi_concentration_pct: float = Field(..., description="Front month OI as % of total")
    avg_daily_volume: int
    steepness: float = Field(..., description="Curve steepness metric (slope)")


class TermStructureCurve(BaseModel):
    """Full term structure across the futures chain for a given contract."""

    contract: str
    timestamp: datetime
    as_of_date: date_type
    structure_type: Literal["contango", "backwardation", "mixed", "flat"]
    months: list[TermStructureMonth]
    curve_metrics: CurveMetrics | None = None


class NearbyContract(BaseModel):
    """Nearby or deferred contract data for roll pressure."""

    month: str = Field(..., description="e.g. 'Jun 26'")
    open_interest: int
    volume: int
    settlement_price: float


class RollPressureMetrics(BaseModel):
    """Roll pressure sub-metrics."""

    index: float = Field(..., ge=0, le=100, description="Roll pressure score (higher = more pressure to roll)")
    oi_decay_pct: float = Field(..., description="Nearby OI decline as % of total OI in last 5 sessions")
    spread_basis: float = Field(..., description="Deferred - nearby price spread (positive = contango)")
    days_to_expiry: int
    roll_window: Literal["pre_roll", "active_roll", "post_roll"] = Field(
        ..., description="Phase of the roll cycle"
    )


class RollPressureIndex(BaseModel):
    """Quantifies pressure to roll nearby positions to deferred contracts."""

    contract: str
    timestamp: datetime
    nearby: NearbyContract
    deferred: NearbyContract
    roll_pressure: RollPressureMetrics


class SpreadSummary(BaseModel):
    """M1/M2 spread summary for contango alert."""

    front_month_price: float
    next_month_price: float
    m1_m2_spread: float = Field(..., description="Next - front month spread in price units")
    m1_m2_annualized: float = Field(..., description="Annualized % spread")
    z_score: float = Field(..., description="Z-score of current spread vs 1-year range")


class ContangoAlert(BaseModel):
    """Signals when a market shifts between contango and backwardation."""

    contract: str
    timestamp: datetime
    structure: Literal["contango", "backwardation", "flat"] = Field(
        ..., description="Current term structure state"
    )
    alert_type: Literal[
        "transition", "extreme_contango", "extreme_backwardation", "steepening", "flattening"
    ] = Field(..., description="What triggered this alert")
    spread_summary: SpreadSummary
    prior_structure: Literal["contango", "backwardation", "flat"]
    days_in_current_state: int
    severity: Literal["info", "warning", "critical"]


# ---------------------------------------------------------------------------
# Request / Response wrappers
# ---------------------------------------------------------------------------


class SignalsRequest(BaseModel):
    """Query parameters for GET /signals/{contract}."""

    include_history: bool = False
    weeks_back: int = Field(4, ge=1, le=52)


class TermStructureRequest(BaseModel):
    """Query parameters for GET /term-structure/{contract}."""

    date: date_type | None = None
    include_history: bool = False
    days_back: int = Field(30, ge=1, le=365)


class COTRequest(BaseModel):
    """Query parameters for GET /cot/{contract}."""

    weeks_back: int = Field(12, ge=1, le=260)


class RollPressureRequest(BaseModel):
    """Query parameters for GET /roll-pressure/{contract}."""

    include_history: bool = False
    days_back: int = Field(30, ge=1, le=365)


class SignalsResponse(BaseModel):
    """Response envelope for positioning signals."""

    contract: str
    current: PositioningSignal
    history: list[PositioningSignal] | None = None


class TermStructureResponse(BaseModel):
    """Response envelope for term structure."""

    contract: str
    current: TermStructureCurve
    contango_alerts: list[ContangoAlert] | None = None
    history: list[TermStructureCurve] | None = None


class COTTraderDetail(BaseModel):
    """COT trader detail with computed z-scores."""

    long: int
    short: int
    net: int
    z_score_52w: float
    percentile_52w: float


class COTReport(BaseModel):
    """Single COT report entry."""

    as_of_date: date_type
    published_date: date_type
    commercial: COTTraderDetail
    non_commercial: COTTraderDetail
    non_reportable: COTTraderDetail
    total_open_interest: int


class COTResponse(BaseModel):
    """Response envelope for COT data."""

    contract: str
    reports: list[COTReport]


class RollPressureResponse(BaseModel):
    """Response envelope for roll pressure."""

    contract: str
    current: RollPressureIndex
    history: list[RollPressureIndex] | None = None


class SignalAlignment(StrEnum):
    """Alignment of the three component signals in the composite."""

    ALIGNED_BULLISH = "ALIGNED_BULLISH"
    ALIGNED_BEARISH = "ALIGNED_BEARISH"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"


class SignalBreakdownItem(BaseModel):
    """Individual signal's contribution to the composite score."""

    signal_type: str = Field(..., description="Type of signal: positioning, term_structure, roll_pressure")
    raw_value: float = Field(..., description="Raw signal value before normalization")
    score: float = Field(..., description="Normalized score (-100 to +100)")
    weight: float = Field(..., ge=0, le=1, description="Weight assigned to this signal")
    contribution: float = Field(..., description="Score * weight (raw contribution to composite)")
    contribution_pct: float = Field(..., ge=-100, le=100, description="Percentage of total absolute contribution")


class HistoricalComparison(BaseModel):
    """Comparison of current composite vs recent history."""

    current: float = Field(..., description="Current composite score")
    average: float = Field(..., description="Average composite over the lookback period")
    min: float = Field(..., description="Minimum composite over the lookback period")
    max: float = Field(..., description="Maximum composite over the lookback period")
    percentile_rank: float = Field(..., ge=0, le=100, description="Percentile rank of current score in lookback window")
    values: list[float] = Field(default_factory=list, description="Historical composite scores")


class CompositeSignalResponse(BaseModel):
    """Unified market structure composite signal response.

    Combines positioning, term structure, and roll pressure signals
    into one actionable market structure assessment.
    """

    contract: str = Field(..., description="Root symbol, e.g. 'ES'")
    timestamp: datetime = Field(..., description="When this composite was computed")
    positioning_score: float | None = Field(None, description="Positioning signal score (-100 to +100)")
    term_structure_score: float | None = Field(None, description="Term structure signal score (-100 to +100)")
    roll_pressure_score: float | None = Field(None, description="Roll pressure signal score (-100 to +100)")
    composite_score: float = Field(..., description="Weighted composite score (-100 to +100)")
    signal_alignment: SignalAlignment = Field(..., description="How aligned the three signals are")
    confidence: float = Field(..., ge=0, le=1, description="Confidence based on signal agreement (0-1)")
    interpretation: str = Field(..., description="Human-readable interpretation of the composite score")
    historical_comparison: HistoricalComparison | None = Field(None, description="How today's composite compares to the last 30 days")
    weights: dict[str, float] = Field(..., description="Weight configuration used for the composite")
    breakdown: list[SignalBreakdownItem] = Field(default_factory=list, description="Per-signal contribution breakdown")


class ContractMetadata(BaseModel):
    """Contract listing metadata."""

    symbol: str
    exchange: str
    asset_class: str
    full_name: str
    tick_size: float
    contract_size: float
    months_traded: list[str]
    data_available_from: str
    signals_available: list[str]


class ContractsResponse(BaseModel):
    """Response envelope for contract listing."""

    contracts: list[ContractMetadata]


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    error: str
    message: str
    retry_after: int | None = None


# ---------------------------------------------------------------------------
# Week 3: Signal computation request/response models
# ---------------------------------------------------------------------------


class SignalRequest(BaseModel):
    """Request body for signal computation endpoints."""

    commodity: str | None = Field(
        None,
        min_length=1,
        max_length=10,
        description="Root symbol, e.g. 'ES'. If None, computes for all tracked commodities.",
    )
    date_range_start: date_type | None = Field(
        None,
        description="Start date for lookback window (YYYY-MM-DD). Defaults to 52 weeks ago.",
    )
    date_range_end: date_type | None = Field(
        None,
        description="End date for lookback window (YYYY-MM-DD). Defaults to today.",
    )
    lookback_weeks: int = Field(
        52,
        ge=4,
        le=260,
        description="Number of weeks for Z-score/percentile lookback window.",
    )
    signal_types: list[str] = Field(
        default_factory=lambda: ["positioning"],
        description="Which signal types to compute: 'positioning', 'smart_money', 'retail_contrarian'.",
    )


class TraderPositionBreakdown(BaseModel):
    """Detailed position breakdown for a single trader category."""

    long: int = Field(..., description="Long contracts")
    short: int = Field(..., description="Short contracts")
    net: int = Field(..., description="Net position (long - short)")
    z_score: float = Field(..., description="Z-score vs lookback window")
    percentile: float = Field(..., ge=0, le=100, description="Percentile rank (0-100)")
    direction: Literal["long", "short", "neutral"] = Field(..., description="Net direction")


class PositioningBreakdown(BaseModel):
    """Full positioning breakdown across trader categories."""

    commercial: TraderPositionBreakdown = Field(
        ..., description="Commercial hedger positions with Z-scores"
    )
    non_commercial: TraderPositionBreakdown = Field(
        ..., description="Non-commercial (managed money) positions with Z-scores"
    )
    non_reportable: TraderPositionBreakdown = Field(
        ..., description="Non-reportable (retail) positions with Z-scores"
    )


class SignalMetadata(BaseModel):
    """Metadata about a signal computation."""

    lookback_weeks: int = Field(..., description="Number of weeks used for statistics")
    data_points: int = Field(..., description="Number of COT reports used in calculation")
    as_of_date: date_type = Field(..., description="Most recent COT reference date used")
    computed_at: datetime = Field(..., description="When this signal was computed")
    cache_hit: bool = Field(False, description="Whether the result was served from cache")


class SignalResponse(BaseModel):
    """Response for a single signal computation."""

    signal_type: str = Field(..., description="Type of signal: 'positioning', 'smart_money', 'retail_contrarian'")
    commodity: str = Field(..., description="Root symbol, e.g. 'ES'")
    value: float = Field(..., description="Signal value (Z-score for positioning)")
    confidence: float = Field(..., ge=0, le=1, description="Signal confidence (0=weak, 1=strong)")
    direction: Literal["bullish", "bearish", "neutral"] = Field(..., description="Signal direction")
    metadata: SignalMetadata = Field(..., description="Computation metadata")
    positioning_breakdown: PositioningBreakdown | None = Field(
        None, description="Detailed breakdown if signal_type is 'positioning'"
    )


class PositioningSignalResponse(BaseModel):
    """Full positioning signal response — combines smart money, retail, and composite."""

    commodity: str = Field(..., description="Root symbol")
    signal: PositioningSignal = Field(..., description="The computed positioning signal")
    breakdown: PositioningBreakdown = Field(..., description="Detailed position breakdown with Z-scores")
    metadata: SignalMetadata = Field(..., description="Computation metadata")


class MultiCommoditySignalResponse(BaseModel):
    """Response for signals across multiple commodities."""

    signals: list[PositioningSignalResponse] = Field(
        default_factory=list, description="Positioning signals per commodity"
    )
    computed_at: datetime = Field(..., description="Timestamp of computation")