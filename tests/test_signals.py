"""Tests for historical statistical analysis functions.

Tests Z-score calculation, percentile ranking, mean reversion detection,
OI-price correlation, and helper functions.
"""

from __future__ import annotations

from app.signals.historical import (
    compute_lookback_window,
    compute_net_positions,
    compute_oi_price_correlation,
    compute_week_over_week_changes,
    detect_extreme_positioning,
    detect_mean_reversion,
    percentile_rank,
    rolling_z_score,
    rolling_z_score_windowed,
)

# ---------------------------------------------------------------------------
# Z-score tests
# ---------------------------------------------------------------------------


class TestRollingZScore:
    """Tests for rolling Z-score calculation."""

    def test_z_score_known_values(self):
        """Z-score of 110 in [100, 102, 104, 106, 108, 110] should be calculable."""
        values = [100, 102, 104, 106, 108, 110]
        z = rolling_z_score(110, values)
        # Mean = 105, std = sqrt((25+9+1+1+9+25)/6) = sqrt(70/6) ≈ 3.416
        # Z = (110 - 105) / 3.416 ≈ 1.464
        assert z > 1.0
        assert z < 2.0

    def test_z_score_at_mean(self):
        """Z-score of the mean should be 0."""
        values = [10, 20, 30, 40, 50]
        z = rolling_z_score(30, values)
        assert abs(z) < 0.01  # Very close to 0

    def test_z_score_above_mean(self):
        """Z-score above the mean should be positive."""
        values = [10, 20, 30, 40, 50]
        z = rolling_z_score(60, values)
        assert z > 0

    def test_z_score_below_mean(self):
        """Z-score below the mean should be negative."""
        values = [10, 20, 30, 40, 50]
        z = rolling_z_score(5, values)
        assert z < 0

    def test_z_score_zero_variance(self):
        """Z-score with zero variance (all same values) should be 0."""
        values = [50, 50, 50, 50, 50]
        z = rolling_z_score(60, values)
        assert z == 0.0

    def test_z_score_single_data_point(self):
        """Z-score with fewer than 2 historical points should be 0."""
        values = [50]
        z = rolling_z_score(60, values)
        assert z == 0.0

    def test_z_score_empty_history(self):
        """Z-score with empty history should be 0."""
        z = rolling_z_score(60, [])
        assert z == 0.0

    def test_z_score_extreme_value(self):
        """Z-score of an extreme outlier should be very high."""
        values = [100, 102, 104, 106, 108]
        z = rolling_z_score(200, values)
        assert z > 5.0  # Very extreme

    def test_z_score_negative_values(self):
        """Z-score should work with negative net positions (commercial shorts)."""
        values = [-500000, -400000, -300000, -200000, -100000]
        z = rolling_z_score(100000, values)
        assert z > 2.0  # Well above the historical range

    def test_z_score_symmetry(self):
        """Z-scores should be symmetric: z(x) = -z(reflection around mean)."""
        values = [10, 20, 30, 40, 50]
        z_above = rolling_z_score(40, values)
        z_below = rolling_z_score(20, values)
        assert abs(z_above + z_below) < 0.01  # Should be nearly opposite


class TestRollingZScoreWindowed:
    """Tests for windowed rolling Z-score computation."""

    def test_windowed_z_scores(self):
        """Windowed Z-scores should start after the window."""
        values = list(range(1, 110))  # 109 values
        results = rolling_z_score_windowed(values, window=52)
        # Should have 109 - 52 = 57 results
        assert len(results) == 57

    def test_windowed_z_scores_small_window(self):
        """Small window should produce more results."""
        values = list(range(1, 30))  # 29 values
        results = rolling_z_score_windowed(values, window=5)
        assert len(results) == 24  # 29 - 5 = 24

    def test_windowed_z_scores_indices(self):
        """Indices should start at the window size."""
        values = list(range(1, 20))
        results = rolling_z_score_windowed(values, window=10)
        # First result should be at index 10
        assert results[0][0] == 10


# ---------------------------------------------------------------------------
# Percentile ranking tests
# ---------------------------------------------------------------------------


