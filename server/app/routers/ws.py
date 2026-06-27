"""WebSocket router for OpenInterest Lens.

Endpoints:
- WS /ws/v1/signals — main WebSocket endpoint for real-time signal updates

Authentication:
- API key via query parameter `api_key` (e.g. ws://host/ws/v1/signals?api_key=oil_sk_live_demo_pro)
- Or via first message: {"action": "auth", "api_key": "..."}

Message protocol:
- Client → Server:
  - {"action": "auth", "api_key": "..."} — authenticate
  - {"action": "subscribe", "signal_types": [...], "contracts": [...]} — subscribe
  - {"action": "unsubscribe", "signal_types": [...], "contracts": [...]} — unsubscribe
  - {"action": "ping"} — heartbeat

- Server → Client:
  - {"type": "auth_success", "tier": "...", "user_id": "..."}
  - {"type": "auth_error", "message": "..."}
  - {"type": "subscribed", "signal_types": [...], "contracts": [...]}
  - {"type": "unsubscribed", "signal_types": [...], "contracts": [...]}
  - {"type": "signal_update", "signal_type": "...", "contract": "...", "data": {...}}
  - {"type": "pong"} — heartbeat response
  - {"type": "error", "message": "..."}

Tier enforcement:
- Free: WebSocket connections are rejected (403 on upgrade)
- Pro: 15-minute delayed signals
- Enterprise: Real-time signals
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.config import Settings, get_settings
from app.middleware.auth import TIER_LIMITS, APIKeyAuth, TierInfo
from app.services.ws_manager import (
    get_ws_manager,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ws/v1", tags=["websocket"])

_auth = APIKeyAuth()


async def _authenticate_ws(
    api_key: str, settings: Settings
) -> tuple[TierInfo | None, str | None]:
    """Validate an API key for WebSocket authentication.

    Returns (tier_info, None) on success, or (None, error_message) on failure.
    """
    if not api_key:
        return None, "Missing API key. Provide via query parameter or first message."

    try:
        tier_info = await _auth.validate_key(api_key, settings)
    except Exception as exc:
        error_msg = str(exc)
        # Extract detail from HTTPException
        if hasattr(exc, "detail") and isinstance(exc.detail, dict):
            error_msg = exc.detail.get("message", str(exc))
        return None, error_msg

    # Check if tier allows WebSocket access
    tier_data = TIER_LIMITS.get(tier_info.tier, {})
    if not tier_data.get("websocket", False):
        return None, "WebSocket access requires Pro or Enterprise tier. Upgrade at https://openinterestlens.com/pricing"

    return tier_info, None


@router.websocket("/signals")
async def websocket_signals(
    websocket: WebSocket,
    api_key: str | None = Query(None),
) -> None:
    """Main WebSocket endpoint for real-time signal updates.

    Accepts connections, authenticates via API key, and manages subscriptions.
    """
    manager = get_ws_manager()
    settings = get_settings()
    conn_id: str | None = None
    authenticated = False

    # --- Pre-accept authentication check ---
    # If api_key is provided in query params, authenticate immediately
    if api_key:
        tier_info, error = await _authenticate_ws(api_key, settings)
        if error:
            # For free tier or invalid key, close with 403/401 code
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=error)
            logger.info("ws_auth_rejected", reason=error)
            return

        # Accept and register connection
        conn_id = await manager.connect(websocket, tier_info)
        authenticated = True

        # Send auth success
        await websocket.send_text(json.dumps({
            "type": "auth_success",
            "tier": tier_info.tier,
            "user_id": tier_info.user_id,
            "update_frequency": TIER_LIMITS.get(tier_info.tier, {}).get("update_frequency", "15min"),
        }))
    else:
        # Accept connection but require auth via first message
        # We need to accept first to receive messages
        await websocket.accept()

        # Wait for auth message (with timeout)
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            msg = json.loads(raw)
            if msg.get("action") != "auth" or not msg.get("api_key"):
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="First message must be auth",
                )
                return

            tier_info, error = await _authenticate_ws(msg["api_key"], settings)
            if error:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason=error,
                )
                return

            # Register connection (websocket already accepted)
            conn_id = f"{tier_info.user_id}:{id(websocket)}"
            conn = WSConnection(websocket=websocket, tier_info=tier_info)
            async with manager._lock:
                manager._connections[conn_id] = conn

            logger.info(
                "ws_connected_via_message",
                conn_id=conn_id,
                tier=tier_info.tier,
                user_id=tier_info.user_id,
            )

            # Send auth success
            await websocket.send_text(json.dumps({
                "type": "auth_success",
                "tier": tier_info.tier,
                "user_id": tier_info.user_id,
                "update_frequency": TIER_LIMITS.get(tier_info.tier, {}).get("update_frequency", "15min"),
            }))

            authenticated = True
        except TimeoutError:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Authentication timeout",
            )
            return
        except json.JSONDecodeError:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid JSON in auth message",
            )
            return

    if not authenticated or not conn_id:
        return

    # --- Main message loop ---
    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON",
                }))
                continue

            action = msg.get("action", "")

            if action == "subscribe":
                signal_types = msg.get("signal_types", [])
                contracts = msg.get("contracts")
                result = await manager.subscribe(conn_id, signal_types, contracts)
                await websocket.send_text(json.dumps(result))

            elif action == "unsubscribe":
                signal_types = msg.get("signal_types", [])
                contracts = msg.get("contracts")
                result = await manager.unsubscribe(conn_id, signal_types, contracts)
                await websocket.send_text(json.dumps(result))

            elif action == "ping":
                await manager.update_heartbeat(conn_id)
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif action == "auth":
                # Already authenticated, just acknowledge
                await websocket.send_text(json.dumps({
                    "type": "auth_success",
                    "message": "Already authenticated",
                }))

            else:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Unknown action: {action}",
                }))

    except WebSocketDisconnect:
        logger.info("ws_client_disconnected", conn_id=conn_id)
    except Exception as exc:
        logger.error("ws_error", conn_id=conn_id, error=str(exc))
    finally:
        if conn_id:
            await manager.disconnect(conn_id)


# Need to import WSConnection for the inline path
from app.services.ws_manager import WSConnection