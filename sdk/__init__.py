"""OpenInterest Lens — Python SDK for the futures market structure API."""

from sdk.client import OpenInterestLensClient
from sdk.async_client import AsyncOpenInterestLensClient
from sdk.models import (
    PositioningSignal,
    TermStructureCurve,
    RollPressureIndex,
    COTReport,
    Contract,
    HealthResponse,
)
from sdk.exceptions import (
    OpenInterestLensError,
    AuthenticationError,
    RateLimitError,
    NotFoundError,
    ServerError,
    ConnectionError,
    ValidationError,
)
from sdk.builder import ClientBuilder

__version__ = "0.1.0"

__all__ = [
    "OpenInterestLensClient",
    "AsyncOpenInterestLensClient",
    "PositioningSignal",
    "TermStructureCurve",
    "RollPressureIndex",
    "COTReport",
    "Contract",
    "HealthResponse",
    "OpenInterestLensError",
    "AuthenticationError",
    "RateLimitError",
    "NotFoundError",
    "ServerError",
    "ConnectionError",
    "ValidationError",
    "ClientBuilder",
]