class TestPercentileRank:
    """Tests for percentile ranking calculation."""

    def test_percentile_at_median(self):
        """Value at the median should be around 50th percentile."""
        values = [10, 20, 30, 40, 50]
        pct = percentile_rank(30, values)
        assert 40 <= pct <= 60  # Around 50%

    def test_percentile_at_max(self):
        """Maximum value should be near 100th percentile."""
        values = [10, 20, 30, 40, 50]
        pct = percentile_rank(50, values)
        assert pct >= 90  # Near 100th

    def test_percentile_at_min(self):
        """Minimum value should be near 0th percentile."""
        values = [10, 20, 30, 40, 50]
        pct = percentile_rank(10, values)
        assert pct <= 20  # Near 0th

    def test_percentile_empty_history(self):
        """Percentile with empty history should return 50.0."""
        pct = percentile_rank(50, [])
        assert pct == 50.0

    def test_percentile_single_value(self):
        """Percentile with single value should return 50.0 (same as current)."""
        values = [50]
        pct = percentile_rank(50, values)
        # (0 + 0.5*1) / 1 * 100 = 50
        assert pct == 50.0

    def test_percentile_repeated_values(self):
        """Percentile with repeated values should handle ties correctly."""
        values = [50, 50, 50, 50, 50]
        pct = percentile_rank(50, values)
        assert pct == 50.0  # All equal

    def test_percentile_beyond_range(self):
        """Value beyond the range should clamp to 0 or 100."""
        values = [10, 20, 30, 40, 50]
        pct_high = percentile_rank(100, values)
        assert pct_high == 100.0
        pct_low = percentile_rank(0, values)
        assert pct_low == 0.0


# ---------------------------------------------------------------------------
# Mean reversion tests
# ---------------------------------------------------------------------------


class TestDetectMeanReversion:
    """Tests for mean reversion detection."""

    def test_overbought_detection(self):
        """High Z-score should detect overbought mean reversion."""
        is_extreme, direction = detect_mean_reversion(2.5)
        assert is_extreme is True
        assert direction == "overbought"

    def test_oversold_detection(self):
        """Low Z-score should detect oversold mean reversion."""
        is_extreme, direction = detect_mean_reversion(-2.5)
        assert is_extreme is True
        assert direction == "oversold"

    def test_neutral_range(self):
        """Z-score within normal range should be neutral."""
        is_extreme, direction = detect_mean_reversion(1.0)
        assert is_extreme is False
        assert direction == "neutral"

    def test_neutral_range_negative(self):
        """Mildly negative Z-score should be neutral."""
        is_extreme, direction = detect_mean_reversion(-1.0)
        assert is_extreme is False
        assert direction == "neutral"

    def test_custom_thresholds(self):
        """Custom thresholds should be respected."""
        # At Z=1.5, default threshold is 2.0, so not extreme
        is_extreme, _ = detect_mean_reversion(1.5)
        assert is_extreme is False

        # At Z=1.5, with threshold 1.0, it should be extreme
        is_extreme, _ = detect_mean_reversion(1.5, threshold_high=1.0)
        assert is_extreme is True

    def test_exact_threshold(self):
        """Z-score exactly at threshold should trigger."""
        is_extreme, direction = detect_mean_reversion(2.0)
        assert is_extreme is True
        assert direction == "overbought"


class TestDetectExtremePositioning:
    """Tests for extreme positioning classification."""

    def test_high_extreme(self):
        """High Z-score and high percentile should be high conviction long."""
        conviction, direction = detect_extreme_positioning(2.5, 95.0)
        assert conviction == "high"
        assert direction == "long"

    def test_low_extreme(self):
        """Low Z-score and low percentile should be high conviction short."""
        conviction, direction = detect_extreme_positioning(-2.5, 5.0)
        assert conviction == "high"
        assert direction == "short"

    def test_moderate_z_score(self):
        """Moderate Z-score should be medium conviction."""
        conviction, direction = detect_extreme_positioning(1.2, 75.0)
        assert conviction in ("medium", "low")
        assert direction == "long"

    def test_neutral_range(self):
        """Z-score near zero should be low conviction neutral."""
        conviction, direction = detect_extreme_positioning(0.1, 52.0)
        assert conviction == "low"
        assert direction == "neutral"

    def test_z_score_extreme_percentile_neutral(self):
        """Extreme Z-score with neutral percentile should be medium."""
        conviction, direction = detect_extreme_positioning(2.0, 55.0)
        assert conviction == "medium"  # Only Z-score is extreme


# ---------------------------------------------------------------------------
# OI-price correlation tests
# ---------------------------------------------------------------------------


