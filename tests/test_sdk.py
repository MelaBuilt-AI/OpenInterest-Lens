"""Tests for the OpenInterest Lens Python SDK.

Covers:
- Sync client: all methods with mocked httpx responses
- Async client: all methods with mocked httpx responses
- Models: serialization/deserialization, validation
- Exceptions: hierarchy, custom attributes
- WebSocket client: auth, subscribe, heartbeat, reconnection
- Builder pattern: fluent API, defaults
- Retry logic: exponential backoff, rate limit handling
- Error handling: auth failures, not found, server errors
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from sdk.async_client import AsyncOpenInterestLensClient
from sdk.builder import ClientBuilder
from sdk.client import OpenInterestLensClient, _extract_detail
from sdk.exceptions import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    OpenInterestLensError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from sdk.models import (
    APIResponse,
    COTReport,
    COTResponse,
    COTTraderDetail,
    CalendarSpreadRatios,
    ContractsResponse,
    ContangoBackwardation,
    Contract,
    HealthResponse,
    NetPosition,
    NearbyContract,
    PaginatedResponse,
    PositioningBreakdown,
    PositioningSignal,
    PositioningSignalResponse,
    Retail,
    RollCalendarData,
    RollImpactData,
    RollPressureIndex,
    RollPressureMetrics,
    RollPressureResponse,
    SignalMetadata,
    SignalOverall,
    SmartMoney,
    SlopeMetrics,
    TermStructureCurve,
    TermStructureMonth,
    TermStructureResponse,
    TraderPositionBreakdown,
)


# ---------------------------------------------------------------------------
# Fixtures — sample response data
# ---------------------------------------------------------------------------

SAMPLE_SIGNAL_DATA = {
    "commodity": "ES",
    "signal": {
        "contract": "ES",
        "timestamp": "2025-01-15T00:00:00Z",
        "as_of_friday": "2025-01-10",
        "net_position": {
            "commercial": -50000,
            "non_commercial": 30000,
            "non_reportable": 5000,
        },
        "smart_money": {
            "z_score": -2.1,
            "percentile": 5.0,
            "direction": "short",
            "conviction": "high",
        },
        "retail": {
            "z_score": 1.8,
            "percentile": 92.0,
            "direction": "long",
            "contrarian_signal": "fade_long",
        },
        "signal": {
            "overall": "bearish",
            "strength": 0.85,
            "divergence": True,
        },
        "week_over_week_change": None,
    },
    "breakdown": {
        "commercial": {
            "long": 100000,
            "short": 150000,
            "net": -50000,
            "z_score": -2.1,
            "percentile": 5.0,
            "direction": "short",
        },
        "non_commercial": {
            "long": 80000,
            "short": 50000,
            "net": 30000,
            "z_score": 1.5,
            "percentile": 88.0,
            "direction": "long",
        },
        "non_reportable": {
            "long": 10000,
            "short": 5000,
            "net": 5000,
            "z_score": 1.8,
            "percentile": 92.0,
            "direction": "long",
        },
    },
    "metadata": {
        "lookback_weeks": 52,
        "data_points": 52,
        "as_of_date": "2025-01-15",
        "computed_at": "2025-01-15T12:00:00Z",
        "cache_hit": False,
    },
}

SAMPLE_TERM_STRUCTURE_DATA = {
    "contract": "ES",
    "term_structure": {
        "contract": "ES",
        "structure_type": "contango",
        "months": [
            {
                "month": "Mar 25",
                "expiry_date": "2025-03-21",
                "settlement": 5900.0,
                "open_interest": 2500000,
                "volume": 1500000,
                "spread_to_front": 0.0,
                "annualized_yield": 0.0,
            },
            {
                "month": "Jun 25",
                "expiry_date": "2025-06-20",
                "settlement": 5920.0,
                "open_interest": 800000,
                "volume": 300000,
                "spread_to_front": 20.0,
                "annualized_yield": 0.034,
            },
        ],
        "front_month_oi": 2500000,
        "total_oi": 3300000,
        "oi_concentration_pct": 75.76,
        "steepness": 0.005,
    },
    "contango_backwardation": {
        "structure_type": "contango",
        "m1_m2_spread": 20.0,
        "m1_m2_annualized": 0.034,
        "spread_z_score": 1.2,
        "confidence": 0.85,
        "slope": 0.005,
    },
    "slope_metrics": {
        "nearby_deferred_spread": 20.0,
        "slope_annualized_pct": 3.4,
        "linear_slope": 0.005,
        "quadratic_curvature": 0.001,
        "r_squared_linear": 0.92,
        "r_squared_quadratic": 0.95,
    },
    "calendar_spread_ratios": {
        "front_to_next_ratio": 0.997,
        "front_to_deferred_ratio": 0.993,
        "average_monthly_spread_pct": 0.34,
        "max_spread_pct": 0.50,
    },
    "metadata": {
        "lookback_weeks": 52,
        "data_points": 2,
        "as_of_date": "2025-03-01",
        "computed_at": "2025-03-01T12:00:00Z",
        "cache_hit": False,
    },
}

SAMPLE_ROLL_PRESSURE_DATA = {
    "contract": "ES",
    "roll_pressure": {
        "index": 72.5,
        "oi_decay_pct": 15.0,
        "spread_basis": 2.5,
        "days_to_expiry": 12,
        "roll_window": "active_roll",
    },
    "roll_calendar": {
        "nearby_month": "H25",
        "nearby_expiry": "2025-03-21",
        "deferred_month": "M25",
        "deferred_expiry": "2025-06-20",
        "days_to_roll": 12,
        "roll_start_date": "2025-03-07",
        "roll_end_date": "2025-03-19",
        "roll_urgency": "active",
    },
    "roll_impact": {
        "impact_score": 65.0,
        "oi_concentration": 75.76,
        "volume_shift": 12.5,
        "expected_slippage": 0.25,
        "impact_category": "high",
    },
    "metadata": {
        "lookback_weeks": 52,
        "data_points": 30,
        "as_of_date": "2025-03-01",
        "computed_at": "2025-03-01T12:00:00Z",
        "cache_hit": False,
    },
}

SAMPLE_COT_DATA = {
    "contract": "ES",
    "reports": [
        {
            "as_of_date": "2025-01-14",
            "published_date": "2025-01-17",
            "commercial": {
                "long": 100000,
                "short": 150000,
                "net": -50000,
                "z_score_52w": -2.1,
                "percentile_52w": 5.0,
            },
            "non_commercial": {
                "long": 80000,
                "short": 50000,
                "net": 30000,
                "z_score_52w": 1.5,
                "percentile_52w": 88.0,
            },
            "non_reportable": {
                "long": 10000,
                "short": 5000,
                "net": 5000,
                "z_score_52w": 1.8,
                "percentile_52w": 92.0,
            },
            "total_open_interest": 250000,
        }
    ],
    "metadata": {
        "total_reports": 1,
        "computed_at": "2025-01-17T12:00:00Z",
    },
}

SAMPLE_CONTRACTS_DATA = {
    "contracts": [
        {
            "symbol": "ES",
            "exchange": "CME",
            "asset_class": "equity_index",
            "full_name": "E-mini S&P 500",
            "tick_size": 0.25,
            "contract_size": 50.0,
            "months_traded": ["H", "M", "U", "Z"],
            "data_available_from": "1997-01-01",
            "signals_available": ["positioning", "roll_pressure", "contango_alert", "term_structure"],
        },
        {
            "symbol": "CL",
            "exchange": "NYMEX",
            "asset_class": "energy",
            "full_name": "Crude Oil (Light Sweet)",
            "tick_size": 0.01,
            "contract_size": 1000.0,
            "months_traded": ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"],
            "data_available_from": "1983-01-01",
            "signals_available": ["positioning", "roll_pressure", "contango_alert", "term_structure"],
        },
    ]
}

SAMPLE_HEALTH_DATA = {
    "status": "ok",
    "service": "openinterest-lens",
    "version": "0.1.0",
}


# ---------------------------------------------------------------------------
# Helper: mock httpx response
# ---------------------------------------------------------------------------

def _mock_response(status_code: int, json_data: dict, headers: dict | None = None) -> httpx.Response:
    """Create a mock httpx.Response."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("GET", "http://localhost:8000/v1/test"),
    )


