"""Security API router — API key management endpoints.

Endpoints:
- POST /v1/keys/rotate — rotate an API key
- GET /v1/keys/me — get current key info
"""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.dependencies import require_api_key
from app.middleware.auth import TierInfo
from app.middleware.security import (
    rotate_api_key,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/keys", tags=["keys"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RotateKeyRequest(BaseModel):
    """Request body for key rotation."""

    grace_period_hours: int = Field(
        default=1,
        ge=0,
        le=72,
        description="Hours the old key remains valid after rotation (0 = immediate revocation).",
    )


class RotateKeyResponse(BaseModel):
    """Response for key rotation."""

    new_api_key: str = Field(..., description="The newly generated API key. Store this securely — it won't be shown again.")
    new_key_hash: str = Field(..., description="SHA-256 hash of the new key for verification.")
    old_key_expires_at: str = Field(..., description="ISO 8601 timestamp when the old key stops working.")
    grace_period_hours: int = Field(..., description="Grace period in hours.")


class KeyInfoResponse(BaseModel):
    """Response for key info."""

    tier: str
    user_id: str
    key_prefix: str
    contracts_accessible: str  # "all" or "limited"
    rate_limit: int


# ---------------------------------------------------------------------------
# POST /v1/keys/rotate — rotate an API key
# ---------------------------------------------------------------------------


@router.post("/rotate", response_model=RotateKeyResponse)
async def rotate_key(
    request: RotateKeyRequest,
    tier_info: TierInfo = Depends(require_api_key),
):
    """Rotate the current API key.

    Generates a new key and deprecates the current key after a configurable
    grace period (0–72 hours, default 1 hour). During the grace period,
    both old and new keys are valid.

    Requires authentication with the current key.
    """
    grace_seconds = request.grace_period_hours * 3600

    new_key, new_hash = rotate_api_key(
        old_key=tier_info.user_id,  # Use user_id as identifier
        old_tier=tier_info.tier,
        old_user_id=tier_info.user_id,
        grace_period_seconds=grace_seconds,
    )

    expires_at = time.time() + grace_seconds

    return RotateKeyResponse(
        new_api_key=new_key,
        new_key_hash=new_hash,
        old_key_expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at)),
        grace_period_hours=request.grace_period_hours,
    )


# ---------------------------------------------------------------------------
# GET /v1/keys/me — get current key info
# ---------------------------------------------------------------------------


@router.get("/me", response_model=KeyInfoResponse)
async def get_key_info(
    tier_info: TierInfo = Depends(require_api_key),
):
    """Get information about the current API key."""
    return KeyInfoResponse(
        tier=tier_info.tier,
        user_id=tier_info.user_id,
        key_prefix=tier_info.user_id[:12] if len(tier_info.user_id) > 12 else tier_info.user_id,
        contracts_accessible="all" if tier_info.tier in ("pro", "enterprise") else "limited",
        rate_limit=tier_info.limits.get("rate_limit", 60),
    )