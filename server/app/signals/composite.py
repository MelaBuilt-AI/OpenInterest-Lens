"""Composite market structure signal engine for OpenInterest Lens.

Combines positioning, term structure, and roll pressure signals into one
unified market structure score. Handles missing signals gracefully through
weight renormalization.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Contract, RawSettlement
from app.models.signal import (
    CompositeSignalResponse,
    HistoricalComparison,
    SignalAlignment,
    SignalBreakdownItem,
)
from app.signals.historical import percentile_rank
from app.signals.positioning import compute_positioning_signal
from app.signals.roll_pressure import compute_roll_pressure
from app.signals.term_structure import (
    compute_contango_backwardation,
    compute_term_structure,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Default weight configuration
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "positioning": 0.40,
    "term_structure": 0.30,
    "roll_pressure": 0.30,
}


# ---------------------------------------------------------------------------
# Composite signal calculator
# ---------------------------------------------------------------------------


class CompositeSignalCalculator:
    """Combines positioning, term structure, and roll pressure into one score.

    The composite signal provides a single actionable view of market
    structure by merging three complementary perspectives:

    - **Positioning** (40%): Smart money vs retail divergence from COT data
    - **Term structure** (30%): Contango/backwardation classification
    - **Roll pressure** (30%): Roll activity intensity and timing

    Missing signals are handled by renormalizing remaining weights to sum to 1.
    """

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        """Initialize with optional custom weights.

        Args:
            weights: Dict with keys 'positioning', 'term_structure', 'roll_pressure'.
                     Values must be >= 0. Missing keys use defaults.
        """
        self.weights: dict[str, float] = DEFAULT_WEIGHTS.copy()
        if weights:
            for key in ("positioning", "term_structure", "roll_pressure"):
                if key in weights:
                    value = weights[key]
                    if value < 0:
                        raise ValueError(f"Weight '{key}' must be >= 0, got {value}")
                    self.weights[key] = value

    async def compute(
        self,
        contract_symbol: str,
        db: AsyncSession,
    ) -> CompositeSignalResponse:
        """Compute the full composite signal for a contract.

        Args:
            contract_symbol: Root symbol, e.g. 'ES'.
            db: Async database session.

        Returns:
            CompositeSignalResponse with all fields populated.

        Raises:
            ValueError: If contract not found or no data available.
        """
        # Validate contract exists
        contract_result = await db.execute(
            select(Contract).where(
                Contract.symbol == contract_symbol.upper(),
                Contract.is_active.is_(True),
            )
        )
        contract = contract_result.scalar_one_or_none()
        if contract is None:
            raise ValueError(f"Contract '{contract_symbol}' not found or not active")

        contract_symbol = contract_symbol.upper()

        # Fetch individual signals
        positioning_score, ts_score, rp_score = await self._fetch_signals(
            contract_symbol=contract_symbol,
            db=db,
        )

        # Build active signals dict: only non-None signals contribute
        active_signals: dict[str, float] = {}
        if positioning_score is not None:
            active_signals["positioning"] = positioning_score
        if ts_score is not None:
            active_signals["term_structure"] = ts_score
        if rp_score is not None:
            active_signals["roll_pressure"] = rp_score

        if not active_signals:
            raise ValueError(
                f"No signal data available for '{contract_symbol}'. "
                "At least one signal (positioning, term structure, or roll pressure) is required."
            )

        # Renormalize weights for active signals
        active_weights = self._renormalize_weights(list(active_signals.keys()))

        # Compute composite score (weighted sum)
        composite = sum(
            active_signals[sig] * active_weights[sig]
            for sig in active_signals
        )

        # Round to 2 decimal places
        composite = round(composite, 2)

        # Determine alignment
        alignment = self._determine_alignment(
            positioning_score=positioning_score,
            term_structure_score=ts_score,
            roll_pressure_score=rp_score,
        )

        # Calculate confidence
        confidence = self._calculate_confidence(
            positioning_score=positioning_score,
            term_structure_score=ts_score,
            roll_pressure_score=rp_score,
        )

        # Build breakdown
        breakdown = self._build_breakdown(
            active_signals=active_signals,
            active_weights=active_weights,
            total_score=composite,
        )

        # Generate interpretation
        interpretation = self._generate_interpretation(
            composite_score=composite,
            alignment=alignment,
            confidence=confidence,
            positioning_score=positioning_score,
            term_structure_score=ts_score,
            roll_pressure_score=rp_score,
        )

        # Historical comparison — fetch from DB
        historical = await self._compute_historical_comparison(
            contract_symbol=contract_symbol,
            db=db,
            current_score=composite,
        )

        now = datetime.now(UTC)

        return CompositeSignalResponse(
            contract=contract_symbol,
            timestamp=now,
            positioning_score=positioning_score,
            term_structure_score=ts_score,
            roll_pressure_score=rp_score,
            composite_score=composite,
            signal_alignment=alignment,
            confidence=confidence,
            interpretation=interpretation,
            historical_comparison=historical,
            weights=active_weights,
            breakdown=breakdown,
        )

    # -----------------------------------------------------------------------
    # Signal fetching
    # -----------------------------------------------------------------------

    async def _fetch_signals(
        self,
        contract_symbol: str,
        db: AsyncSession,
    ) -> tuple[float | None, float | None, float | None]:
        """Fetch and score all three individual signals.

        Returns:
            Tuple of (positioning_score, term_structure_score, roll_pressure_score).
            Each is None if data is unavailable for that signal.
        """
        positioning_score: float | None = None
        ts_score: float | None = None
        rp_score: float | None = None

        # 1. Positioning signal
        try:
            positioning_resp = await compute_positioning_signal(
                contract_symbol=contract_symbol,
                db=db,
            )
            positioning_score = self._positioning_to_score(
                direction=positioning_resp.signal.signal.overall,
                strength=positioning_resp.signal.signal.strength,
            )
        except ValueError as e:
            logger.debug("positioning_unavailable", symbol=contract_symbol, error=str(e))

        # 2. Term structure
        try:
            ts_resp = await compute_term_structure(
                contract_symbol=contract_symbol,
                db=db,
            )
            cb = compute_contango_backwardation(ts_resp.months)
            ts_score = self._term_structure_to_score(
                structure_type=cb["structure_type"],
                confidence=cb["confidence"],
                z_score=cb["spread_z_score"],
                slope=cb["slope"],
            )
        except ValueError as e:
            logger.debug("term_structure_unavailable", symbol=contract_symbol, error=str(e))

        # 3. Roll pressure
        try:
            rp_resp = await compute_roll_pressure(
                contract_symbol=contract_symbol,
                db=db,
            )
            raw_index = rp_resp.roll_pressure.index
            rp_score = self._roll_pressure_to_score(
                raw_index=raw_index,
                contango_direction=ts_score,  # use term structure score for sign
            )
        except ValueError as e:
            logger.debug("roll_pressure_unavailable", symbol=contract_symbol, error=str(e))

        return positioning_score, ts_score, rp_score

    # -----------------------------------------------------------------------
    # Signal-to-score conversions
    # -----------------------------------------------------------------------

    @staticmethod
    def _positioning_to_score(
        direction: str,
        strength: float,
    ) -> float:
        """Convert positioning signal to -100 to +100 score.

        Bullish → positive score (strength * 100)
        Bearish → negative score (-strength * 100)
        Neutral → 0
        """
        if direction == "bullish":
            return round(strength * 100, 2)
        elif direction == "bearish":
            return round(-strength * 100, 2)
        return 0.0

    @staticmethod
    def _term_structure_to_score(
        structure_type: str,
        confidence: float,
        z_score: float,
        slope: float,
    ) -> float:
        """Convert term structure classification to -100 to +100 score.

        Contango (higher future prices) → bearish for spot → negative.
        Backwardation (lower future prices) → bullish for spot → positive.
        Flat → 0.
        Mixed → follows the dominant slope direction.

        Magnitude scales with z-score (capped at ±3) and confidence.
        """
        if structure_type == "contango":
            sign = -1.0
        elif structure_type == "backwardation":
            sign = 1.0
        elif structure_type == "mixed":
            # Follow the dominant nearby slope
            sign = -1.0 if slope < 0 else 1.0
        else:  # flat
            return 0.0

        # Magnitude from z-score capped at ±3, scaled to 0-1
        magnitude = min(abs(z_score) / 3.0, 1.0) if abs(z_score) > 0 else confidence

        # Boost by confidence when z-score is small
        magnitude = max(magnitude, confidence * 0.5)

        return round(sign * magnitude * 100, 2)

    @staticmethod
    def _roll_pressure_to_score(
        raw_index: float,
        contango_direction: float | None,
    ) -> float:
        """Convert roll pressure index to -100 to +100 score.

        The raw roll pressure index is 0-100 (magnitude only).
        Direction comes from the term structure:
        - In contango (negative ts direction), high roll pressure is
          bearish → negative score
        - In backwardation (positive ts direction), high roll pressure
          reflects market tightness → positive score
        - If term structure is unavailable, roll pressure is treated
          as absolute magnitude centered at 0.

        Args:
            raw_index: Roll pressure index (0-100).
            contango_direction: Term structure score (positive = backwardation,
                              negative = contango). None if unavailable.

        Returns:
            Score from -100 to +100.
        """
        if contango_direction is not None and contango_direction >= 0:
            # Backwardation / bullish term structure → positive
            return round(raw_index, 2)
        elif contango_direction is not None and contango_direction < 0:
            # Contango / bearish term structure → negative
            return round(-raw_index, 2)
        else:
            # No term structure context → center at 0
            return round(raw_index - 50, 2)

    # -----------------------------------------------------------------------
    # Weight renormalization
    # -----------------------------------------------------------------------

    def _renormalize_weights(self, active_signal_names: list[str]) -> dict[str, float]:
        """Renormalize weights to sum to 1 for the active signals.

        If a signal is missing, its weight is redistributed proportionally
        among the remaining signals.
        """
        if not active_signal_names:
            return {}

        if len(active_signal_names) == len(self.weights):
            return dict(self.weights)

        # Sum weights of active signals
        active_sum = sum(self.weights[sig] for sig in active_signal_names)
        if active_sum <= 0:
            # Fall back to equal weights
            equal = round(1.0 / len(active_signal_names), 4)
            return {sig: equal for sig in active_signal_names}

        return {
            sig: round(self.weights[sig] / active_sum, 4)
            for sig in active_signal_names
        }

    # -----------------------------------------------------------------------
    # Alignment calculation
    # -----------------------------------------------------------------------

    @staticmethod
    def _determine_alignment(
        positioning_score: float | None,
        term_structure_score: float | None,
        roll_pressure_score: float | None,
    ) -> SignalAlignment:
        """Determine how aligned the individual signals are.

        A signal is "bullish" if its score > +20, "bearish" if < -20,
        and "neutral" otherwise.

        Returns:
            ALIGNED_BULLISH — all non-neutral signals agree bullish
            ALIGNED_BEARISH — all non-neutral signals agree bearish
            MIXED — signals disagree
            NEUTRAL — all signals neutral or missing
        """
        scores = [s for s in (positioning_score, term_structure_score, roll_pressure_score) if s is not None]

        if not scores:
            return SignalAlignment.NEUTRAL

        bullish = sum(1 for s in scores if s > 20)
        bearish = sum(1 for s in scores if s < -20)
        neutral = sum(1 for s in scores if -20 <= s <= 20)

        if bullish > 0 and bearish > 0:
            return SignalAlignment.MIXED
        if bullish > 0 and neutral == 0:
            return SignalAlignment.ALIGNED_BULLISH
        if bearish > 0 and neutral == 0:
            return SignalAlignment.ALIGNED_BEARISH
        if bullish > 0:
            # Some bullish, rest neutral → still aligned bullish but weak
            return SignalAlignment.ALIGNED_BULLISH
        if bearish > 0:
            return SignalAlignment.ALIGNED_BEARISH
        return SignalAlignment.NEUTRAL

    # -----------------------------------------------------------------------
    # Confidence calculation
    # -----------------------------------------------------------------------

    @staticmethod
    def _calculate_confidence(
        positioning_score: float | None,
        term_structure_score: float | None,
        roll_pressure_score: float | None,
    ) -> float:
        """Calculate confidence based on signal agreement and availability.

        - Higher when signals agree (all same direction)
        - Lower when signals disagree
        - Bonus for having all 3 signals available
        - Extra bonus for extreme scores (strong conviction)

        Returns:
            Confidence value from 0.0 to 1.0.
        """
        scores = [s for s in (positioning_score, term_structure_score, roll_pressure_score) if s is not None]
        n_available = len(scores)

        if n_available == 0:
            return 0.0

        # Base: 0.3 for 1 signal, 0.5 for 2, 0.7 for 3
        base = {1: 0.30, 2: 0.50, 3: 0.70}[n_available]

        # Categorize each signal's direction
        directions: list[str] = []
        for s in scores:
            if s > 20:
                directions.append("bullish")
            elif s < -20:
                directions.append("bearish")
            else:
                directions.append("neutral")

        # Agreement bonus: +0.2 if all non-neutral signals agree
        non_neutral = [d for d in directions if d != "neutral"]
        if len(non_neutral) >= 2 and len(set(non_neutral)) == 1:
            base += 0.20
        elif len(non_neutral) == 0:
            # All neutral: moderate confidence
            base += 0.10
        elif "bullish" in non_neutral and "bearish" in non_neutral:
            # Mixed: -0.1 penalty
            base -= 0.10

        # Extreme score bonus: +0.1 if any signal > +80 or < -80
        for s in scores:
            if abs(s) >= 80:
                base += 0.10
                break

        return round(min(max(base, 0.0), 1.0), 4)

    # -----------------------------------------------------------------------
    # Breakdown
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_breakdown(
        active_signals: dict[str, float],
        active_weights: dict[str, float],
        total_score: float,
    ) -> list[SignalBreakdownItem]:
        """Build per-signal contribution breakdown.

        Each item shows the signal's score, weight, absolute contribution,
        and percentage of the total absolute contribution.
        """
        total_abs = sum(abs(s) for s in active_signals.values()) or 1.0

        items: list[SignalBreakdownItem] = []
        for sig_type in ("positioning", "term_structure", "roll_pressure"):
            if sig_type in active_signals:
                score = active_signals[sig_type]
                weight = active_weights[sig_type]
                contribution = round(score * weight, 2)
                contribution_pct = round(
                    (abs(score) / total_abs) * 100.0 * (1 if score >= 0 else -1),
                    2,
                )
                items.append(
                    SignalBreakdownItem(
                        signal_type=sig_type,
                        raw_value=score,
                        score=score,
                        weight=weight,
                        contribution=contribution,
                        contribution_pct=contribution_pct,
                    )
                )

        return items

    # -----------------------------------------------------------------------
    # Interpretation text
    # -----------------------------------------------------------------------

    @staticmethod
    def _generate_interpretation(
        composite_score: float,
        alignment: SignalAlignment,
        confidence: float,
        positioning_score: float | None,
        term_structure_score: float | None,
        roll_pressure_score: float | None,
    ) -> str:
        """Generate human-readable interpretation text.

        Explains what the composite score means in plain English,
        referencing which signals are driving the score.
        """
        # Determine overall bias
        if composite_score > 60:
            bias = "strongly bullish"
            intensity = "high conviction"
        elif composite_score > 20:
            bias = "moderately bullish"
            intensity = "moderate conviction"
        elif composite_score < -60:
            bias = "strongly bearish"
            intensity = "high conviction"
        elif composite_score < -20:
            bias = "moderately bearish"
            intensity = "moderate conviction"
        else:
            bias = "neutral"
            intensity = "low conviction"

        parts: list[str] = []
        parts.append(f"Market structure is **{bias}** ({composite_score:+.1f}) with {intensity}.")

        # Add alignment context
        if alignment == SignalAlignment.ALIGNED_BULLISH:
            parts.append("All signals align bullish — positioning, term structure, and roll pressure agree on upward bias.")
        elif alignment == SignalAlignment.ALIGNED_BEARISH:
            parts.append("All signals align bearish — positioning, term structure, and roll pressure agree on downward bias.")
        elif alignment == SignalAlignment.MIXED:
            parts.append("Signals are mixed, suggesting conflicting market structure forces at play.")
        elif alignment == SignalAlignment.NEUTRAL:
            parts.append("Signals are predominantly neutral with no strong directional bias.")

        # Add individual signal context
        signal_details: list[str] = []
        if positioning_score is not None:
            if positioning_score > 40:
                signal_details.append("positioning is strongly bullish (smart money vs retail divergence)")
            elif positioning_score > 10:
                signal_details.append("positioning is mildly bullish")
            elif positioning_score < -40:
                signal_details.append("positioning is strongly bearish")
            elif positioning_score < -10:
                signal_details.append("positioning is mildly bearish")
            else:
                signal_details.append("positioning is neutral")
        if term_structure_score is not None:
            if term_structure_score > 40:
                signal_details.append("term structure is strongly backwardated (bullish)")
            elif term_structure_score > 10:
                signal_details.append("term structure is mildly backwardated")
            elif term_structure_score < -40:
                signal_details.append("term structure is strongly contangoed (bearish)")
            elif term_structure_score < -10:
                signal_details.append("term structure is mildly contangoed")
            else:
                signal_details.append("term structure is flat")
        if roll_pressure_score is not None:
            if roll_pressure_score > 40:
                signal_details.append("roll pressure is elevated (bullish context)")
            elif roll_pressure_score < -40:
                signal_details.append("roll pressure is elevated (bearish context)")
            elif abs(roll_pressure_score) < 10:
                signal_details.append("roll pressure is low")
            else:
                direction = "supportive" if roll_pressure_score > 0 else "restrictive"
                signal_details.append(f"roll pressure is {direction}")

        if signal_details:
            parts.append("Drivers: " + "; ".join(signal_details) + ".")

        return " ".join(parts)

    # -----------------------------------------------------------------------
    # Historical comparison
    # -----------------------------------------------------------------------

    async def _compute_historical_comparison(
        self,
        contract_symbol: str,
        db: AsyncSession,
        current_score: float,
        lookback_days: int = 30,
    ) -> HistoricalComparison | None:
        """Compute historical statistics for the composite score.

        Queries settlement data for the last `lookback_days` days and
        computes a simplified composite score for each day with enough data.

        Args:
            contract_symbol: Root symbol.
            db: Async database session.
            current_score: Current composite score to compare against.
            lookback_days: Number of days to look back.

        Returns:
            HistoricalComparison or None if insufficient historical data.
        """
        # Look up contract
        contract_result = await db.execute(
            select(Contract).where(
                Contract.symbol == contract_symbol,
                Contract.is_active.is_(True),
            )
        )
        contract = contract_result.scalar_one_or_none()
        if contract is None:
            return None

        # Get distinct settlement dates — prefer using latest available date
        # as reference rather than date.today() for test reproducibility
        max_date_result = await db.execute(
            select(sa_func.max(RawSettlement.settlement_date))
            .where(RawSettlement.contract_id == contract.id)
        )
        max_date_row = max_date_result.scalar_one_or_none()
        if max_date_row is None:
            return None

        # Normalize max_date_row to date (handles datetime, date, and string from SQLite)
        if isinstance(max_date_row, str):
            # SQLite returns dates as strings like '2026-05-13 00:00:00.000000'
            max_date = datetime.fromisoformat(max_date_row.split(" ")[0].split("T")[0]).date()
        elif isinstance(max_date_row, datetime):
            max_date = max_date_row.date()
        else:
            max_date = max_date_row

        # Use the latest date or today, whichever is earlier
        reference_date = date.today()
        if max_date < reference_date:
            reference_date = max_date

        cutoff = reference_date - timedelta(days=lookback_days)
        dates_result = await db.execute(
            select(sa_func.distinct(RawSettlement.settlement_date))
            .where(RawSettlement.contract_id == contract.id)
            .where(RawSettlement.settlement_date >= cutoff)
            .where(RawSettlement.settlement_date <= max_date_row)
            .order_by(RawSettlement.settlement_date.asc())
        )
        raw_dates = [row[0] for row in dates_result.all()]

        if not raw_dates:
            return None

        # Normalize each date (handles datetime, date, and string from SQLite)
        hist_dates: list[date] = []
        for d in raw_dates:
            if isinstance(d, str):
                hist_dates.append(datetime.fromisoformat(d.split(" ")[0].split("T")[0]).date())
            elif isinstance(d, datetime):
                hist_dates.append(d.date())
            else:
                hist_dates.append(d)

        if len(hist_dates) < 3:
            # Not enough historical data for meaningful comparison
            return None

        if len(hist_dates) < 3:
            # Not enough historical data for meaningful comparison
            return None

        # For each historical date, compute a simplified composite score
        historical_scores: list[float] = []
        for hist_date in hist_dates:
            score = await self._compute_simplified_composite(
                contract_id=contract.id,
                db=db,
                as_of_date=hist_date,
            )
            if score is not None:
                historical_scores.append(score)

        if len(historical_scores) < 3:
            return None

        # Compute statistics
        hist_avg = sum(historical_scores) / len(historical_scores)
        hist_min = min(historical_scores)
        hist_max = max(historical_scores)
        hist_pct = percentile_rank(current_score, historical_scores)

        return HistoricalComparison(
            current=current_score,
            average=round(hist_avg, 2),
            min=round(hist_min, 2),
            max=round(hist_max, 2),
            percentile_rank=round(hist_pct, 2),
            values=historical_scores,
        )

    async def _compute_simplified_composite(
        self,
        contract_id: int,
        db: AsyncSession,
        as_of_date: date,
    ) -> float | None:
        """Compute a simplified composite score for a single historical date.

        Uses settlement data directly to compute term structure and
        roll pressure proxy scores. Does NOT fetch COT positioning data
        (weekly data would be too sparse for daily history).

        Args:
            contract_id: Contract internal ID.
            db: Async database session.
            as_of_date: The date to compute for.

        Returns:
            Simplified composite score, or None if insufficient data.
        """
        # Get settlements for this date
        as_of_dt = datetime.combine(as_of_date, datetime.min.time())
        result = await db.execute(
            select(RawSettlement)
            .where(RawSettlement.contract_id == contract_id)
            .where(RawSettlement.settlement_date == as_of_dt)
            .order_by(RawSettlement.month_code.asc())
        )
        settlements = list(result.scalars().all())

        if len(settlements) < 2:
            return None

        # Sort by month code for term structure
        settlements.sort(key=lambda s: s.month_code)

        front = settlements[0]
        deferred = settlements[1]

        # Simplified term structure score
        spread = deferred.settlement_price - front.settlement_price
        spread_pct = abs(spread) / front.settlement_price if front.settlement_price > 0 else 0.0

        if spread_pct < 0.002:
            ts_score = 0.0  # flat
        elif spread > 0:
            ts_score = -min(spread_pct * 5000, 100.0)  # contango → negative
        else:
            ts_score = min(abs(spread_pct) * 5000, 100.0)  # backwardation → positive

        # Simplified roll pressure proxy
        total_oi = front.open_interest + deferred.open_interest
        oi_concentration = (front.open_interest / total_oi * 100) if total_oi > 0 else 50.0
        # Higher OI concentration = more roll pressure
        rp_raw = oi_concentration
        rp_score = self._roll_pressure_to_score(raw_index=rp_raw, contango_direction=ts_score)

        # Use default weights (positioning not available for historical)
        signal_w = self.weights
        active_sum = signal_w["term_structure"] + signal_w["roll_pressure"]
        if active_sum <= 0:
            return None

        # Renormalize (exclude positioning)
        ts_weight = signal_w["term_structure"] / active_sum
        rp_weight = signal_w["roll_pressure"] / active_sum

        composite = ts_score * ts_weight + rp_score * rp_weight
        return round(composite, 2)
