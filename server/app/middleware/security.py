"""Security module — API key rotation, CORS hardening, per-endpoint rate limits."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# API Key Rotation
# ---------------------------------------------------------------------------

# In-memory store for rotated keys with grace periods.
# In production, this would be a database table.
_rotated_keys: dict[str, _RotatedKeyInfo] = {}

GRACE_PERIOD_SECONDS = 3600  # 1 hour grace period for rotated keys


@dataclass
class _RotatedKeyInfo:
    """Track rotated keys during their grace period."""

    old_key: str
    new_key: str
    new_key_hash: str
    old_key_expires_at: float
    tier: str
    user_id: str


def generate_api_key(prefix: str = "oil_sk_live") -> str:
    """Generate a new API key with the given prefix."""
    token = secrets.token_urlsafe(32)
    return f"{prefix}_{token}"


def hash_api_key(key: str) -> str:
    """SHA-256 hash of an API key for secure storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def rotate_api_key(
    old_key: str,
    old_tier: str,
    old_user_id: str,
    prefix: str = "oil_sk_live",
    grace_period_seconds: int = GRACE_PERIOD_SECONDS,
) -> tuple[str, str]:
    """Rotate an API key, returning (new_key, new_key_hash).

    The old key remains valid for `grace_period_seconds` after rotation.
    """
    new_key = generate_api_key(prefix)
    new_hash = hash_api_key(new_key)

    # Store rotation info so old key still works during grace period
    _rotated_keys[old_key] = _RotatedKeyInfo(
        old_key=old_key,
        new_key=new_key,
        new_key_hash=new_hash,
        old_key_expires_at=time.time() + grace_period_seconds,
        tier=old_tier,
        user_id=old_user_id,
    )

    logger.info("api_key_rotated", old_prefix=old_key[:12], new_prefix=new_key[:12], grace_period_seconds=grace_period_seconds)

    return new_key, new_hash


def check_rotated_key(api_key: str) -> _RotatedKeyInfo | None:
    """Check if a key is a rotated key still within its grace period.

    Returns the rotation info if the key is valid during grace period,
    None otherwise.
    """
    info = _rotated_keys.get(api_key)
    if info is None:
        return None

    # Check if grace period has expired
    if time.time() > info.old_key_expires_at:
        # Grace period expired — remove from store
        del _rotated_keys[api_key]
        logger.info("rotated_key_expired", key_prefix=api_key[:12])
        return None

    return info


# ---------------------------------------------------------------------------
# Per-endpoint rate limit configuration
# ---------------------------------------------------------------------------

# Stricter rate limits for expensive endpoints
# Keys are endpoint path patterns; values are (requests_per_hour, burst_allowance)
ENDPOINT_RATE_LIMITS: dict[str, dict[str, int]] = {
    # Expensive data endpoints — lower limits
    "/v1/cot/{contract}": {"free": 20, "pro": 200, "enterprise": 3000},
    "/v1/settlements/{contract}": {"free": 20, "pro": 200, "enterprise": 3000},
    "/v1/roll-pressure/{contract}": {"free": 30, "pro": 300, "enterprise": 4000},
    "/v1/term-structure/{contract}": {"free": 30, "pro": 300, "enterprise": 4000},
    # Standard endpoints — default limits
    "/v1/signals/positioning": {"free": 40, "pro": 400, "enterprise": 5000},
    "/v1/signals/positioning/{commodity}": {"free": 40, "pro": 400, "enterprise": 5000},
    "/v1/contracts": {"free": 60, "pro": 600, "enterprise": 6000},
    "/v1/health": {"free": 120, "pro": 1200, "enterprise": 12000},
    "/v1/quality": {"free": 20, "pro": 200, "enterprise": 2000},
}


def get_endpoint_rate_limit(path: str, tier: str) -> int | None:
    """Get the per-endpoint rate limit for a request path and tier.

    Returns the requests-per-hour limit, or None if no per-endpoint limit applies.
    """
    # Try exact match first
    if path in ENDPOINT_RATE_LIMITS:
        return ENDPOINT_RATE_LIMITS[path].get(tier)

    # Try pattern match (replace dynamic segments)
    parts = path.rstrip("/").split("/")
    for pattern, limits in ENDPOINT_RATE_LIMITS.items():
        pattern_parts = pattern.rstrip("/").split("/")
        if len(parts) != len(pattern_parts):
            continue
        match = True
        for p_part, pat_part in zip(parts, pattern_parts, strict=False):
            if pat_part.startswith("{") and pat_part.endswith("}"):
                continue  # Dynamic segment matches anything
            if p_part != pat_part:
                match = False
                break
        if match:
            return limits.get(tier)

    return None


# ---------------------------------------------------------------------------
# CORS validation
# ---------------------------------------------------------------------------

def validate_cors_origin(origin: str, allowed_origins: list[str]) -> bool:
    """Validate that an origin is in the allowed list.

    Rejects wildcard '*' in production. Supports exact match and
    subdomain matching (e.g., '*.openinterestlens.com').
    """
    if not origin:
        return False

    for allowed in allowed_origins:
        if allowed == "*":
            # Wildcard only allowed in debug mode
            continue
        if origin == allowed:
            return True
        # Support subdomain patterns like *.openinterestlens.com
        if allowed.startswith("*."):
            domain = allowed[2:]
            if origin.endswith(f".{domain}") or origin == f"https://{domain}" or origin == f"http://{domain}":
                return True
        elif allowed.startswith("https://*.") or allowed.startswith("http://*."):
            # Pattern like https://*.openinterestlens.com
            scheme = allowed.split("://")[0]
            domain_suffix = allowed.split("*.")[1]
            if origin.startswith(f"{scheme}://") and origin.endswith(f".{domain_suffix}"):
                return True
            if origin == f"{scheme}://{domain_suffix}":
                return True

    return False