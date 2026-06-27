"""Asynchronous OpenInterest Lens client using httpx."""

from __future__ import annotations

import asyncio
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


class AsyncOpenInterestLensClient:
    """Asynchronous client for the OpenInterest Lens API.

    Usage::

        async with AsyncOpenInterestLensClient(api_key="oil_sk_live_...") as client:
            signals = await client.get_signals("ES")
            ts = await client.get_term_structure("ES")
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
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-API-Key": self.api_key} if self.api_key else {},
        )

    # --- Async context manager ---

    async def __aenter__(self) -> AsyncOpenInterestLensClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # --- Internal request helpers ---

    async def _request(self, method: str, path: str, *, params: dict | None = None) -> Any:
        """Make an async HTTP request with retry and error handling."""
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                response = await self._client.request(method, path, params=params)
            except httpx.ConnectError as exc:
                raise ConnectionError(f"Cannot connect to {self.base_url}") from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                    continue
                raise ConnectionError(f"Request timed out after {self.max_retries} attempts") from exc

            # Handle rate limiting
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", response.headers.get("X-RateLimit-Reset", "60")))
                detail = response.json().get("detail", {}) if response.headers.get("content-type", "").startswith("application/json") else {}
                retry_after = detail.get("retry_after", retry_after) if isinstance(detail, dict) else retry_after
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(retry_after)
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
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
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

        raise ConnectionError("Max retries exceeded") from last_exc

    async def _get(self, path: str, *, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    # --- Public API methods ---

    async def get_signals(
        self,
        contract: str,
        signal_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        lookback_weeks: int = 52,
    ) -> PositioningSignalResponse:
        """Get positioning signals for a specific contract."""
        params: dict[str, Any] = {"lookback_weeks": lookback_weeks}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        data = await self._get(f"/v1/signals/positioning/{contract.upper()}", params=params)
        return PositioningSignalResponse.model_validate(data)

    async def get_term_structure(
        self,
        contract: str,
        as_of_date: str | None = None,
    ) -> TermStructureResponse:
        """Get term structure curve for a specific contract."""
        params: dict[str, Any] = {}
        if as_of_date:
            params["date"] = as_of_date

        data = await self._get(f"/v1/signals/term-structure/{contract.upper()}", params=params)
        return TermStructureResponse.model_validate(data)

    async def get_cot(
        self,
        contract: str,
        report_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> COTResponse:
        """Get COT data for a contract."""
        params: dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if report_type:
            params["format"] = report_type

        data = await self._get(f"/v1/cot/{contract.upper()}", params=params)
        return COTResponse.model_validate(data)

    async def get_roll_pressure(
        self,
        contract: str,
        as_of_date: str | None = None,
        days_back: int = 30,
    ) -> RollPressureResponse:
        """Get roll pressure index for a contract."""
        params: dict[str, Any] = {"days_back": days_back}
        if as_of_date:
            params["end_date"] = as_of_date

        data = await self._get(f"/v1/roll-pressure/{contract.upper()}", params=params)
        return RollPressureResponse.model_validate(data)

    async def get_contracts(self) -> ContractsResponse:
        """List all tracked contracts."""
        data = await self._get("/v1/contracts")
        return ContractsResponse.model_validate(data)

    async def get_health(self) -> HealthResponse:
        """Check API health status."""
        data = await self._get("/v1/health")
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