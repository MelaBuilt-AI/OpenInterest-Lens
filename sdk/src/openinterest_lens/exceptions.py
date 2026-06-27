"""Custom exception hierarchy for the OpenInterest Lens SDK."""


class OpenInterestLensError(Exception):
    """Base exception for all SDK errors."""

    def __init__(self, message: str = "", *, status_code: int | None = None, response: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response

    def __str__(self) -> str:
        return self.message


class AuthenticationError(OpenInterestLensError):
    """Raised when the API key is invalid or missing (401)."""

    def __init__(self, message: str = "Invalid or missing API key", **kwargs):
        super().__init__(message, status_code=kwargs.pop("status_code", 401), **kwargs)


class RateLimitError(OpenInterestLensError):
    """Raised when the rate limit is exceeded (429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: float | None = None,
        **kwargs,
    ):
        super().__init__(message, status_code=kwargs.pop("status_code", 429), **kwargs)
        self.retry_after = retry_after


class NotFoundError(OpenInterestLensError):
    """Raised when the requested resource is not found (404)."""

    def __init__(self, message: str = "Resource not found", **kwargs):
        super().__init__(message, status_code=kwargs.pop("status_code", 404), **kwargs)


class ServerError(OpenInterestLensError):
    """Raised when the server returns a 5xx error."""

    def __init__(self, message: str = "Server error", **kwargs):
        super().__init__(message, status_code=kwargs.pop("status_code", 500), **kwargs)


class ConnectionError(OpenInterestLensError):
    """Raised when a connection to the server cannot be established."""

    def __init__(self, message: str = "Connection error", **kwargs):
        super().__init__(message, **kwargs)


class ValidationError(OpenInterestLensError):
    """Raised when request parameters are invalid (400)."""

    def __init__(self, message: str = "Validation error", **kwargs):
        super().__init__(message, status_code=kwargs.pop("status_code", 400), **kwargs)