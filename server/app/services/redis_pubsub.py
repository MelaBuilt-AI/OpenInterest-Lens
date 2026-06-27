"""Redis pub/sub integration for WebSocket signal push.

Subscribes to Redis channels for signal updates and pushes them
to relevant WebSocket connections via the ConnectionManager.
Falls back to in-memory pub/sub when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import structlog

logger = structlog.get_logger(__name__)

# Redis channel pattern: oil:signals:{signal_type}
SIGNAL_CHANNEL_PREFIX = "oil:signals"


class RedisPubSubManager:
    """Manages Redis pub/sub for real-time signal updates.

    When a signal update is published to Redis (e.g., after data ingestion),
    this manager receives it and pushes it to the appropriate WebSocket
    connections via the ConnectionManager.

    Falls back to an in-memory broadcast when Redis is unavailable.
    """

    def __init__(self, redis=None, ws_manager=None) -> None:
        self._redis = redis
        self._ws_manager = ws_manager
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._in_memory_subscribers: list[callable] = []
        self._running = False

    async def start(self) -> None:
        """Start listening for Redis pub/sub messages."""
        if self._redis is None:
            logger.info("pubsub_no_redis_using_in_memory")
            self._running = True
            return

        try:
            self._pubsub = self._redis.pubsub()
            # Subscribe to all signal channels
            channels = [
                f"{SIGNAL_CHANNEL_PREFIX}:positioning",
                f"{SIGNAL_CHANNEL_PREFIX}:term_structure",
                f"{SIGNAL_CHANNEL_PREFIX}:roll_pressure",
                f"{SIGNAL_CHANNEL_PREFIX}:cot",
                f"{SIGNAL_CHANNEL_PREFIX}:contango_alert",
            ]
            await self._pubsub.subscribe(*channels)
            self._running = True
            self._listener_task = asyncio.create_task(self._listen_loop())
            logger.info("pubsub_started", channels=channels)
        except Exception as exc:
            logger.warning("pubsub_start_failed", error=str(exc))
            self._running = True  # Continue with in-memory fallback

    async def stop(self) -> None:
        """Stop listening for Redis pub/sub messages."""
        self._running = False

        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

        if self._pubsub:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

        logger.info("pubsub_stopped")

    async def publish(self, signal_type: str, contract: str, data: dict) -> int:
        """Publish a signal update to Redis (or in-memory subscribers).

        Args:
            signal_type: Type of signal (positioning, term_structure, etc.)
            contract: Contract symbol (ES, NQ, CL, etc.)
            data: Signal payload dict.

        Returns:
            Number of subscribers that received the message.
        """
        channel = f"{SIGNAL_CHANNEL_PREFIX}:{signal_type}"
        message = json.dumps({
            "signal_type": signal_type,
            "contract": contract,
            "data": data,
        })

        count = 0

        # Publish to Redis
        if self._redis:
            try:
                count = await self._redis.publish(channel, message)
            except Exception as exc:
                logger.warning("pubsub_redis_publish_failed", error=str(exc))

        # Also notify in-memory subscribers (for when Redis isn't available)
        for callback in self._in_memory_subscribers:
            try:
                await callback(signal_type, contract, data)
                count += 1
            except Exception as exc:
                logger.warning("pubsub_memory_callback_failed", error=str(exc))

        return count

    def subscribe_in_memory(self, callback: callable) -> None:
        """Register an in-memory subscriber callback.

        The callback should accept (signal_type, contract, data) arguments.
        Used when Redis is unavailable or for testing.
        """
        self._in_memory_subscribers.append(callback)

    def unsubscribe_in_memory(self, callback: callable) -> None:
        """Remove an in-memory subscriber callback."""
        self._in_memory_subscribers = [
            cb for cb in self._in_memory_subscribers if cb != callback
        ]

    async def _listen_loop(self) -> None:
        """Listen for Redis pub/sub messages and push to WebSocket connections."""
        if not self._pubsub:
            return

        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message["type"] == "message":
                    await self._handle_message(message)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("pubsub_listen_error", error=str(exc))
                await asyncio.sleep(1)  # Back off on error

    async def _handle_message(self, message: dict) -> None:
        """Handle a Redis pub/sub message and push to WebSocket connections."""
        try:
            data = json.loads(message["data"])
            signal_type = data.get("signal_type")
            contract = data.get("contract")
            payload = data.get("data", {})

            if not signal_type or not contract:
                logger.warning("pubsub_invalid_message", message=data)
                return

            if self._ws_manager:
                count = await self._ws_manager.broadcast(
                    signal_type=signal_type,
                    contract=contract,
                    data=payload,
                )
                logger.debug(
                    "pubsub_pushed_to_connections",
                    signal_type=signal_type,
                    contract=contract,
                    connections=count,
                )
        except json.JSONDecodeError:
            logger.warning("pubsub_json_decode_error", raw=message.get("data"))
        except Exception as exc:
            logger.error("pubsub_handle_error", error=str(exc))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_pubsub_manager: RedisPubSubManager | None = None


def get_pubsub_manager(redis=None, ws_manager=None) -> RedisPubSubManager:
    """Get or create the global RedisPubSubManager instance."""
    global _pubsub_manager
    if _pubsub_manager is None:
        _pubsub_manager = RedisPubSubManager(redis=redis, ws_manager=ws_manager)
    return _pubsub_manager


def reset_pubsub_manager() -> None:
    """Reset the global pub/sub manager. Used primarily in testing."""
    global _pubsub_manager
    _pubsub_manager = None