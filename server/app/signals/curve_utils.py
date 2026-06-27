"""Curve fitting and analysis utilities for term structure computation.

Provides polynomial curve fitting, derivative calculation, interpolation
for missing contract months, and normalization across different price ranges.
All functions use pure Python (no numpy/scipy dependency) for portability.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Polynomial curve fitting (least-squares via normal equations)
# ---------------------------------------------------------------------------


def fit_polynomial(
    x_values: list[float],
    y_values: list[float],
    degree: int = 2,
) -> list[float]:
    """Fit a polynomial of given degree to (x, y) data using least squares.

    Solves the normal equations (X^T X) c = X^T y via Gaussian elimination
    with partial pivoting. No external dependencies required.

    Args:
        x_values: Independent variable values (e.g., month indices).
        y_values: Dependent variable values (e.g., settlement prices).
        degree: Polynomial degree (1=linear, 2=quadratic).

    Returns:
        List of polynomial coefficients [c0, c1, c2, ...] such that
        y ≈ c0 + c1*x + c2*x^2 + ...

    Raises:
        ValueError: If input lengths mismatch or insufficient data points.
    """
    n = len(x_values)
    if n != len(y_values):
        raise ValueError(f"x and y lengths must match: {n} vs {len(y_values)}")
    if n <= degree:
        raise ValueError(
            f"Need at least {degree + 1} data points for degree {degree} fit, got {n}"
        )

    # Build the Vandermonde-like matrix X^T X and X^T y
    # X[i][j] = x_i^j for j in 0..degree
    # (X^T X)[j][k] = sum(x_i^(j+k)) for i in 0..n-1
    # (X^T y)[j] = sum(x_i^j * y_i) for i in 0..n-1
    m = degree + 1  # Number of coefficients

    # Build augmented matrix [A | b] for (X^T X) c = X^T y
    aug = [[0.0] * (m + 1) for _ in range(m)]

    for j in range(m):
        for k in range(m):
            aug[j][k] = sum(x ** (j + k) for x in x_values)
        aug[j][m] = sum((x ** j) * y for x, y in zip(x_values, y_values, strict=False))

    # Gaussian elimination with partial pivoting
    for col in range(m):
        # Find pivot
        max_row = col
        max_val = abs(aug[col][col])
        for row in range(col + 1, m):
            if abs(aug[row][col]) > max_val:
                max_val = abs(aug[row][col])
                max_row = row
        # Swap rows
        aug[col], aug[max_row] = aug[max_row], aug[col]

        pivot = aug[col][col]
        if abs(pivot) < 1e-12:
            # Singular or near-singular — fall back to lower degree
            if degree > 0:
                return fit_polynomial(x_values, y_values, degree=degree - 1)
            # degree 0 with singular matrix → return constant
            avg_y = sum(y_values) / len(y_values)
            return [avg_y]

        # Eliminate below
        for row in range(col + 1, m):
            factor = aug[row][col] / pivot
            for k in range(col, m + 1):
                aug[row][k] -= factor * aug[col][k]

    # Back substitution
    coeffs = [0.0] * m
    for i in range(m - 1, -1, -1):
        coeffs[i] = aug[i][m]
        for j in range(i + 1, m):
            coeffs[i] -= aug[i][j] * coeffs[j]
        coeffs[i] /= aug[i][i]

    return coeffs


def evaluate_polynomial(coeffs: list[float], x: float) -> float:
    """Evaluate a polynomial at a given point using Horner's method.

    Args:
        coeffs: Polynomial coefficients [c0, c1, c2, ...].
        x: Point at which to evaluate.

    Returns:
        Polynomial value at x.
    """
    # Horner's method: c0 + x*(c1 + x*(c2 + ...))
    result = 0.0
    for c in reversed(coeffs):
        result = result * x + c
    return result


def polynomial_derivative(coeffs: list[float]) -> list[float]:
    """Compute the derivative of a polynomial.

    If p(x) = c0 + c1*x + c2*x^2 + ... + cn*x^n,
    then p'(x) = c1 + 2*c2*x + 3*c3*x^2 + ... + n*cn*x^(n-1).

    Args:
        coeffs: Polynomial coefficients [c0, c1, c2, ...].

    Returns:
        Derivative coefficients [c1, 2*c2, 3*c3, ...].
        Returns [0.0] for constant polynomials.
    """
    if len(coeffs) <= 1:
        return [0.0]
    return [coeffs[i] * i for i in range(1, len(coeffs))]


def evaluate_derivative_at(coeffs: list[float], x: float) -> float:
    """Evaluate the first derivative of a polynomial at a given point.

    Args:
        coeffs: Polynomial coefficients.
        x: Point at which to evaluate the derivative.

    Returns:
        Value of the derivative at x.
    """
    deriv_coeffs = polynomial_derivative(coeffs)
    return evaluate_polynomial(deriv_coeffs, x)


def compute_curve_slope(
    coeffs: list[float],
    x_start: float,
    x_end: float,
) -> float:
    """Compute the average slope of a fitted curve between two points.

    Slope = (p(x_end) - p(x_start)) / (x_end - x_start)

    Args:
        coeffs: Polynomial coefficients.
        x_start: Start of the interval.
        x_end: End of the interval.

    Returns:
        Average slope over the interval.
    """
    if x_end == x_start:
        return evaluate_derivative_at(coeffs, x_start)
    y_start = evaluate_polynomial(coeffs, x_start)
    y_end = evaluate_polynomial(coeffs, x_end)
    return (y_end - y_start) / (x_end - x_start)


# ---------------------------------------------------------------------------
# Curve shape classification
# ---------------------------------------------------------------------------


def classify_curve(coeffs: list[float], x_range: tuple[float, float]) -> str:
    """Classify a term structure curve based on its fitted polynomial.

    Classification logic:
    - **contango**: Overall upward-sloping (deferred prices higher than nearby).
      Average derivative positive across the curve.
    - **backwardation**: Overall downward-sloping (nearby prices higher than deferred).
      Average derivative negative across the curve.
    - **humped**: Curve has an interior maximum (concave down quadratic with
      positive linear term), i.e., prices rise then fall.
    - **flat**: Near-zero average slope, prices barely change across months.

    Args:
        coeffs: Fitted polynomial coefficients.
        x_range: Tuple of (x_min, x_max) for the curve domain.

    Returns:
        One of 'contango', 'backwardation', 'flat', 'humped'.
    """
    x_min, x_max = x_range
    n_samples = max(10, int(x_max - x_min) + 1)
    step = (x_max - x_min) / (n_samples - 1) if x_max > x_min else 1.0

    # Sample derivative at multiple points
    derivatives = []
    values = []
    for i in range(n_samples):
        x = x_min + i * step
        derivatives.append(evaluate_derivative_at(coeffs, x))
        values.append(evaluate_polynomial(coeffs, x))

    avg_derivative = sum(derivatives) / len(derivatives)

    # Check for hump: values rise then fall (interior max)
    if len(values) >= 3:
        max_val = max(values)
        max_idx = values.index(max_val)
        # Interior max (not at the ends) → humped
        if 0 < max_idx < len(values) - 1:
            # Verify it's a real hump: start and end values are both below the max
            if values[0] < max_val * 0.999 and values[-1] < max_val * 0.999:
                return "humped"

    # Flat threshold: average derivative < 0.1% of price range
    price_range = max(values) - min(values) if values else 0
    mid_price = (max(values) + min(values)) / 2 if values else 1.0
    flat_threshold = mid_price * 0.001  # 0.1% of mid-price

    if price_range < flat_threshold and abs(avg_derivative) < flat_threshold:
        return "flat"

    # Positive average derivative → contango, negative → backwardation
    if avg_derivative > 0:
        return "contango"
    elif avg_derivative < 0:
        return "backwardation"
    else:
        return "flat"


# ---------------------------------------------------------------------------
# Interpolation for missing contract months
# ---------------------------------------------------------------------------


def interpolate_missing_months(
    known_months: list[tuple[float, float]],
    all_month_indices: list[float],
    method: str = "linear",
) -> list[tuple[float, float]]:
    """Interpolate prices for missing contract months.

    Given a set of (month_index, price) pairs with known settlements,
    fills in prices for all month indices using the specified method.

    Args:
        known_months: List of (month_index, price) pairs for months with data.
            Must be sorted by month_index ascending.
        all_month_indices: All month indices that need prices (including known ones).
        method: Interpolation method — 'linear' (default) or 'nearest'.
            'linear': Linear interpolation between known points, extrapolation at edges.
            'nearest': Nearest-neighbor interpolation.

    Returns:
        List of (month_index, price) pairs for all requested months.

    Raises:
        ValueError: If known_months is empty.
    """
    if not known_months:
        raise ValueError("known_months must not be empty")

    # Sort known months by index
    known = sorted(known_months, key=lambda p: p[0])
    known_indices = [p[0] for p in known]
    known_prices = [p[1] for p in known]

    if len(known) == 1:
        # Only one known point — constant interpolation
        return [(idx, known[0][1]) for idx in all_month_indices]

    result: list[tuple[float, float]] = []

    for idx in all_month_indices:
        # Check if we have an exact match
        if idx in known_indices:
            match_pos = known_indices.index(idx)
            result.append((idx, known_prices[match_pos]))
            continue

        if method == "nearest":
            # Find nearest known month
            min_dist = float("inf")
            nearest_price = known_prices[0]
            for ki, kp in zip(known_indices, known_prices, strict=False):
                dist = abs(ki - idx)
                if dist < min_dist:
                    min_dist = dist
                    nearest_price = kp
            result.append((idx, nearest_price))

        else:  # linear
            # Find bracketing known months
            # idx < first known → extrapolate using first two points
            if idx < known_indices[0]:
                if len(known) >= 2:
                    # Linear extrapolation from first two points
                    x0, y0 = known[0]
                    x1, y1 = known[1]
                    if x1 != x0:
                        slope = (y1 - y0) / (x1 - x0)
                        price = y0 + slope * (idx - x0)
                    else:
                        price = y0
                else:
                    price = known_prices[0]
                result.append((idx, price))
                continue

            # idx > last known → extrapolate using last two points
            if idx > known_indices[-1]:
                if len(known) >= 2:
                    x0, y0 = known[-2]
                    x1, y1 = known[-1]
                    if x1 != x0:
                        slope = (y1 - y0) / (x1 - x0)
                        price = y1 + slope * (idx - x1)
                    else:
                        price = y1
                else:
                    price = known_prices[-1]
                result.append((idx, price))
                continue

            # Interpolation: find bracketing known months
            left_idx = 0
            for i in range(len(known_indices) - 1):
                if known_indices[i] <= idx <= known_indices[i + 1]:
                    left_idx = i
                    break

            x0, y0 = known[left_idx]
            x1, y1 = known[left_idx + 1]
            if x1 != x0:
                t = (idx - x0) / (x1 - x0)
                price = y0 + t * (y1 - y0)
            else:
                price = y0
            result.append((idx, price))

    return result


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_curve(
    months: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """Normalize term structure prices to percentage of front month.

    Converts absolute prices to relative percentages, making curves
    comparable across different price ranges (e.g., CL at $80 vs GC at $2000).

    Args:
        months: List of (month_code, settlement_price, spread_to_front) tuples.

    Returns:
        List of (month_code, normalized_price_pct, spread_to_front_pct) tuples.
        normalized_price_pct = (price / front_month_price) * 100
        spread_to_front_pct = (spread / front_month_price) * 100
    """
    if not months:
        return []

    front_price = months[0][1]
    if front_price == 0:
        return [(m, 0.0, 0.0) for m, _, _ in months]

    normalized: list[tuple[str, float, float]] = []
    for month_code, price, spread in months:
        norm_price = (price / front_price) * 100.0 if front_price != 0 else 0.0
        norm_spread = (spread / front_price) * 100.0 if front_price != 0 else 0.0
        normalized.append((month_code, round(norm_price, 4), round(norm_spread, 4)))

    return normalized


def compute_annualized_yield(
    near_price: float,
    far_price: float,
    days_between: float,
) -> float:
    """Compute annualized yield between two contract months.

    Annualized yield represents the return from holding the deferred contract
    vs the nearby, annualized based on the time between expiries.

    Formula: ((far / near) ^ (365 / days_between) - 1) * 100

    Args:
        near_price: Nearby (front) month settlement price.
        far_price: Deferred month settlement price.
        days_between: Calendar days between the two contract expiry dates.

    Returns:
        Annualized yield as a percentage (e.g., 5.25 for 5.25%).
    """
    if near_price <= 0 or far_price <= 0 or days_between <= 0:
        return 0.0

    try:
        ratio = far_price / near_price
        annualized = (ratio ** (365.0 / days_between) - 1.0) * 100.0
        return round(annualized, 4)
    except (OverflowError, ZeroDivisionError):
        return 0.0


def compute_spread_to_front(
    front_price: float,
    month_price: float,
) -> float:
    """Compute the price spread from the front month to a given month.

    Positive = deferred is higher (contango).
    Negative = deferred is lower (backwardation).

    Args:
        front_price: Front month settlement price.
        month_price: Deferred month settlement price.

    Returns:
        Spread in price units (month_price - front_price).
    """
    return month_price - front_price


def fit_term_structure_curve(
    month_indices: list[float],
    prices: list[float],
    degree: int = 2,
) -> tuple[list[float], dict[str, float]]:
    """Fit a polynomial curve to term structure data and return key metrics.

    Args:
        month_indices: Month indices (0 = front month, 1 = next month, etc.).
        prices: Corresponding settlement prices.
        degree: Polynomial degree for fitting (1=linear, 2=quadratic).

    Returns:
        Tuple of (coefficients, metrics) where metrics includes:
        - slope: Average slope across the curve
        - curvature: Second derivative at the midpoint
        - r_squared: Goodness of fit (coefficient of determination)
        - classification: 'contango', 'backwardation', 'flat', or 'humped'
    """
    if len(month_indices) < 2:
        # Not enough data for fitting
        avg_price = sum(prices) / len(prices) if prices else 0
        return [avg_price], {
            "slope": 0.0,
            "curvature": 0.0,
            "r_squared": 0.0,
            "classification": "flat",
        }

    # Fit polynomial
    # Use linear fit for 2 points, otherwise use requested degree
    actual_degree = min(degree, len(month_indices) - 1)
    coeffs = fit_polynomial(month_indices, prices, degree=actual_degree)

    # Compute R-squared
    y_mean = sum(prices) / len(prices)
    ss_tot = sum((y - y_mean) ** 2 for y in prices)
    ss_res = sum((y - evaluate_polynomial(coeffs, x)) ** 2 for x, y in zip(month_indices, prices, strict=False))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Compute slope (average of derivative across the curve)
    x_min = min(month_indices)
    x_max = max(month_indices)
    slope = compute_curve_slope(coeffs, x_min, x_max)

    # Compute curvature (second derivative at midpoint)
    if len(coeffs) >= 3:
        # Second derivative coefficients: 2*c2 + 6*c3*x + ...
        second_deriv = polynomial_derivative(polynomial_derivative(coeffs))
        midpoint = (x_min + x_max) / 2.0
        curvature = evaluate_polynomial(second_deriv, midpoint)
    else:
        curvature = 0.0

    # Classify the curve
    classification = classify_curve(coeffs, (x_min, x_max))

    metrics = {
        "slope": round(slope, 6),
        "curvature": round(curvature, 6),
        "r_squared": round(r_squared, 6),
        "classification": classification,
    }

    return coeffs, metrics