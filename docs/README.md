# OpenInterest Lens

**Real-time futures market structure API** — OI + COT + term structure as developer-ready signals.

OpenInterest Lens (OIL) transforms raw CFTC Commitments of Traders and CME settlement data into clean, typed API responses with computed signals: positioning Z-scores, roll pressure indices, contango/backwardation alerts, and term structure curves.

## Architecture

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  CFTC COT    │  │  CME Settle  │  │  CME Volume  │
│  (weekly)    │  │  (daily)     │  │  (intraday)  │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                  │
       └────────────┬────┴──────────────────┘
                    │
         ┌──────────▼──────────┐
         │   Ingestion Layer   │  ← Retry, validation, dedup
         │   (Celery + Redis)  │
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │   Signal Engine     │
         │   ┌───────────────┐│
         │   │ Positioning   ││  ← Z-scores, percentiles
         │   │ Roll Pressure ││  ← OI decay, spread basis
         │   │ Term Structure││  ← Forward curves, slope
         │   │ Contango/BW   ││  ← Structural shift alerts
         │   └───────────────┘│
         └──────────┬──────────┘
                    │
         ┌──────────▼──────────┐
         │   FastAPI Server    │  ← REST + WebSocket
         │   + Redis Cache     │
         └──────────┬──────────┘
                    │
       ┌────────────┼────────────┐
       │            │            │
   Free Tier    Pro Tier   Enterprise
   (ES,NQ,CL)   (50+)      (Unlimited)
```

## Quick Start

```bash
# Install
pip install openinterest-lens

# Set your API key
export OIL_API_KEY=oil_sk_live_...

# First call
python -c "
from sdk import OpenInterestLensClient
client = OpenInterestLensClient()
response = client.get_signals('ES')
sig = response.signal
print(f'Smart money: {sig.smart_money.direction} (z={sig.smart_money.z_score})')
client.close()
"
```

## API Overview

| Endpoint | Method | Description | Tier |
|---|---|---|---|
| `/v1/signals/positioning` | GET | Positioning signals for all commodities | Free |
| `/v1/signals/positioning/{symbol}` | GET | Positioning signal for a commodity | Free |
| `/v1/term-structure/{symbol}` | GET | Term structure curve + alerts | Pro |
| `/v1/cot/{symbol}` | GET | Raw COT data with computed metrics | Free |
| `/v1/roll-pressure/{symbol}` | GET | Roll pressure index | Pro |
| `/v1/contracts` | GET | List tracked contracts | Free |
| `/v1/health` | GET | Service health check | Free |
| `/v1/keys/rotate` | POST | Rotate API key | All |
| `/v1/keys/me` | GET | Current key info | All |
| `/metrics` | GET | Prometheus metrics | Internal |
| `/ws/v1/signals` | WS | Live signal streaming | Pro+ |

All endpoints require `X-API-Key` header. Rate limits apply per tier.

## SDK Usage

### Sync Client

```python
from sdk import OpenInterestLensClient

client = OpenInterestLensClient(api_key="oil_sk_live_...")

# Get positioning signal
response = client.get_signals("ES")
sig = response.signal
print(sig.smart_money.direction)  # "long"
print(sig.smart_money.z_score)    # 2.34

# Get roll pressure
roll = client.get_roll_pressure("CL")
print(roll.roll_pressure.index)  # 0.72
print(roll.roll_calendar.roll_urgency)  # "imminent"

# Get term structure
ts = client.get_term_structure("GC")
print(ts.term_structure.structure_type)  # "contango"

client.close()
```

### Async Client

```python
import asyncio
from sdk import AsyncOpenInterestLensClient

async def main():
    async with AsyncOpenInterestLensClient(api_key="oil_sk_live_...") as client:
        response = await client.get_signals("ES")
        print(response.signal.smart_money.direction)

asyncio.run(main())
```

### WebSocket Streaming

```python
import asyncio
from sdk import AsyncSignalStream

async def stream():
    stream = AsyncSignalStream(api_key="oil_sk_live_...", contracts=["ES", "NQ", "CL"])
    async for update in stream:
        print(f"{update.get('contract')}: {update.get('data', {})}")

asyncio.run(stream())
```

### Builder Pattern

```python
from sdk import ClientBuilder

client = (ClientBuilder()
    .api_key("oil_sk_live_...")
    .base_url("https://api.openinterestlens.com")
    .timeout(30)
    .max_retries(3)
    .build())
```

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `OIL_API_KEY` | — | Your API key (required) |
| `OIL_BASE_URL` | `http://localhost:8000` | API base URL |
| `OIL_TIMEOUT` | `30` | Request timeout (seconds) |
| `OIL_MAX_RETRIES` | `3` | Max retry attempts |

## Development

```bash
# Clone and setup
git clone https://github.com/openinterestlens/openinterest-lens.git
cd openinterest-lens

# Install server dependencies
cd server && pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run locally
uvicorn app.main:app --reload --port 8000
```

## License

MIT
