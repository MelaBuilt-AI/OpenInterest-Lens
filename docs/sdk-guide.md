# Python SDK Guide — OpenInterest Lens

Complete guide for the Python SDK: installation, sync/async clients, WebSocket streaming, and builder pattern.

## Installation

```bash
pip install openinterest-lens
```

Requires Python 3.11+.

## Configuration

The SDK reads configuration from environment variables with sensible defaults:

| Variable | Default | Description |
|---|---|---|
| `OIL_API_KEY` | — | Your API key (required) |
| `OIL_BASE_URL` | `http://localhost:8000` | API base URL |
| `OIL_TIMEOUT` | `30` | Request timeout (seconds) |
| `OIL_MAX_RETRIES` | `3` | Max retry attempts for transient errors |

## Sync Client

The simplest way to use the SDK:

```python
from sdk import OpenInterestLensClient

# Uses OIL_API_KEY env var
client = OpenInterestLensClient()

# Or pass explicitly
client = OpenInterestLensClient(api_key="oil_sk_live_...")
```

### Positioning Signals

```python
# Single commodity
response = client.get_signals("ES")
sig = response.signal

print(sig.smart_money.direction)   # "long"
print(sig.smart_money.z_score)     # 2.34
print(sig.smart_money.percentile)  # 98.1
print(sig.net_position.commercial) # -42520

# Retail contrarian signal
print(sig.retail.contrarian_signal)    # "fade_short"
print(sig.retail.z_score)              # -1.87

# Composite signal
print(sig.signal.overall)   # "bullish"
print(sig.signal.strength)  # 0.87

# Full breakdown by trader category
breakdown = response.breakdown
print(breakdown.commercial.direction)  # "long"
print(breakdown.non_commercial.z_score) # -1.45
print(breakdown.non_reportable.percentile) # 12.3

# Custom lookback window
response = client.get_signals("ES", lookback_weeks=26)
```

### COT Data

```python
cot = client.get_cot("CL")

# Most recent report
latest = cot.reports[0]
print(latest.as_of_date)
print(latest.commercial.net)
print(latest.commercial.z_score_52w)
print(latest.commercial.percentile_52w)

# Historical range
cot = client.get_cot("CL", start_date="2026-01-01", end_date="2026-05-14")

# Summary format
cot = client.get_cot("ES", report_type="summary")
```

### Roll Pressure

```python
roll = client.get_roll_pressure("ES")

# Roll pressure metrics
print(roll.roll_pressure.index)          # 0.72
print(roll.roll_pressure.oi_decay_pct)   # -12.4
print(roll.roll_pressure.spread_basis)   # -0.38
print(roll.roll_pressure.days_to_expiry) # 8

# Roll calendar
print(roll.roll_calendar.nearby_month)   # "M"
print(roll.roll_calendar.days_to_roll)   # 8
print(roll.roll_calendar.roll_urgency)  # "imminent"

# Roll impact
print(roll.roll_impact.impact_score)      # 0.65
print(roll.roll_impact.expected_slippage) # 0.12
```

### Term Structure

```python
ts = client.get_term_structure("GC")
curve = ts.term_structure

# Structure type
print(curve.structure_type)  # "contango"

# Monthly data
for month in curve.months:
    print(f"{month.month}: settle={month.settlement}, OI={month.open_interest}")

# Contango/backwardation
print(ts.contango_backwardation.m1_m2_spread)
print(ts.contango_backwardation.m1_m2_annualized)

# Slope metrics
print(ts.slope_metrics.nearby_deferred_spread)

# Calendar spread ratios
print(ts.calendar_spread_ratios)

# Historical term structure
ts = client.get_term_structure("GC", as_of_date="2026-03-01")
```

### Contracts

```python
contracts = client.get_contracts()

for c in contracts.contracts:
    print(f"{c.symbol} ({c.exchange}): {c.full_name}")
    print(f"  Signals: {', '.join(c.signals_available)}")
```

### Health

