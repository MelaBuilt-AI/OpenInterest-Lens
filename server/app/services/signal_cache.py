"""Signal cache service for OpenInterest Lens.

Provides in-memory LRU cache with TTL for computed positioning signals.
Avoids recomputation on repeated requests for the same commodity+date+type.

Cache keys are based on commodity symbol + date + signal type.
TTL defaults to 1 hour, configurable via settings.
Cache invalidation happens when new COT data is ingested.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """A single cached signal entry with TTL tracking."""

    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 3600.0  # Default 1 hour

    @property
    def is_expired(self) -> bool:
        """Check if this entry has exceeded its TTL."""
        return (time.time() - self.created_at) > self.ttl_seconds


# ---------------------------------------------------------------------------
# Signal cache
# ---------------------------------------------------------------------------


class SignalCache:
    """In-memory LRU cache for computed signals.

    Features:
    - LRU eviction when max_size is reached
    - TTL-based expiration
    - Cache key based on commodity + date + signal_type
    - Invalidation by commodity or full flush
    - Thread-safe via GIL (sufficient for async FastAPI)
    """

    def __init__(self, max_size: int = 256, default_ttl: float = 3600.0) -> None:
        """Initialize the signal cache.

        Args:
            max_size: Maximum number of entries. Oldest evicted when full.
            default_ttl: Default TTL in seconds (default: 1 hour).
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(
        commodity: str,
        signal_type: str = "positioning",
        as_of_date: Optional[date] = None,
    ) -> str:
        """Generate a cache key from commodity, signal type, and date.

        Args:
            commodity: Contract symbol, e.g. 'ES'.
            signal_type: Type of signal, e.g. 'positioning'.
            as_of_date: Optional date of the COT report. If None, uses 'latest'.

        Returns:
            String cache key like 'positioning:ES:2026-05-12' or 'positioning:ES:latest'.
        """
        date_str = as_of_date.isoformat() if as_of_date else "latest"
        return f"{signal_type}:{commodity}:{date_str}"

    def get(self, key: str) -> Optional[Any]:
        """Retrieve a cached value by key.

        Returns None if the key doesn't exist or has expired.
        On hit, moves the entry to the end (most recently used).

        Args:
            key: Cache key.

        Returns:
            Cached value or None if miss/expired.
        """
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired:
            # Expired entry — remove and count as miss
            del self._cache[key]
            self._misses += 1
            return None

        # Hit — move to end for LRU
        self._cache.move_to_end(key)
        self._hits += 1
        return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Store a value in the cache.

        If the cache is full, evicts the least recently used entry first.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl_seconds: Optional TTL override. Uses default if None.
        """
        # Evict LRU if at capacity
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)  # Remove oldest (first inserted)

        self._cache[key] = CacheEntry(
            key=key,
            value=value,
            ttl_seconds=ttl_seconds or self._default_ttl,
        )

    def invalidate(self, commodity: Optional[str] = None) -> int:
        """Invalidate cache entries.

        Args:
            commodity: If provided, invalidate only entries for this commodity.
                       If None, invalidates the entire cache (full flush).

        Returns:
            Number of entries invalidated.
        """
        if commodity is None:
            count = len(self._cache)
            self._cache.clear()
            logger.info("cache_flushed", entries_cleared=count)
            return count

        # Find keys for this commodity
        prefix = f":{commodity}:"
        keys_to_remove = [k for k in self._cache if prefix in k]
        for key in keys_to_remove:
            del self._cache[key]

        logger.info("cache_invalidated", commodity=commodity, entries_cleared=len(keys_to_remove))
        return len(keys_to_remove)

    def invalidate_on_ingestion(self, commodity: str) -> int:
        """Invalidate cache after new COT data is ingested.

        Removes both 'latest' and date-specific entries for the commodity.

        Args:
            commodity: Contract symbol that received new data.

        Returns:
            Number of entries invalidated.
        """
        # Invalidate all signal types for this commodity
        total_removed = 0
        for signal_type in ("positioning", "smart_money", "retail_contrarian"):
            total_removed += self.invalidate(commodity)
            # Also invalidate with the commodity prefix approach
        prefix = f":{commodity}:"
        keys_to_remove = [k for k in self._cache if prefix in k]
        for key in keys_to_remove:
            del self._cache[key]
        total_removed += len(keys_to_remove)
        return total_removed

    @property
    def size(self) -> int:
        """Current number of entries in the cache."""
        return len(self._cache)

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0 to 1.0)."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    def stats(self) -> dict:
        """Return cache statistics."""
        return {
            "size": self.size,
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 4),
            "default_ttl": self._default_ttl,
        }

    def cleanup_expired(self) -> int:
        """Remove all expired entries from the cache.

        Returns:
            Number of entries removed.
        """
        expired_keys = [k for k, v in self._cache.items() if v.is_expired]
        for key in expired_keys:
            del self._cache[key]
        if expired_keys:
            logger.info("cache_cleanup", expired_removed=len(expired_keys))
        return len(expired_keys)


# ---------------------------------------------------------------------------
# Module-level cache instance (singleton)
# ---------------------------------------------------------------------------

_signal_cache: Optional[SignalCache] = None


def get_signal_cache() -> SignalCache:
    """Get or create the global signal cache instance.

    Returns:
        The singleton SignalCache instance.
    """
    global _signal_cache
    if _signal_cache is None:
        _signal_cache = SignalCache()
    return _signal_cache


def reset_signal_cache() -> None:
    """Reset the global signal cache. Used primarily in testing."""
    global _signal_cache
    _signal_cache = None