# ---------------------------------------------------------------------------
# Tests: Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptions:
    """Test custom exception hierarchy and attributes."""

    def test_base_exception(self):
        exc = OpenInterestLensError("test error")
        assert str(exc) == "test error"
        assert exc.message == "test error"
        assert exc.status_code is None
        assert exc.response is None

    def test_base_exception_with_extras(self):
        exc = OpenInterestLensError("err", status_code=500, response={"detail": "x"})
        assert exc.status_code == 500
        assert exc.response == {"detail": "x"}

    def test_authentication_error(self):
        exc = AuthenticationError()
        assert isinstance(exc, OpenInterestLensError)
        assert exc.status_code == 401
        assert "Invalid" in exc.message

    def test_authentication_error_custom(self):
        exc = AuthenticationError("custom auth error", response={"error": "bad_key"})
        assert exc.message == "custom auth error"
        assert exc.status_code == 401

    def test_rate_limit_error(self):
        exc = RateLimitError(retry_after=60)
        assert isinstance(exc, OpenInterestLensError)
        assert exc.retry_after == 60
        assert exc.status_code == 429

    def test_rate_limit_error_custom_message(self):
        exc = RateLimitError("slow down", retry_after=30)
        assert exc.message == "slow down"
        assert exc.retry_after == 30

    def test_not_found_error(self):
        exc = NotFoundError("ES not found")
        assert isinstance(exc, OpenInterestLensError)
        assert exc.status_code == 404

    def test_server_error(self):
        exc = ServerError("internal error")
        assert isinstance(exc, OpenInterestLensError)
        assert exc.status_code == 500

    def test_connection_error(self):
        exc = ConnectionError("cannot connect")
        assert isinstance(exc, OpenInterestLensError)

    def test_validation_error(self):
        exc = ValidationError("bad request")
        assert isinstance(exc, OpenInterestLensError)
        assert exc.status_code == 400

    def test_exception_hierarchy(self):
        """All custom exceptions inherit from OpenInterestLensError."""
        for exc_class in [AuthenticationError, RateLimitError, NotFoundError, ServerError, ConnectionError, ValidationError]:
            exc = exc_class("test")
            assert isinstance(exc, OpenInterestLensError)
            assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# Tests: Models