```python
health = client.get_health()
print(f"Status: {health.status}")
print(f"Service: {health.service}")
print(f"Version: {health.version}")
```

## Async Client

For async applications (FastAPI, asyncio-based services):

```python
import asyncio
from sdk import AsyncOpenInterestLensClient

async def main():
    async with AsyncOpenInterestLensClient(api_key="oil_sk_live_...") as client:
        # All the same methods, but async
        response = await client.get_signals("ES")
        print(response.signal.smart_money.direction)

        # Concurrent requests
        es, cl, gc = await asyncio.gather(
            client.get_signals("ES"),
            client.get_roll_pressure("CL"),
            client.get_term_structure("GC"),
        )

asyncio.run(main())
```

### Async Context Manager

The `AsyncOpenInterestLensClient` supports both context manager and manual lifecycle:

```python
# Context manager (recommended)
async with AsyncOpenInterestLensClient() as client:
    response = await client.get_signals("ES")

# Manual lifecycle
client = AsyncOpenInterestLensClient()
try:
    response = await client.get_signals("ES")
finally:
    await client.close()
```

## WebSocket Streaming

Real-time signal updates via WebSocket:

```python
import asyncio
from sdk import AsyncSignalStream

async def stream_signals():
    stream = AsyncSignalStream(api_key="oil_sk_live_...", contracts=["ES", "NQ", "CL"])

    async for update in stream:
        print(f"{update.get('contract')}: {update.get('data', {}).get('signal', {}).get('overall')}")

asyncio.run(stream_signals())
```

### WebSocket with Reconnection

```python
from sdk import AsyncSignalStream

async def resilient_stream():
    stream = AsyncSignalStream(
        api_key="oil_sk_live_...",
        contracts=["ES"],
        auto_reconnect=True,
    )
    async for update in stream:
        print(f"{update.get('contract')}: {update.get('data', {})}")
```

## Builder Pattern

For fine-grained configuration:

```python
from sdk import ClientBuilder

client = (ClientBuilder()
    .api_key("oil_sk_live_...")
    .base_url("https://api.openinterestlens.com")
    .timeout(30)
    .max_retries(3)
    .retry_delay(0.5)
    .build())

# Same builder for async
async_client = (ClientBuilder()
    .api_key("oil_sk_live_...")
    .base_url("https://api.openinterestlens.com")
    .build_async())
```

## Error Handling

```python
from sdk import OpenInterestLensClient, OpenInterestLensError, RateLimitError, AuthenticationError

client = OpenInterestLensClient()

try:
    response = client.get_signals("ES")
except AuthenticationError as e:
    print(f"Auth failed: {e}")
    # Check your API key
except RateLimitError as e:
    print(f"Rate limited. Retry after {e.retry_after} seconds")
    # Back off and retry
except OpenInterestLensError as e:
    print(f"API error: {e.status_code} - {e.message}")
    # Handle other API errors
```

## Pagination

For endpoints returning large datasets:

```python
# COT data with date range
cot = client.get_cot("ES", start_date="2025-01-01", end_date="2026-05-14")
# Returns all reports in the range (max 260 per request)
```

## Type Hints

The SDK is fully typed for IDE autocomplete and type checking:

```python
from sdk import OpenInterestLensClient
from sdk.models import PositioningSignalResponse, RollPressureResponse, TermStructureResponse

client: OpenInterestLensClient = OpenInterestLensClient()
response: PositioningSignalResponse = client.get_signals("ES")
```

## Best Practices

1. **Use environment variables for API keys** — don't hard-code them
2. **Use async for concurrent requests** — `AsyncOpenInterestLensClient` + `asyncio.gather`
3. **Handle rate limits** — catch `RateLimitError` and respect `Retry-After`
4. **Rotate keys periodically** — use `/v1/keys/rotate` with a grace period
5. **Subscribe to specific contracts** — don't subscribe to all contracts on WebSocket
6. **Use context managers** — `with` / `async with` ensures connections close properly
