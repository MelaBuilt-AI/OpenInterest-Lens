"""Rate limiting service for OpenInterest Lens.

Per-tier rate limits using a sliding window counter.
Supports Redis (production) and in-memory fallback (dev/testing).

Tiers:
- Free: 60 req/hour
- Pro: 600 req/hour
- Enterprise: 6000 req/hour
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.middleware.auth import TIER_LIMITS

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Rate limit configuration
# ---------------------------------------------------------------------------

TIER_RATE_LIMITS: dict[str, int] = {
    "free": 60,
    "pro": 600,
    "enterprise": 6000,
}

RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour sliding window


# ---------------------------------------------------------------------------
# In-memory rate limiter
# ---------------------------------------------------------------------------


@dataclass
class _RateBucket:
    """Simple sliding window bucket."""

    count: int = 0
    window_start: float = 0.0


class InMemoryRateLimiter:
    """In-memory rate limiter for dev/testing.

    Uses a sliding window per (user_id, tier) combination.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _RateBucket] = {}

    def check(self, key: str, limit: int) -> tuple[int, int, int]:
        """Check rate limit for a key.

        Returns (remaining, limit, reset_time_seconds).
        """
        now = time.time()
        bucket = self._buckets.get(key)

        if bucket is None or (now - bucket.window_start) >= RATE_LIMIT_WINDOW_SECONDS:
            bucket = _RateBucket(count=1, window_start=now)
            self._buckets[key] = bucket
            return (limit - 1, limit, RATE_LIMIT_WINDOW_SECONDS)

        bucket.count += 1
        remaining = max(0, limit - bucket.count)
        reset_time = int(RATE_LIMIT_WINDOW_SECONDS - (now - bucket.window_start))
        return (remaining, limit, reset_time)


# Global in-memory limiter
_memory_limiter = InMemoryRateLimiter()


# ---------------------------------------------------------------------------
# Rate limit functions (called from dependency or middleware)
# ---------------------------------------------------------------------------


async def check_rate_limit(tier: str, user_id: str, redis=None) -> tuple[int, int, int]:
    """Check rate limit for a request.

    Args:
        tier: User's tier (free, pro, enterprise).
        user_id: User's identifier.
        redis: Optional Redis client.

    Returns:
        (remaining, limit, reset_time_seconds)
    """
    limit = TIER_RATE_LIMITS.get(tier, 60)
    rate_key = f"rate_limit:{user_id}:{tier}"

    if redis:
        try:
            pipe = redis.pipeline()
            pipe.incr(rate_key)
            pipe.ttl(rate_key)
            results = await pipe.execute()

            current_count = results[0]
            ttl = results[1]

            if ttl == -1:
                await redis.expire(rate_key, RATE_LIMIT_WINDOW_SECONDS)
                ttl = RATE_LIMIT_WINDOW_SECONDS
            elif ttl == -2:
                await redis.expire(rate_key, RATE_LIMIT_WINDOW_SECONDS)
                ttl = RATE_LIMIT_WINDOW_SECONDS

            remaining = max(0, limit - current_count)
            reset_time = ttl if ttl > 0 else RATE_LIMIT_WINDOW_SECONDS

            return (remaining, limit, reset_time)
        except Exception as exc:
            logger.warning("redis_rate_limit_fallback", error=str(exc))

    # In-memory fallback
    return _memory_limiter.check(rate_key, limit)


def is_rate_limited(remaining: int) -> bool:
    """Check if the rate limit has been exceeded."""
    return remaining < 0


# ---------------------------------------------------------------------------
# Response headers middleware
# ---------------------------------------------------------------------------


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """Add X-RateLimit-* headers to responses when rate limit info is available.

    Reads rate limit info from request.state (set by require_api_key dependency).
    Skips health check endpoints.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip health checks
        if request.url.path.endswith("/health"):
            return await call_next(request)

        response = await call_next(request)

        # Add rate limit headers if available
        remaining = getattr(request.state, "rate_limit_remaining", None)
        limit = getattr(request.state, "rate_limit_limit", None)
        reset_time = getattr(request.state, "rate_limit_reset", None)

        if limit is not None:
            response.headers["X-RateLimit-Limit"] = str(limit)
        if remaining is not None:
            response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        if reset_time is not None:
            response.headers["X-RateLimit-Reset"] = str(reset_time)

        return response