"""Tests for WebSocket endpoint, connection manager, and pub/sub integration.

Covers:
- WebSocket connection and authentication
- Subscribe/unsubscribe message handling
- Tier enforcement (free rejected, pro delayed, enterprise realtime)
- Heartbeat mechanism
- Connection cleanup on disconnect
- Redis pub/sub integration (mocked)
- Broadcasting to multiple connections
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.main import create_app
from app.middleware.auth import TIER_LIMITS, TierInfo
from app.services.redis_pubsub import (
    RedisPubSubManager,
    get_pubsub_manager,
    reset_pubsub_manager,
)
from app.services.ws_manager import (
    HEARTBEAT_TIMEOUT,
    TIER_UPDATE_FREQUENCIES,
    ConnectionManager,
    WSConnection,
    get_ws_manager,
    reset_ws_manager,
)
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global singletons before each test."""
    reset_ws_manager()
    reset_pubsub_manager()
    yield
    reset_ws_manager()
    reset_pubsub_manager()


@pytest.fixture
def app():
    """Create a fresh app instance for testing."""
    return create_app()


@pytest.fixture
def client(app):
    """Test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def manager():
    """Fresh ConnectionManager instance."""
    return ConnectionManager()


# ---------------------------------------------------------------------------
# Tier info fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def free_tier_info():
    return TierInfo(
        api_key_id=None,
        tier="free",
        user_id="test_free",
        contracts_allowed=None,
        limits=TIER_LIMITS["free"],
    )


@pytest.fixture
def pro_tier_info():
    return TierInfo(
        api_key_id=None,
        tier="pro",
        user_id="test_pro",
        contracts_allowed=None,
        limits=TIER_LIMITS["pro"],
    )


@pytest.fixture
def enterprise_tier_info():
    return TierInfo(
        api_key_id=None,
        tier="enterprise",
        user_id="test_enterprise",
        contracts_allowed=None,
        limits=TIER_LIMITS["enterprise"],
    )


# ---------------------------------------------------------------------------
# ConnectionManager unit tests
# ---------------------------------------------------------------------------


class TestConnectionManager:
    """Unit tests for ConnectionManager."""

    def test_tier_update_frequencies(self):
        """Verify tier-gated update frequencies are defined correctly."""
        assert TIER_UPDATE_FREQUENCIES["free"] == 0  # No WS access
        assert TIER_UPDATE_FREQUENCIES["pro"] == 900  # 15 minutes
        assert TIER_UPDATE_FREQUENCIES["enterprise"] == 0  # Real-time

    def test_ws_connection_is_subscribed_to(self, pro_tier_info):
        """Test WSConnection subscription checking."""
        conn = WSConnection(
            websocket=MagicMock(),
            tier_info=pro_tier_info,
        )

        # Not subscribed to anything
        assert not conn.is_subscribed_to("positioning", "ES")

        # Subscribe to all contracts for positioning
        conn.subscriptions["positioning"] = set()  # Empty = all contracts
        assert conn.is_subscribed_to("positioning", "ES")
        assert conn.is_subscribed_to("positioning", "NQ")

        # Subscribe to specific contracts for term_structure
        conn.subscriptions["term_structure"] = {"CL", "GC"}
        assert conn.is_subscribed_to("term_structure", "CL")
        assert not conn.is_subscribed_to("term_structure", "ES")

    def test_ws_connection_can_push_pro(self, pro_tier_info):
        """Pro tier has 15-minute delay between pushes."""
        conn = WSConnection(
            websocket=MagicMock(),
            tier_info=pro_tier_info,
        )

        # First push is always allowed
        assert conn.can_push("positioning")

        # Record a push
        conn.record_push("positioning")

        # Immediately after, pro should be rate-limited
        assert not conn.can_push("positioning")

        # Different signal type should still be allowed
        assert conn.can_push("term_structure")

    def test_ws_connection_can_push_enterprise(self, enterprise_tier_info):
        """Enterprise tier has real-time (immediate) push."""
        conn = WSConnection(
            websocket=MagicMock(),
            tier_info=enterprise_tier_info,
        )

        # Enterprise can always push
        assert conn.can_push("positioning")
        conn.record_push("positioning")
        assert conn.can_push("positioning")

    def test_ws_connection_can_push_free(self, free_tier_info):
        """Free tier should not have WS access (frequency = 0 means no limit, but
        they shouldn't connect at all)."""
        # Free tier has freq 0, which in the implementation means "real-time"
        # but they're rejected at connection time
        conn = WSConnection(
            websocket=MagicMock(),
            tier_info=free_tier_info,
        )
        # The freq of 0 means real-time in the can_push logic
        assert conn.can_push("positioning")

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self, manager, pro_tier_info):
        """Test basic connect and disconnect."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        assert conn_id is not None
        assert await manager.get_active_count() == 1

        # Connection should exist
        conn = manager.get_connection(conn_id)
        assert conn is not None
        assert conn.tier == "pro"

        # Disconnect
        await manager.disconnect(conn_id)
        assert await manager.get_active_count() == 0
        assert manager.get_connection(conn_id) is None

    @pytest.mark.asyncio
    async def test_subscribe(self, manager, pro_tier_info):
        """Test subscribing to signal types and contracts."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)

        # Subscribe to positioning for ES and NQ
        result = await manager.subscribe(
            conn_id, ["positioning"], ["ES", "NQ"]
        )
        assert result["action"] == "subscribed"
        assert "positioning" in result["signal_types"]

        # Check subscription
        subs = await manager.get_subscriptions(conn_id)
        assert "positioning" in subs
        assert "ES" in subs["positioning"]

    @pytest.mark.asyncio
    async def test_subscribe_all_contracts(self, manager, pro_tier_info):
        """Test subscribing to all contracts for a signal type."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)

        # Subscribe to all contracts
        result = await manager.subscribe(conn_id, ["positioning"])
        assert result["contracts"] == ["all"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, manager, pro_tier_info):
        """Test unsubscribing from signal types."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        await manager.subscribe(conn_id, ["positioning", "term_structure"], ["ES"])

        # Unsubscribe from positioning
        result = await manager.unsubscribe(conn_id, ["positioning"])
        assert result["action"] == "unsubscribed"

        subs = await manager.get_subscriptions(conn_id)
        assert "positioning" not in subs
        assert "term_structure" in subs

    @pytest.mark.asyncio
    async def test_unsubscribe_specific_contracts(self, manager, pro_tier_info):
        """Test unsubscribing from specific contracts."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        await manager.subscribe(conn_id, ["positioning"], ["ES", "NQ", "CL"])

        # Unsubscribe from ES only
        await manager.unsubscribe(conn_id, ["positioning"], ["ES"])

        subs = await manager.get_subscriptions(conn_id)
        assert "ES" not in subs["positioning"]
        assert "NQ" in subs["positioning"]

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(self, manager):
        """Disconnecting a non-existent connection should not raise."""
        await manager.disconnect("nonexistent_id")  # Should not raise

    @pytest.mark.asyncio
    async def test_subscribe_nonexistent_connection(self, manager):
        """Subscribing a non-existent connection returns error."""
        result = await manager.subscribe("nonexistent", ["positioning"])
        assert result.get("error") == "connection_not_found"

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_connection(self, manager):
        """Unsubscribing a non-existent connection returns error."""
        result = await manager.unsubscribe("nonexistent", ["positioning"])
        assert result.get("error") == "connection_not_found"

    @pytest.mark.asyncio
    async def test_broadcast_to_subscribed(self, manager, enterprise_tier_info):
        """Test broadcasting to connections subscribed to a signal type."""
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws1.send_text = AsyncMock()

        conn_id = await manager.connect(mock_ws1, enterprise_tier_info)
        await manager.subscribe(conn_id, ["positioning"])  # All contracts

        # Broadcast a signal update
        data = {"signal": "bullish", "strength": 0.85}
        count = await manager.broadcast("positioning", "ES", data)

        assert count == 1
        mock_ws1.send_text.assert_called_once()

        # Verify the message content
        sent_msg = json.loads(mock_ws1.send_text.call_args[0][0])
        assert sent_msg["type"] == "signal_update"
        assert sent_msg["signal_type"] == "positioning"
        assert sent_msg["contract"] == "ES"
        assert sent_msg["data"] == data

    @pytest.mark.asyncio
    async def test_broadcast_respects_subscriptions(self, manager, enterprise_tier_info):
        """Broadcasts only go to subscribed connections."""
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws1.send_text = AsyncMock()

        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()
        mock_ws2.send_text = AsyncMock()

        conn_id1 = await manager.connect(mock_ws1, enterprise_tier_info)
        await manager.connect(mock_ws2, enterprise_tier_info)

        # Only conn1 subscribes to positioning
        await manager.subscribe(conn_id1, ["positioning"])

        # Broadcast positioning
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 1

        # Broadcast term_structure — neither subscribed
        count = await manager.broadcast("term_structure", "ES", {"structure": "contango"})
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_respects_tier_gating_pro(self, manager, pro_tier_info):
        """Pro tier: 15-minute delay between pushes for same signal type."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        await manager.subscribe(conn_id, ["positioning"])

        # First push should succeed
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 1

        # Second push immediately after should be rate-limited
        count = await manager.broadcast("positioning", "ES", {"signal": "bearish"})
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_enterprise_realtime(self, manager, enterprise_tier_info):
        """Enterprise tier: no delay between pushes."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()

        conn_id = await manager.connect(mock_ws, enterprise_tier_info)
        await manager.subscribe(conn_id, ["positioning"])

        # Multiple pushes should all succeed
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 1

        count = await manager.broadcast("positioning", "ES", {"signal": "bearish"})
        assert count == 1

    @pytest.mark.asyncio
    async def test_broadcast_respects_tier_contract_access(self, manager, free_tier_info):
        """Broadcast skips connections whose tier can't access the contract."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()

        # Free tier can only access ES, NQ, CL
        conn_id = await manager.connect(mock_ws, free_tier_info)
        await manager.subscribe(conn_id, ["positioning"])

        # GC is not accessible for free tier
        count = await manager.broadcast("positioning", "GC", {"signal": "bullish"})
        assert count == 0

    @pytest.mark.asyncio
    async def test_heartbeat_update(self, manager, pro_tier_info):
        """Test heartbeat timestamp update."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        conn = manager.get_connection(conn_id)

        old_hb = conn.last_heartbeat
        # Small delay to ensure timestamp changes
        await asyncio.sleep(0.01)

        result = await manager.update_heartbeat(conn_id)
        assert result is True
        assert conn.last_heartbeat > old_hb

    @pytest.mark.asyncio
    async def test_heartbeat_nonexistent(self, manager):
        """Heartbeat for non-existent connection returns False."""
        result = await manager.update_heartbeat("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cleanup_stale(self, manager, pro_tier_info):
        """Test stale connection cleanup."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        conn = manager.get_connection(conn_id)

        # Set heartbeat to past
        conn.last_heartbeat = time.time() - HEARTBEAT_TIMEOUT - 10

        # Cleanup should remove stale connection
        removed = await manager.cleanup_stale()
        assert removed == 1
        assert await manager.get_active_count() == 0

    @pytest.mark.asyncio
    async def test_cleanup_no_stale(self, manager, pro_tier_info):
        """Fresh connections should not be cleaned up."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        await manager.connect(mock_ws, pro_tier_info)
        removed = await manager.cleanup_stale()
        assert removed == 0
        assert await manager.get_active_count() == 1

    @pytest.mark.asyncio
    async def test_send_to_connection(self, manager, pro_tier_info):
        """Test sending a message to a specific connection."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)

        message = {"type": "test", "data": "hello"}
        result = await manager.send_to_connection(conn_id, message)
        assert result is True
        mock_ws.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_connection(self, manager):
        """Sending to non-existent connection returns False."""
        result = await manager.send_to_connection("nonexistent", {"type": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_connections_broadcast(self, manager, enterprise_tier_info):
        """Test broadcasting to multiple connections."""
        connections = []
        for i in range(3):
            mock_ws = AsyncMock()
            mock_ws.accept = AsyncMock()
            mock_ws.send_text = AsyncMock()

            # Create slightly different tier_info for each
            tier = TierInfo(
                api_key_id=None,
                tier="enterprise",
                user_id=f"test_ent_{i}",
                contracts_allowed=None,
                limits=TIER_LIMITS["enterprise"],
            )
            conn_id = await manager.connect(mock_ws, tier)
            await manager.subscribe(conn_id, ["positioning"])
            connections.append(mock_ws)

        # Broadcast to all
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 3

        # Each connection should have received the message
        for mock_ws in connections:
            mock_ws.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_subscriptions(self, manager, pro_tier_info):
        """Test getting subscriptions for a connection."""
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()

        conn_id = await manager.connect(mock_ws, pro_tier_info)
        await manager.subscribe(conn_id, ["positioning"], ["ES", "NQ"])
        await manager.subscribe(conn_id, ["term_structure"])

        subs = await manager.get_subscriptions(conn_id)
        assert "positioning" in subs
        assert "ES" in subs["positioning"]
        assert "term_structure" in subs


# ---------------------------------------------------------------------------
# WebSocket endpoint tests
# ---------------------------------------------------------------------------


class TestWebSocketEndpoint:
    """Integration tests for the WebSocket endpoint using TestClient."""

    def test_ws_connect_with_pro_key(self, client):
        """Test WebSocket connection with a valid pro API key."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Should receive auth_success message
            data = ws.receive_json()
            assert data["type"] == "auth_success"
            assert data["tier"] == "pro"

    def test_ws_connect_with_enterprise_key(self, client):
        """Test WebSocket connection with an enterprise API key."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_enterprise") as ws:
            data = ws.receive_json()
            assert data["type"] == "auth_success"
            assert data["tier"] == "enterprise"

    def test_ws_reject_free_tier(self, client):
        """Free tier API keys should be rejected from WebSocket."""
        # TestClient raises an exception when the WebSocket closes immediately
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_free") as ws:
                ws.receive_json()

    def test_ws_reject_no_key(self, client):
        """Missing API key should be rejected."""
        # WebSocket accept happens before auth check in the "no query param" path,
        # but the auth timeout / message-based auth will close it
        with pytest.raises(Exception), client.websocket_connect("/ws/v1/signals"):
            # Connection accepted, but we don't send auth → timeout
            # In test, this may just hang — let's test with explicit rejection
            pass

    def test_ws_subscribe_message(self, client):
        """Test subscribing to signal types via WebSocket message."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success
            data = ws.receive_json()
            assert data["type"] == "auth_success"

            # Subscribe
            ws.send_json({
                "action": "subscribe",
                "signal_types": ["positioning", "term_structure"],
                "contracts": ["ES", "NQ"],
            })

            data = ws.receive_json()
            assert data["action"] == "subscribed"
            assert "positioning" in data["signal_types"]
            assert "term_structure" in data["signal_types"]

    def test_ws_unsubscribe_message(self, client):
        """Test unsubscribing from signal types via WebSocket message."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success
            ws.receive_json()

            # Subscribe first
            ws.send_json({
                "action": "subscribe",
                "signal_types": ["positioning"],
                "contracts": ["ES"],
            })
            ws.receive_json()

            # Unsubscribe
            ws.send_json({
                "action": "unsubscribe",
                "signal_types": ["positioning"],
            })

            data = ws.receive_json()
            assert data["action"] == "unsubscribed"

    def test_ws_ping_pong(self, client):
        """Test ping/pong heartbeat mechanism."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success
            ws.receive_json()

            # Send ping
            ws.send_json({"action": "ping"})

            # Receive pong
            data = ws.receive_json()
            assert data["type"] == "pong"

    def test_ws_invalid_json(self, client):
        """Test handling of invalid JSON messages."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success
            ws.receive_json()

            # Send invalid JSON
            ws.send_text("not json")

            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Invalid JSON" in data["message"]

    def test_ws_unknown_action(self, client):
        """Test handling of unknown action types."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success
            ws.receive_json()

            # Send unknown action
            ws.send_json({"action": "unknown_action"})

            data = ws.receive_json()
            assert data["type"] == "error"
            assert "Unknown action" in data["message"]

    def test_ws_duplicate_auth_message(self, client):
        """Test sending auth message when already authenticated."""
        with client.websocket_connect("/ws/v1/signals?api_key=oil_sk_live_demo_pro") as ws:
            # Auth success (from query param)
            ws.receive_json()

            # Send another auth message
            ws.send_json({"action": "auth", "api_key": "oil_sk_live_demo_pro"})

            data = ws.receive_json()
            assert data["type"] == "auth_success"
            assert "Already authenticated" in data["message"]


# ---------------------------------------------------------------------------
# RedisPubSubManager tests
# ---------------------------------------------------------------------------


class TestRedisPubSubManager:
    """Tests for Redis pub/sub manager."""

    @pytest.mark.asyncio
    async def test_publish_in_memory(self):
        """Test in-memory publish/subscribe without Redis."""
        manager = RedisPubSubManager(redis=None, ws_manager=None)
        received = []

        async def callback(signal_type, contract, data):
            received.append((signal_type, contract, data))

        manager.subscribe_in_memory(callback)
        count = await manager.publish("positioning", "ES", {"signal": "bullish"})

        assert count == 1
        assert len(received) == 1
        assert received[0][0] == "positioning"
        assert received[0][1] == "ES"
        assert received[0][2] == {"signal": "bullish"}

    @pytest.mark.asyncio
    async def test_unsubscribe_in_memory(self):
        """Test unsubscribing from in-memory callbacks."""
        manager = RedisPubSubManager(redis=None, ws_manager=None)
        received = []

        async def callback(signal_type, contract, data):
            received.append((signal_type, contract, data))

        manager.subscribe_in_memory(callback)
        manager.unsubscribe_in_memory(callback)

        count = await manager.publish("positioning", "ES", {"signal": "bullish"})
        assert count == 0
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_publish_with_redis(self):
        """Test publishing to Redis channel."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value=1)

        manager = RedisPubSubManager(redis=mock_redis, ws_manager=None)

        await manager.publish("positioning", "ES", {"signal": "bullish"})

        # Should have called Redis publish
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "oil:signals:positioning"

    @pytest.mark.asyncio
    async def test_publish_redis_error_fallback(self):
        """Test that publish falls back gracefully on Redis error."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("Redis connection error"))

        received = []

        async def callback(signal_type, contract, data):
            received.append((signal_type, contract, data))

        manager = RedisPubSubManager(redis=mock_redis, ws_manager=None)
        manager.subscribe_in_memory(callback)

        # Should not raise, and in-memory subscriber should still work
        count = await manager.publish("positioning", "ES", {"signal": "bullish"})
        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_start_without_redis(self):
        """Test starting pub/sub manager without Redis."""
        manager = RedisPubSubManager(redis=None, ws_manager=None)
        await manager.start()
        assert manager._running is True
        await manager.stop()

    @pytest.mark.asyncio
    async def test_handle_message(self):
        """Test handling a Redis pub/sub message."""
        mock_ws_manager = AsyncMock(spec=ConnectionManager)
        mock_ws_manager.broadcast = AsyncMock(return_value=2)

        manager = RedisPubSubManager(redis=None, ws_manager=mock_ws_manager)

        message = {
            "type": "message",
            "data": json.dumps({
                "signal_type": "positioning",
                "contract": "ES",
                "data": {"signal": "bullish"},
            }),
        }

        await manager._handle_message(message)

        # Should have called broadcast on the ws_manager
        mock_ws_manager.broadcast.assert_called_once()
        call_kwargs = mock_ws_manager.broadcast.call_args[1]
        assert call_kwargs["signal_type"] == "positioning"
        assert call_kwargs["contract"] == "ES"


# ---------------------------------------------------------------------------
# Module-level singleton tests
# ---------------------------------------------------------------------------


class TestSingletons:
    """Test module-level singleton management."""

    def test_get_ws_manager_creates_instance(self):
        manager = get_ws_manager()
        assert isinstance(manager, ConnectionManager)

    def test_get_ws_manager_returns_same_instance(self):
        manager1 = get_ws_manager()
        manager2 = get_ws_manager()
        assert manager1 is manager2

    def test_reset_ws_manager(self):
        manager1 = get_ws_manager()
        reset_ws_manager()
        manager2 = get_ws_manager()
        assert manager1 is not manager2

    def test_get_pubsub_manager_creates_instance(self):
        manager = get_pubsub_manager()
        assert isinstance(manager, RedisPubSubManager)

    def test_get_pubsub_manager_returns_same_instance(self):
        manager1 = get_pubsub_manager()
        manager2 = get_pubsub_manager()
        assert manager1 is manager2

    def test_reset_pubsub_manager(self):
        manager1 = get_pubsub_manager()
        reset_pubsub_manager()
        manager2 = get_pubsub_manager()
        assert manager1 is not manager2


# ---------------------------------------------------------------------------
# Broadcast integration test
# ---------------------------------------------------------------------------


class TestBroadcastIntegration:
    """Integration tests for broadcast with multiple connections."""

    @pytest.mark.asyncio
    async def test_broadcast_different_signal_types(self, enterprise_tier_info):
        """Test broadcasting different signal types to different subscriptions."""
        manager = ConnectionManager()

        # Connection 1: subscribed to positioning
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws1.send_text = AsyncMock()
        tier1 = TierInfo(
            api_key_id=None, tier="enterprise", user_id="user1",
            contracts_allowed=None, limits=TIER_LIMITS["enterprise"],
        )
        conn_id1 = await manager.connect(mock_ws1, tier1)
        await manager.subscribe(conn_id1, ["positioning"])

        # Connection 2: subscribed to term_structure
        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()
        mock_ws2.send_text = AsyncMock()
        tier2 = TierInfo(
            api_key_id=None, tier="enterprise", user_id="user2",
            contracts_allowed=None, limits=TIER_LIMITS["enterprise"],
        )
        conn_id2 = await manager.connect(mock_ws2, tier2)
        await manager.subscribe(conn_id2, ["term_structure"])

        # Broadcast positioning — only conn1 should receive
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 1
        mock_ws1.send_text.assert_called_once()
        mock_ws2.send_text.assert_not_called()

        # Broadcast term_structure — only conn2 should receive
        count = await manager.broadcast("term_structure", "ES", {"structure": "contango"})
        assert count == 1

    @pytest.mark.asyncio
    async def test_broadcast_with_stale_connection_cleanup(self, enterprise_tier_info):
        """Test that broadcast cleans up stale connections."""
        manager = ConnectionManager()

        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock(side_effect=Exception("Connection closed"))

        conn_id = await manager.connect(mock_ws, enterprise_tier_info)
        await manager.subscribe(conn_id, ["positioning"])

        # Broadcast should fail to send and clean up the connection
        count = await manager.broadcast("positioning", "ES", {"signal": "bullish"})
        assert count == 0

        # Connection should be removed
        assert manager.get_connection(conn_id) is None