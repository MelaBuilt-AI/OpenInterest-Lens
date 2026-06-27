"""WebSocket client for real-time signal streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

from sdk.exceptions import AuthenticationError, ConnectionError

logger = logging.getLogger(__name__)

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    ws_connect = None  # type: ignore

_DEFAULT_WS_URL = "ws://localhost:8000/ws/v1/signals"
_RECONNECT_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0
_HEARTBEAT_INTERVAL = 30.0


class AsyncSignalStream:
    """Async WebSocket client for streaming real-time signal updates.

    Usage::

        stream = AsyncSignalStream(api_key="oil_sk_live_...", contracts=["ES"])
        async for signal in stream:
            print(signal)

    Or with the client::

        async with AsyncOpenInterestLensClient(api_key="...") as client:
            async for signal in client.stream_signals(contracts=["ES"]):
                print(signal)
    """

    def __init__(
        self,
        api_key: str,
        ws_url: str = _DEFAULT_WS_URL,
        contracts: list[str] | None = None,
        signal_types: list[str] | None = None,
        auto_reconnect: bool = True,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    ) -> None:
        if ws_connect is None:
            raise ImportError("websockets package is required for WebSocket streaming. Install with: pip install websockets")
        self.api_key = api_key
        self.ws_url = ws_url
        self.contracts = contracts or []
        self.signal_types = signal_types or ["positioning"]
        self.auto_reconnect = auto_reconnect
        self.heartbeat_interval = heartbeat_interval
        self._ws = None  # type: ignore
        self._connected = False
        self._reconnect_delay = _RECONNECT_DELAY
        self._ws_context = None  # type: ignore

    async def _connect(self) -> None:
        """Establish WebSocket connection and authenticate."""
        url = f"{self.ws_url}?api_key={self.api_key}"
        try:
            self._ws_context = ws_connect(url)
            self._ws = await self._ws_context.__aenter__()
            self._connected = True
            self._reconnect_delay = _RECONNECT_DELAY

            # Wait for auth_success
            msg = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            data = json.loads(msg)
            if data.get("type") == "auth_error":
                await self._disconnect()
                raise AuthenticationError(data.get("message", "WebSocket authentication failed"))

            tier = data.get("tier", "unknown")
            if tier == "free":
                logger.warning("WebSocket access may be limited on free tier")

            # Subscribe
            sub_msg = {
                "action": "subscribe",
                "signal_types": self.signal_types,
                "contracts": self.contracts or None,
            }
            await self._ws.send(json.dumps(sub_msg))

            # Wait for subscribe confirmation
            sub_resp = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            sub_data = json.loads(sub_resp)
            if sub_data.get("type") == "error":
                logger.error("Subscribe error: %s", sub_data.get("message"))

        except AuthenticationError:
            raise
        except ConnectionError:
            raise
        except Exception as exc:
            self._connected = False
            if self._ws_context:
                try:
                    await self._ws_context.__aexit__(type(exc), exc, None)
                except Exception:
                    pass
                self._ws_context = None
            raise ConnectionError(f"WebSocket connection failed: {exc}") from exc

    async def _disconnect(self) -> None:
        """Disconnect the WebSocket."""
        self._connected = False
        if self._ws_context:
            try:
                await self._ws_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._ws_context = None
            self._ws = None

    async def _send_heartbeat(self) -> None:
        """Send periodic heartbeat pings."""
        while self._connected and self._ws:
            await asyncio.sleep(self.heartbeat_interval)
            if self._connected and self._ws:
                try:
                    await self._ws.send(json.dumps({"action": "ping"}))
                except Exception:
                    break

    async def __aiter__(self) -> AsyncGenerator[dict, None]:
        """Yield signal updates as they arrive."""
        reconnect_attempts = 0
        max_reconnect = 10 if self.auto_reconnect else 0

        while True:
            if not self._connected:
                try:
                    await self._connect()
                    reconnect_attempts = 0
                except (AuthenticationError, ConnectionError) as exc:
                    if reconnect_attempts >= max_reconnect:
                        raise
                    reconnect_attempts += 1
                    delay = min(self._reconnect_delay * (2 ** reconnect_attempts), _RECONNECT_MAX_DELAY)
                    logger.warning("Reconnect attempt %d in %.1fs: %s", reconnect_attempts, delay, exc)
                    await asyncio.sleep(delay)
                    continue

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._send_heartbeat())

            try:
                async for raw_message in self._ws:
                    try:
                        data = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "pong":
                        continue
                    if msg_type == "error":
                        logger.error("WebSocket error: %s", data.get("message"))
                        continue
                    if msg_type in ("auth_success", "subscribed", "unsubscribed"):
                        continue

                    # Yield signal updates and other messages
                    yield data

            except Exception as exc:
                self._connected = False
                heartbeat_task.cancel()
                if not self.auto_reconnect:
                    raise ConnectionError(f"WebSocket error: {exc}") from exc
                reconnect_attempts += 1
                if reconnect_attempts > max_reconnect:
                    raise ConnectionError("WebSocket connection lost, max reconnect attempts exceeded")
                delay = min(self._reconnect_delay * (2 ** reconnect_attempts), _RECONNECT_MAX_DELAY)
                logger.warning("Connection lost, reconnecting in %.1fs...", delay)
                await asyncio.sleep(delay)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        await self._disconnect()