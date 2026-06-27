"""Tests for GET /v1/contracts endpoint."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_API_KEY_FREE, TEST_API_KEY_PRO, TEST_API_KEY_ENTERPRISE


@pytest.mark.asyncio
async def test_list_contracts_free_tier(client: AsyncClient):
    """Free tier should see only ES, NQ, CL (not GC)."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_FREE},
    )
    assert response.status_code == 200
    data = response.json()
    symbols = [c["symbol"] for c in data["contracts"]]
    assert "ES" in symbols
    assert "NQ" in symbols
    assert "CL" in symbols
    assert "GC" not in symbols


@pytest.mark.asyncio
async def test_list_contracts_pro_tier(client: AsyncClient):
    """Pro tier should see all 4 contracts."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["contracts"]) == 4
    symbols = [c["symbol"] for c in data["contracts"]]
    assert set(symbols) == {"ES", "NQ", "CL", "GC"}


@pytest.mark.asyncio
async def test_list_contracts_enterprise_tier(client: AsyncClient):
    """Enterprise tier should see all 4 contracts."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_ENTERPRISE},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["contracts"]) == 4


@pytest.mark.asyncio
async def test_list_contracts_filter_exchange(client: AsyncClient):
    """Filtering by exchange should work."""
    response = await client.get(
        "/v1/contracts?exchange=CME",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200
    data = response.json()
    for contract in data["contracts"]:
        assert contract["exchange"] == "CME"


@pytest.mark.asyncio
async def test_list_contracts_filter_asset_class(client: AsyncClient):
    """Filtering by asset class should work."""
    response = await client.get(
        "/v1/contracts?asset_class=energy",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["contracts"]) == 1
    assert data["contracts"][0]["symbol"] == "CL"


@pytest.mark.asyncio
async def test_contract_metadata_fields(client: AsyncClient):
    """Each contract should have all required metadata fields."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    assert response.status_code == 200
    for contract in response.json()["contracts"]:
        assert "symbol" in contract
        assert "exchange" in contract
        assert "asset_class" in contract
        assert "full_name" in contract
        assert "tick_size" in contract
        assert "contract_size" in contract
        assert "months_traded" in contract
        assert "data_available_from" in contract
        assert "signals_available" in contract
        assert isinstance(contract["months_traded"], list)
        assert isinstance(contract["signals_available"], list)


@pytest.mark.asyncio
async def test_es_contract_details(client: AsyncClient):
    """Verify ES contract has expected metadata."""
    response = await client.get(
        "/v1/contracts",
        headers={"X-API-Key": TEST_API_KEY_PRO},
    )
    contracts = response.json()["contracts"]
    es = next(c for c in contracts if c["symbol"] == "ES")
    assert es["exchange"] == "CME"
    assert es["asset_class"] == "equity_index"
    assert es["full_name"] == "E-mini S&P 500"
    assert es["tick_size"] == 0.25
    assert es["contract_size"] == 50
    assert "H" in es["months_traded"]
    assert "positioning" in es["signals_available"]