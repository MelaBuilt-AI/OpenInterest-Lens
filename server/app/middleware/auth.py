"""API key authentication middleware for OpenInterest Lens.

Validates X-API-Key header, resolves tier, and enforces tier-based access.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Literal, Optional

import structlog
from fastapi import HTTPException, status

from app.config import Settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

TIER_LIMITS = {
    "free": {
        "max_contracts": ["ES", "NQ", "CL"],
        "update_frequency": "daily",
        "history_weeks": 4,
        "websocket": False,
        "rate_limit": 60,
        "signals": ["positioning"],
        "term_structure": "current_only",
    },
    "pro": {
        "max_contracts": 50,
        "update_frequency": "15min",
        "history_weeks": 104,
        "websocket": True,
        "rate_limit": 600,
        "signals": "all",
        "term_structure": "historical",
    },
    "enterprise": {
        "max_contracts": float("inf"),
        "update_frequency": "realtime",
        "history_weeks": 260,
        "websocket": True,
        "rate_limit": 6000,
        "signals": "all",
        "term_structure": "historical_and_futures",
    },
}

# Known test/demo keys — in production these come from the database
_DEMO_KEYS = {
    "oil_sk_live_demo_free": {
        "tier": "free",
        "user_id": "demo_free",
        "contracts_allowed": None,
    },
    "oil_sk_live_demo_pro": {
        "tier": "pro",
        "user_id": "demo_pro",
        "contracts_allowed": None,
    },
    "oil_sk_live_demo_enterprise": {
        "tier": "enterprise",
        "user_id": "demo_enterprise",
        "contracts_allowed": None,
    },
}


@dataclass
class TierInfo:
    """Resolved tier information for an authenticated request."""

    api_key_id: Optional[int]
    tier: str
    user_id: str
    contracts_allowed: Optional[list] = None
    limits: dict = field(default_factory=dict)

    def can_access_contract(self, symbol: str) -> bool:
        """Check if this tier can access the given contract symbol."""
        tier_limits = TIER_LIMITS.get(self.tier, {})
        max_contracts = tier_limits.get("max_contracts", [])

        # If it's a list, it's an allowlist (free tier)
        if isinstance(max_contracts, list):
            return symbol in max_contracts

        # Numeric or inf means count-based — check contracts_allowed if set
        if self.contracts_allowed is not None:
            return symbol in self.contracts_allowed

        # No restriction
        return True

    def can_access_signal_type(self, signal_type: str) -> bool:
        """Check if this tier can access the given signal type."""
        tier_limits = TIER_LIMITS.get(self.tier, {})
        allowed_signals = tier_limits.get("signals", [])

        if isinstance(allowed_signals, str) and allowed_signals == "all":
            return True

        return signal_type in allowed_signals


class APIKeyAuth:
    """Validates API keys and resolves tier information.

    In production, keys are stored hashed in the database.
    For dev/testing, demo keys are accepted.
    Supports rotated keys during their grace period.
    """

    def __init__(self) -> None:
        self._key_cache: dict[str, TierInfo] = {}
        self._revoked_keys: set[str] = set()  # Explicitly revoked keys

    @staticmethod
    def _hash_key(key: str) -> str:
        """SHA-256 hash of the API key for secure storage."""
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _key_prefix(key: str) -> str:
        """Extract the identifiable prefix (first 12 chars)."""
        return key[:12]

    def revoke_key(self, api_key: str) -> None:
        """Revoke a key immediately (no grace period)."""
        self._revoked_keys.add(api_key)
        self._key_cache.pop(api_key, None)
        logger.info("api_key_revoked", prefix=self._key_prefix(api_key))

    async def validate_key(self, api_key: str, settings: Settings) -> TierInfo:
        """Validate an API key and return tier info.

        Raises HTTPException(401) if the key is missing, invalid, or revoked.
        Checks rotated keys during their grace period.
        """
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_api_key", "message": "Missing X-API-Key header"},
            )

        # Check if key has been explicitly revoked
        if api_key in self._revoked_keys:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "api_key_revoked", "message": "This API key has been revoked."},
            )

        # Check cache first
        if api_key in self._key_cache:
            return self._key_cache[api_key]

        # Check rotated key grace period
        from app.middleware.security import check_rotated_key
        rotated = check_rotated_key(api_key)
        if rotated is not None:
            info = TierInfo(
                api_key_id=None,
                tier=rotated.tier,
                user_id=rotated.user_id,
                contracts_allowed=None,
                limits=TIER_LIMITS.get(rotated.tier, TIER_LIMITS["free"]),
            )
            self._key_cache[api_key] = info
            return info

        # Check master key
        if api_key == settings.master_api_key:
            info = TierInfo(
                api_key_id=None,
                tier="enterprise",
                user_id="master",
                contracts_allowed=None,
                limits=TIER_LIMITS["enterprise"],
            )
            self._key_cache[api_key] = info
            return info

        # Check demo keys
        if api_key in _DEMO_KEYS:
            demo = _DEMO_KEYS[api_key]
            tier = demo["tier"]
            info = TierInfo(
                api_key_id=None,
                tier=tier,
                user_id=demo["user_id"],
                contracts_allowed=demo.get("contracts_allowed"),
                limits=TIER_LIMITS[tier],
            )
            self._key_cache[api_key] = info
            return info

        # In production: look up hashed key in database
        # For Week 1: accept any properly-formatted key as free tier
        if api_key.startswith("oil_sk_live_" ) or api_key.startswith("oil_sk_test_"):
            logger.info("unknown_api_key_accepted_as_free", prefix=self._key_prefix(api_key))
            info = TierInfo(
                api_key_id=None,
                tier="free",
                user_id=f"user_{self._key_prefix(api_key)}",
                contracts_allowed=None,
                limits=TIER_LIMITS["free"],
            )
            self._key_cache[api_key] = info
            return info

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "Invalid API key"},
        )