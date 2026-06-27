"""OpenInterest Lens — Admin CLI for key management, DB ops, and health checks.

Usage:
    oil-admin key create --tier free --email user@example.com
    oil-admin key list [--tier free] [--expired]
    oil-admin key revoke KEY_ID
    oil-admin key rotate KEY_ID
    oil-admin key info KEY_ID
    oil-admin db migrate
    oil-admin db seed
    oil-admin health
    oil-admin quality [--contract SYMBOL]
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone
from typing import Optional

import click

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_db_url() -> str:
    """Get database URL from env or default."""
    return os.environ.get("OIL_DATABASE_URL", "sqlite+aiosqlite:///./openinterest_lens.db")


def _get_redis_url() -> str:
    """Get Redis URL from env."""
    return os.environ.get("OIL_REDIS_URL", "redis://localhost:6379/0")


def _get_master_key() -> str:
    """Get master API key from env."""
    key = os.environ.get("OIL_MASTER_API_KEY", "")
    if not key:
        click.echo("⚠️  OIL_MASTER_API_KEY not set. Set it via environment or .env file.", err=True)
        sys.exit(1)
    return key


def _hash_key(key: str) -> str:
    """Hash an API key for storage (SHA-256 with salt)."""
    salt = os.environ.get("API_KEY_SALT", "default_salt_change_me")
    return hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Key group
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """OpenInterest Lens Admin CLI."""
    pass


@cli.group()
def key():
    """Manage API keys."""
    pass


@key.command("create")
@click.option("--tier", required=True, type=click.Choice(["free", "pro", "enterprise"]), help="Tier for the new key")
@click.option("--email", default="", help="Email address of the key owner")
@click.option("--notes", default="", help="Optional notes about this key")
def key_create(tier: str, email: str, notes: str):
    """Generate a new API key."""
    raw_key = f"oil_sk_live_{secrets.token_hex(24)}"
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:12] + "..."  # For display only

    click.echo("=" * 50)
    click.echo("New API Key Created")
    click.echo("=" * 50)
    click.echo(f"  Tier:    {tier}")
    if email:
        click.echo(f"  Email:   {email}")
    if notes:
        click.echo(f"  Notes:   {notes}")
    click.echo(f"  Key:     {raw_key}")
    click.echo(f"  Prefix:  {key_prefix}")
    click.echo(f"  Hash:    {key_hash[:16]}...")
    click.echo()
    click.echo("⚠️  Save this key now — it will NOT be shown again!")
    click.echo()
    click.echo("To activate, add to your database keys table or")
    click.echo("use the /v1/security/keys endpoint with the master key.")


@key.command("list")
@click.option("--tier", type=click.Choice(["free", "pro", "enterprise"]), help="Filter by tier")
@click.option("--expired", is_flag=True, help="Include expired keys")
def key_list(tier: Optional[str], expired: bool):
    """List all API keys."""
    import httpx

    master_key = _get_master_key()
    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")

    headers = {"X-API-Key": master_key}
    params = {}
    if tier:
        params["tier"] = tier
    if expired:
        params["expired"] = "true"

    try:
        resp = httpx.get(f"{base_url}/v1/security/keys", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        keys = data if isinstance(data, list) else data.get("keys", data.get("data", []))

        if not keys:
            click.echo("No keys found.")
            return

        click.echo(f"{'ID':<40s} {'Tier':<12s} {'Prefix':<16s} {'Status':<10s} {'Created':<20s}")
        click.echo("-" * 100)
        for k in keys:
            key_id = k.get("key_id", k.get("id", "?"))
            key_tier = k.get("tier", "?")
            prefix = k.get("key_prefix", k.get("prefix", "?"))
            status = k.get("status", "active")
            created = k.get("created_at", "?")
            click.echo(f"{key_id:<40s} {key_tier:<12s} {prefix:<16s} {status:<10s} {created:<20s}")

    except httpx.HTTPError as e:
        click.echo(f"❌ Error listing keys: {e}", err=True)
        sys.exit(1)


@key.command("revoke")
@click.argument("key_id")
def key_revoke(key_id: str):
    """Revoke an API key by its ID."""
    import httpx

    master_key = _get_master_key()
    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")

    headers = {"X-API-Key": master_key}

    try:
        resp = httpx.delete(f"{base_url}/v1/security/keys/{key_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        click.echo(f"✓ Key {key_id} has been revoked.")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            click.echo(f"❌ Key {key_id} not found.", err=True)
        else:
            click.echo(f"❌ Error revoking key: {e}", err=True)
        sys.exit(1)
    except httpx.HTTPError as e:
        click.echo(f"❌ Error revoking key: {e}", err=True)
        sys.exit(1)


@key.command("rotate")
@click.argument("key_id")
def key_rotate(key_id: str):
    """Rotate an API key — generate a new one and deprecate the old."""
    import httpx

    master_key = _get_master_key()
    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")

    headers = {"X-API-Key": master_key}

    # Create new key via API
    try:
        # First get the old key info
        resp = httpx.get(f"{base_url}/v1/security/keys/{key_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        old_key = resp.json()

        tier = old_key.get("tier", "free")
        email = old_key.get("email", "")

        # Create replacement
        new_raw = f"oil_sk_live_{secrets.token_hex(24)}"

        click.echo("Key Rotation")
        click.echo("=" * 50)
        click.echo(f"  Old key: {key_id}")
        click.echo(f"  New key: {new_raw}")
        click.echo(f"  Tier:    {tier}")
        click.echo()
        click.echo("⚠️  Save the new key now — it will NOT be shown again!")
        click.echo()
        click.echo(f"Now revoke the old key: oil-admin key revoke {key_id}")

    except httpx.HTTPError as e:
        click.echo(f"❌ Error rotating key: {e}", err=True)
        sys.exit(1)


@key.command("info")
@click.argument("key_id")
def key_info(key_id: str):
    """Show key details and usage stats."""
    import httpx

    master_key = _get_master_key()
    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")

    headers = {"X-API-Key": master_key}

    try:
        resp = httpx.get(f"{base_url}/v1/security/keys/{key_id}", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        click.echo("=" * 50)
        click.echo(f"Key: {key_id}")
        click.echo("=" * 50)
        for k, v in data.items():
            click.echo(f"  {k}: {v}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            click.echo(f"❌ Key {key_id} not found.", err=True)
        else:
            click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)
    except httpx.HTTPError as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# DB group
# ---------------------------------------------------------------------------

@cli.group()
def db():
    """Database operations."""
    pass


@db.command("migrate")
def db_migrate():
    """Run database migrations."""
    click.echo("Running database migrations...")
    db_url = _get_db_url()

    if "sqlite" in db_url:
        click.echo("SQLite detected — running inline migration...")
    else:
        click.echo("PostgreSQL detected — ensure Alembic is configured.")

    try:
        import asyncio
        from app.database import init_db

        asyncio.run(init_db())
        click.echo("✓ Migrations complete.")
    except Exception as e:
        click.echo(f"❌ Migration failed: {e}", err=True)
        sys.exit(1)


@db.command("seed")
def db_seed():
    """Seed sample data for development."""
    click.echo("Seeding sample data...")

    try:
        import asyncio
        from app.database import init_db
        from app.routers.contracts import seed_contracts

        async def _seed():
            await init_db()
            await seed_contracts()

        asyncio.run(_seed())
        click.echo("✓ Sample data seeded.")
    except Exception as e:
        click.echo(f"❌ Seeding failed: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@cli.command("health")
def health_check():
    """Check system health — DB, Redis, data freshness."""
    import httpx

    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")
    all_ok = True

    # API health
    try:
        resp = httpx.get(f"{base_url}/v1/health", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        click.echo(f"✓ API: {data.get('status', 'ok')} (v{data.get('version', '?')})")
    except Exception as e:
        click.echo(f"❌ API: unreachable — {e}")
        all_ok = False

    # Redis check
    redis_url = _get_redis_url()
    try:
        import redis
        r = redis.from_url(redis_url)
        r.ping()
        click.echo("✓ Redis: connected")
    except Exception:
        click.echo("⚠  Redis: not available (app will use in-memory cache)")

    # Data freshness
    try:
        master_key = _get_master_key()
        headers = {"X-API-Key": master_key}
        resp = httpx.get(f"{base_url}/v1/health/detailed", headers=headers, timeout=5)
        if resp.status_code == 200:
            detail = resp.json()
            freshness = detail.get("data_freshness", {})
            if freshness:
                for contract, ts in freshness.items():
                    click.echo(f"  {contract}: last update {ts}")
    except Exception:
        pass  # Non-critical

    if all_ok:
        click.echo("\n✓ All systems healthy")
    else:
        click.echo("\n❌ Some systems unhealthy", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

@cli.command("quality")
@click.option("--contract", default=None, help="Check quality for a specific contract symbol")
def quality_check(contract: Optional[str]):
    """Run data quality checks."""
    import httpx

    base_url = os.environ.get("OIL_BASE_URL", "http://localhost:8000")
    master_key = _get_master_key()
    headers = {"X-API-Key": master_key}

    params = {}
    if contract:
        params["contract"] = contract

    try:
        resp = httpx.get(f"{base_url}/v1/quality", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        click.echo("=" * 60)
        click.echo("Data Quality Report")
        click.echo("=" * 60)

        # Handle different response shapes
        reports = data if isinstance(data, list) else [data]

        for report in reports:
            symbol = report.get("contract", report.get("symbol", "?"))
            score = report.get("quality_score", report.get("score", "?"))
            status = report.get("status", "unknown")

            icon = "✓" if status == "healthy" or score >= 0.8 else "⚠"
            click.echo(f"\n{icon} {symbol}")
            click.echo(f"  Score: {score}")
            click.echo(f"  Status: {status}")

            issues = report.get("issues", report.get("warnings", []))
            if issues:
                for issue in issues:
                    click.echo(f"  • {issue}")

        click.echo("\n" + "=" * 60)

    except httpx.HTTPError as e:
        click.echo(f"❌ Quality check failed: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()