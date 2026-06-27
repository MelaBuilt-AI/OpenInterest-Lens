"""Request size limit middleware for OpenInterest Lens.

Rejects requests with bodies exceeding the configured maximum size.
Default: 256KB — sufficient for API queries but blocks large uploads.
"""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)

# Default max request body size: 256KB
DEFAULT_MAX_REQUEST_SIZE = 256 * 1024  # 262144 bytes


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that rejects requests with bodies exceeding a size limit.

    Applied to all non-WebSocket, non-health endpoints.
    The limit is configurable via the max_size parameter.
    """

    def __init__(self, app, max_size: int = DEFAULT_MAX_REQUEST_SIZE):
        super().__init__(app)
        self.max_size = max_size

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip WebSocket and health check endpoints
        if request.url.path.startswith("/ws/"):
            return await call_next(request)
        if request.url.path.endswith("/health"):
            return await call_next(request)

        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_size:
                    logger.warning("request_too_large", path=request.url.path, content_length=content_length)
                    return Response(
                        status_code=413,
                        content='{"error": "request_too_large", "message": "Request body exceeds maximum allowed size (256KB)"}',
                        media_type="application/json",
                    )
            except (ValueError, TypeError):
                pass

        response = await call_next(request)
        return response