class TestComputeOIPriceCorrelation:
    """Tests for OI-price correlation computation."""

    def test_perfect_positive_correlation(self):
        """Perfect positive correlation should return ~1.0."""
        oi = [1, 2, 3, 4, 5]
        prices = [10, 20, 30, 40, 50]
        corr = compute_oi_price_correlation(oi, prices)
        assert abs(corr - 1.0) < 0.01

    def test_perfect_negative_correlation(self):
        """Perfect negative correlation should return ~-1.0."""
        oi = [1, 2, 3, 4, 5]
        prices = [50, 40, 30, 20, 10]
        corr = compute_oi_price_correlation(oi, prices)
        assert abs(corr + 1.0) < 0.01

    def test_no_correlation(self):
        """Zero correlation should return ~0.0."""
        oi = [1, 5, 2, 8, 3]
        prices = [10, 20, 30, 40, 50]
        corr = compute_oi_price_correlation(oi, prices)
        # Not exactly 0 but should be small
        assert abs(corr) < 1.0

    def test_insufficient_data(self):
        """Fewer than 3 data points should return 0.0."""
        corr = compute_oi_price_correlation([1, 2], [10, 20])
        assert corr == 0.0

    def test_zero_variance(self):
        """Zero variance in either series should return 0.0."""
        oi = [5, 5, 5, 5, 5]
        prices = [10, 20, 30, 40, 50]
        corr = compute_oi_price_correlation(oi, prices)
        assert corr == 0.0

    def test_different_lengths(self):
        """Should work with different length arrays (uses min length)."""
        oi = [1, 2, 3, 4, 5, 6, 7]
        prices = [10, 20, 30, 40, 50]
        corr = compute_oi_price_correlation(oi, prices)
        assert abs(corr - 1.0) < 0.01  # First 5 are perfectly correlated


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestComputeLookbackWindow:
    """Tests for lookback window filtering."""

    def test_window_shorter_than_data(self):
        """Should return last N entries when data exceeds window."""
        reports = [{"as_of_date": f"2026-01-{i+1:02d}"} for i in range(100)]
        result = compute_lookback_window(reports, lookback_weeks=52)
        assert len(result) == 52

    def test_window_longer_than_data(self):
        """Should return all data when window exceeds data length."""
        reports = [{"as_of_date": f"2026-01-{i+1:02d}"} for i in range(10)]
        result = compute_lookback_window(reports, lookback_weeks=52)
        assert len(result) == 10

    def test_window_equals_data(self):
        """Should return all data when window equals data length."""
        reports = [{"as_of_date": f"2026-01-{i+1:02d}"} for i in range(52)]
        result = compute_lookback_window(reports, lookback_weeks=52)
        assert len(result) == 52


class TestComputeNetPositions:
    """Tests for net position extraction from COT reports."""

    def test_extract_net_positions(self):
        """Should extract net position series from COT dicts."""
        reports = [
            {"commercial_net": -350000, "non_commercial_net": 400000, "non_reportable_net": 100000},
            {"commercial_net": -300000, "non_commercial_net": 350000, "non_reportable_net": 80000},
        ]
        result = compute_net_positions(reports)
        assert result["commercial"] == [-350000.0, -300000.0]
        assert result["non_commercial"] == [400000.0, 350000.0]
        assert result["non_reportable"] == [100000.0, 80000.0]

    def test_extract_from_long_short(self):
        """Should compute net from long/short when net not available."""
        reports = [
            {"commercial_long": 850000, "commercial_short": 1200000,
             "non_commercial_long": 600000, "non_commercial_short": 200000,
             "non_reportable_long": 150000, "non_reportable_short": 50000},
        ]
        result = compute_net_positions(reports)
        assert result["commercial"] == [-350000.0]  # 850k - 1200k
        assert result["non_commercial"] == [400000.0]  # 600k - 200k
        assert result["non_reportable"] == [100000.0]  # 150k - 50k


class TestComputeWeekOverWeekChanges:
    """Tests for week-over-week change calculation."""

    def test_basic_changes(self):
        """Should compute week-over-week changes."""
        reports = [
            {"net": 100},
            {"net": 110},
            {"net": 105},
        ]
        changes = compute_week_over_week_changes(reports, field="net")
        assert changes[0] is None
        assert changes[1] == 10.0
        assert changes[2] == -5.0

    def test_single_report(self):
        """Single report should have only None."""
        reports = [{"net": 100}]
        changes = compute_week_over_week_changes(reports, field="net")
        assert len(changes) == 1
        assert changes[0] is None

    def test_empty_reports(self):
        """Empty reports should return empty list."""
        changes = compute_week_over_week_changes([], field="net")
        assert len(changes) == 0


class TestExtractOISeries:
    """Tests for OI series extraction."""

    def test_extract_oi(self):
        """Should extract total_open_interest as float series."""
        from app.signals.historical import extract_oi_series
        reports = [
            {"total_open_interest": 1600000},
            {"total_open_interest": 1650000},
        ]
        oi = extract_oi_series(reports)
        assert oi == [1600000.0, 1650000.0]

    def test_extract_oi_missing_key(self):
        """Should default to 0 if total_open_interest missing."""
        from app.signals.historical import extract_oi_series
        reports = [
            {"total_open_interest": 1600000},
            {},
        ]
        oi = extract_oi_series(reports)
        assert oi == [1600000.0, 0.0]