"""Redis-backed response caching service for OpenInterest Lens.

Provides:
- Redis caching with TTL for latest/current data (1 hour default)
- No caching for historical range queries
- X-Cache and X-Cache-Age response headers
- Graceful fallback to in-memory cache when Redis unavailable
- Cache invalidation on new data ingestion
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime

import structlog

logger = structlog.get_logger(__name__)

# Default TTLs
LATEST_TTL_SECONDS = 3600  # 1 hour for latest/current data
HISTORY_TTL_SECONDS = 0  # No caching for historical range queries


class RedisCacheService:
    """Redis-backed caching service for API responses.

    Uses Redis STRING type with JSON serialization.
    Falls back to in-memory dict when Redis is unavailable.

    Cache keys follow the pattern:
        oil:{signal_type}:{contract}:latest
        oil:{signal_type}:{contract}:{date}

    where signal_type is one of: positioning, term_structure, cot, roll_pressure, contracts
    """

    def __init__(self, redis=None, default_ttl: int = LATEST_TTL_SECONDS) -> None:
        self._redis = redis
        self._default_ttl = default_ttl
        # In-memory fallback
        self._memory_cache: dict[str, tuple[str, float, int]] = {}  # key -> (json_value, created_at, ttl)
        self._max_memory_entries = 512

    @staticmethod
    def make_key(
        signal_type: str,
        contract: str,
        as_of_date: date | None = None,
    ) -> str:
        """Generate a cache key.

        Args:
            signal_type: One of 'positioning', 'term_structure', 'cot', 'roll_pressure', 'contracts'.
            contract: Contract symbol, e.g. 'ES'.
            as_of_date: Optional date. If None, uses 'latest'.

        Returns:
            Cache key like 'oil:positioning:ES:latest' or 'oil:positioning:ES:2026-05-12'.
        """
        date_str = as_of_date.isoformat() if as_of_date else "latest"
        return f"oil:{signal_type}:{contract}:{date_str}"

    async def get(self, key: str) -> dict | None:
        """Get a cached response by key.

        Returns None on cache miss. Returns parsed dict on cache hit.
        """
        if self._redis:
            try:
                raw = await self._redis.get(key)
                if raw is None:
                    return None
                data = json.loads(raw)
                return data
            except Exception as exc:
                logger.warning("redis_cache_get_failed", key=key, error=str(exc))
                # Fall through to memory cache

        # In-memory fallback
        entry = self._memory_cache.get(key)
        if entry is None:
            return None
        value, created_at, ttl = entry
        if ttl > 0 and (time.time() - created_at) > ttl:
            del self._memory_cache[key]
            return None
        return json.loads(value)

    async def set(
        self,
        key: str,
        value: dict,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a response in cache.

        Args:
            key: Cache key.
            value: Response dict to cache.
            ttl_seconds: TTL in seconds. 0 means no caching. None uses default.
        """
        if ttl_seconds == 0:
            return  # Explicitly no caching

        effective_ttl = ttl_seconds or self._default_ttl

        serialized = json.dumps(value, default=self._json_serializer)

        if self._redis:
            try:
                await self._redis.set(key, serialized, ex=effective_ttl)
                return
            except Exception as exc:
                logger.warning("redis_cache_set_failed", key=key, error=str(exc))
                # Fall through to memory cache

        # In-memory fallback
        if len(self._memory_cache) >= self._max_memory_entries:
            # Evict oldest entry
            oldest_key = next(iter(self._memory_cache))
            del self._memory_cache[oldest_key]

        self._memory_cache[key] = (serialized, time.time(), effective_ttl)

    async def delete(self, key: str) -> bool:
        """Delete a cached entry.

        Returns True if the key existed, False otherwise.
        """
        found = False

        if self._redis:
            try:
                result = await self._redis.delete(key)
                found = result > 0
            except Exception as exc:
                logger.warning("redis_cache_delete_failed", key=key, error=str(exc))

        if key in self._memory_cache:
            del self._memory_cache[key]
            found = True

        return found

    async def invalidate(self, contract: str | None = None) -> int:
        """Invalidate cache entries.

        Args:
            contract: If provided, invalidate entries for this contract only.
                      If None, invalidate all entries.

        Returns:
            Number of entries invalidated.
        """
        count = 0

        if self._redis:
            try:
                if contract:
                    # Find and delete keys matching oil:*:contract:*
                    pattern = f"oil:*:{contract}:*"
                    keys = []
                    async for key in self._redis.scan_iter(match=pattern):
                        keys.append(key)
                    if keys:
                        count = await self._redis.delete(*keys)
                else:
                    # Flush all oil:* keys
                    keys = []
                    async for key in self._redis.scan_iter(match="oil:*"):
                        keys.append(key)
                    if keys:
                        count = await self._redis.delete(*keys)
            except Exception as exc:
                logger.warning("redis_cache_invalidate_failed", error=str(exc))

        # Always clear memory cache too
        if contract:
            prefix = f":{contract}:"
            keys_to_remove = [k for k in self._memory_cache if prefix in k]
            for key in keys_to_remove:
                del self._memory_cache[key]
            count += len(keys_to_remove)
        else:
            count += len(self._memory_cache)
            self._memory_cache.clear()

        return count

    async def invalidate_on_ingestion(self, contract: str) -> int:
        """Invalidate cache after new data is ingested for a contract.

        Args:
            contract: Contract symbol that received new data.

        Returns:
            Number of entries invalidated.
        """
        return await self.invalidate(contract)

    @staticmethod
    def _json_serializer(obj):
        """Custom JSON serializer for datetime objects."""
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache_service: RedisCacheService | None = None


def get_cache_service(redis=None) -> RedisCacheService:
    """Get or create the global cache service instance.

    Args:
        redis: Optional Redis client. If None, uses in-memory fallback.

    Returns:
        The singleton RedisCacheService instance.
    """
    global _cache_service
    if _cache_service is None:
        _cache_service = RedisCacheService(redis=redis)
    elif redis and _cache_service._redis is None:
        _cache_service._redis = redis
    return _cache_service


def reset_cache_service() -> None:
    """Reset the global cache service. Used primarily in testing."""
    global _cache_service
    _cache_service = None


def cache_headers(hit: bool, age_seconds: int | None = None) -> dict[str, str]:
    """Generate standard cache response headers.

    Args:
        hit: Whether the response was served from cache.
        age_seconds: Age of the cached response in seconds (None for MISS).

    Returns:
        Dict of headers to add to the response.
    """
    headers = {
        "X-Cache": "HIT" if hit else "MISS",
    }
    if age_seconds is not None:
        headers["X-Cache-Age"] = str(age_seconds)
    return headers