"""Roll calendar for futures contract roll tracking and prediction.

Tracks and predicts roll dates for futures contracts, estimates roll volume
based on OI patterns, generates roll schedules, and classifies roll urgency.
Uses CME-style third-Friday expiry rules for equity index futures and
common patterns for other asset classes.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Contract month codes and expiry rules
# ---------------------------------------------------------------------------

# Month code mapping: letter → month number
MONTH_CODES: dict[str, int] = {
    "F": 1,   # January
    "G": 2,   # February
    "H": 3,   # March
    "J": 4,   # April
    "K": 5,   # May
    "M": 6,   # June
    "N": 7,   # July
    "Q": 8,   # August
    "U": 9,   # September
    "V": 10,  # October
    "X": 11,  # November
    "Z": 12,  # December
}

# Reverse mapping: month number → letter
MONTH_NUM_TO_CODE: dict[int, str] = {v: k for k, v in MONTH_CODES.items()}

# Active months per contract (CME standard)
# Maps commodity symbol → list of month codes traded
CONTRACT_ACTIVE_MONTHS: dict[str, list[str]] = {
    "ES": ["H", "M", "U", "Z"],  # Mar, Jun, Sep, Dec (quarterly)
    "NQ": ["H", "M", "U", "Z"],  # Mar, Jun, Sep, Dec (quarterly)
    "CL": ["F", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],  # Monthly (minus some)
    "GC": ["G", "J", "M", "Q", "V", "Z"],  # Feb, Apr, Jun, Aug, Oct, Dec
}

# Days before expiry when roll typically becomes active
# This varies by contract: equity indices roll ~5 days before expiry,
# energy rolls ~7 days before, metals roll ~3 days before
ROLL_START_DAYS_BEFORE_EXPIRY: dict[str, int] = {
    "ES": 5,
    "NQ": 5,
    "CL": 7,
    "GC": 3,
}

# Default roll window (days before expiry) for unknown contracts
DEFAULT_ROLL_START_DAYS = 5

# Days after expiry when roll is considered complete
ROLL_END_DAYS_AFTER_EXPIRY = 3


# ---------------------------------------------------------------------------
# Expiry date calculation
# ---------------------------------------------------------------------------


def calculate_expiry_date(
    year: int,
    month: int,
    contract_symbol: str = "ES",
) -> date:
    """Calculate the expiry date for a futures contract.

    Uses CME rules:
    - Equity index futures (ES, NQ): Third Friday of the contract month
    - Energy (CL): Third business day before the 25th of the month prior
      to the contract month (simplified: 3rd business day before the 25th
      of the prior month)
    - Metals (GC): Third-to-last business day of the contract month
    - Default: Third Friday of the contract month

    Args:
        year: Contract year (e.g., 2026).
        month: Contract month number (1-12).
        contract_symbol: Root symbol for contract-specific rules.

    Returns:
        The expiry date.
    """
    if contract_symbol in ("ES", "NQ"):
        # Third Friday of the contract month
        return _nth_weekday_of_month(year, month, weekday=4, n=3)  # Friday=4
    elif contract_symbol == "CL":
        # Simplified: 25th of the prior month minus 3 business days
        # For simplicity, use the 22nd of the prior month
        if month == 1:
            prior_year, prior_month = year - 1, 12
        else:
            prior_year, prior_month = year, month - 1
        # Find the 25th, then go back 3 business days
        twenty_fifth = date(prior_year, prior_month, 25)
        return _subtract_business_days(twenty_fifth, 3)
    elif contract_symbol == "GC":
        # Third-to-last business day of the contract month
        last_day = _last_day_of_month(year, month)
        return _subtract_business_days(last_day, 2)
    else:
        # Default: Third Friday
        return _nth_weekday_of_month(year, month, weekday=4, n=3)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Find the nth occurrence of a weekday in a month.

    Args:
        year: Year.
        month: Month (1-12).
        weekday: 0=Monday, 4=Friday, 6=Sunday.
        n: Which occurrence (1=first, 2=second, 3=third).

    Returns:
        Date of the nth weekday.
    """
    first_day = date(year, month, 1)
    # Days until first occurrence of weekday
    days_until = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=days_until)
    return first_occurrence + timedelta(weeks=n - 1)