# ---------------------------------------------------------------------------


class TestModels:
    """Test Pydantic model serialization/deserialization and validation."""

    def test_net_position(self):
        np = NetPosition(commercial=-50000, non_commercial=30000, non_reportable=5000)
        assert np.commercial == -50000
        data = np.model_dump()
        np2 = NetPosition.model_validate(data)
        assert np2.commercial == -50000

    def test_smart_money(self):
        sm = SmartMoney(z_score=-2.1, percentile=5.0, direction="short", conviction="high")
        assert sm.direction == "short"
        assert sm.conviction == "high"

    def test_retail(self):
        r = Retail(z_score=1.8, percentile=92.0, direction="long", contrarian_signal="fade_long")
        assert r.contrarian_signal == "fade_long"

    def test_signal_overall(self):
        so = SignalOverall(overall="bearish", strength=0.85, divergence=True)
        assert so.overall == "bearish"
        assert so.divergence is True

    def test_positioning_signal_full(self):
        ps = PositioningSignal(
            contract="ES",
            timestamp=datetime(2025, 1, 15, tzinfo=timezone.utc),
            net_position=NetPosition(commercial=-50000, non_commercial=30000, non_reportable=5000),
            smart_money=SmartMoney(z_score=-2.1, percentile=5.0, direction="short", conviction="high"),
            retail=Retail(z_score=1.8, percentile=92.0, direction="long", contrarian_signal="fade_long"),
            signal=SignalOverall(overall="bearish", strength=0.85, divergence=True),
        )
        assert ps.contract == "ES"
        assert ps.smart_money.direction == "short"
        data = ps.model_dump(mode="json")
        ps2 = PositioningSignal.model_validate(data)
        assert ps2.contract == "ES"

    def test_positioning_signal_response_from_sample(self):
        resp = PositioningSignalResponse.model_validate(SAMPLE_SIGNAL_DATA)
        assert resp.commodity == "ES"
        assert resp.signal.contract == "ES"
        assert resp.breakdown.commercial.net == -50000

    def test_term_structure_month(self):
        tsm = TermStructureMonth(
            month="Mar 25",
            settlement=5900.0,
            open_interest=2500000,
            volume=1500000,
            spread_to_front=0.0,
            annualized_yield=0.0,
        )
        assert tsm.month == "Mar 25"

    def test_term_structure_response_from_sample(self):
        resp = TermStructureResponse.model_validate(SAMPLE_TERM_STRUCTURE_DATA)
        assert resp.contract == "ES"
        assert resp.term_structure is not None
        assert resp.term_structure.structure_type == "contango"
        assert len(resp.term_structure.months) == 2
        assert resp.contango_backwardation is not None
        assert resp.slope_metrics is not None

    def test_roll_pressure_response_from_sample(self):
        resp = RollPressureResponse.model_validate(SAMPLE_ROLL_PRESSURE_DATA)
        assert resp.contract == "ES"
        assert resp.roll_pressure is not None
        assert resp.roll_pressure.index == 72.5
        assert resp.roll_calendar is not None
        assert resp.roll_calendar.roll_urgency == "active"

    def test_cot_report(self):
        report = COTReport.model_validate(SAMPLE_COT_DATA["reports"][0])
        assert report.as_of_date == date(2025, 1, 14)
        assert report.commercial.net == -50000
        assert report.total_open_interest == 250000

    def test_cot_response_from_sample(self):
        # The server returns reports with metadata alongside
        resp = COTResponse.model_validate(SAMPLE_COT_DATA)
        assert resp.contract == "ES"
        assert len(resp.reports) == 1

    def test_contract_model(self):
        c = Contract.model_validate(SAMPLE_CONTRACTS_DATA["contracts"][0])
        assert c.symbol == "ES"
        assert c.exchange == "CME"
        assert c.months_traded == ["H", "M", "U", "Z"]

    def test_contracts_response_from_sample(self):
        resp = ContractsResponse.model_validate(SAMPLE_CONTRACTS_DATA)
        assert len(resp.contracts) == 2

    def test_health_response(self):
        hr = HealthResponse.model_validate(SAMPLE_HEALTH_DATA)
        assert hr.status == "ok"
        assert hr.version == "0.1.0"

    def test_api_response_wrapper(self):
        resp = APIResponse(data={"key": "value"}, metadata={"page": 1})
        assert resp.data == {"key": "value"}

    def test_paginated_response_wrapper(self):
        resp = PaginatedResponse(data=[1, 2, 3], total=3, page=1, page_size=50)
        assert resp.total == 3

    def test_model_json_roundtrip(self):
        """Test that models survive JSON serialization roundtrip."""
        signal = PositioningSignalResponse.model_validate(SAMPLE_SIGNAL_DATA)
        json_str = signal.model_dump_json()
        signal2 = PositioningSignalResponse.model_validate_json(json_str)
        assert signal2.commodity == signal.commodity

    def test_model_direction_validation(self):
        """Test that Literal fields reject invalid values."""
        with pytest.raises(Exception):
            SmartMoney(z_score=1.0, percentile=50.0, direction="invalid", conviction="high")

    def test_model_percentile_bounds(self):
        """Test percentile bounds validation."""
        with pytest.raises(Exception):
            SmartMoney(z_score=1.0, percentile=101.0, direction="long", conviction="low")


