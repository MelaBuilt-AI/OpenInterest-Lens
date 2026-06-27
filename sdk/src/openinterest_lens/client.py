"""Synchronous OpenInterest Lens client using httpx."""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional

import httpx

from openinterest_lens.exceptions import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    OpenInterestLensError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from openinterest_lens.models import (
    COTResponse,
    ContractsResponse,
    HealthResponse,
    PositioningSignalResponse,
    RollPressureResponse,
    TermStructureResponse,
)

_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 1.0


class OpenInterestLensClient:
    """Synchronous client for the OpenInterest Lens API.

    Usage::

        client = OpenInterestLensClient(api_key="oil_sk_live_...")
        signals = client.get_signals("ES")
        ts = client.get_term_structure("ES")
        client.close()

    Or as a context manager::

        with OpenInterestLensClient(api_key="oil_sk_live_...") as client:
            signals = client.get_signals("ES")
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str = "",
        timeout: int = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_delay: float = _DEFAULT_RETRY_DELAY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-API-Key": self.api_key} if self.api_key else {},
        )

    # --- Context manager ---

    def __enter__(self) -> OpenInterestLensClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # --- Internal request helpers ---

    def _request(self, method: str, path: str, *, params: dict | None = None) -> Any:
        """Make an HTTP request with retry and error handling."""
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = self._client.request(method, path, params=params)
            except httpx.ConnectError as exc:
                raise ConnectionError(f"Cannot connect to {self.base_url}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                raise ConnectionError(f"Request timed out after {self.max_retries} attempts") from exc

            # Handle rate limiting
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", response.headers.get("X-RateLimit-Reset", "60")))
                detail = response.json().get("detail", {}) if response.headers.get("content-type", "").startswith("application/json") else {}
                retry_after = detail.get("retry_after", retry_after) if isinstance(detail, dict) else retry_after
                if attempt < self.max_retries - 1:
                    time.sleep(retry_after)
                    continue
                raise RateLimitError(
                    message=f"Rate limit exceeded. Retry after {retry_after}s",
                    retry_after=retry_after,
                    status_code=429,
                    response=response.json() if response.headers.get("content-type", "").startswith("application/json") else None,
                )

            # Handle other status codes
            if response.status_code == 401:
                detail = _extract_detail(response)
                raise AuthenticationError(detail.get("message", "Invalid or missing API key"), status_code=401, response=detail)
            if response.status_code == 403:
                detail = _extract_detail(response)
                raise AuthenticationError(detail.get("message", "Access forbidden"), status_code=403, response=detail)
            if response.status_code == 404:
                detail = _extract_detail(response)
                raise NotFoundError(detail.get("message", "Resource not found"), status_code=404, response=detail)
            if response.status_code == 400:
                detail = _extract_detail(response)
                raise ValidationError(detail.get("message", "Validation error"), status_code=400, response=detail)
            if response.status_code >= 500:
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                detail = _extract_detail(response)
                raise ServerError(
                    detail.get("message", f"Server error: {response.status_code}"),
                    status_code=response.status_code,
                    response=detail,
                )
            if response.status_code >= 400:
                detail = _extract_detail(response)
                raise OpenInterestLensError(
                    detail.get("message", f"HTTP {response.status_code}"),
                    status_code=response.status_code,
                    response=detail,
                )

            # Success
            return response.json()

        # Should not reach here, but just in case
        raise ConnectionError("Max retries exceeded") from last_exc

    def _get(self, path: str, *, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    # --- Public API methods ---

    def get_signals(
        self,
        contract: str,
        signal_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        lookback_weeks: int = 52,
    ) -> PositioningSignalResponse:
        """Get positioning signals for a specific contract.

        Args:
            contract: Root symbol (e.g. 'ES', 'NQ', 'CL')
            signal_type: Optional signal type filter
            start_date: Optional start date (YYYY-MM-DD)
            end_date: Optional end date (YYYY-MM-DD)
            lookback_weeks: Lookback window for Z-scores (default 52)

        Returns:
            PositioningSignalResponse with signal data and breakdown.
        """
        params: dict[str, Any] = {"lookback_weeks": lookback_weeks}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        data = self._get(f"/v1/signals/positioning/{contract.upper()}", params=params)
        return PositioningSignalResponse.model_validate(data)

    def get_term_structure(
        self,
        contract: str,
        as_of_date: str | None = None,
    ) -> TermStructureResponse:
        """Get term structure curve for a specific contract.

        Args:
            contract: Root symbol (e.g. 'ES', 'NQ', 'CL')
            as_of_date: Optional as-of date (YYYY-MM-DD)

        Returns:
            TermStructureResponse with curve data and metrics.
        """
        params: dict[str, Any] = {}
        if as_of_date:
            params["date"] = as_of_date

        data = self._get(f"/v1/signals/term-structure/{contract.upper()}", params=params)
        return TermStructureResponse.model_validate(data)

    def get_cot(
        self,
        contract: str,
        report_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> COTResponse:
        """Get COT (Commitments of Traders) data for a contract.

        Args:
            contract: Root symbol (e.g. 'ES', 'NQ', 'CL')
            report_type: Optional report format ('full' or 'summary')
            start_date: Optional start date (YYYY-MM-DD)
            end_date: Optional end date (YYYY-MM-DD)

        Returns:
            COTResponse with report data.
        """
        params: dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if report_type:
            params["format"] = report_type

        data = self._get(f"/v1/cot/{contract.upper()}", params=params)
        return COTResponse.model_validate(data)

    def get_roll_pressure(
        self,
        contract: str,
        as_of_date: str | None = None,
        days_back: int = 30,
    ) -> RollPressureResponse:
        """Get roll pressure index for a contract.

        Args:
            contract: Root symbol (e.g. 'ES', 'NQ', 'CL')
            as_of_date: Optional as-of date (YYYY-MM-DD)
            days_back: Days of history for OI decay analysis (default 30)

        Returns:
            RollPressureResponse with roll pressure metrics.
        """
        params: dict[str, Any] = {"days_back": days_back}
        if as_of_date:
            params["end_date"] = as_of_date

        data = self._get(f"/v1/roll-pressure/{contract.upper()}", params=params)
        return RollPressureResponse.model_validate(data)

    def get_contracts(self) -> ContractsResponse:
        """List all tracked contracts with metadata.

        Returns:
            ContractsResponse with list of contracts.
        """
        data = self._get("/v1/contracts")
        return ContractsResponse.model_validate(data)

    def get_health(self) -> HealthResponse:
        """Check API health status.

        Returns:
            HealthResponse with status, service name, and version.
        """
        data = self._get("/v1/health")
        return HealthResponse.model_validate(data)


def _extract_detail(response: httpx.Response) -> dict:
    """Extract error detail from response JSON."""
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            detail = body["detail"]
            return detail if isinstance(detail, dict) else {"message": str(detail)}
        return body if isinstance(body, dict) else {"message": str(body)}
    except Exception:
        return {"message": f"HTTP {response.status_code}"}