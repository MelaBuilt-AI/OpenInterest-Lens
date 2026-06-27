"""Historical statistical analysis for OpenInterest Lens signal computation.

Provides rolling Z-score calculation, percentile ranking, mean reversion
detection, and OI-price correlation analysis. All methods operate on
lists of COT report data points and return statistical results suitable
for feeding into signal generators.
"""

from __future__ import annotations

import math

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rolling Z-score
# ---------------------------------------------------------------------------


def rolling_z_score(
    current_value: float,
    historical_values: list[float],
) -> float:
    """Calculate Z-score of current_value against a historical distribution.

    Z = (x - μ) / σ, where μ and σ are computed from historical_values.

    If σ is zero (all historical values are identical), returns 0.0
    to avoid division by zero. If fewer than 2 data points, returns 0.0.

    Args:
        current_value: The value to score.
        historical_values: Reference distribution (lookback window).

    Returns:
        Z-score as a float.
    """
    if len(historical_values) < 2:
        return 0.0

    mean = sum(historical_values) / len(historical_values)
    variance = sum((x - mean) ** 2 for x in historical_values) / len(historical_values)
    std_dev = math.sqrt(variance)

    if std_dev == 0:
        return 0.0

    return (current_value - mean) / std_dev


def rolling_z_score_windowed(
    values: list[float],
    window: int = 52,
) -> list[tuple[int, float]]:
    """Compute rolling Z-scores with a sliding window.

    For each index i >= window, the Z-score is computed against the
    preceding `window` values. Returns list of (index, z_score) pairs.

    Args:
        values: Time-ordered series of values (oldest first).
        window: Lookback window size.

    Returns:
        List of (index, z_score) tuples for indices where a full window exists.
    """
    results: list[tuple[int, float]] = []

    for i in range(window, len(values)):
        historical = values[i - window : i]
        z = rolling_z_score(values[i], historical)
        results.append((i, z))

    return results


# ---------------------------------------------------------------------------
# Percentile ranking
# ---------------------------------------------------------------------------


def percentile_rank(
    current_value: float,
    historical_values: list[float],
) -> float:
    """Calculate percentile rank of current_value within historical distribution.

    Uses the "fractional rank" method: (number of values below x + 0.5 * number
    of values equal to x) / total_count * 100.

    Returns 50.0 if there are fewer than 2 data points.

    Args:
        current_value: The value to rank.
        historical_values: Reference distribution.

    Returns:
        Percentile as a float between 0 and 100.
    """
    if len(historical_values) == 0:
        return 50.0

    below = sum(1 for v in historical_values if v < current_value)
    equal = sum(1 for v in historical_values if v == current_value)
    n = len(historical_values)

    # Fractional rank method
    percentile = (below + 0.5 * equal) / n * 100.0
    return min(max(percentile, 0.0), 100.0)


# ---------------------------------------------------------------------------
# Mean reversion detection
# ---------------------------------------------------------------------------


def detect_mean_reversion(
    current_z_score: float,
    threshold_high: float = 2.0,
    threshold_low: float = -2.0,
) -> tuple[bool, str]:
    """Detect mean reversion signals from Z-scores.

    Mean reversion occurs when positioning deviates significantly from
    the mean, suggesting a likely snap-back.

    Args:
        current_z_score: Current Z-score value.
        threshold_high: Z-score above this triggers "overbought" reversion signal.
        threshold_low: Z-score below this triggers "oversold" reversion signal.

    Returns:
        Tuple of (is_extreme: bool, direction: str).
        direction is "overbought", "oversold", or "neutral".
    """
    if current_z_score >= threshold_high:
        return True, "overbought"
    elif current_z_score <= threshold_low:
        return True, "oversold"
    else:
        return False, "neutral"


def detect_extreme_positioning(
    z_score: float,
    percentile: float,
    extreme_z_threshold: float = 1.5,
    extreme_percentile_high: float = 85.0,
    extreme_percentile_low: float = 15.0,
) -> tuple[str, str]:
    """Classify positioning extremity based on Z-score and percentile.

    Combines statistical significance (Z-score) with practical
    significance (percentile rank) for robust classification.

    Args:
        z_score: Current Z-score.
        percentile: Current percentile rank (0-100).
        extreme_z_threshold: Z-score threshold for extreme positioning.
        extreme_percentile_high: Percentile above this is "high" extremity.
        extreme_percentile_low: Percentile below this is "low" extremity.

    Returns:
        Tuple of (conviction: str, direction: str).
        conviction: "high", "medium", or "low"
        direction: "long", "short", or "neutral"
    """
    is_extreme_z = abs(z_score) >= extreme_z_threshold
    is_extreme_percentile = percentile >= extreme_percentile_high or percentile <= extreme_percentile_low
    both_extreme = is_extreme_z and is_extreme_percentile

    # Determine direction
    if z_score > 0.5 or percentile > 70:
        direction = "long"
    elif z_score < -0.5 or percentile < 30:
        direction = "short"
    else:
        direction = "neutral"

    # Determine conviction
    if both_extreme:
        conviction = "high"
    elif is_extreme_z or is_extreme_percentile:
        conviction = "medium"
    else:
        conviction = "low"

    return conviction, direction


