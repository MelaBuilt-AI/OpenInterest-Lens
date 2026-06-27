"""Background ingestion scheduler using asyncio.

Schedules:
- COT fetches: weekly, Friday evening (8:00 PM ET)
- Settlement fetches: daily, after CME close (5:30 PM ET)

Configurable intervals via environment variables.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from app.ingestion.cot import ingest_cot_reports
from app.ingestion.settlements import ingest_settlements

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default schedule intervals (in seconds)
DEFAULT_COT_INTERVAL = 7 * 24 * 3600  # 1 week
DEFAULT_SETTLEMENT_INTERVAL = 24 * 3600  # 1 day

# COT runs Friday at 8 PM ET = Saturday 00:00 UTC
COT_DAY_OF_WEEK = 4  # Friday (0=Monday)
COT_HOUR_UTC = 0  # Saturday 00:00 UTC = Friday 8 PM ET
COT_MINUTE_UTC = 0

# Settlements run daily at 5:30 PM ET = 21:30 UTC
SETTLEMENT_HOUR_UTC = 21
SETTLEMENT_MINUTE_UTC = 30


@dataclass
class IngestionRun:
    """Record of a single ingestion run."""

    source: str  # "cot" or "settlements"
    started_at: datetime
    completed_at: datetime | None = None
    status: str = "running"  # running, success, partial, failed
    records_ingested: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SchedulerState:
    """Mutable scheduler state tracking runs and timing."""

    started_at: datetime | None = None
    cot_last_run: datetime | None = None
    cot_last_result: IngestionRun | None = None
    settlement_last_run: datetime | None = None
    settlement_last_result: IngestionRun | None = None
    is_running: bool = False
    cot_task: asyncio.Task | None = None
    settlement_task: asyncio.Task | None = None


class IngestionScheduler:
    """Async background scheduler for COT and settlement data ingestion.

    Uses asyncio tasks to schedule periodic fetches:
    - COT: weekly, timed for Friday evening ET
    - Settlements: daily, timed for after CME close

    Also exposes methods for manual triggering via the API.
    """

    def __init__(
        self,
        cot_interval: int = DEFAULT_COT_INTERVAL,
        settlement_interval: int = DEFAULT_SETTLEMENT_INTERVAL,
        auto_start: bool = False,
    ) -> None:
        self.cot_interval = cot_interval
        self.settlement_interval = settlement_interval
        self.state = SchedulerState()
        self._shutdown = False
        self._cot_periodic_task: asyncio.Task | None = None
        self._settlement_periodic_task: asyncio.Task | None = None

        if auto_start:
            self.start()

    def start(self) -> None:
        """Start the scheduler — launch periodic tasks."""
        if self.state.is_running:
            logger.warning("scheduler_already_running")
            return

        self._shutdown = False
        self.state.started_at = datetime.now(UTC)
        self.state.is_running = True

        # Launch periodic tasks
        self._cot_periodic_task = asyncio.create_task(self._cot_periodic())
        self._settlement_periodic_task = asyncio.create_task(self._settlement_periodic())

        logger.info(
            "scheduler_started",
            cot_interval=self.cot_interval,
            settlement_interval=self.settlement_interval,
        )

    async def stop(self) -> None:
        """Stop the scheduler and cancel all periodic tasks."""
        self._shutdown = True
        self.state.is_running = False

        # Cancel periodic tasks
        for task in (self._cot_periodic_task, self._settlement_periodic_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        logger.info("scheduler_stopped")

    # -----------------------------------------------------------------------
    # Manual triggers (called from API)
    # -----------------------------------------------------------------------

    async def trigger_cot_ingestion(self) -> IngestionRun:
        """Manually trigger a COT data fetch.

        Returns the IngestionRun record with results.
        """
        run = IngestionRun(source="cot", started_at=datetime.now(UTC))

        logger.info("cot_ingestion_triggered")
        try:
            result = await ingest_cot_reports(report_type="futures")
            run.status = result.status
            run.records_ingested = result.reports_ingested
            run.records_skipped = result.reports_skipped
            run.errors = result.errors
        except Exception as e:
            run.status = "failed"
            run.errors = [str(e)]
            logger.error("cot_ingestion_error", error=str(e), exc_info=True)

        run.completed_at = datetime.now(UTC)
        self.state.cot_last_run = run.started_at
        self.state.cot_last_result = run

        logger.info(
            "cot_ingestion_complete",
            status=run.status,
            ingested=run.records_ingested,
            skipped=run.records_skipped,
            errors=len(run.errors),
        )

        return run

    async def trigger_settlement_ingestion(self, symbols: list[str] | None = None) -> IngestionRun:
        """Manually trigger a settlement data fetch.

        Args:
            symbols: Optional list of contract symbols. Defaults to all.

        Returns the IngestionRun record with results.
        """
        run = IngestionRun(source="settlements", started_at=datetime.now(UTC))

        logger.info("settlement_ingestion_triggered", symbols=symbols)
        try:
            result = await ingest_settlements(symbols=symbols)
            run.status = result.status
            run.records_ingested = result.settlements_ingested
            run.records_skipped = result.settlements_skipped
            run.errors = result.errors
        except Exception as e:
            run.status = "failed"
            run.errors = [str(e)]
            logger.error("settlement_ingestion_error", error=str(e), exc_info=True)

        run.completed_at = datetime.now(UTC)
        self.state.settlement_last_run = run.started_at
        self.state.settlement_last_result = run

        logger.info(
            "settlement_ingestion_complete",
            status=run.status,
            ingested=run.records_ingested,
            skipped=run.records_skipped,
            errors=len(run.errors),
        )

        return run

    # -----------------------------------------------------------------------
    # Periodic tasks
    # -----------------------------------------------------------------------

    async def _cot_periodic(self) -> None:
        """Periodic COT ingestion task.

        Calculates the next Friday 8PM ET and sleeps until then,
        then triggers the COT ingestion.
        """
        while not self._shutdown:
            try:
                # Calculate time until next scheduled run
                next_run = self._next_cot_run_time()
                now = datetime.now(UTC)
                delay = (next_run - now).total_seconds()

                if delay > 0:
                    logger.info("cot_next_run_scheduled", next_run=next_run.isoformat(), delay_seconds=delay)
                    await asyncio.sleep(min(delay, 60))  # Wake every 60s to check for shutdown
                    # Recalculate after sleep
                    continue

                # Time to run
                await self.trigger_cot_ingestion()

                # Sleep for the interval before next check
                await asyncio.sleep(60)  # Check again in 1 minute

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("cot_periodic_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)  # Back off 5 minutes on error

    async def _settlement_periodic(self) -> None:
        """Periodic settlement ingestion task.

        Runs daily at 5:30 PM ET (21:30 UTC).
        """
        while not self._shutdown:
            try:
                next_run = self._next_settlement_run_time()
                now = datetime.now(UTC)
                delay = (next_run - now).total_seconds()

                if delay > 0:
                    logger.info("settlement_next_run_scheduled", next_run=next_run.isoformat(), delay_seconds=delay)
                    await asyncio.sleep(min(delay, 60))
                    continue

                await self.trigger_settlement_ingestion()
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("settlement_periodic_error", error=str(e), exc_info=True)
                await asyncio.sleep(300)

    def _next_cot_run_time(self) -> datetime:
        """Calculate the next COT run time (Friday 8 PM ET = Saturday 00:00 UTC)."""
        now = datetime.now(UTC)
        # Find next Saturday 00:00 UTC (which is Friday 8 PM ET)
        days_until_saturday = (COT_DAY_OF_WEEK + 1 - now.weekday()) % 7
        if days_until_saturday == 0 and now.hour >= COT_HOUR_UTC:
            days_until_saturday = 7  # Already past this week's run time

        next_run = now.replace(hour=COT_HOUR_UTC, minute=COT_MINUTE_UTC, second=0, microsecond=0) + timedelta(days=days_until_saturday)
        return next_run

    def _next_settlement_run_time(self) -> datetime:
        """Calculate the next settlement run time (daily 5:30 PM ET = 21:30 UTC)."""
        now = datetime.now(UTC)
        target = now.replace(hour=SETTLEMENT_HOUR_UTC, minute=SETTLEMENT_MINUTE_UTC, second=0, microsecond=0)

        # If we've already passed today's time, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        return target

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current scheduler status for the API."""
        now = datetime.now(UTC)
        uptime = (now - self.state.started_at).total_seconds() if self.state.started_at else 0

        return {
            "is_running": self.state.is_running,
            "started_at": self.state.started_at.isoformat() if self.state.started_at else None,
            "uptime_seconds": uptime,
            "cot": {
                "source": "cot",
                "last_run": self.state.cot_last_run.isoformat() if self.state.cot_last_run else None,
                "last_status": self.state.cot_last_result.status if self.state.cot_last_result else "never_run",
                "last_records_ingested": self.state.cot_last_result.records_ingested if self.state.cot_last_result else 0,
                "last_errors": self.state.cot_last_result.errors if self.state.cot_last_result else [],
                "next_scheduled": self._next_cot_run_time().isoformat() if self.state.is_running else None,
            },
            "settlements": {
                "source": "settlements",
                "last_run": self.state.settlement_last_run.isoformat() if self.state.settlement_last_run else None,
                "last_status": self.state.settlement_last_result.status if self.state.settlement_last_result else "never_run",
                "last_records_ingested": self.state.settlement_last_result.records_ingested if self.state.settlement_last_result else 0,
                "last_errors": self.state.settlement_last_result.errors if self.state.settlement_last_result else [],
                "next_scheduled": self._next_settlement_run_time().isoformat() if self.state.is_running else None,
            },
        }


# ---------------------------------------------------------------------------
# Global scheduler instance
# ---------------------------------------------------------------------------

_scheduler: IngestionScheduler | None = None


def get_scheduler() -> IngestionScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = IngestionScheduler(auto_start=False)
    return _scheduler