"""FastAPI dependencies — auth, rate limiting, DB session injection."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.database import AsyncSession, get_db
from app.middleware.auth import APIKeyAuth, TierInfo
from app.middleware.rate_limit import check_rate_limit, is_rate_limited
from app.middleware.security import get_endpoint_rate_limit

_auth = APIKeyAuth()


async def require_api_key(
    request: Request,
    x_api_key: str = Header("", alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> TierInfo:
    """FastAPI dependency that validates the X-API-Key header and checks rate limits.

    Also stores tier_info in request.state for downstream use.
    Adds X-RateLimit-* headers to the response.
    """
    tier_info = await _auth.validate_key(x_api_key, settings)
    request.state.tier_info = tier_info

    # Check rate limit
    redis = getattr(request.app.state, "redis", None)
    remaining, limit, reset_time = await check_rate_limit(tier_info.tier, tier_info.user_id, redis)

    if is_rate_limited(remaining):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": f"Rate limit exceeded: {limit} requests per hour for {tier_info.tier} tier. Retry after {reset_time} seconds.",
                "retry_after": reset_time,
            },
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_time),
                "Retry-After": str(reset_time),
            },
        )

    # Store rate limit info on request state for response headers
    request.state.rate_limit_remaining = remaining
    request.state.rate_limit_limit = limit
    request.state.rate_limit_reset = reset_time

    # Check per-endpoint rate limit (stricter for expensive endpoints)
    endpoint_limit = get_endpoint_rate_limit(request.url.path, tier_info.tier)
    if endpoint_limit is not None and endpoint_limit < limit:
        # Use the more restrictive limit
        endpoint_remaining, endpoint_limit_val, endpoint_reset = await check_rate_limit(
            f"{tier_info.user_id}:{tier_info.tier}:{request.url.path}",
            endpoint_limit,
            redis,
        )
        if is_rate_limited(endpoint_remaining):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": f"Endpoint rate limit exceeded: {endpoint_limit} requests per hour for {tier_info.tier} tier on {request.url.path}. Retry after {endpoint_reset} seconds.",
                    "retry_after": endpoint_reset,
                },
                headers={
                    "X-RateLimit-Limit": str(endpoint_limit_val),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(endpoint_reset),
                    "Retry-After": str(endpoint_reset),
                },
            )

    return tier_info


async def require_pro_tier(
    tier_info: TierInfo = Depends(require_api_key),
) -> TierInfo:
    """Dependency that enforces Pro or Enterprise tier."""
    if tier_info.tier not in ("pro", "enterprise"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tier_limit_exceeded",
                "message": "This endpoint requires a Pro or Enterprise plan. Upgrade at https://openinterestlens.com/pricing",
            },
        )
    return tier_info


DBSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]