# ---------------------------------------------------------------------------
# OI-price correlation
# ---------------------------------------------------------------------------


def compute_oi_price_correlation(
    oi_changes: list[float],
    price_changes: list[float],
) -> float:
    """Compute Pearson correlation between OI changes and price changes.

    Measures whether open interest tends to increase when prices rise
    (positive correlation) or decrease (negative correlation). Useful
    for assessing whether positioning is trend-following or contrarian.

    Args:
        oi_changes: List of OI change values (week-over-week).
        price_changes: Corresponding list of price change values.

    Returns:
        Pearson correlation coefficient (-1 to 1).
        Returns 0.0 if insufficient data or zero variance.
    """
    n = min(len(oi_changes), len(price_changes))
    if n < 3:
        return 0.0

    oi = oi_changes[:n]
    prices = price_changes[:n]

    mean_oi = sum(oi) / n
    mean_price = sum(prices) / n

    cov = sum((oi[i] - mean_oi) * (prices[i] - mean_price) for i in range(n)) / n
    var_oi = sum((x - mean_oi) ** 2 for x in oi) / n
    var_price = sum((x - mean_price) ** 2 for x in prices) / n

    denom = math.sqrt(var_oi * var_price)
    if denom == 0:
        return 0.0

    return cov / denom


def compute_week_over_week_changes(
    reports: list[dict],
    field: str = "net",
) -> list[float | None]:
    """Compute week-over-week changes for a given field from COT reports.

    Args:
        reports: List of COT report dicts sorted by date (oldest first).
            Each must have keys like f"commercial_{field}",
            f"non_commercial_{field}", f"non_reportable_{field}".
        field: Which field to compute changes for ("net", "long", "short").

    Returns:
        List of week-over-week changes. First element is None (no prior week).
    """
    if not reports:
        return []
    changes: list[float | None] = [None]

    for i in range(1, len(reports)):
        current = reports[i].get(field, 0) if isinstance(reports[i].get(field), (int, float)) else 0
        prior = reports[i - 1].get(field, 0) if isinstance(reports[i - 1].get(field), (int, float)) else 0
        changes.append(float(current - prior))

    return changes


# ---------------------------------------------------------------------------
# Windowed statistics helpers
# ---------------------------------------------------------------------------


def compute_lookback_window(
    reports: list[dict],
    lookback_weeks: int = 52,
) -> list[dict]:
    """Filter COT reports to the most recent `lookback_weeks` entries.

    Expects reports sorted by date ascending (oldest first).
    Returns the last `lookback_weeks` entries, or all if fewer exist.

    Args:
        reports: COT report dicts with an 'as_of_date' key.
        lookback_weeks: Number of weeks to include.

    Returns:
        Filtered list of reports.
    """
    if len(reports) <= lookback_weeks:
        return reports
    return reports[-lookback_weeks:]


def compute_net_positions(
    reports: list[dict],
) -> dict[str, list[float]]:
    """Extract net position series from COT reports.

    Args:
        reports: COT report dicts with commercial_net, non_commercial_net,
                 non_reportable_net keys (or commercial_long/short etc).

    Returns:
        Dict with keys 'commercial', 'non_commercial', 'non_reportable',
        each mapping to a list of net position values (floats).
    """
    commercial: list[float] = []
    non_commercial: list[float] = []
    non_reportable: list[float] = []

    for r in reports:
        # Compute net from long/short if net not directly available
        c_net = r.get("commercial_net", r.get("commercial_long", 0) - r.get("commercial_short", 0))
        nc_net = r.get("non_commercial_net", r.get("non_commercial_long", 0) - r.get("non_commercial_short", 0))
        nr_net = r.get("non_reportable_net", r.get("non_reportable_long", 0) - r.get("non_reportable_short", 0))

        commercial.append(float(c_net))
        non_commercial.append(float(nc_net))
        non_reportable.append(float(nr_net))

    return {
        "commercial": commercial,
        "non_commercial": non_commercial,
        "non_reportable": non_reportable,
    }


def extract_oi_series(reports: list[dict]) -> list[float]:
    """Extract total open interest series from COT reports.

    Args:
        reports: COT report dicts with total_open_interest key.

    Returns:
        List of OI values as floats.
    """
    return [float(r.get("total_open_interest", 0)) for r in reports]