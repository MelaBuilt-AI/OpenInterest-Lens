"""Fluent builder pattern for OpenInterestLensClient."""

from __future__ import annotations

from sdk.client import OpenInterestLensClient, _DEFAULT_BASE_URL, _DEFAULT_MAX_RETRIES, _DEFAULT_RETRY_DELAY, _DEFAULT_TIMEOUT


class ClientBuilder:
    """Fluent builder for constructing OpenInterestLensClient instances.

    Usage::

        client = (
            ClientBuilder()
            .base_url("https://api.openinterestlens.com")
            .api_key("oil_sk_live_...")
            .timeout(60)
            .max_retries(5)
            .retry_delay(2.0)
            .build()
        )
    """

    def __init__(self) -> None:
        self._base_url: str = _DEFAULT_BASE_URL
        self._api_key: str = ""
        self._timeout: int = _DEFAULT_TIMEOUT
        self._max_retries: int = _DEFAULT_MAX_RETRIES
        self._retry_delay: float = _DEFAULT_RETRY_DELAY

    def base_url(self, url: str) -> ClientBuilder:
        """Set the API base URL."""
        self._base_url = url
        return self

    def api_key(self, key: str) -> ClientBuilder:
        """Set the API key for authentication."""
        self._api_key = key
        return self

    def timeout(self, seconds: int) -> ClientBuilder:
        """Set the request timeout in seconds."""
        self._timeout = seconds
        return self

    def max_retries(self, retries: int) -> ClientBuilder:
        """Set the maximum number of retries."""
        self._max_retries = retries
        return self

    def retry_delay(self, delay: float) -> ClientBuilder:
        """Set the initial retry delay in seconds (uses exponential backoff)."""
        self._retry_delay = delay
        return self

    def build(self) -> OpenInterestLensClient:
        """Build and return the client instance."""
        return OpenInterestLensClient(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
        )

    def build_async(self):
        """Build and return an async client instance."""
        from sdk.async_client import AsyncOpenInterestLensClient

        return AsyncOpenInterestLensClient(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
        )