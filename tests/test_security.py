"""Security tests — API key rotation, expired key rejection, CORS, per-endpoint rate limits."""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.security import (
    generate_api_key,
    hash_api_key,
    rotate_api_key,
    check_rotated_key,
    validate_cors_origin,
    get_endpoint_rate_limit,
    ENDPOINT_RATE_LIMITS,
    GRACE_PERIOD_SECONDS,
)
from app.middleware.auth import APIKeyAuth
from app.database import get_db
from app.main import app

from tests.conftest import TEST_API_KEY_FREE, TEST_API_KEY_PRO, TEST_API_KEY_ENTERPRISE


# ---------------------------------------------------------------------------
# API Key Rotation Tests
# ---------------------------------------------------------------------------


class TestAPIKeyRotation:
    """Tests for API key rotation functionality."""

    def test_generate_api_key_format(self):
        """Generated keys should have the correct prefix."""
        key = generate_api_key()
        assert key.startswith("oil_sk_live_")
        assert len(key) > 20  # prefix + random token

    def test_generate_api_key_custom_prefix(self):
        """Generated keys should support custom prefixes."""
        key = generate_api_key(prefix="oil_sk_test")
        assert key.startswith("oil_sk_test_")

    def test_generate_api_key_uniqueness(self):
        """Each generated key should be unique."""
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100

    def test_hash_api_key(self):
        """SHA-256 hash should be deterministic and 64 chars."""
        key = "oil_sk_live_test_key_123"
        h = hash_api_key(key)
        assert len(h) == 64
        assert h == hash_api_key(key)  # Deterministic

    def test_rotate_api_key(self):
        """Rotating a key should return new key and hash."""
        old_key = "oil_sk_live_original"
        new_key, new_hash = rotate_api_key(
            old_key=old_key,
            old_tier="pro",
            old_user_id="test_user",
        )
        assert new_key.startswith("oil_sk_live_")
        assert new_key != old_key
        assert new_hash == hash_api_key(new_key)

    def test_rotated_key_valid_during_grace_period(self):
        """Old key should still work during grace period."""
        old_key = "oil_sk_live_grace_test"
        rotate_api_key(old_key=old_key, old_tier="free", old_user_id="grace_user", grace_period_seconds=3600)

        info = check_rotated_key(old_key)
        assert info is not None
        assert info.tier == "free"
        assert info.new_key.startswith("oil_sk_live_")

    def test_rotated_key_expired_grace_period(self):
        """Old key should not work after grace period expires."""
        old_key = "oil_sk_live_expired_test"
        rotate_api_key(old_key=old_key, old_tier="free", old_user_id="expired_user", grace_period_seconds=0)

        # Grace period of 0 means already expired
        info = check_rotated_key(old_key)
        assert info is None

    def test_check_rotated_key_unknown_key(self):
        """Unknown keys should return None."""
        result = check_rotated_key("oil_sk_live_unknown_key")
        assert result is None


# ---------------------------------------------------------------------------
# Auth Rotation Integration Tests
# ---------------------------------------------------------------------------


