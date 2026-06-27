"""Prometheus monitoring middleware for OpenInterest Lens.

Captures request duration and status code, exports them as Prometheus metrics.
Adds a /metrics endpoint serving the default Prometheus format.
"""

from __future__ import annotations

import time

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import Match

from app.monitoring.metrics import http_request_duration_seconds, http_requests_total

logger = structlog.get_logger(__name__)

# Paths to skip from metrics collection
_SKIP_PATHS = {"/metrics", "/health", "/v1/health", "/openapi.json", "/docs", "/redoc"}


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Middleware that records request duration and status code as Prometheus metrics.

    Skips health checks and the /metrics endpoint itself.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip non-request paths
        path = request.url.path

        if path in _SKIP_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)

        # Resolve route name for label cardinality control
        endpoint = self._resolve_endpoint(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        method = request.method
        status = response.status_code

        # Record metrics
        http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
        http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)

        return response

    @staticmethod
    def _resolve_endpoint(request: Request) -> str:
        """Resolve a route template from the request, falling back to the raw path."""
        app = request.app
        for route in app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL and hasattr(route, "path"):
                return route.path
        # Fallback: normalise dynamic segments to keep cardinality bounded
        path = request.url.path
        parts = path.rstrip("/").split("/")
        # Replace likely-ID segments (UUIDs, numeric, long hex)
        normalised = []
        for part in parts:
            if len(part) > 20 or part.isdigit():
                normalised.append(":id")
            else:
                normalised.append(part)
        return "/".join(normalised) or "/"


def add_metrics_endpoint(app: FastAPI) -> None:
    """Register the /metrics endpoint on the FastAPI app."""

    @app.get("/metrics", tags=["monitoring"], include_in_schema=False)
    async def metrics_endpoint() -> Response:
        """Expose Prometheus-format metrics."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        body = generate_latest()
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)