"""Pydantic models matching server response schemas for the OpenInterest Lens SDK."""

from __future__ import annotations

from datetime import date, datetime
from typing import Generic, Literal, Optional, TypeVar

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
    percentile: float = Field(..., ge=0, le=100, description="Percentile rank (0-100)")
    direction: Literal["long", "short", "neutral"] = Field(..., description="Net direction")
    conviction: Literal["low", "medium", "high"] = Field(..., description="Position extremity")


class Retail(BaseModel):
    """Retail positioning metrics derived from non-reportable traders."""

    z_score: float = Field(..., description="Z-score of non-reportable net position")
    percentile: float = Field(..., ge=0, le=100, description="Percentile rank (0-100)")
    direction: Literal["long", "short", "neutral"] = Field(...)
    contrarian_signal: Literal["fade_long", "fade_short", "none"] = Field(
        ..., description="Contrarian fade signal"
    )


class SignalOverall(BaseModel):
    """Composite signal from smart money vs retail divergence."""

    overall: Literal["bullish", "bearish", "neutral"] = Field(
        ..., description="Composite signal direction"
    )
    strength: float = Field(..., ge=0, le=1, description="Signal confidence (0=weak, 1=strong)")
    divergence: bool = Field(..., description="True when smart money and retail are on opposite sides")


# ---------------------------------------------------------------------------
# Core signal models
# ---------------------------------------------------------------------------


class PositioningSignal(BaseModel):
    """Smart money vs retail positioning derived from COT data."""

    contract: str = Field(..., description="Root symbol, e.g. 'ES'")
    timestamp: datetime = Field(..., description="COT report reference date")
    as_of_friday: Optional[date] = Field(None, description="Date the COT report was published")
    net_position: NetPosition
    smart_money: SmartMoney
    retail: Retail
    signal: SignalOverall
    week_over_week_change: Optional[NetPosition] = Field(
        None, description="Change in net positions from prior week"
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

    commercial: TraderPositionBreakdown
    non_commercial: TraderPositionBreakdown
    non_reportable: TraderPositionBreakdown


class SignalMetadata(BaseModel):
    """Metadata about a signal computation."""

    lookback_weeks: int = Field(..., description="Number of weeks used for statistics")
    data_points: int = Field(..., description="Number of COT reports used")
    as_of_date: date = Field(..., description="Most recent COT reference date")
    computed_at: datetime = Field(..., description="When this signal was computed")
    cache_hit: bool = Field(False, description="Whether the result was served from cache")


class PositioningSignalResponse(BaseModel):
    """Full positioning signal response."""

    commodity: str
    signal: PositioningSignal
    breakdown: PositioningBreakdown
    metadata: SignalMetadata


# ---------------------------------------------------------------------------
# Term Structure models
# ---------------------------------------------------------------------------


class TermStructureMonth(BaseModel):
    """Single month entry in a term structure curve."""

    month: str = Field(..., description="Month code, e.g. 'Jun 26'")
    expiry_date: Optional[date] = None
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
    steepness: float = Field(..., description="Curve steepness (slope)")


class ContangoBackwardation(BaseModel):
    """Contango/backwardation indicators."""

    structure_type: Literal["contango", "backwardation", "flat", "mixed"]
    m1_m2_spread: float
    m1_m2_annualized: float
    spread_z_score: float
    confidence: float = Field(..., ge=0, le=1)
    slope: float


class SlopeMetrics(BaseModel):
    """Detailed term structure slope metrics."""

    nearby_deferred_spread: float
    slope_annualized_pct: float
    linear_slope: float
    quadratic_curvature: float
    r_squared_linear: float
    r_squared_quadratic: float


class CalendarSpreadRatios(BaseModel):
    """Calendar spread ratio metrics."""

    front_to_next_ratio: float
    front_to_deferred_ratio: float
    average_monthly_spread_pct: float
    max_spread_pct: float


class TermStructureCurve(BaseModel):
    """Full term structure across the futures chain for a given contract."""

    contract: str
    structure_type: Literal["contango", "backwardation", "flat", "mixed"]
    months: list[TermStructureMonth]
    front_month_oi: int = 0
    total_oi: int = 0
    oi_concentration_pct: float = 0.0
    steepness: float = 0.0


class TermStructureResponse(BaseModel):
    """Response envelope for term structure endpoint."""

    contract: str
    term_structure: Optional[TermStructureCurve] = None
    contango_backwardation: Optional[ContangoBackwardation] = None
    slope_metrics: Optional[SlopeMetrics] = None
    calendar_spread_ratios: Optional[CalendarSpreadRatios] = None
    metadata: SignalMetadata


# ---------------------------------------------------------------------------
# Roll Pressure models
# ---------------------------------------------------------------------------


class NearbyContract(BaseModel):
    """Nearby or deferred contract data for roll pressure."""

    month: str
    open_interest: int
    volume: int
    settlement_price: float


class RollPressureMetrics(BaseModel):
    """Roll pressure sub-metrics."""

    index: float = Field(..., ge=0, le=100, description="Roll pressure score (0-100)")
    oi_decay_pct: float
    spread_basis: float
    days_to_expiry: int
    roll_window: Literal["pre_roll", "active_roll", "post_roll"]


class RollCalendarData(BaseModel):
    """Roll calendar information."""

    nearby_month: str
    nearby_expiry: date
    deferred_month: str
    deferred_expiry: date
    days_to_roll: int
    roll_start_date: date
    roll_end_date: date
    roll_urgency: Literal["imminent", "active", "normal", "relaxed"]


class RollImpactData(BaseModel):
    """Roll impact estimation."""

    impact_score: float = Field(..., ge=0, le=100)
    oi_concentration: float
    volume_shift: float
    expected_slippage: float
    impact_category: Literal["low", "medium", "high", "extreme"]


class RollPressureIndex(BaseModel):
    """Quantifies pressure to roll nearby positions to deferred contracts."""

    contract: str
    timestamp: datetime
    nearby: NearbyContract
    deferred: NearbyContract
    roll_pressure: RollPressureMetrics


class RollPressureResponse(BaseModel):
    """Response envelope for roll pressure endpoint."""

    contract: str
    roll_pressure: Optional[RollPressureMetrics] = None
    roll_calendar: Optional[RollCalendarData] = None
    roll_impact: Optional[RollImpactData] = None
    metadata: SignalMetadata


# ---------------------------------------------------------------------------
# COT Report models
# ---------------------------------------------------------------------------


class COTTraderDetail(BaseModel):
    """COT trader detail with computed z-scores."""

    long: int
    short: int
    net: int
    z_score_52w: float
    percentile_52w: float


class COTReport(BaseModel):
    """Single COT report entry."""

    as_of_date: date
    published_date: Optional[date] = None
    commercial: COTTraderDetail
    non_commercial: COTTraderDetail
    non_reportable: COTTraderDetail
    total_open_interest: int


class COTResponse(BaseModel):
    """Response envelope for COT data."""

    contract: str
    reports: list[COTReport]


# ---------------------------------------------------------------------------
# Contract models
# ---------------------------------------------------------------------------


class Contract(BaseModel):
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

    contracts: list[Contract]


# ---------------------------------------------------------------------------
# Health response
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str = "openinterest-lens"
    version: str


# ---------------------------------------------------------------------------
# Response wrappers (generic)
# ---------------------------------------------------------------------------

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """Generic API response wrapper."""

    data: T
    metadata: Optional[dict] = None


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated response wrapper."""

    data: list[T]
    total: int = 0
    page: int = 1
    page_size: int = 50
    metadata: Optional[dict] = None