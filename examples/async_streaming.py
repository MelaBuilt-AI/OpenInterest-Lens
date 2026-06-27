#!/usr/bin/env python3
"""Async streaming — subscribe to live signal updates via WebSocket."""

import asyncio
import signal
import sys

from sdk import AsyncOpenInterestLensClient

API_KEY = "oil_sk_live_your_api_key_here"

# Graceful shutdown
shutdown = False


def handle_sigint(_, __):
    global shutdown
    shutdown = True


signal.signal(signal.SIGINT, handle_sigint)


async def stream_signals(contracts: list[str] | None = None):
    """Connect via WebSocket and print live signal updates using AsyncSignalStream."""
    contracts = contracts or ["ES", "NQ", "CL"]

    from sdk import AsyncSignalStream

    stream = AsyncSignalStream(api_key=API_KEY, contracts=contracts)

    print("=" * 60)
    print("OpenInterest Lens — Live Streaming")
    print(f"Subscribing to: {', '.join(contracts)}")
    print("Press Ctrl+C to stop\n")

    async for update in stream:
        if shutdown:
            print("\nShutting down...")
            break

        ts = update.get("timestamp", "?")
        contract = update.get("contract", update.get("commodity", "?"))
        data = update.get("data", update)

        zscore = None
        direction = None
        oi = None

        # Navigate the nested update structure
        if isinstance(data, dict):
            signal_data = data.get("signal", data)
            sm = signal_data.get("smart_money", {})
            zscore = sm.get("z_score")
            sig = signal_data.get("signal", {})
            direction = sig.get("overall")
            np_data = signal_data.get("net_position", {})
            if isinstance(np_data, dict):
                oi = np_data.get("commercial")

        print(
            f"[{ts}] "
            f"{str(contract):>4s} "
            f"z-score: {zscore if zscore is not None:+.2f}" if zscore is not None else f"z-score: N/A "
            f"direction: {direction or 'N/A'} "
            f"OI: {oi or 'N/A'}"
        )

    await stream.close()


if __name__ == "__main__":
    # Override contracts from CLI args
    contracts = sys.argv[1:] if len(sys.argv) > 1 else None
    asyncio.run(stream_signals(contracts))
