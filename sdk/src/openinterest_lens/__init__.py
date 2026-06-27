"""OpenInterest Lens — Python SDK for the futures market structure API."""

from openinterest_lens.client import OpenInterestLensClient
from openinterest_lens.async_client import AsyncOpenInterestLensClient
from openinterest_lens.models import (
    PositioningSignal,
    TermStructureCurve,
    RollPressureIndex,
    COTReport,
    Contract,
    HealthResponse,
)
from openinterest_lens.exceptions import (
    OpenInterestLensError,
    AuthenticationError,
    RateLimitError,
    NotFoundError,
    ServerError,
    ConnectionError,
    ValidationError,
)
from openinterest_lens.builder import ClientBuilder

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


def __getattr__(name: str):
    """Lazy-load WebSocket client so websockets is not required unless used."""
    if name == "AsyncSignalStream":
        from openinterest_lens.websocket import AsyncSignalStream
        return AsyncSignalStream
    raise AttributeError(f"module 'openinterest_lens' has no attribute {name!r}")