"""Positioning signal engine for OpenInterest Lens.

Computes smart money positioning (commercial hedgers), retail contrarian
signals (non-reportable traders), and composite signals from COT data.
Each signal returns a value, confidence score, direction, and metadata.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Contract, RawCOTReport
from app.models.signal import (
    NetPosition,
    PositioningBreakdown,
    PositioningSignal,
    PositioningSignalResponse,
    Retail,
    SignalMetadata,
    SignalOverall,
    SmartMoney,
    TraderPositionBreakdown,
)
from app.signals.historical import (
    compute_lookback_window,
    compute_net_positions,
    detect_extreme_positioning,
    detect_mean_reversion,
    percentile_rank,
    rolling_z_score,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Signal computation constants
# ---------------------------------------------------------------------------

# Z-score thresholds for classification
Z_SCORE_EXTREME = 1.5        # Beyond this = high conviction
Z_SCORE_MODERATE = 1.0       # Beyond this = medium conviction
RETAIL_EXTREME_Z = 1.5       # Z-score for retail contrarian trigger
RETAIL_EXTREME_PERCENTILE = 85.0  # Percentile for retail contrarian trigger
RETAIL_EXTREME_PERCENTILE_LOW = 15.0

# Default lookback
DEFAULT_LOOKBACK_WEEKS = 52


# ---------------------------------------------------------------------------
# Core signal computation functions
# ---------------------------------------------------------------------------


def compute_smart_money_signal(
    commercial_net: float,
    commercial_nets_history: list[float],
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> SmartMoney:
    """Compute smart money (commercial hedger) positioning signal.

    Commercial hedgers are considered "smart money" because they trade
    to hedge physical exposure — when they reach extremes, it often
    signals market turning points.

    Args:
        commercial_net: Current net commercial position.
        commercial_nets_history: Historical net commercial positions (lookback window).
        lookback_weeks: Number of weeks for statistical calculations.

    Returns:
        SmartMoney with Z-score, percentile, direction, and conviction.
    """
    # Filter to lookback window
    history = commercial_nets_history[-lookback_weeks:] if len(commercial_nets_history) > lookback_weeks else commercial_nets_history

    z = rolling_z_score(commercial_net, history)
    pct = percentile_rank(commercial_net, history)
    conviction, direction = detect_extreme_positioning(z, pct)

    return SmartMoney(
        z_score=round(z, 4),
        percentile=round(pct, 2),
        direction=direction,
        conviction=conviction,
    )


def compute_retail_signal(
    non_reportable_net: float,
    non_reportable_nets_history: list[float],
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> Retail:
    """Compute retail (non-reportable) contrarian positioning signal.

    Retail traders are often wrong at extremes — when they pile into
    one side, it's typically a contrarian signal. This function detects
    extreme retail positioning and generates fade signals.

    Args:
        non_reportable_net: Current net non-reportable position.
        non_reportable_nets_history: Historical net non-reportable positions.
        lookback_weeks: Number of weeks for statistical calculations.

    Returns:
        Retail signal with Z-score, percentile, direction, and contrarian signal.
    """
    history = non_reportable_nets_history[-lookback_weeks:] if len(non_reportable_nets_history) > lookback_weeks else non_reportable_nets_history

    z = rolling_z_score(non_reportable_net, history)
    pct = percentile_rank(non_reportable_net, history)
    _, direction = detect_extreme_positioning(z, pct)

    # Contrarian logic: when retail is extremely long → fade_long (bearish contrarian)
    # When retail is extremely short → fade_short (bullish contrarian)
    is_extreme, reversion_dir = detect_mean_reversion(z, threshold_high=RETAIL_EXTREME_Z, threshold_low=-RETAIL_EXTREME_Z)

    if reversion_dir == "overbought" and pct >= RETAIL_EXTREME_PERCENTILE:
        contrarian_signal = "fade_long"
    elif reversion_dir == "oversold" and pct <= RETAIL_EXTREME_PERCENTILE_LOW:
        contrarian_signal = "fade_short"
    else:
        contrarian_signal = "none"

    return Retail(
        z_score=round(z, 4),
        percentile=round(pct, 2),
        direction=direction,
        contrarian_signal=contrarian_signal,
    )


def compute_composite_signal(
    smart_money: SmartMoney,
    retail: Retail,
    net_position: NetPosition,
) -> SignalOverall:
    """Compute the composite positioning signal.

    Combines smart money and retail signals into a single directional
    assessment. The composite considers:
    - Smart money direction and conviction
    - Retail contrarian signal
    - Divergence between smart money and retail

    Logic:
    - If smart money is long AND retail contrarian says fade_short → bullish
    - If smart money is short AND retail contrarian says fade_long → bearish
    - If smart money and retail agree → moderate signal in that direction
    - Divergence = smart money and retail on opposite sides

    Args:
        smart_money: Computed SmartMoney signal.
        retail: Computed Retail signal.
        net_position: Current net positions.

    Returns:
        SignalOverall with direction, strength, and divergence flag.
    """
    sm_direction = smart_money.direction
    retail_direction = retail.direction
    contrarian = retail.contrarian_signal

    # Detect divergence: smart money and retail on opposite sides
    divergence = False
    if sm_direction == "long" and retail_direction == "short":
        divergence = True
    elif sm_direction == "short" and retail_direction == "long":
        divergence = True

    # Determine overall direction
    overall = "neutral"
    strength = 0.3  # Base strength

    # Smart money alignment with contrarian signal
    if sm_direction == "long" and contrarian == "fade_short":
        # Smart money long + retail extremely short → strong bullish
        overall = "bullish"
        strength = min(0.5 + abs(smart_money.z_score) * 0.2, 1.0)
    elif sm_direction == "short" and contrarian == "fade_long":
        # Smart money short + retail extremely long → strong bearish
        overall = "bearish"
        strength = min(0.5 + abs(smart_money.z_score) * 0.2, 1.0)
    elif sm_direction == "long" and contrarian == "none":
        # Smart money long, no retail extreme → moderate bullish
        overall = "bullish"
        strength = 0.3 + min(abs(smart_money.z_score) * 0.15, 0.4)
    elif sm_direction == "short" and contrarian == "none":
        # Smart money short, no retail extreme → moderate bearish
        overall = "bearish"
        strength = 0.3 + min(abs(smart_money.z_score) * 0.15, 0.4)
    elif divergence:
        # Divergence: follow smart money direction with moderate confidence
        if sm_direction == "long":
            overall = "bullish"
        elif sm_direction == "short":
            overall = "bearish"
        else:
            overall = "neutral"
        strength = 0.5 + min(abs(smart_money.z_score) * 0.1, 0.3)
    else:
        # Both neutral or same side without extremes
        if sm_direction == "long":
            overall = "bullish"
            strength = 0.2 + min(abs(smart_money.z_score) * 0.1, 0.3)
        elif sm_direction == "short":
            overall = "bearish"
            strength = 0.2 + min(abs(smart_money.z_score) * 0.1, 0.3)
        elif retail_direction != "neutral":
            overall = "neutral"  # Don't follow retail alone
            strength = 0.2

    # Boost strength based on conviction
    if smart_money.conviction == "high":
        strength = min(strength + 0.1, 1.0)
    elif smart_money.conviction == "low":
        strength = max(strength - 0.1, 0.1)

    return SignalOverall(
        overall=overall,
        strength=round(strength, 4),
        divergence=divergence,
    )


# ---------------------------------------------------------------------------
# Full positioning signal computation (DB-backed)
# ---------------------------------------------------------------------------


async def compute_positioning_signal(
    contract_symbol: str,
    db: AsyncSession,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> PositioningSignalResponse:
    """Compute the full positioning signal for a single commodity.

    Fetches COT data from the database, computes Z-scores, percentiles,
    smart money, retail, and composite signals.

    Args:
        contract_symbol: Root symbol, e.g. 'ES'.
        db: Async database session.
        lookback_weeks: Lookback window for Z-scores.

    Returns:
        PositioningSignalResponse with full signal and breakdown.

    Raises:
        ValueError: If contract not found or no COT data available.
    """
    # Look up contract
    contract_result = await db.execute(
        select(Contract).where(Contract.symbol == contract_symbol, Contract.is_active.is_(True))
    )
    contract = contract_result.scalar_one_or_none()
    if contract is None:
        raise ValueError(f"Contract '{contract_symbol}' not found or not active")

    # Fetch COT reports for this contract, ordered by date
    reports_result = await db.execute(
        select(RawCOTReport)
        .where(RawCOTReport.contract_id == contract.id)
        .order_by(RawCOTReport.as_of_date.asc())
    )
    reports = list(reports_result.scalars().all())

    if not reports:
        raise ValueError(f"No COT data available for '{contract_symbol}'")

    # Apply lookback window
    windowed = reports[-lookback_weeks:] if len(reports) > lookback_weeks else reports
    latest = windowed[-1]

    # Extract net position series
    commercial_nets = [float(r.commercial_net) for r in windowed]
    non_commercial_nets = [float(r.non_commercial_net) for r in windowed]
    non_reportable_nets = [float(r.non_reportable_net) for r in windowed]

    # Current values
    current_commercial_net = float(latest.commercial_net)
    current_non_commercial_net = float(latest.non_commercial_net)
    current_non_reportable_net = float(latest.non_reportable_net)

    # Compute smart money signal
    smart_money = compute_smart_money_signal(
        commercial_net=current_commercial_net,
        commercial_nets_history=commercial_nets,
        lookback_weeks=len(windowed),
    )

    # Compute retail signal
    retail = compute_retail_signal(
        non_reportable_net=current_non_reportable_net,
        non_reportable_nets_history=non_reportable_nets,
        lookback_weeks=len(windowed),
    )

    # Net position model
    net_position = NetPosition(
        commercial=int(current_commercial_net),
        non_commercial=int(current_non_commercial_net),
        non_reportable=int(current_non_reportable_net),
    )

    # Composite signal
    signal = compute_composite_signal(smart_money, retail, net_position)

    # Week-over-week change
    wow_change = None
    if len(windowed) >= 2:
        prior = windowed[-2]
        wow_change = NetPosition(
            commercial=int(current_commercial_net - prior.commercial_net),
            non_commercial=int(current_non_commercial_net - prior.non_commercial_net),
            non_reportable=int(current_non_reportable_net - prior.non_reportable_net),
        )

    # Build positioning signal
    positioning_signal = PositioningSignal(
        contract=contract_symbol,
        timestamp=latest.as_of_date if isinstance(latest.as_of_date, datetime) else datetime.combine(latest.as_of_date, datetime.min.time(), tzinfo=timezone.utc),
        as_of_friday=latest.published_date.date() if isinstance(latest.published_date, datetime) else latest.published_date,
        net_position=net_position,
        smart_money=smart_money,
        retail=retail,
        signal=signal,
        week_over_week_change=wow_change,
    )

    # Build positioning breakdown with Z-scores per trader category
    # Compute Z-scores for long/short/net per category
    commercial_longs = [float(r.commercial_long) for r in windowed]
    commercial_shorts = [float(r.commercial_short) for r in windowed]
    nc_longs = [float(r.non_commercial_long) for r in windowed]
    nc_shorts = [float(r.non_commercial_short) for r in windowed]
    nr_longs = [float(r.non_reportable_long) for r in windowed]
    nr_shorts = [float(r.non_reportable_short) for r in windowed]

    breakdown = PositioningBreakdown(
        commercial=TraderPositionBreakdown(
            long=int(latest.commercial_long),
            short=int(latest.commercial_short),
            net=int(latest.commercial_net),
            z_score=smart_money.z_score,
            percentile=smart_money.percentile,
            direction=smart_money.direction,
        ),
        non_commercial=TraderPositionBreakdown(
            long=int(latest.non_commercial_long),
            short=int(latest.non_commercial_short),
            net=int(latest.non_commercial_net),
            z_score=round(rolling_z_score(current_non_commercial_net, non_commercial_nets), 4),
            percentile=round(percentile_rank(current_non_commercial_net, non_commercial_nets), 2),
            direction=compute_smart_money_signal(
                current_non_commercial_net, non_commercial_nets, len(windowed)
            ).direction,
        ),
        non_reportable=TraderPositionBreakdown(
            long=int(latest.non_reportable_long),
            short=int(latest.non_reportable_short),
            net=int(latest.non_reportable_net),
            z_score=retail.z_score,
            percentile=retail.percentile,
            direction=retail.direction,
        ),
    )

    metadata = SignalMetadata(
        lookback_weeks=min(lookback_weeks, len(windowed)),
        data_points=len(windowed),
        as_of_date=latest.as_of_date.date() if isinstance(latest.as_of_date, datetime) else latest.as_of_date,
        computed_at=datetime.now(timezone.utc),
        cache_hit=False,
    )

    return PositioningSignalResponse(
        commodity=contract_symbol,
        signal=positioning_signal,
        breakdown=breakdown,
        metadata=metadata,
    )


async def compute_positioning_signals_multi(
    symbols: list[str] | None,
    db: AsyncSession,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> list[PositioningSignalResponse]:
    """Compute positioning signals for multiple commodities.

    Args:
        symbols: List of contract symbols. If None, computes for all active contracts.
        db: Async database session.
        lookback_weeks: Lookback window for Z-scores.

    Returns:
        List of PositioningSignalResponse, one per commodity with data.
    """
    # Determine which contracts to compute for
    if symbols:
        query = select(Contract).where(
            Contract.symbol.in_(symbols),
            Contract.is_active.is_(True),
        )
    else:
        query = select(Contract).where(Contract.is_active.is_(True))

    result = await db.execute(query)
    contracts = list(result.scalars().all())

    responses: list[PositioningSignalResponse] = []
    for contract in contracts:
        try:
            response = await compute_positioning_signal(
                contract_symbol=contract.symbol,
                db=db,
                lookback_weeks=lookback_weeks,
            )
            responses.append(response)
        except ValueError as e:
            logger.warning("skipping_contract_no_data", symbol=contract.symbol, error=str(e))
            continue

    return responses