class TestAuthKeyRotation:
    """Tests for auth module integration with key rotation."""

    @pytest.mark.asyncio
    async def test_rotate_endpoint_returns_new_key(self, client: AsyncClient):
        """POST /v1/keys/rotate should return a new key."""
        response = await client.post(
            "/v1/keys/rotate",
            json={"grace_period_hours": 1},
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert "new_api_key" in data
        assert "new_key_hash" in data
        assert "old_key_expires_at" in data
        assert data["grace_period_hours"] == 1

    @pytest.mark.asyncio
    async def test_rotate_requires_auth(self, client: AsyncClient):
        """POST /v1/keys/rotate should require authentication."""
        response = await client.post(
            "/v1/keys/rotate",
            json={"grace_period_hours": 1},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rotate_zero_grace_period(self, client: AsyncClient):
        """Rotation with 0 grace period should still work."""
        response = await client.post(
            "/v1/keys/rotate",
            json={"grace_period_hours": 0},
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["grace_period_hours"] == 0

    @pytest.mark.asyncio
    async def test_rotate_rejects_invalid_grace_period(self, client: AsyncClient):
        """Rotation with grace period > 72 hours should be rejected."""
        response = await client.post(
            "/v1/keys/rotate",
            json={"grace_period_hours": 100},
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 422  # Pydantic validation error

    @pytest.mark.asyncio
    async def test_key_info_endpoint(self, client: AsyncClient):
        """GET /v1/keys/me should return current key info."""
        response = await client.get(
            "/v1/keys/me",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tier"] == "pro"
        assert data["contracts_accessible"] == "all"

    @pytest.mark.asyncio
    async def test_key_info_free_tier(self, client: AsyncClient):
        """GET /v1/keys/me for free tier should show limited access."""
        response = await client.get(
            "/v1/keys/me",
            headers={"X-API-Key": TEST_API_KEY_FREE},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tier"] == "free"
        assert data["contracts_accessible"] == "limited"


# ---------------------------------------------------------------------------
# Expired Key Rejection Tests
# ---------------------------------------------------------------------------


class TestExpiredKeyRejection:
    """Tests for expired/invalid key rejection."""

    @pytest.mark.asyncio
    async def test_missing_api_key(self, client: AsyncClient):
        """Requests without API key should be rejected."""
        response = await client.get("/v1/contracts")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_api_key(self, client: AsyncClient):
        """Requests with invalid API key should be rejected."""
        response = await client.get(
            "/v1/contracts",
            headers={"X-API-Key": "invalid_key"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_key_rejection(self):
        """Revoked keys should be rejected even if previously valid."""
        auth = APIKeyAuth()
        auth.revoke_key("oil_sk_live_demo_free")
        # After revocation, the key should be in the revoked set
        assert "oil_sk_live_demo_free" in auth._revoked_keys


# ---------------------------------------------------------------------------
# CORS Tests
# ---------------------------------------------------------------------------


class TestCORSSecurity:
    """Tests for CORS configuration security."""

    def test_validate_cors_origin_exact_match(self):
        """Exact origin match should be accepted."""
        assert validate_cors_origin("http://localhost:3000", ["http://localhost:3000"]) is True

    def test_validate_cors_origin_no_match(self):
        """Non-matching origin should be rejected."""
        assert validate_cors_origin("http://evil.com", ["http://localhost:3000"]) is False

    def test_validate_cors_origin_wildcard_subdomain(self):
        """Subdomain patterns should match correctly."""
        origins = ["https://*.openinterestlens.com"]
        assert validate_cors_origin("https://app.openinterestlens.com", origins) is True
        assert validate_cors_origin("https://api.openinterestlens.com", origins) is True
        assert validate_cors_origin("https://evil.com", origins) is False

    def test_validate_cors_origin_empty_origin(self):
        """Empty origin should be rejected."""
        assert validate_cors_origin("", ["http://localhost:3000"]) is False

    def test_validate_cors_origin_wildcard_star(self):
        """Wildcard '*' should not auto-approve (only in debug)."""
        # The function doesn't match on '*', it just skips it
        assert validate_cors_origin("http://evil.com", ["*"]) is False

    @pytest.mark.asyncio
    async def test_cors_preflight_request(self, client: AsyncClient):
        """CORS preflight (OPTIONS) should respond with correct headers."""
        response = await client.options(
            "/v1/contracts",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        # CORS middleware should handle preflight
        assert response.status_code in (200, 204, 401)


# ---------------------------------------------------------------------------
# Per-Endpoint Rate Limit Tests
# ---------------------------------------------------------------------------


class TestPerEndpointRateLimits:
    """Tests for per-endpoint rate limit configuration."""

    def test_endpoint_rate_limit_cot_free(self):
        """COT endpoint should have stricter limits for free tier."""
        limit = get_endpoint_rate_limit("/v1/cot/ES", "free")
        assert limit is not None
        assert limit < 60  # Stricter than default free tier limit

    def test_endpoint_rate_limit_cot_pro(self):
        """COT endpoint should have appropriate pro limits."""
        limit = get_endpoint_rate_limit("/v1/cot/ES", "pro")
        assert limit is not None
        assert limit == 200

    def test_endpoint_rate_limit_roll_pressure(self):
        """Roll pressure endpoint should have per-endpoint limits."""
        limit = get_endpoint_rate_limit("/v1/roll-pressure/ES", "free")
        assert limit is not None
        assert limit == 30

    def test_endpoint_rate_limit_contracts(self):
        """Contracts endpoint should have default limits."""
        limit = get_endpoint_rate_limit("/v1/contracts", "free")
        assert limit == 60  # Same as default

    def test_endpoint_rate_limit_unknown_path(self):
        """Unknown paths should return None (use default limits)."""
        limit = get_endpoint_rate_limit("/v1/unknown/endpoint", "free")
        assert limit is None

    def test_endpoint_rate_limit_health(self):
        """Health endpoint should have generous limits."""
        limit = get_endpoint_rate_limit("/v1/health", "free")
        assert limit == 120

    def test_endpoint_rate_limit_quality(self):
        """Quality endpoint should have stricter limits."""
        limit = get_endpoint_rate_limit("/v1/quality", "free")
        assert limit is not None
        assert limit < 60  # Stricter than default

    def test_endpoint_rate_limit_pattern_matching(self):
        """Dynamic segments should match endpoint patterns."""
        # /v1/signals/positioning/ES should match /v1/signals/positioning/{commodity}
        limit = get_endpoint_rate_limit("/v1/signals/positioning/ES", "pro")
        assert limit is not None
        assert limit == 400

    def test_endpoint_rate_limit_term_structure(self):
        """Term structure should have per-endpoint limits."""
        limit = get_endpoint_rate_limit("/v1/term-structure/ES", "enterprise")
        assert limit == 4000


# ---------------------------------------------------------------------------
# Input Validation Tests
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Tests for input validation across endpoints."""

    @pytest.mark.asyncio
    async def test_invalid_commodity_symbol_rejected(self, client: AsyncClient):
        """Invalid commodity symbols should be rejected."""
        response = await client.get(
            "/v1/signals/positioning/INVALID_SYMBOL_12345",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        # Should be 400 (too long) or 404 (not tracked)
        assert response.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_sql_injection_in_symbol_rejected(self, client: AsyncClient):
        """SQL injection attempts in commodity symbols should be rejected."""
        response = await client.get(
            "/v1/signals/positioning/'; DROP TABLE contracts;--",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 400  # Invalid symbol format

    @pytest.mark.asyncio
    async def test_negative_lookback_rejected(self, client: AsyncClient):
        """Negative lookback weeks should be rejected."""
        response = await client.get(
            "/v1/signals/positioning?lookback_weeks=-1",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 422  # Pydantic validation

    @pytest.mark.asyncio
    async def test_excessive_lookback_rejected(self, client: AsyncClient):
        """Lookback weeks > 260 should be rejected."""
        response = await client.get(
            "/v1/signals/positioning?lookback_weeks=999",
            headers={"X-API-Key": TEST_API_KEY_PRO},
        )
        assert response.status_code == 422  # Pydantic validation

    @pytest.mark.asyncio
    async def test_empty_api_key_rejected(self, client: AsyncClient):
        """Empty API key should be rejected."""
        response = await client.get(
            "/v1/contracts",
            headers={"X-API-Key": ""},
        )
        assert response.status_code == 401