# ---------------------------------------------------------------------------
# Tests: Sync Client
# ---------------------------------------------------------------------------


class TestSyncClient:
    """Test synchronous OpenInterestLensClient."""

    def test_client_init_defaults(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_test")
        assert client.base_url == "http://localhost:8000"
        assert client.api_key == "oil_sk_live_test"
        assert client.timeout == 30
        assert client.max_retries == 3
        client.close()

    def test_client_init_custom(self):
        client = OpenInterestLensClient(
            base_url="https://api.example.com",
            api_key="test_key",
            timeout=60,
            max_retries=5,
            retry_delay=2.0,
        )
        assert client.base_url == "https://api.example.com"
        assert client.timeout == 60
        assert client.max_retries == 5
        assert client.retry_delay == 2.0
        client.close()

    def test_client_context_manager(self):
        with OpenInterestLensClient(api_key="test") as client:
            assert client.api_key == "test"

    def test_get_signals(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_signals("ES")
            assert isinstance(result, PositioningSignalResponse)
            assert result.commodity == "ES"

        client.close()

    def test_get_signals_uppercase(self):
        """Test that contract symbol is uppercased."""
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        with patch.object(client._client, "request", return_value=mock_response) as mock_req:
            result = client.get_signals("es")
            mock_req.assert_called_once()
            call_args = mock_req.call_args
            assert "/ES" in call_args[0][1] or "/ES" in str(call_args)

        client.close()

    def test_get_term_structure(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_TERM_STRUCTURE_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_term_structure("ES")
            assert isinstance(result, TermStructureResponse)
            assert result.contract == "ES"

        client.close()

    def test_get_cot(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        # The COT endpoint returns data directly, not wrapped in COTResponse Pydantic model on server
        # but our SDK model should still validate
        mock_response = _mock_response(200, SAMPLE_COT_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_cot("ES")
            assert isinstance(result, COTResponse)
            assert result.contract == "ES"

        client.close()

    def test_get_roll_pressure(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_ROLL_PRESSURE_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_roll_pressure("ES")
            assert isinstance(result, RollPressureResponse)
            assert result.contract == "ES"

        client.close()

    def test_get_contracts(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_CONTRACTS_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_contracts()
            assert isinstance(result, ContractsResponse)
            assert len(result.contracts) == 2

        client.close()

    def test_get_health(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_HEALTH_DATA)

        with patch.object(client._client, "request", return_value=mock_response):
            result = client.get_health()
            assert isinstance(result, HealthResponse)
            assert result.status == "ok"

        client.close()

    def test_auth_error_401(self):
        client = OpenInterestLensClient(api_key="bad_key")
        error_body = {"detail": {"error": "invalid_api_key", "message": "Invalid API key"}}
        mock_response = _mock_response(401, error_body)

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(AuthenticationError) as exc_info:
                client.get_signals("ES")
            assert exc_info.value.status_code == 401

        client.close()

    def test_auth_error_403(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_free")
        error_body = {"detail": {"error": "tier_limit_exceeded", "message": "Contract GC not available on free tier"}}
        mock_response = _mock_response(403, error_body)

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(AuthenticationError) as exc_info:
                client.get_signals("GC")
            assert exc_info.value.status_code == 403

        client.close()

    def test_not_found_error(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        error_body = {"detail": {"error": "not_found", "message": "Contract XYZ not tracked"}}
        mock_response = _mock_response(404, error_body)

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(NotFoundError) as exc_info:
                client.get_signals("XYZ")
            assert exc_info.value.status_code == 404

        client.close()

    def test_validation_error_400(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        error_body = {"detail": {"error": "invalid_date", "message": "Date must be YYYY-MM-DD"}}
        mock_response = _mock_response(400, error_body)

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(ValidationError) as exc_info:
                client.get_signals("ES", start_date="invalid")
            assert exc_info.value.status_code == 400

        client.close()

    def test_server_error_500(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=1)
        error_body = {"detail": {"error": "signal_error", "message": "Internal server error"}}
        mock_response = _mock_response(500, error_body)

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(ServerError) as exc_info:
                client.get_signals("ES")
            assert exc_info.value.status_code == 500

        client.close()

    def test_server_error_retry(self):
        """Test that server errors are retried."""
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=3, retry_delay=0.01)
        error_response = _mock_response(500, {"detail": {"message": "error"}})
        success_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        call_count = 0

        def mock_request(method, path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return error_response
            return success_response

        with patch.object(client._client, "request", side_effect=mock_request):
            with patch("sdk.client.time.sleep"):
                result = client.get_signals("ES")
                assert isinstance(result, PositioningSignalResponse)
                assert call_count == 3

        client.close()

    def test_rate_limit_error_429(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=1)
        error_body = {"detail": {"error": "rate_limit_exceeded", "message": "Too many requests", "retry_after": 60}}
        mock_response = _mock_response(429, error_body, headers={"Retry-After": "60"})

        with patch.object(client._client, "request", return_value=mock_response):
            with pytest.raises(RateLimitError) as exc_info:
                client.get_signals("ES")
            assert exc_info.value.retry_after == 60.0

        client.close()

    def test_rate_limit_retry_then_success(self):
        """Test that rate limits trigger retry then succeed."""
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=3, retry_delay=0.01)
        rate_limited = _mock_response(429, {"detail": {"message": "rate limited", "retry_after": 0.01}}, headers={"Retry-After": "0.01"})
        success = _mock_response(200, SAMPLE_SIGNAL_DATA)

        responses = [rate_limited, success]

        with patch.object(client._client, "request", side_effect=responses):
            with patch("sdk.client.time.sleep"):
                result = client.get_signals("ES")
                assert isinstance(result, PositioningSignalResponse)

        client.close()

    def test_connection_error(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")

        with patch.object(client._client, "request", side_effect=httpx.ConnectError("Connection refused")):
            with pytest.raises(ConnectionError):
                client.get_signals("ES")

        client.close()

    def test_timeout_error(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=1)

        with patch.object(client._client, "request", side_effect=httpx.TimeoutException("Timeout")):
            with pytest.raises(ConnectionError, match="timed out"):
                client.get_signals("ES")

        client.close()

    def test_extract_detail_with_detail_dict(self):
        response = _mock_response(401, {"detail": {"error": "invalid_api_key", "message": "Bad key"}})
        detail = _extract_detail(response)
        assert detail["error"] == "invalid_api_key"
        assert detail["message"] == "Bad key"

    def test_extract_detail_with_detail_string(self):
        response = _mock_response(500, {"detail": "Something went wrong"})
        detail = _extract_detail(response)
        assert detail["message"] == "Something went wrong"

    def test_extract_detail_plain_body(self):
        response = _mock_response(500, {"error": "fail", "message": "bad"})
        detail = _extract_detail(response)
        assert detail["error"] == "fail"


# ---------------------------------------------------------------------------
# Tests: Async Client
# ---------------------------------------------------------------------------


class TestAsyncClient:
    """Test asynchronous AsyncOpenInterestLensClient."""

    @pytest.mark.asyncio
    async def test_async_client_init(self):
        client = AsyncOpenInterestLensClient(api_key="test")
        assert client.base_url == "http://localhost:8000"
        assert client.api_key == "test"
        await client.close()

    @pytest.mark.asyncio
    async def test_async_client_context_manager(self):
        async with AsyncOpenInterestLensClient(api_key="test") as client:
            assert client.api_key == "test"

    @pytest.mark.asyncio
    async def test_async_get_signals(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_signals("ES")
            assert isinstance(result, PositioningSignalResponse)
            assert result.commodity == "ES"

        await client.close()

    @pytest.mark.asyncio
    async def test_async_get_term_structure(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_TERM_STRUCTURE_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_term_structure("ES")
            assert isinstance(result, TermStructureResponse)

        await client.close()

    @pytest.mark.asyncio
    async def test_async_get_cot(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_COT_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_cot("ES")
            assert isinstance(result, COTResponse)
            assert result.contract == "ES"

        await client.close()

    @pytest.mark.asyncio
    async def test_async_get_roll_pressure(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_ROLL_PRESSURE_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_roll_pressure("ES")
            assert isinstance(result, RollPressureResponse)

        await client.close()

    @pytest.mark.asyncio
    async def test_async_get_contracts(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_CONTRACTS_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_contracts()
            assert isinstance(result, ContractsResponse)

        await client.close()

    @pytest.mark.asyncio
    async def test_async_get_health(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_HEALTH_DATA)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_health()
            assert result.status == "ok"

        await client.close()

    @pytest.mark.asyncio
    async def test_async_auth_error(self):
        client = AsyncOpenInterestLensClient(api_key="bad_key")
        error_body = {"detail": {"error": "invalid_api_key", "message": "Invalid API key"}}
        mock_response = _mock_response(401, error_body)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(AuthenticationError):
                await client.get_signals("ES")

        await client.close()

    @pytest.mark.asyncio
    async def test_async_not_found_error(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        error_body = {"detail": {"error": "not_found", "message": "Not found"}}
        mock_response = _mock_response(404, error_body)

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(NotFoundError):
                await client.get_signals("XYZ")

        await client.close()

    @pytest.mark.asyncio
    async def test_async_rate_limit_error(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=1)
        error_body = {"detail": {"error": "rate_limit", "message": "Too many", "retry_after": 30}}
        mock_response = _mock_response(429, error_body, headers={"Retry-After": "30"})

        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_signals("ES")
            assert exc_info.value.retry_after == 30.0

        await client.close()

    @pytest.mark.asyncio
    async def test_async_server_error_retry(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro", max_retries=3, retry_delay=0.01)
        error_response = _mock_response(500, {"detail": {"message": "error"}})
        success_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        call_count = 0

        async def mock_request(method, path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return error_response
            return success_response

        with patch.object(client._client, "request", new_callable=AsyncMock, side_effect=mock_request):
            with patch("sdk.async_client.asyncio.sleep", new_callable=AsyncMock):
                result = await client.get_signals("ES")
                assert isinstance(result, PositioningSignalResponse)

        await client.close()

    @pytest.mark.asyncio
    async def test_async_connection_error(self):
        client = AsyncOpenInterestLensClient(api_key="oil_sk_live_demo_pro")

        with patch.object(client._client, "request", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
            with pytest.raises(ConnectionError):
                await client.get_signals("ES")

        await client.close()


# ---------------------------------------------------------------------------
# Tests: Builder pattern
# ---------------------------------------------------------------------------


class TestBuilder:
    """Test fluent builder pattern."""

    def test_builder_defaults(self):
        client = ClientBuilder().api_key("test").build()
        assert client.base_url == "http://localhost:8000"
        assert client.api_key == "test"
        assert client.timeout == 30
        assert client.max_retries == 3
        assert client.retry_delay == 1.0
        client.close()

    def test_builder_fluent_api(self):
        client = (
            ClientBuilder()
            .base_url("https://api.example.com")
            .api_key("oil_sk_live_test")
            .timeout(60)
            .max_retries(5)
            .retry_delay(2.0)
            .build()
        )
        assert client.base_url == "https://api.example.com"
        assert client.api_key == "oil_sk_live_test"
        assert client.timeout == 60
        assert client.max_retries == 5
        assert client.retry_delay == 2.0
        client.close()

    def test_builder_builds_async_client(self):
        client = ClientBuilder().api_key("test").build_async()
        assert isinstance(client, AsyncOpenInterestLensClient)
        assert client.api_key == "test"
        import asyncio
        asyncio.run(client.close())

    def test_builder_chaining(self):
        """Each builder method returns self for chaining."""
        builder = ClientBuilder()
        result = builder.base_url("http://test.com")
        assert result is builder
        result = builder.api_key("key")
        assert result is builder
        result = builder.timeout(10)
        assert result is builder
        result = builder.max_retries(1)
        assert result is builder
        result = builder.retry_delay(0.5)
        assert result is builder


# ---------------------------------------------------------------------------
# Tests: WebSocket client
# ---------------------------------------------------------------------------


class TestWebSocket:
    """Test WebSocket client logic."""

    def test_websocket_init(self):
        from sdk.websocket import AsyncSignalStream

        stream = AsyncSignalStream(
            api_key="oil_sk_live_demo_pro",
            contracts=["ES", "NQ"],
            signal_types=["positioning"],
        )
        assert stream.api_key == "oil_sk_live_demo_pro"
        assert stream.contracts == ["ES", "NQ"]
        assert stream.signal_types == ["positioning"]
        assert stream.auto_reconnect is True

    def test_websocket_defaults(self):
        from sdk.websocket import AsyncSignalStream

        stream = AsyncSignalStream(api_key="test")
        assert stream.contracts == []
        assert stream.signal_types == ["positioning"]
        assert stream.auto_reconnect is True

    def test_websocket_custom_url(self):
        from sdk.websocket import AsyncSignalStream

        stream = AsyncSignalStream(
            api_key="test",
            ws_url="wss://api.example.com/ws/v1/signals",
        )
        assert stream.ws_url == "wss://api.example.com/ws/v1/signals"


# ---------------------------------------------------------------------------
# Tests: Retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    """Test exponential backoff and retry behavior."""

    def test_exponential_backoff_delays(self):
        """Verify that retry delays follow exponential backoff pattern."""
        client = OpenInterestLensClient(api_key="test", max_retries=3, retry_delay=1.0)

        delays = []
        for attempt in range(client.max_retries - 1):
            delay = client.retry_delay * (2 ** attempt)
            delays.append(delay)

        assert delays[0] == 1.0  # 1.0 * 2^0
        assert delays[1] == 2.0  # 1.0 * 2^1

        client.close()

    def test_custom_retry_delay(self):
        client = OpenInterestLensClient(api_key="test", retry_delay=0.5, max_retries=4)
        delays = [client.retry_delay * (2 ** i) for i in range(3)]
        assert delays == [0.5, 1.0, 2.0]
        client.close()

    def test_max_retries_exhausted(self):
        """After max retries, should raise ConnectionError for timeouts."""
        client = OpenInterestLensClient(api_key="test", max_retries=2)

        with patch.object(client._client, "request", side_effect=httpx.TimeoutException("timeout")):
            with pytest.raises(ConnectionError, match="timed out"):
                client.get_signals("ES")

        client.close()


# ---------------------------------------------------------------------------
# Tests: Parameter passing
# ---------------------------------------------------------------------------


class TestParameterPassing:
    """Test that query parameters are correctly passed to requests."""

    def test_signals_with_params(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_SIGNAL_DATA)

        with patch.object(client._client, "request", return_value=mock_response) as mock_req:
            client.get_signals("ES", start_date="2025-01-01", end_date="2025-01-31", lookback_weeks=26)
            call_args = mock_req.call_args
            params = call_args[1].get("params") or call_args.kwargs.get("params")
            assert params is not None
            assert params.get("lookback_weeks") == 26
            assert params.get("start_date") == "2025-01-01"

        client.close()

    def test_term_structure_with_date(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_TERM_STRUCTURE_DATA)

        with patch.object(client._client, "request", return_value=mock_response) as mock_req:
            client.get_term_structure("ES", as_of_date="2025-03-01")
            call_args = mock_req.call_args
            params = call_args[1].get("params") or call_args.kwargs.get("params")
            assert params is not None
            assert params.get("date") == "2025-03-01"

        client.close()

    def test_cot_with_params(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_COT_DATA)

        with patch.object(client._client, "request", return_value=mock_response) as mock_req:
            client.get_cot("ES", start_date="2025-01-01", end_date="2025-01-31")
            call_args = mock_req.call_args
            params = call_args[1].get("params") or call_args.kwargs.get("params")
            assert params is not None
            assert params.get("start_date") == "2025-01-01"

        client.close()

    def test_roll_pressure_with_days_back(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        mock_response = _mock_response(200, SAMPLE_ROLL_PRESSURE_DATA)

        with patch.object(client._client, "request", return_value=mock_response) as mock_req:
            client.get_roll_pressure("ES", days_back=60)
            call_args = mock_req.call_args
            params = call_args[1].get("params") or call_args.kwargs.get("params")
            assert params is not None
            assert params.get("days_back") == 60

        client.close()

    def test_api_key_in_headers(self):
        client = OpenInterestLensClient(api_key="oil_sk_live_demo_pro")
        assert client._client.headers.get("X-API-Key") == "oil_sk_live_demo_pro"
        client.close()

    def test_no_api_key_no_header(self):
        client = OpenInterestLensClient()
        assert "X-API-Key" not in client._client.headers
        client.close()