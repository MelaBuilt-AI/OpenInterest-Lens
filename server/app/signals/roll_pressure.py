"""Roll pressure engine for OpenInterest Lens.

Calculates roll pressure metrics: roll timing windows, roll impact score,
roll date proximity signals, and historical roll patterns. Uses OI decay
rates, settlement price spreads, and roll calendar information to quantify
the pressure on market participants to roll their positions from the nearby
to the deferred contract.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Contract, RawSettlement
from app.models.signal import (
    NearbyContract,
    RollPressureIndex,
    RollPressureMetrics,
)
from app.signals.historical import percentile_rank, rolling_z_score
from app.signals.roll_calendar import (
    ROLL_START_DAYS_BEFORE_EXPIRY,
    calculate_expiry_date,
    calculate_roll_info,
    classify_roll_urgency,
    estimate_oi_decay_rate,
    estimate_roll_volume,
    generate_month_code,
    get_active_contract_months,
    parse_month_code,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lookback window for OI decay calculation (in sessions/days)
DEFAULT_OI_DECAY_LOOKBACK = 5

# Weight factors for roll pressure composite score
WEIGHT_OI_DECAY = 0.30      # How much OI has already moved
WEIGHT_SPREAD_BASIS = 0.25  # Price spread between nearby and deferred
WEIGHT_PROXIMITY = 0.25      # How close to expiry
WEIGHT_VOLUME_RATIO = 0.20   # Volume concentration

# Maximum roll pressure score
MAX_ROLL_PRESSURE = 100.0


# ---------------------------------------------------------------------------
# Roll pressure computation
# ---------------------------------------------------------------------------


async def compute_roll_pressure(
    contract_symbol: str,
    db: AsyncSession,
    days_back: int = 30,
) -> RollPressureIndex:
    """Compute the roll pressure index for a commodity.

    The roll pressure index quantifies the pressure on market participants
    to roll their positions from the nearby (front) to the deferred (next)
    contract. It considers:
    1. OI decay rate — how quickly OI is leaving the nearby contract
    2. Spread basis — the price difference between deferred and nearby
    3. Days to expiry — proximity to the roll date
    4. Volume concentration — is volume shifting to the deferred?

    Args:
        contract_symbol: Root symbol, e.g. 'ES'.
        db: Async database session.
        days_back: Days of historical data to use for decay analysis.

    Returns:
        RollPressureIndex with all computed metrics.

    Raises:
        ValueError: If contract not found or insufficient data.
    """
    contract_symbol = contract_symbol.upper()

    # Look up contract
    contract_result = await db.execute(
        select(Contract).where(Contract.symbol == contract_symbol, Contract.is_active.is_(True))
    )
    contract = contract_result.scalar_one_or_none()
    if contract is None:
        raise ValueError(f"Contract '{contract_symbol}' not found or not active")

    # Get roll calendar info
    as_of_date = date.today()
    roll_info = calculate_roll_info(contract_symbol, as_of_date)

    # Fetch settlement data for the nearby and deferred months
    # Get all recent settlements to find the latest for each month
    recent_cutoff = datetime.combine(as_of_date - timedelta(days=days_back + 30), datetime.min.time())

    settlements_result = await db.execute(
        select(RawSettlement)
        .where(RawSettlement.contract_id == contract.id)
        .where(RawSettlement.settlement_date >= recent_cutoff)
        .order_by(RawSettlement.settlement_date.desc(), RawSettlement.month_code.asc())
    )
    all_settlements = list(settlements_result.scalars().all())

    if not all_settlements:
        raise ValueError(f"No settlement data available for '{contract_symbol}'")

    # Find latest date in data
    latest_date = all_settlements[0].settlement_date
    if isinstance(latest_date, datetime):
        latest_date = latest_date.date()

    # Find nearby and deferred month settlements on latest date
    nearby_month = roll_info.nearby_month_code
    deferred_month = roll_info.deferred_month_code

    nearby_settlement = None
    deferred_settlement = None

    # Get the most recent settlement for each month
    for s in all_settlements:
        s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
        if s_date == latest_date:
            if s.month_code == nearby_month:
                nearby_settlement = s
            elif s.month_code == deferred_month:
                deferred_settlement = s

    # If exact month codes not found, try partial matching
    if nearby_settlement is None:
        nearby_settlement = _find_settlement_by_month(all_settlements, nearby_month, latest_date)
    if deferred_settlement is None:
        deferred_settlement = _find_settlement_by_month(all_settlements, deferred_month, latest_date)

    # Fall back to first two available months if specific months not found
    if nearby_settlement is None or deferred_settlement is None:
        month_settlements: dict[str, RawSettlement] = {}
        for s in all_settlements:
            s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
            if s_date == latest_date and s.month_code not in month_settlements:
                month_settlements[s.month_code] = s

        sorted_months = sorted(month_settlements.keys())
        if len(sorted_months) >= 2:
            if nearby_settlement is None:
                nearby_settlement = month_settlements[sorted_months[0]]
                nearby_month = sorted_months[0]
            if deferred_settlement is None:
                deferred_settlement = month_settlements[sorted_months[1]]
                deferred_month = sorted_months[1]

    if nearby_settlement is None or deferred_settlement is None:
        raise ValueError(
            f"Insufficient settlement data for '{contract_symbol}': "
            f"need at least 2 contract months, found nearby={nearby_settlement is not None}, "
            f"deferred={deferred_settlement is not None}"
        )

    # Compute OI decay rate
    nearby_oi_series = _extract_oi_series(all_settlements, nearby_month, days_back)
    total_oi = nearby_settlement.open_interest + deferred_settlement.open_interest
    oi_decay_pct = estimate_oi_decay_rate(nearby_oi_series, total_oi, DEFAULT_OI_DECAY_LOOKBACK)

    # Compute spread basis (deferred - nearby)
    spread_basis = deferred_settlement.settlement_price - nearby_settlement.settlement_price

    # Days to expiry
    days_to_expiry = roll_info.days_to_roll

    # Determine roll window phase
    roll_start_days = ROLL_START_DAYS_BEFORE_EXPIRY.get(contract_symbol, 5)
    if days_to_expiry <= 0:
        roll_window = "post_roll"
    elif days_to_expiry <= roll_start_days:
        roll_window = "active_roll"
    else:
        roll_window = "pre_roll"

    # Compute composite roll pressure score
    roll_pressure_index = _compute_roll_pressure_score(
        oi_decay_pct=oi_decay_pct,
        spread_basis=spread_basis,
        nearby_price=nearby_settlement.settlement_price,
        deferred_price=deferred_settlement.settlement_price,
        days_to_expiry=days_to_expiry,
        nearby_volume=nearby_settlement.volume,
        deferred_volume=deferred_settlement.volume,
        nearby_oi=nearby_settlement.open_interest,
        deferred_oi=deferred_settlement.open_interest,
        roll_start_days=roll_start_days,
    )

    # Build response
    nearby_contract = NearbyContract(
        month=nearby_month,
        open_interest=nearby_settlement.open_interest,
        volume=nearby_settlement.volume,
        settlement_price=nearby_settlement.settlement_price,
    )

    deferred_contract = NearbyContract(
        month=deferred_month,
        open_interest=deferred_settlement.open_interest,
        volume=deferred_settlement.volume,
        settlement_price=deferred_settlement.settlement_price,
    )

    roll_pressure_metrics = RollPressureMetrics(
        index=round(roll_pressure_index, 2),
        oi_decay_pct=round(oi_decay_pct, 2),
        spread_basis=round(spread_basis, 4),
        days_to_expiry=days_to_expiry,
        roll_window=roll_window,  # type: ignore
    )

    return RollPressureIndex(
        contract=contract_symbol,
        timestamp=datetime.now(timezone.utc),
        nearby=nearby_contract,
        deferred=deferred_contract,
        roll_pressure=roll_pressure_metrics,
    )


def _compute_roll_pressure_score(
    oi_decay_pct: float,
    spread_basis: float,
    nearby_price: float,
    deferred_price: float,
    days_to_expiry: int,
    nearby_volume: int,
    deferred_volume: int,
    nearby_oi: int,
    deferred_oi: int,
    roll_start_days: int = 5,
) -> float:
    """Compute the composite roll pressure score (0-100).

    The score combines four factors:
    1. OI decay rate (0-100 component): How fast OI is leaving the nearby
    2. Spread basis (0-100 component): Contango/backwardation magnitude
    3. Proximity (0-100 component): Days until expiry
    4. Volume ratio (0-100 component): Is volume shifting to deferred?

    Each factor is normalized to 0-100 and weighted by its contribution.

    Args:
        oi_decay_pct: OI decay percentage from roll_calendar.
        spread_basis: Deferred price - nearby price.
        nearby_price: Nearby contract settlement price.
        deferred_price: Deferred contract settlement price.
        days_to_expiry: Calendar days until nearby expiry.
        nearby_volume: Nearby contract volume.
        deferred_volume: Deferred contract volume.
        nearby_oi: Nearby contract open interest.
        deferred_oi: Deferred contract open interest.
        roll_start_days: Days before expiry when roll starts.

    Returns:
        Composite roll pressure score (0-100).
    """
    # Factor 1: OI decay (already 0-100 scale, roughly)
    oi_decay_score = min(oi_decay_pct * 10, 100)  # Scale up: 5% decay → 50

    # Factor 2: Spread basis magnitude
    # Normalized by the nearby price to get a percentage
    if nearby_price > 0:
        spread_pct = abs(spread_basis) / nearby_price * 100
    else:
        spread_pct = 0.0
    # Map spread % to 0-100: 0.5% → 50, 1% → 75, 2%+ → 100
    spread_score = min(spread_pct * 100, 100)  # Linear scaling with cap

    # Factor 3: Proximity to expiry
    if days_to_expiry <= 0:
        proximity_score = 100.0
    elif days_to_expiry <= roll_start_days:
        # Active roll window: high proximity
        proximity_score = 70.0 + 30.0 * (1.0 - days_to_expiry / roll_start_days)
    elif days_to_expiry <= 15:
        # Approaching roll window
        proximity_score = 40.0 + 30.0 * (1.0 - (days_to_expiry - roll_start_days) / (15 - roll_start_days))
    elif days_to_expiry <= 30:
        proximity_score = 20.0 + 20.0 * (1.0 - (days_to_expiry - 15) / 15.0)
    else:
        # Far from roll
        proximity_score = max(0.0, 20.0 - (days_to_expiry - 30) * 0.5)

    # Factor 4: Volume ratio (volume shifting to deferred?)
    total_volume = nearby_volume + deferred_volume
    if total_volume > 0:
        deferred_volume_pct = deferred_volume / total_volume * 100
        # Higher deferred volume % → more roll activity → higher pressure
        volume_score = min(deferred_volume_pct, 100)
    else:
        volume_score = 0.0

    # Composite score: weighted average
    composite = (
        WEIGHT_OI_DECAY * oi_decay_score +
        WEIGHT_SPREAD_BASIS * spread_score +
        WEIGHT_PROXIMITY * proximity_score +
        WEIGHT_VOLUME_RATIO * volume_score
    )

    return min(max(composite, 0.0), MAX_ROLL_PRESSURE)


# ---------------------------------------------------------------------------
# Historical roll analysis
# ---------------------------------------------------------------------------


def analyze_historical_roll_pattern(
    historical_oi_nearby: list[tuple[date, int]],
    historical_oi_deferred: list[tuple[date, int]],
    lookback_days: int = 90,
) -> dict:
    """Analyze historical OI patterns around roll dates.

    Examines how OI transitions from nearby to deferred contracts
    over recent roll cycles to identify patterns and predict timing.

    Args:
        historical_oi_nearby: List of (date, oi) for the nearby contract.
        historical_oi_deferred: List of (date, oi) for the deferred contract.
        lookback_days: How many days of history to analyze.

    Returns:
        Dict with:
        - avg_roll_duration_days: Average number of days for OI transition
        - peak_roll_day_offset: Days before expiry when roll volume peaks
        - typical_oi_shift_pct: Typical % of nearby OI that shifts to deferred
        - roll_pattern: 'gradual', 'concentrated', or 'delayed'
    """
    if len(historical_oi_nearby) < 5 or len(historical_oi_deferred) < 5:
        return {
            "avg_roll_duration_days": 0.0,
            "peak_roll_day_offset": 0,
            "typical_oi_shift_pct": 0.0,
            "roll_pattern": "unknown",
        }

    # Find the point where nearby OI starts declining significantly
    # This is the "roll start" point
    nearby_dates = [d for d, _ in historical_oi_nearby]
    nearby_oi_values = [oi for _, oi in historical_oi_nearby]
    deferred_oi_values = [oi for _, oi in historical_oi_deferred]

    # Find peak nearby OI (before decline)
    peak_oi_idx = nearby_oi_values.index(max(nearby_oi_values))
    peak_oi = nearby_oi_values[peak_oi_idx]

    # Find the point where nearby OI has declined by 50% from peak
    midpoint_oi = peak_oi * 0.5
    midpoint_idx = peak_oi_idx
    for i in range(peak_oi_idx, len(nearby_oi_values)):
        if nearby_oi_values[i] <= midpoint_oi:
            midpoint_idx = i
            break

    # Roll duration: days from peak to midpoint
    if midpoint_idx > peak_oi_idx:
        roll_duration_days = (nearby_dates[midpoint_idx] - nearby_dates[peak_oi_idx]).days
    else:
        roll_duration_days = 5  # Default assumption

    # Peak roll day: when nearby OI decline is steepest
    max_decline = 0
    peak_decline_idx = peak_oi_idx
    for i in range(1, len(nearby_oi_values)):
        if i > peak_oi_idx:
            decline = nearby_oi_values[i - 1] - nearby_oi_values[i]
            if decline > max_decline:
                max_decline = decline
                peak_decline_idx = i

    # OI shift percentage
    if peak_oi > 0:
        oi_shift_pct = ((peak_oi - nearby_oi_values[-1]) / peak_oi) * 100
    else:
        oi_shift_pct = 0.0

    # Classify roll pattern
    if roll_duration_days <= 3:
        roll_pattern = "concentrated"
    elif roll_duration_days <= 7:
        roll_pattern = "gradual"
    else:
        roll_pattern = "delayed"

    return {
        "avg_roll_duration_days": round(roll_duration_days, 1),
        "peak_roll_day_offset": peak_decline_idx,
        "typical_oi_shift_pct": round(min(oi_shift_pct, 100.0), 1),
        "roll_pattern": roll_pattern,
    }


def compute_roll_impact_score(
    nearby_oi: int,
    deferred_oi: int,
    nearby_volume: int,
    deferred_volume: int,
    spread_basis: float,
    days_to_expiry: int,
    contract_symbol: str = "ES",
) -> dict:
    """Compute roll impact score estimating potential price impact.

    The roll impact score estimates how much the roll might affect prices
    based on OI concentration, volume, and spread conditions.

    Args:
        nearby_oi: Open interest in the nearby contract.
        deferred_oi: Open interest in the deferred contract.
        nearby_volume: Volume in the nearby contract.
        deferred_volume: Volume in the deferred contract.
        spread_basis: Deferred - nearby price spread.
        days_to_expiry: Days until nearby contract expiry.
        contract_symbol: Contract symbol for roll window lookup.

    Returns:
        Dict with:
        - impact_score: 0-100 (higher = more expected price impact)
        - oi_concentration: Nearby OI as % of total OI
        - volume_shift: Volume shifting to deferred (% of total)
        - expected_slippage: Estimated slippage in price units
        - impact_category: 'low', 'medium', 'high', or 'extreme'
    """
    total_oi = nearby_oi + deferred_oi
    total_volume = nearby_volume + deferred_volume

    # OI concentration: how much OI is still in the nearby
    oi_concentration = (nearby_oi / total_oi * 100) if total_oi > 0 else 50.0

    # Volume shift: how much volume has moved to deferred
    volume_shift = (deferred_volume / total_volume * 100) if total_volume > 0 else 0.0

    # Impact score: combines concentration, volume shift, and proximity
    roll_start_days = ROLL_START_DAYS_BEFORE_EXPIRY.get(contract_symbol, 5)

    # Proximity factor (0-100)
    if days_to_expiry <= 0:
        proximity_factor = 30.0  # Past expiry, impact decreasing
    elif days_to_expiry <= roll_start_days:
        proximity_factor = 100.0  # Active roll, maximum impact
    elif days_to_expiry <= 15:
        proximity_factor = 80.0 - (days_to_expiry - roll_start_days) * 4.0
    elif days_to_expiry <= 30:
        proximity_factor = 20.0
    else:
        proximity_factor = 5.0

    # Concentration factor (0-100): more concentration → more impact
    concentration_factor = oi_concentration  # Already 0-100

    # Volume stress (0-100): if volume hasn't shifted, more stress
    volume_stress = 100.0 - volume_shift  # Inverse: less shift → more stress

    # Composite impact score
    impact_score = (
        proximity_factor * 0.35 +
        concentration_factor * 0.35 +
        volume_stress * 0.30
    )

    # Cap at 100
    impact_score = min(max(impact_score, 0.0), 100.0)

    # Expected slippage: based on spread and volume
    # Thinner volume → more slippage per unit of OI that needs to roll
    if total_volume > 0 and nearby_oi > 0:
        # Slippage ≈ spread * (OI to roll / daily volume)
        oi_to_roll = nearby_oi * 0.8  # ~80% of nearby OI needs to roll
        volume_ratio = oi_to_roll / total_volume
        expected_slippage = abs(spread_basis) * min(volume_ratio, 5.0) * 0.1
    else:
        expected_slippage = abs(spread_basis) * 0.5

    # Impact category
    if impact_score >= 80:
        impact_category = "extreme"
    elif impact_score >= 60:
        impact_category = "high"
    elif impact_score >= 30:
        impact_category = "medium"
    else:
        impact_category = "low"

    return {
        "impact_score": round(impact_score, 1),
        "oi_concentration": round(oi_concentration, 1),
        "volume_shift": round(volume_shift, 1),
        "expected_slippage": round(expected_slippage, 4),
        "impact_category": impact_category,
    }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _find_settlement_by_month(
    settlements: list,
    target_month: str,
    target_date: date,
) -> Optional["RawSettlement"]:
    """Find a settlement record for a specific month code on or near a target date.

    Tries exact month code match first, then partial matches.

    Args:
        settlements: List of RawSettlement objects.
        target_month: Target month code (e.g., 'Jun 26').
        target_date: Target date for settlement.

    Returns:
        Matching RawSettlement or None.
    """
    # First, try exact match on month code and date
    for s in settlements:
        s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
        if s.month_code == target_month and s_date == target_date:
            return s

    # Try exact month code match on nearest prior date
    for s in settlements:
        if s.month_code == target_month:
            return s

    # Try partial match: parse the month and find any settlement for that month
    try:
        target_m, target_y = parse_month_code(target_month)
        # Try 3-letter month name match (e.g., 'Jun' in 'Jun 26')
        target_prefix = target_month.split()[0][:3].lower() if ' ' in target_month else ""
        for s in settlements:
            s_month_lower = s.month_code.lower()[:3]
            if target_prefix and s_month_lower == target_prefix:
                return s
    except ValueError:
        pass

    return None


def _extract_oi_series(
    settlements: list,
    month_code: str,
    days_back: int,
) -> list[tuple[date, int]]:
    """Extract an OI time series for a specific month code.

    Args:
        settlements: List of RawSettlement objects.
        month_code: Target month code to extract.
        days_back: Number of days to look back.

    Returns:
        List of (date, oi) tuples sorted by date ascending.
    """
    today = date.today()
    cutoff = today - timedelta(days=days_back)

    series: list[tuple[date, int]] = []
    for s in settlements:
        s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
        if s.month_code == month_code and s_date >= cutoff:
            series.append((s_date, s.open_interest))

    # Sort by date ascending
    series.sort(key=lambda x: x[0])
    return series