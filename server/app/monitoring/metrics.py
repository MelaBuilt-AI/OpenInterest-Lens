"""Prometheus metrics definitions for OpenInterest Lens.

Metrics:
- http_requests_total: Counter — method, endpoint, status
- http_request_duration_seconds: Histogram — method, endpoint
- active_websocket_connections: Gauge — tier
- signals_computed_total: Counter — signal_type
- data_ingestion_events: Counter — source, status
- data_quality_score: Gauge — contract
- api_key_usage_total: Counter — key_hash, endpoint
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge

# ---------------------------------------------------------------------------
# HTTP metrics
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "http_requests_total",
    "Total count of HTTP requests by method, endpoint, and status code.",
    ["method", "endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Histogram of HTTP request duration in seconds.",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ---------------------------------------------------------------------------
# WebSocket metrics
# ---------------------------------------------------------------------------

active_websocket_connections = Gauge(
    "active_websocket_connections",
    "Number of currently active WebSocket connections by tier.",
    ["tier"],
)

# ---------------------------------------------------------------------------
# Signal metrics
# ---------------------------------------------------------------------------

signals_computed_total = Counter(
    "signals_computed_total",
    "Total number of signals computed by type.",
    ["signal_type"],
)

# ---------------------------------------------------------------------------
# Data ingestion metrics
# ---------------------------------------------------------------------------

data_ingestion_events = Counter(
    "data_ingestion_events",
    "Total data ingestion events by source and status.",
    ["source", "status"],
)

# ---------------------------------------------------------------------------
# Data quality metrics
# ---------------------------------------------------------------------------

data_quality_score = Gauge(
    "data_quality_score",
    "Data quality score per contract (0.0 – 1.0).",
    ["contract"],
)

# ---------------------------------------------------------------------------
# API key usage metrics
# ---------------------------------------------------------------------------

api_key_usage_total = Counter(
    "api_key_usage_total",
    "Total API requests per key hash and endpoint.",
    ["key_hash", "endpoint"],
)