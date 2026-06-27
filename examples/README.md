# OpenInterest Lens — Examples

Copy-paste ready scripts demonstrating the Python SDK.

## Setup

```bash
pip install openinterest-lens
export OIL_API_KEY="oil_sk_live_your_key_here"
```

Get an API key at [openinterest.lens](https://openinterest.lens).

## Scripts

| Script | Description |
|--------|-------------|
| `quickstart.py` | Sync client basics — health check, signals, term structure, roll pressure |
| `async_streaming.py` | Async client with WebSocket streaming — live signal updates |
| `smart_money_tracker.py` | Monitor z-scores across contracts, detect threshold crossings |
| `roll_calendar.py` | Show roll pressure for all contracts, highlight upcoming rolls |

## Running

```bash
# Edit API key in each script, or set environment variable
export OIL_API_KEY="oil_sk_live_..."

python quickstart.py
python async_streaming.py                    # streams ES, NQ, CL
python async_streaming.py ES NQ GC ZN        # custom contract list
python smart_money_tracker.py
python roll_calendar.py
```

## Notes

- All scripts use the **sync client** (`OpenInterestLensClient`) except `async_streaming.py` which uses `AsyncOpenInterestLensClient`
- WebSocket streaming requires a Pro or Enterprise tier API key
- Rate limits: Free=60/hr, Pro=600/hr, Enterprise=6000/hr