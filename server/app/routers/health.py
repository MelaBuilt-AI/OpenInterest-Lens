"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Health check endpoint — returns API status and version."""
    return {
        "status": "ok",
        "service": "openinterest-lens",
        "version": "0.1.0",
    }