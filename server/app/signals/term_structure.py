"""Term structure engine for OpenInterest Lens.

Computes term structure curves from settlement data across contract months.
Calculates contango/backwardation indicators, term structure slope,
calendar spread ratios, and curve shape classification. Each indicator
returns value, confidence, and metadata.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Contract, RawSettlement
from app.models.signal import (
    ContangoAlert,
    CurveMetrics,
    RollPressureIndex,
    RollPressureMetrics,
    SpreadSummary,
    TermStructureCurve,
    TermStructureMonth,
)
from app.signals.curve_utils import (
    classify_curve,
    compute_annualized_yield,
    compute_curve_slope,
    compute_spread_to_front,
    evaluate_polynomial,
    fit_polynomial,
    fit_term_structure_curve,
    interpolate_missing_months,
)
from app.signals.roll_calendar import (
    calculate_expiry_date,
    calculate_roll_info,
    classify_roll_urgency,
    estimate_oi_decay_rate,
    estimate_roll_volume,
    generate_month_code,
    get_active_contract_months,
    parse_month_code,
)
from app.signals.historical import percentile_rank, rolling_z_score

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum number of contract months needed for term structure analysis
MIN_MONTHS_FOR_ANALYSIS = 2

# Spread thresholds for contango/backwardation classification
# A spread less than this % of front price is considered "flat"
FLAT_THRESHOLD_PCT = 0.002  # 0.2%

# Z-score threshold for extreme contango/backwardation alerts
EXTREME_SPREAD_Z_THRESHOLD = 2.0

# Days assumed between consecutive contract months for annualization
# This varies by contract but 30 is a reasonable default for monthly expiries
DEFAULT_DAYS_BETWEEN_MONTHS = 30

# Minimum OI for a contract month to be included in analysis
MIN_OI_FOR_ANALYSIS = 100


# ---------------------------------------------------------------------------
# Term structure computation
# ---------------------------------------------------------------------------


async def compute_term_structure(
    contract_symbol: str,
    db: AsyncSession,
    as_of_date: Optional[date] = None,
    lookback_days: int = 0,
) -> TermStructureCurve:
    """Compute the full term structure curve for a commodity.

    Fetches settlement data across contract months, computes spreads,
    annualized yields, curve classification, and aggregate metrics.

    Args:
        contract_symbol: Root symbol, e.g. 'ES'.
        db: Async database session.
        as_of_date: Date for the term structure snapshot. Defaults to latest available.
        lookback_days: If > 0, return history for this many days back.

    Returns:
        TermStructureCurve with all computed metrics.

    Raises:
        ValueError: If contract not found or insufficient settlement data.
    """
    # Look up contract
    contract_result = await db.execute(
        select(Contract).where(Contract.symbol == contract_symbol.upper(), Contract.is_active.is_(True))
    )
    contract = contract_result.scalar_one_or_none()
    if contract is None:
        raise ValueError(f"Contract '{contract_symbol}' not found or not active")

    # Fetch settlement data
    query = select(RawSettlement).where(
        RawSettlement.contract_id == contract.id
    ).order_by(RawSettlement.settlement_date.desc(), RawSettlement.month_code.asc())

    if as_of_date is not None:
        # Get data for the specific date
        as_of_datetime = datetime.combine(as_of_date, datetime.min.time())
        query = query.where(RawSettlement.settlement_date <= as_of_datetime)

    result = await db.execute(query)
    settlements = list(result.scalars().all())

    if not settlements:
        raise ValueError(f"No settlement data available for '{contract_symbol}'")

    # Get the latest date in the data
    latest_date = settlements[0].settlement_date
    if isinstance(latest_date, datetime):
        latest_date = latest_date.date()

    # Filter to the latest settlement date
    if as_of_date is None:
        as_of_date = latest_date

    # Group settlements by month code for the target date
    as_of_datetime = datetime.combine(as_of_date, datetime.min.time())
    month_settlements: dict[str, RawSettlement] = {}
    for s in settlements:
        s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
        if s_date == as_of_date:
            month_settlements[s.month_code] = s

    # If no data on exact date, find the nearest prior date
    if not month_settlements:
        for s in settlements:
            s_date = s.settlement_date.date() if isinstance(s.settlement_date, datetime) else s.settlement_date
            if s_date <= as_of_date:
                month_settlements[s.month_code] = s
                break

    if not month_settlements:
        # Fall back to whatever data is available
        for s in settlements:
            if s.month_code not in month_settlements:
                month_settlements[s.month_code] = s

    # Build term structure months
    months_data = _build_term_structure_months(
        month_settlements=month_settlements,
        contract_symbol=contract_symbol,
        as_of_date=as_of_date,
    )

    if len(months_data) < MIN_MONTHS_FOR_ANALYSIS:
        raise ValueError(
            f"Insufficient settlement data for '{contract_symbol}': "
            f"need at least {MIN_MONTHS_FOR_ANALYSIS} months, got {len(months_data)}"
        )

    # Classify the curve
    front_price = months_data[0].settlement
    month_indices = list(range(len(months_data)))
    prices = [m.settlement for m in months_data]

    # Fit polynomial curve
    coeffs, metrics = fit_term_structure_curve(month_indices, prices)

    # Compute curve metrics
    total_oi = sum(m.open_interest for m in months_data)
    front_month_oi = months_data[0].open_interest
    avg_daily_volume = sum(m.volume for m in months_data) // max(len(months_data), 1)
    oi_concentration = (front_month_oi / total_oi * 100.0) if total_oi > 0 else 0.0

    curve_metrics = CurveMetrics(
        front_month_oi=front_month_oi,
        total_oi=total_oi,
        oi_concentration_pct=round(oi_concentration, 2),
        avg_daily_volume=avg_daily_volume,
        steepness=round(metrics["slope"], 6),
    )

    # Build the term structure response
    term_structure = TermStructureCurve(
        contract=contract_symbol.upper(),
        timestamp=datetime.now(timezone.utc),
        as_of_date=as_of_date,
        structure_type=metrics["classification"],
        months=months_data,
        curve_metrics=curve_metrics,
    )

    return term_structure


def _build_term_structure_months(
    month_settlements: dict[str, "RawSettlement"],
    contract_symbol: str,
    as_of_date: date,
) -> list[TermStructureMonth]:
    """Build TermStructureMonth objects from settlement data.

    Sorts months by expiry date and computes spreads and annualized yields.

    Args:
        month_settlements: Dict mapping month code to RawSettlement.
        contract_symbol: Root symbol for expiry calculation.
        as_of_date: Reference date for days-between calculation.

    Returns:
        Sorted list of TermStructureMonth objects.
    """
    if not month_settlements:
        return []

    # Parse and sort settlements by expiry date
    parsed: list[tuple[date, RawSettlement]] = []
    for month_code, settlement in month_settlements.items():
        try:
            month, year = parse_month_code(month_code)
            expiry = calculate_expiry_date(year, month, contract_symbol)
            parsed.append((expiry, settlement))
        except (ValueError, KeyError):
            # Skip unparseable month codes
            logger.warning("skip_unparseable_month", month_code=month_code)
            continue

    # Sort by expiry date
    parsed.sort(key=lambda x: x[0])

    if not parsed:
        return []

    # Get front month price for spread calculation
    front_price = parsed[0][1].settlement_price

    # Build TermStructureMonth objects
    months: list[TermStructureMonth] = []
    for i, (expiry, settlement) in enumerate(parsed):
        # Compute spread to front month
        spread = compute_spread_to_front(front_price, settlement.settlement_price)

        # Compute annualized yield
        if i == 0:
            annualized_yield = 0.0
        else:
            days_between = (expiry - parsed[0][0]).days
            if days_between <= 0:
                days_between = DEFAULT_DAYS_BETWEEN_MONTHS * i
            annualized_yield = compute_annualized_yield(
                front_price, settlement.settlement_price, days_between
            )

        months.append(TermStructureMonth(
            month=settlement.month_code,
            expiry_date=expiry,
            settlement=settlement.settlement_price,
            open_interest=settlement.open_interest,
            volume=settlement.volume,
            spread_to_front=round(spread, 4),
            annualized_yield=round(annualized_yield, 4),
        ))

    return months


# ---------------------------------------------------------------------------
# Contango/backwardation detection
# ---------------------------------------------------------------------------


def compute_contango_backwardation(
    months: list[TermStructureMonth],
    historical_spreads: Optional[list[float]] = None,
) -> dict:
    """Compute contango/backwardation indicators from term structure months.

    Args:
        months: Sorted list of TermStructureMonth objects.
        historical_spreads: Optional historical M1/M2 spreads for Z-score.

    Returns:
        Dict with:
        - structure_type: 'contango', 'backwardation', 'flat', or 'mixed'
        - m1_m2_spread: Front month to next month spread in price units
        - m1_m2_annualized: Annualized M1/M2 spread percentage
        - spread_z_score: Z-score of current spread vs historical
        - confidence: 0-1 confidence score
        - slope: Overall curve slope
    """
    if len(months) < 2:
        return {
            "structure_type": "flat",
            "m1_m2_spread": 0.0,
            "m1_m2_annualized": 0.0,
            "spread_z_score": 0.0,
            "confidence": 0.0,
            "slope": 0.0,
        }

    front = months[0]
    next_month = months[1]

    # M1/M2 spread
    m1_m2_spread = next_month.settlement - front.settlement

    # Annualized M1/M2 spread
    days_between = 30  # Default assumption
    if front.expiry_date and next_month.expiry_date:
        days_between = (next_month.expiry_date - front.expiry_date).days
        if days_between <= 0:
            days_between = 30

    m1_m2_annualized = compute_annualized_yield(
        front.settlement, next_month.settlement, days_between
    )

    # Z-score of spread vs historical
    spread_z_score = 0.0
    if historical_spreads and len(historical_spreads) >= 5:
        spread_z_score = rolling_z_score(m1_m2_spread, historical_spreads)

    # Determine structure type based on M1/M2 spread
    front_price = front.settlement
    if front_price > 0:
        spread_pct = abs(m1_m2_spread) / front_price
    else:
        spread_pct = 0.0

    if spread_pct < FLAT_THRESHOLD_PCT:
        structure_type = "flat"
    elif m1_m2_spread > 0:
        structure_type = "contango"
    else:
        structure_type = "backwardation"

    # Check for mixed structure: front in backwardation, back in contango
    if len(months) >= 3:
        last = months[-1]
        last_spread = last.settlement - front.settlement
        if m1_m2_spread * last_spread < 0:
            structure_type = "mixed"

    # Compute overall curve slope
    month_indices = list(range(len(months)))
    prices = [m.settlement for m in months]
    if len(prices) >= 2:
        coeffs = fit_polynomial(month_indices, prices, degree=min(1, len(prices) - 1))
        slope = compute_curve_slope(coeffs, 0, len(months) - 1)
    else:
        slope = 0.0

    # Confidence: more months → higher confidence
    confidence = min(len(months) / 6.0, 1.0)

    # Boost confidence if spread is significant
    if spread_pct > 0.01:  # > 1% spread
        confidence = min(confidence + 0.1, 1.0)

    return {
        "structure_type": structure_type,
        "m1_m2_spread": round(m1_m2_spread, 4),
        "m1_m2_annualized": round(m1_m2_annualized, 4),
        "spread_z_score": round(spread_z_score, 4),
        "confidence": round(confidence, 3),
        "slope": round(slope, 6),
    }


def compute_calendar_spread_ratio(
    months: list[TermStructureMonth],
) -> dict[str, float]:
    """Compute calendar spread ratios across the term structure.

    Calendar spread ratios measure the relative price difference between
    consecutive months, normalized by the front month price.

    Args:
        months: Sorted list of TermStructureMonth objects.

    Returns:
        Dict with:
        - front_to_next_ratio: M1/M2 price ratio
        - front_to_deferred_ratio: M1/M_n price ratio (last month)
        - average_monthly_spread_pct: Average monthly spread as % of front
        - max_spread_pct: Maximum single-month spread as % of front
    """
    if len(months) < 2:
        return {
            "front_to_next_ratio": 1.0,
            "front_to_deferred_ratio": 1.0,
            "average_monthly_spread_pct": 0.0,
            "max_spread_pct": 0.0,
        }

    front_price = months[0].settlement

    # M1/M2 ratio
    front_to_next_ratio = months[1].settlement / front_price if front_price != 0 else 1.0

    # M1/M_n ratio (last month)
    front_to_deferred_ratio = months[-1].settlement / front_price if front_price != 0 else 1.0

    # Monthly spreads as % of front
    spreads_pct = []
    for i in range(1, len(months)):
        if front_price > 0:
            spread_pct = abs(months[i].settlement - months[i - 1].settlement) / front_price * 100
            spreads_pct.append(spread_pct)

    avg_spread = sum(spreads_pct) / len(spreads_pct) if spreads_pct else 0.0
    max_spread = max(spreads_pct) if spreads_pct else 0.0

    return {
        "front_to_next_ratio": round(front_to_next_ratio, 6),
        "front_to_deferred_ratio": round(front_to_deferred_ratio, 6),
        "average_monthly_spread_pct": round(avg_spread, 4),
        "max_spread_pct": round(max_spread, 4),
    }


# ---------------------------------------------------------------------------
# Contango alert generation
# ---------------------------------------------------------------------------


def generate_contango_alert(
    current_structure: str,
    months: list[TermStructureMonth],
    prior_structure: str,
    days_in_current_state: int,
    historical_spreads: Optional[list[float]] = None,
) -> Optional[ContangoAlert]:
    """Generate a contango/backwardation alert if conditions warrant.

    Alerts are generated for:
    - Transitions between contango and backwardation
    - Extreme contango (Z-score > threshold)
    - Extreme backwardation (Z-score < -threshold)
    - Steepening or flattening of the curve

    Args:
        current_structure: Current structure type ('contango', 'backwardation', 'flat').
        months: Term structure months (sorted).
        prior_structure: Previous structure type.
        days_in_current_state: Days in the current state.
        historical_spreads: Historical M1/M2 spreads for Z-scoring.

    Returns:
        ContangoAlert if conditions warrant, None otherwise.
    """
    if len(months) < 2:
        return None

    front = months[0]
    next_m = months[1]
    m1_m2_spread = next_m.settlement - front.settlement

    # Z-score of current spread
    spread_z = 0.0
    if historical_spreads and len(historical_spreads) >= 5:
        spread_z = rolling_z_score(m1_m2_spread, historical_spreads)

    # Days between for annualization
    days_between = 30
    if front.expiry_date and next_m.expiry_date:
        days_between = (next_m.expiry_date - front.expiry_date).days
        if days_between <= 0:
            days_between = 30

    m1_m2_annualized = compute_annualized_yield(
        front.settlement, next_m.settlement, days_between
    )

    # Determine alert type
    alert_type: Optional[str] = None
    severity = "info"

    # Transition alert
    if current_structure != prior_structure and current_structure != "flat" and prior_structure != "flat":
        alert_type = "transition"
        severity = "warning"
    # Extreme contango
    elif current_structure == "contango" and spread_z > EXTREME_SPREAD_Z_THRESHOLD:
        alert_type = "extreme_contango"
        severity = "critical"
    # Extreme backwardation
    elif current_structure == "backwardation" and spread_z < -EXTREME_SPREAD_Z_THRESHOLD:
        alert_type = "extreme_backwardation"
        severity = "critical"
    # Steepening (contango getting steeper or backwardation getting steeper)
    elif len(months) >= 3 and abs(m1_m2_spread) > 0 and days_in_current_state < 10:
        # Check if the curve is steepening by comparing M1/M2 vs M1/M_n
        last_spread = months[-1].settlement - front.settlement
        avg_monthly = last_spread / max(len(months) - 1, 1)
        if abs(avg_monthly) > abs(m1_m2_spread) * 1.5:
            alert_type = "steepening"
            severity = "info"
        elif abs(avg_monthly) < abs(m1_m2_spread) * 0.5:
            alert_type = "flattening"
            severity = "info"

    if alert_type is None:
        return None

    return ContangoAlert(
        contract=front.month,  # We'll override this with the actual symbol at the call site
        timestamp=datetime.now(timezone.utc),
        structure=current_structure,
        alert_type=alert_type,
        spread_summary=SpreadSummary(
            front_month_price=front.settlement,
            next_month_price=next_m.settlement,
            m1_m2_spread=round(m1_m2_spread, 4),
            m1_m2_annualized=round(m1_m2_annualized, 4),
            z_score=round(spread_z, 4),
        ),
        prior_structure=prior_structure,
        days_in_current_state=days_in_current_state,
        severity=severity,
    )


def compute_term_structure_slope(
    months: list[TermStructureMonth],
) -> dict:
    """Compute detailed slope metrics for the term structure.

    Args:
        months: Sorted term structure months.

    Returns:
        Dict with:
        - nearby_deferred_spread: Price difference between M1 and M_n
        - slope_annualized_pct: Annualized slope percentage
        - linear_slope: Linear fit slope coefficient
        - quadratic_curvature: Quadratic fit curvature (second derivative)
        - r_squared_linear: R² of linear fit
        - r_squared_quadratic: R² of quadratic fit (if enough data)
    """
    if len(months) < 2:
        return {
            "nearby_deferred_spread": 0.0,
            "slope_annualized_pct": 0.0,
            "linear_slope": 0.0,
            "quadratic_curvature": 0.0,
            "r_squared_linear": 0.0,
            "r_squared_quadratic": 0.0,
        }

    month_indices = [float(i) for i in range(len(months))]
    prices = [m.settlement for m in months]
    front_price = prices[0]

    # Linear fit
    linear_coeffs = fit_polynomial(month_indices, prices, degree=1)
    linear_slope = linear_coeffs[1] if len(linear_coeffs) > 1 else 0.0

    # Linear R²
    y_mean = sum(prices) / len(prices)
    ss_tot = sum((y - y_mean) ** 2 for y in prices)
    ss_res = sum((y - evaluate_polynomial(linear_coeffs, x)) ** 2 for x, y in zip(month_indices, prices))
    r_squared_linear = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Quadratic fit (if enough data)
    quadratic_curvature = 0.0
    r_squared_quadratic = 0.0
    if len(months) >= 3:
        quad_coeffs, quad_metrics = fit_term_structure_curve(month_indices, prices, degree=2)
        quadratic_curvature = quad_metrics["curvature"]
        r_squared_quadratic = quad_metrics["r_squared"]

    # Nearby-deferred spread
    nearby_deferred_spread = months[-1].settlement - months[0].settlement

    # Annualized slope
    total_months = len(months) - 1
    total_days = total_months * DEFAULT_DAYS_BETWEEN_MONTHS
    if total_days > 0 and front_price > 0:
        slope_annualized_pct = ((months[-1].settlement / front_price) ** (365.0 / total_days) - 1) * 100
    else:
        slope_annualized_pct = 0.0

    return {
        "nearby_deferred_spread": round(nearby_deferred_spread, 4),
        "slope_annualized_pct": round(slope_annualized_pct, 4),
        "linear_slope": round(linear_slope, 6),
        "quadratic_curvature": round(quadratic_curvature, 6),
        "r_squared_linear": round(r_squared_linear, 6),
        "r_squared_quadratic": round(r_squared_quadratic, 6),
    }