def _subtract_business_days(from_date: date, num_days: int) -> date:
    """Subtract business days from a date (skipping weekends).

    Args:
        from_date: Starting date.
        num_days: Number of business days to subtract.

    Returns:
        Date that is num_days business days before from_date.
    """
    current = from_date
    subtracted = 0
    while subtracted < num_days:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # Monday=0, Friday=4
            subtracted += 1
    return current


def _last_day_of_month(year: int, month: int) -> date:
    """Get the last day of the given month."""
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


# ---------------------------------------------------------------------------
# Month code parsing
# ---------------------------------------------------------------------------


def parse_month_code(month_code: str) -> tuple[int, int]:
    """Parse a futures month code like 'Jun 26' or 'U26' into (month, year).

    Supported formats:
    - 'Jun 26' → (6, 2026)
    - 'U26' → (9, 2026)
    - 'H25' → (3, 2025)

    Args:
        month_code: Month code string.

    Returns:
        Tuple of (month_number, year) where year is full 4-digit year.

    Raises:
        ValueError: If the month code cannot be parsed.
    """
    month_code = month_code.strip()

    # Format: 'Mon YY' or 'Mon YYYY' (e.g., 'Jun 26', 'Jun 2026')
    month_names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    parts = month_code.split()
    if len(parts) == 2:
        month_str, year_str = parts
        month_str_lower = month_str.lower()[:3]
        if month_str_lower in month_names:
            month = month_names[month_str_lower]
            year = int(year_str)
            if year < 100:
                year += 2000  # '26' → 2026
            return (month, year)

    # Format: single letter + 2 digits (e.g., 'U26', 'H25')
    if len(month_code) == 3:
        letter = month_code[0].upper()
        year_str = month_code[1:]
        if letter in MONTH_CODES:
            month = MONTH_CODES[letter]
            year = int(year_str)
            if year < 100:
                year += 2000
            return (month, year)

    raise ValueError(f"Cannot parse month code: '{month_code}'")


def generate_month_code(month: int, year: int) -> str:
    """Generate a display month code like 'Jun 26' from month and year.

    Args:
        month: Month number (1-12).
        year: Full year (e.g., 2026).

    Returns:
        Display string like 'Jun 26'.
    """
    month_names = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    return f"{month_names[month]} {year % 100:02d}"


def generate_cme_month_code(month: int, year: int) -> str:
    """Generate a CME-style month code like 'U26' from month and year.

    Args:
        month: Month number (1-12).
        year: Full year (e.g., 2026).

    Returns:
        CME code like 'U26'.
    """
    letter = MONTH_NUM_TO_CODE.get(month, "?")
    return f"{letter}{year % 100:02d}"


# ---------------------------------------------------------------------------
# Roll calendar computation
# ---------------------------------------------------------------------------


class RollInfo:
    """Roll information for a single contract cycle."""

    def __init__(
        self,
        contract_symbol: str,
        nearby_month_code: str,
        nearby_expiry: date,
        deferred_month_code: str,
        deferred_expiry: date,
        days_to_roll: int,
        roll_start_date: date,
        roll_end_date: date,
        roll_urgency: str,
    ) -> None:
        self.contract_symbol = contract_symbol
        self.nearby_month_code = nearby_month_code
        self.nearby_expiry = nearby_expiry
        self.deferred_month_code = deferred_month_code
        self.deferred_expiry = deferred_expiry
        self.days_to_roll = days_to_roll
        self.roll_start_date = roll_start_date
        self.roll_end_date = roll_end_date
        self.roll_urgency = roll_urgency

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "contract_symbol": self.contract_symbol,
            "nearby_month_code": self.nearby_month_code,
            "nearby_expiry": self.nearby_expiry.isoformat(),
            "deferred_month_code": self.deferred_month_code,
            "deferred_expiry": self.deferred_expiry.isoformat(),
            "days_to_roll": self.days_to_roll,
            "roll_start_date": self.roll_start_date.isoformat(),
            "roll_end_date": self.roll_end_date.isoformat(),
            "roll_urgency": self.roll_urgency,
        }


def get_active_contract_months(contract_symbol: str) -> list[str]:
    """Get the active (traded) month codes for a contract.

    Falls back to quarterly (H, M, U, Z) for unknown contracts.

    Args:
        contract_symbol: Root symbol like 'ES', 'CL'.

    Returns:
        List of month code letters.
    """
    return CONTRACT_ACTIVE_MONTHS.get(contract_symbol.upper(), ["H", "M", "U", "Z"])


