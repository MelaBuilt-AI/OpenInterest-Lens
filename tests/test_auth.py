"""Tests for API key authentication middleware."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_API_KEY_ENTERPRISE, TEST_API_KEY_FREE, TEST_API_KEY_PRO


@pytest.mark.asyncio
async def test_no_api_key_returns_401(client: AsyncClient):
    """Request without X-API-Key header should return 401."""
    response = await client.get("/v1/contracts")
    assert response.status_code == 401
    data = response.json()
    assert data["detail"]["error"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_invalid_api_key_returns_401(client: AsyncClient):
    """Request with invalid API key should return 401."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": "invalid_key_12345"},
    )
    assert response.status_code == 401
    data = response.json()
    assert data["detail"]["error"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_free_tier_key_works(client: AsyncClient):
    """Free tier key should authenticate successfully."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_FREE},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pro_tier_key_works(client: AsyncClient):
    """Pro tier key should authenticate successfully."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_enterprise_tier_key_works(client: AsyncClient):
    """Enterprise tier key should authenticate successfully."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_ENTERPRISE},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_free_tier_cannot_access_gc(client: AsyncClient):
    """Free tier should not see GC (Gold) — only ES, NQ, CL."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_FREE},
    )
    assert response.status_code == 200
    symbols = [c["symbol"] for c in response.json()["contracts"]]
    assert "GC" not in symbols


@pytest.mark.asyncio
async def test_pro_tier_can_access_all_contracts(client: AsyncClient):
    """Pro tier should see all 4 contracts including GC."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200
    symbols = [c["symbol"] for c in response.json()["contracts"]]
    assert "GC" in symbols


@pytest.mark.asyncio
async def test_properly_formatted_unknown_key_accepted_as_free(client: AsyncClient):
    """A key with the correct prefix format but unknown value should be accepted as free tier."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": "oil_sk_live_unknown_user_key"},
    )
    assert response.status_code == 200
    # Should be free tier — only 3 contracts
    symbols = [c["symbol"] for c in response.json()["contracts"]]
    assert "GC" not in symbols


@pytest.mark.asyncio
async def test_health_endpoint_no_auth_required(client: AsyncClient):
    """Health check should work without authentication."""
    response = await client.get("/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "openinterest-lens"