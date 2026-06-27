# OpenInterest Lens SDK

Real-time futures market structure API — OI + COT + term structure as developer-ready signals.

## Install

```bash
pip install openinterest-lens
```

For WebSocket streaming support:

```bash
pip install openinterest-lens[ws]
```

## Quick Start

```python
from openinterest_lens import OpenInterestLensClient

client = OpenInterestLensClient(
    api_key="oil_sk_live_your_key_here",
    base_url="https://api.openinterestlens.com"  # or http://localhost:8000 for local
)

# Get positioning signals for S&P 500 E-mini
signal = client.get_signals("ES")
print(f"Smart Money Direction: {signal.data.direction}")
print(f"Conviction: {signal.data.conviction}")

# Get term structure
ts = client.get_term_structure("ES")

# Get COT data
cot = client.get_cot("ES")

# List available contracts
contracts = client.get_contracts()

client.close()
```

## Async Client

```python
import asyncio
from openinterest_lens import AsyncOpenInterestLensClient

async def main():
    async with AsyncOpenInterestLensClient(api_key="...") as client:
        signal = await client.get_signals("ES")
        print(signal.data.direction)

asyncio.run(main())
```

## Builder Pattern

```python
from openinterest_lens import ClientBuilder

client = (
    ClientBuilder()
    .base_url("https://api.openinterestlens.com")
    .api_key("oil_sk_live_...")
    .timeout(60)
    .max_retries(5)
    .build()
)
```

## WebSocket Streaming

```python
import asyncio
from openinterest_lens import AsyncSignalStream

async def stream():
    stream = AsyncSignalStream(
        api_key="oil_sk_live_...",
        contracts=["ES", "NQ", "CL"],
    )
    async for event in stream:
        print(f"{event['contract']}: {event['signal']['direction']}")

asyncio.run(stream())
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /v1/contracts` | List available contracts |
| `GET /v1/signals/{symbol}` | Get positioning signals |
| `GET /v1/term-structure/{symbol}` | Get term structure curves |
| `GET /v1/roll-pressure/{symbol}` | Get roll pressure index |
| `GET /v1/cot/{symbol}` | Get COT report data |
| `GET /v1/health` | Health check |
| `GET /v1/quality` | Data quality metrics |
| `WS /ws/v1/signals` | Real-time signal streaming |

## Tiers

| Feature | Free | Pro ($49/mo) | Enterprise |
|----------|------|--------------|------------|
| Requests/day | 100 | 10,000 | Unlimited |
| Contracts | 5 | All | All |
| Historical data | 30 days | 1 year | Full |
| WebSocket | ❌ | ✅ | ✅ |
| Support | Community | Email | Slack |

## License

MIT