def calculate_roll_info(
    contract_symbol: str,
    as_of_date: date,
    nearby_month_code: Optional[str] = None,
) -> RollInfo:
    """Calculate roll information for a contract as of a given date.

    Determines the current nearby and deferred contract months, calculates
    days to expiry, roll window dates, and roll urgency classification.

    Args:
        contract_symbol: Root symbol, e.g. 'ES'.
        as_of_date: The reference date for calculating roll info.
        nearby_month_code: Optional explicit nearby month code (e.g. 'Jun 26').
            If None, it's determined from the as_of_date and contract cycle.

    Returns:
        RollInfo with roll dates, urgency, and timing details.
    """
    contract_symbol = contract_symbol.upper()
    active_months = get_active_contract_months(contract_symbol)
    roll_start_days = ROLL_START_DAYS_BEFORE_EXPIRY.get(contract_symbol, DEFAULT_ROLL_START_DAYS)

    # Determine nearby and deferred contract months
    if nearby_month_code is not None:
        nearby_month, nearby_year = parse_month_code(nearby_month_code)
    else:
        nearby_month, nearby_year = _find_nearby_contract(
            contract_symbol, as_of_date, active_months
        )

    # Find deferred contract
    deferred_month, deferred_year = _find_next_contract(
        nearby_month, nearby_year, active_months
    )

    # Calculate expiry dates
    nearby_expiry = calculate_expiry_date(nearby_year, nearby_month, contract_symbol)
    deferred_expiry = calculate_expiry_date(deferred_year, deferred_month, contract_symbol)

    # Days to roll (days until nearby expiry)
    days_to_roll = (nearby_expiry - as_of_date).days

    # Roll window dates
    roll_start_date = nearby_expiry - timedelta(days=roll_start_days)
    roll_end_date = nearby_expiry + timedelta(days=ROLL_END_DAYS_AFTER_EXPIRY)

    # Classify roll urgency
    roll_urgency = classify_roll_urgency(days_to_roll, roll_start_days)

    nearby_display = generate_month_code(nearby_month, nearby_year)
    deferred_display = generate_month_code(deferred_month, deferred_year)

    return RollInfo(
        contract_symbol=contract_symbol,
        nearby_month_code=nearby_display,
        nearby_expiry=nearby_expiry,
        deferred_month_code=deferred_display,
        deferred_expiry=deferred_expiry,
        days_to_roll=days_to_roll,
        roll_start_date=roll_start_date,
        roll_end_date=roll_end_date,
        roll_urgency=roll_urgency,
    )


def _find_nearby_contract(
    contract_symbol: str,
    as_of_date: date,
    active_months: list[str],
) -> tuple[int, int]:
    """Find the current nearby contract month for a given date.

    The nearby contract is the active contract month whose expiry has not
    yet passed. If we're past the last expiry of the year, roll to next year.

    Args:
        contract_symbol: Root symbol.
        as_of_date: Reference date.
        active_months: Active month codes for this contract.

    Returns:
        Tuple of (month_number, year).
    """
    active_month_nums = sorted(MONTH_CODES[m] for m in active_months if m in MONTH_CODES)

    year = as_of_date.year
    roll_start_days = ROLL_START_DAYS_BEFORE_EXPIRY.get(contract_symbol, DEFAULT_ROLL_START_DAYS)

    for month in active_month_nums:
        expiry = calculate_expiry_date(year, month, contract_symbol)
        # Consider the roll start date, not just the expiry
        roll_start = expiry - timedelta(days=roll_start_days)
        if as_of_date < expiry:
            return (month, year)

    # All expiries this year have passed → first contract of next year
    return (active_month_nums[0], year + 1)


def _find_next_contract(
    current_month: int,
    current_year: int,
    active_months: list[str],
) -> tuple[int, int]:
    """Find the next active contract month after the current one.

    Args:
        current_month: Current nearby month number (1-12).
        current_year: Current nearby year.
        active_months: Active month code letters.

    Returns:
        Tuple of (month_number, year) for the next contract.
    """
    active_month_nums = sorted(MONTH_CODES[m] for m in active_months if m in MONTH_CODES)

    # Find next month in the cycle
    for month in active_month_nums:
        if month > current_month:
            return (month, current_year)

    # Wrap to next year
    return (active_month_nums[0], current_year + 1)


