"""WebSocket connection manager for OpenInterest Lens.

Manages WebSocket connections, subscriptions, and message broadcasting.
Thread-safe with asyncio locks. Supports tier-gated update frequency.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import WebSocket

from app.middleware.auth import TierInfo

logger = structlog.get_logger(__name__)

# Tier-gated update frequencies (seconds)
TIER_UPDATE_FREQUENCIES: dict[str, float] = {
    "free": 0,  # No WS access
    "pro": 900,  # 15 minutes
    "enterprise": 0,  # Real-time (immediate push)
}

# Heartbeat interval
HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 60  # seconds — disconnect after this


@dataclass
class WSConnection:
    """Represents a single WebSocket connection with its subscriptions."""

    websocket: WebSocket
    tier_info: TierInfo
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    subscriptions: dict[str, set[str]] = field(default_factory=dict)
    # subscriptions maps signal_type -> set of contract symbols
    # e.g. {"positioning": {"ES", "NQ"}, "term_structure": {"CL"}}
    last_push: dict[str, float] = field(default_factory=dict)
    # last_push maps signal_type -> timestamp of last push (for tier gating)

    @property
    def user_id(self) -> str:
        return self.tier_info.user_id

    @property
    def tier(self) -> str:
        return self.tier_info.tier

    def is_subscribed_to(self, signal_type: str, contract: str) -> bool:
        """Check if this connection is subscribed to a specific signal type + contract."""
        contracts = self.subscriptions.get(signal_type)
        if contracts is None:
            return False
        # Empty set means subscribed to all contracts for that signal type
        if not contracts:
            return True
        return contract in contracts

    def can_push(self, signal_type: str) -> bool:
        """Check if enough time has elapsed for tier-gated push frequency."""
        freq = TIER_UPDATE_FREQUENCIES.get(self.tier, 900)
        if freq == 0:
            return True  # Real-time

        last = self.last_push.get(signal_type, 0)
        return (time.time() - last) >= freq

    def record_push(self, signal_type: str) -> None:
        """Record that a push was made for tier gating."""
        self.last_push[signal_type] = time.time()


class ConnectionManager:
    """Manages all WebSocket connections for OpenInterest Lens.

    Thread-safe via asyncio lock. Supports:
    - Connection lifecycle (connect/disconnect)
    - Per-connection subscriptions (signal types + contracts)
    - Tier-gated broadcasting
    - Heartbeat tracking
    - Stale connection cleanup
    """

    def __init__(self) -> None:
        self._connections: dict[str, WSConnection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None

    async def connect(self, websocket: WebSocket, tier_info: TierInfo) -> str:
        """Accept a WebSocket connection and register it.

        Returns the connection ID for tracking.
        """
        await websocket.accept()
        conn_id = f"{tier_info.user_id}:{id(websocket)}"
        conn = WSConnection(websocket=websocket, tier_info=tier_info)

        async with self._lock:
            self._connections[conn_id] = conn

        logger.info(
            "ws_connected",
            conn_id=conn_id,
            tier=tier_info.tier,
            user_id=tier_info.user_id,
        )
        return conn_id

    async def disconnect(self, conn_id: str) -> None:
        """Remove a connection and clean up subscriptions."""
        async with self._lock:
            conn = self._connections.pop(conn_id, None)

        if conn:
            logger.info(
                "ws_disconnected",
                conn_id=conn_id,
                tier=conn.tier,
                user_id=conn.user_id,
            )

    async def subscribe(
        self, conn_id: str, signal_types: list[str], contracts: list[str] | None = None
    ) -> dict[str, Any]:
        """Subscribe a connection to signal types and optionally specific contracts.

        Args:
            conn_id: Connection identifier.
            signal_types: List of signal types to subscribe to.
            contracts: Optional list of contract symbols. If empty/None, subscribes to all.

        Returns:
            Confirmation dict with subscribed signal types and contracts.
        """
        async with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return {"error": "connection_not_found"}

            for st in signal_types:
                contract_set = conn.subscriptions.get(st, set())
                if contracts:
                    contract_set.update(c.upper() for c in contracts)
                else:
                    contract_set.clear()  # Empty set = all contracts
                conn.subscriptions[st] = contract_set

        logger.info(
            "ws_subscribed",
            conn_id=conn_id,
            signal_types=signal_types,
            contracts=contracts,
        )

        return {
            "action": "subscribed",
            "signal_types": signal_types,
            "contracts": contracts or ["all"],
        }

    async def unsubscribe(
        self, conn_id: str, signal_types: list[str], contracts: list[str] | None = None
    ) -> dict[str, Any]:
        """Unsubscribe a connection from signal types or specific contracts.

        If contracts is None, unsubscribes entirely from those signal types.
        If contracts is given, removes only those specific contracts.
        """
        async with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return {"error": "connection_not_found"}

            for st in signal_types:
                if contracts:
                    sub = conn.subscriptions.get(st, set())
                    for c in contracts:
                        sub.discard(c.upper())
                else:
                    conn.subscriptions.pop(st, None)

        logger.info(
            "ws_unsubscribed",
            conn_id=conn_id,
            signal_types=signal_types,
            contracts=contracts,
        )

        return {
            "action": "unsubscribed",
            "signal_types": signal_types,
            "contracts": contracts or ["all"],
        }

    async def broadcast(
        self, signal_type: str, contract: str, data: dict, timestamp: float | None = None
    ) -> int:
        """Broadcast a signal update to all eligible connections.

        Only pushes to connections that:
        - Are subscribed to the signal type
        - Are subscribed to the contract (or subscribed to all)
        - Have tier access to the contract
        - Haven't been rate-limited by their tier's update frequency

        Args:
            signal_type: Type of signal (positioning, term_structure, etc.)
            contract: Contract symbol (ES, NQ, CL, etc.)
            data: Signal payload dict.
            timestamp: Signal timestamp for tier gating (defaults to now).

        Returns:
            Number of connections the message was sent to.
        """
        sent_count = 0
        stale_connections: list[str] = []

        async with self._lock:
            for conn_id, conn in list(self._connections.items()):
                # Check subscription
                if not conn.is_subscribed_to(signal_type, contract):
                    continue

                # Check tier access to contract
                if not conn.tier_info.can_access_contract(contract):
                    continue

                # Check tier-gated push frequency
                if not conn.can_push(signal_type):
                    continue

                # Check signal type access
                if not conn.tier_info.can_access_signal_type(signal_type):
                    # Pro+ can access all; free shouldn't have WS at all
                    continue

                try:
                    message = json.dumps({
                        "type": "signal_update",
                        "signal_type": signal_type,
                        "contract": contract,
                        "data": data,
                        "timestamp": timestamp or time.time(),
                    })
                    await conn.websocket.send_text(message)
                    conn.record_push(signal_type)
                    sent_count += 1
                except Exception as exc:
                    logger.warning(
                        "ws_broadcast_send_failed",
                        conn_id=conn_id,
                        error=str(exc),
                    )
                    stale_connections.append(conn_id)

        # Clean up stale connections
        for conn_id in stale_connections:
            await self.disconnect(conn_id)

        return sent_count

    async def send_to_connection(self, conn_id: str, message: dict) -> bool:
        """Send a message to a specific connection.

        Returns True if sent successfully, False otherwise.
        """
        async with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return False

        try:
            await conn.websocket.send_text(json.dumps(message))
            return True
        except Exception:
            await self.disconnect(conn_id)
            return False

    def get_connection(self, conn_id: str) -> WSConnection | None:
        """Get a connection by ID (non-async, for read-only checks)."""
        return self._connections.get(conn_id)

    async def get_active_count(self) -> int:
        """Return number of active connections."""
        async with self._lock:
            return len(self._connections)

    async def get_subscriptions(self, conn_id: str) -> dict:
        """Get current subscriptions for a connection."""
        async with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return {}
            return {
                st: sorted(contracts) if contracts else ["all"]
                for st, contracts in conn.subscriptions.items()
            }

    async def update_heartbeat(self, conn_id: str) -> bool:
        """Update the heartbeat timestamp for a connection.

        Returns True if the connection exists, False otherwise.
        """
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn:
                conn.last_heartbeat = time.time()
                return True
            return False

    async def cleanup_stale(self) -> int:
        """Remove connections that haven't sent a heartbeat within the timeout.

        Returns the number of stale connections removed.
        """
        now = time.time()
        stale_ids: list[str] = []

        async with self._lock:
            for conn_id, conn in list(self._connections.items()):
                if (now - conn.last_heartbeat) > HEARTBEAT_TIMEOUT:
                    stale_ids.append(conn_id)

        for conn_id in stale_ids:
            await self.disconnect(conn_id)

        if stale_ids:
            logger.info("ws_cleanup_stale", count=len(stale_ids))

        return len(stale_ids)

    async def start_heartbeat(self) -> None:
        """Start the periodic heartbeat task that pings all connections."""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        """Stop the heartbeat task."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """Periodically send ping to all connections and clean up stale ones."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self._send_heartbeats()
                await self.cleanup_stale()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ws_heartbeat_error", error=str(exc))

    async def _send_heartbeats(self) -> None:
        """Send a ping message to all active connections."""
        stale_ids: list[str] = []

        async with self._lock:
            for conn_id, conn in list(self._connections.items()):
                try:
                    await conn.websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    stale_ids.append(conn_id)

        for conn_id in stale_ids:
            await self.disconnect(conn_id)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: ConnectionManager | None = None


def get_ws_manager() -> ConnectionManager:
    """Get or create the global ConnectionManager instance."""
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


def reset_ws_manager() -> None:
    """Reset the global ConnectionManager. Used primarily in testing."""
    global _manager
    _manager = None