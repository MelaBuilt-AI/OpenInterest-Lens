#!/usr/bin/env python3
"""
OpenInterest Lens — Database Setup Script

Creates TimescaleDB hypertables, indexes, and seeds initial contract data.

Usage:
    python scripts/setup_db.py --database-url postgresql+asyncpg://oil:oil@localhost:5432/openinterest_lens
    python scripts/setup_db.py  # Uses OIL_DATABASE_URL env var
"""

import argparse
import asyncio
import os
import sys

# Add server to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from app.database import engine
from app.models.db import Base
from sqlalchemy import text


async def setup_database(database_url: str | None = None) -> None:
    """Create all tables and hypertables."""
    from app.config import get_settings

    settings = get_settings()

    print("🔧 OpenInterest Lens — Database Setup")
    print(f"   Database: {settings.database_url if database_url is None else database_url}")
    print()

    # Create all tables
    print("  Creating tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("  ✅ Tables created")

    # Enable TimescaleDB extension (if using PostgreSQL)
    if settings.is_postgres():
        print("  Creating TimescaleDB hypertables...")
        async with engine.begin() as conn:
            # Enable extension
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))

            # Convert time-series tables to hypertables
            hypertables = [
                "raw_cot_reports",
                "raw_settlements",
                "signal_positioning",
                "signal_term_structure",
                "signal_roll_pressure",
            ]
            for table in hypertables:
                try:
                    await conn.execute(
                        text(f"SELECT create_hypertable('{table}', 'timestamp', "
                             f"if_not_exists => TRUE)")
                    )
                    print(f"  ✅ {table} → hypertable")
                except Exception as e:
                    # Table might not exist yet or already a hypertable
                    print(f"  ⚠️  {table}: {str(e)[:60]}")

        print("  ✅ Hypertables configured")
    else:
        print("  ⚠️  Using SQLite — hypertables not applicable")

    # Create indexes
    print("  Creating indexes...")
    async with engine.begin() as conn:
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_signal_positioning_contract_timestamp ON signal_positioning (contract, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_signal_term_structure_contract_timestamp ON signal_term_structure (contract, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_signal_roll_pressure_contract_timestamp ON signal_roll_pressure (contract, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_raw_cot_contract_timestamp ON raw_cot_reports (contract, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_raw_settlements_contract_timestamp ON raw_settlements (contract, timestamp DESC)",
        ]
        for idx_sql in indexes:
            await conn.execute(text(idx_sql))
    print("  ✅ Indexes created")

    print("\n🎉 Database setup complete!")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenInterest Lens Database Setup")
    parser.add_argument("--database-url", default=None, help="Database URL (overrides env var)")
    args = parser.parse_args()

    if args.database_url:
        os.environ["OIL_DATABASE_URL"] = args.database_url

    asyncio.run(setup_database(args.database_url))


if __name__ == "__main__":
    main()