def classify_roll_urgency(days_to_roll: int, roll_start_days: int = 5) -> str:
    """Classify roll urgency based on days to expiry.

    Args:
        days_to_roll: Calendar days until the nearby contract expires.
        roll_start_days: Days before expiry when roll typically starts.

    Returns:
        One of: 'imminent' (≤0 days), 'active' (within roll window),
        'normal' (approaching but not yet in window), 'relaxed' (>30 days).
    """
    if days_to_roll <= 0:
        return "imminent"
    elif days_to_roll <= roll_start_days:
        return "active"
    elif days_to_roll <= 30:
        return "normal"
    else:
        return "relaxed"


def generate_roll_schedule(
    contract_symbol: str,
    as_of_date: date,
    num_cycles: int = 4,
) -> list[RollInfo]:
    """Generate a roll schedule for the next N contract cycles.

    Args:
        contract_symbol: Root symbol, e.g. 'ES'.
        as_of_date: Reference date.
        num_cycles: Number of future roll cycles to generate.

    Returns:
        List of RollInfo objects for upcoming roll cycles.
    """
    contract_symbol = contract_symbol.upper()
    active_months = get_active_contract_months(contract_symbol)
    schedule: list[RollInfo] = []

    # Find the current nearby contract
    nearby_month, nearby_year = _find_nearby_contract(
        contract_symbol, as_of_date, active_months
    )

    # Generate roll info for current and next contracts
    current_month, current_year = nearby_month, nearby_year

    for _ in range(num_cycles):
        display_code = generate_month_code(current_month, current_year)
        roll_info = calculate_roll_info(
            contract_symbol=contract_symbol,
            as_of_date=as_of_date,
            nearby_month_code=display_code,
        )
        schedule.append(roll_info)

        # Move to the next contract in the cycle
        current_month, current_year = _find_next_contract(
            current_month, current_year, active_months
        )

    return schedule


# ---------------------------------------------------------------------------
# Roll volume estimation
# ---------------------------------------------------------------------------


def estimate_roll_volume(
    nearby_oi: int,
    deferred_oi: int,
    days_to_roll: int,
    avg_daily_volume: int = 0,
) -> dict[str, float]:
    """Estimate roll volume based on OI patterns and timing.

    The roll volume represents the expected number of contracts that will
    need to be rolled from the nearby to the deferred month. This is
    estimated from the nearby OI and the time remaining before expiry.

    Args:
        nearby_oi: Current open interest in the nearby contract.
        deferred_oi: Current open interest in the deferred contract.
        days_to_roll: Calendar days until nearby expiry.
        avg_daily_volume: Average daily volume (used for scaling).

    Returns:
        Dict with:
        - estimated_roll_volume: Expected contracts to roll
        - roll_completion_pct: Estimated % of OI already rolled
        - peak_roll_day_volume: Expected volume on peak roll day
    """
    if days_to_roll <= 0:
        # Already past expiry
        return {
            "estimated_roll_volume": 0.0,
            "roll_completion_pct": 100.0,
            "peak_roll_day_volume": 0.0,
        }

    # Typical roll pattern: most volume concentrates in the last 5 days
    # Before the roll window: ~10% of OI rolls early
    # During the roll window: ~80% of OI rolls
    # After the roll window: ~10% of OI rolls (late rollers)

    roll_start_days = 5  # Standard assumption
    total_oi = nearby_oi + deferred_oi

    if days_to_roll > 30:
        # Relaxed period: minimal rolling
        estimated_roll_volume = nearby_oi * 0.05
        completion_pct = 5.0
    elif days_to_roll > roll_start_days:
        # Normal period: gradual rolling
        # Estimate what fraction has already rolled based on time elapsed
        total_period = 30  # Approximate total roll period in days
        elapsed = total_period - days_to_roll
        fraction_complete = min(elapsed / total_period, 1.0) if total_period > 0 else 0.0
        estimated_roll_volume = nearby_oi * (0.3 * fraction_complete)
        completion_pct = min(fraction_complete * 30.0, 30.0)
    else:
        # Active roll period: heavy rolling
        # Estimate remaining roll volume based on OI that hasn't rolled yet
        fraction_of_total = days_to_roll / max(roll_start_days, 1)
        estimated_rolled = nearby_oi * 0.5 * (1.0 - fraction_of_total)
        estimated_roll_volume = nearby_oi - estimated_rolled
        completion_pct = min(50.0 + (1.0 - fraction_of_total) * 50.0, 100.0)

    # Peak day volume: typically 2-3x average on peak roll day
    if avg_daily_volume > 0:
        peak_roll_day_volume = avg_daily_volume * 2.5
    else:
        # Estimate from OI: peak day is about 15-25% of nearby OI
        peak_roll_day_volume = nearby_oi * 0.20

    return {
        "estimated_roll_volume": round(estimated_roll_volume, 0),
        "roll_completion_pct": round(min(completion_pct, 100.0), 1),
        "peak_roll_day_volume": round(peak_roll_day_volume, 0),
    }


def estimate_oi_decay_rate(
    nearby_oi_series: list[tuple[date, int]],
    total_oi: int,
    lookback_days: int = 5,
) -> float:
    """Estimate the rate of OI decay in the nearby contract.

    OI decay is the rate at which open interest leaves the nearby contract
    as traders roll to the deferred month. A higher decay rate indicates
    more aggressive rolling.

    Args:
        nearby_oi_series: List of (date, oi) tuples, sorted by date ascending.
        total_oi: Total OI across all months for the commodity.
        lookback_days: Number of recent days to use for decay calculation.

    Returns:
        OI decay percentage (0-100) representing the fraction of nearby OI
        that has declined over the lookback period relative to total OI.
        Returns 0.0 if insufficient data.
    """
    if len(nearby_oi_series) < 2:
        return 0.0

    # Use the most recent entries
    recent = nearby_oi_series[-lookback_days:] if len(nearby_oi_series) > lookback_days else nearby_oi_series

    if len(recent) < 2:
        return 0.0

    oldest_oi = recent[0][1]
    newest_oi = recent[-1][1]

    if oldest_oi <= 0 or total_oi <= 0:
        return 0.0

    # Decay = (OI decline) / total OI * 100
    oi_decline = oldest_oi - newest_oi
    decay_pct = (oi_decline / total_oi) * 100.0

    return max(0.0, round(decay_pct, 2))  # Cannot be negative (OI can grow, but decay is 0)


def calculate_roll_date_proximity(
    days_to_roll: int,
    contract_symbol: str = "ES",
) -> dict[str, float | str]:
    """Calculate roll date proximity signals.

    Provides both a numeric proximity score and a categorical signal
    indicating how close we are to the roll date.

    Args:
        days_to_roll: Calendar days until nearby contract expiry.
        contract_symbol: Contract symbol for roll window lookup.

    Returns:
        Dict with:
        - proximity_score: 0-100 (100 = expiry day, 0 = far away)
        - roll_window: 'pre_roll', 'active_roll', or 'post_roll'
        - signal_strength: How strong the roll effect is (0-1)
    """
    roll_start_days = ROLL_START_DAYS_BEFORE_EXPIRY.get(contract_symbol, DEFAULT_ROLL_START_DAYS)

    if days_to_roll <= 0:
        # Past expiry
        return {
            "proximity_score": 100.0,
            "roll_window": "post_roll",
            "signal_strength": 0.1,  # Roll effect fades quickly after expiry
        }

    # Proximity score: exponential decay from 100 at expiry to 0 at 60 days out
    max_distance = 60  # Beyond 60 days, proximity is effectively 0
    proximity_score = max(0.0, 100.0 * math.exp(-0.05 * days_to_roll))

    # Roll window classification
    if days_to_roll <= 0:
        roll_window = "post_roll"
    elif days_to_roll <= roll_start_days:
        roll_window = "active_roll"
    elif days_to_roll <= 30:
        roll_window = "pre_roll"
    else:
        roll_window = "pre_roll"  # Far from roll

    # Signal strength: peak during active roll, tapering off
    if days_to_roll <= roll_start_days:
        signal_strength = 1.0
    elif days_to_roll <= 15:
        signal_strength = 0.7
    elif days_to_roll <= 30:
        signal_strength = 0.3
    else:
        signal_strength = 0.1

    return {
        "proximity_score": round(proximity_score, 2),
        "roll_window": roll_window,
        "signal_strength": round(signal_strength, 3),
    }