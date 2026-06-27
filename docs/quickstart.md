# Quickstart Guide — 5 Minutes to Your First Signal

This guide walks you through installing the SDK, getting an API key, and making your first API call.

## Step 1: Install

```bash
pip install openinterest-lens
```

Requires Python 3.11+.

## Step 2: Get Your API Key

1. Visit [openinterestlens.com](https://openinterestlens.com) and sign up
2. Navigate to **Dashboard → API Keys**
3. Copy your key — it starts with `oil_sk_live_`

**Free tier includes:** ES, NQ, CL positioning signals, 60 requests/hour, 4 weeks history.

## Step 3: Set Your API Key

```bash
export OIL_API_KEY=oil_sk_live_your_key_here
```

Or pass it directly to the client:

```python
from sdk import OpenInterestLensClient
client = OpenInterestLensClient(api_key="oil_sk_live_your_key_here")
```

## Step 4: Your First API Call

```python
from sdk import OpenInterestLensClient

client = OpenInterestLensClient()  # Uses OIL_API_KEY env var

# Get positioning signal for E-mini S&P 500
response = client.get_signals("ES")
sig = response.signal

print(f"Commodity: {response.commodity}")
print(f"Smart money: {sig.smart_money.direction} (z={sig.smart_money.z_score})")
print(f"Retail contrarian: {sig.retail.contrarian_signal}")
print(f"Composite: {sig.signal.overall} (strength={sig.signal.strength})")

client.close()
```

Output:
```
Commodity: ES
Smart money: long (z=2.34)
Retail contrarian: fade_long
Composite: bullish (strength=0.87)
```

## Step 5: Explore More Signals

```python
# COT data with Z-scores
cot = client.get_cot("CL")
for report in cot.reports[:3]:
    print(f"{report.as_of_date}: Commercial net={report.commercial.net}, z={report.commercial.z_score_52w}")

# Roll pressure
roll = client.get_roll_pressure("ES")
print(f"Roll index: {roll.roll_pressure.index}")
print(f"Days to expiry: {roll.roll_pressure.days_to_expiry}")
print(f"Roll urgency: {roll.roll_calendar.roll_urgency}")

# Term structure
ts = client.get_term_structure("GC")
curve = ts.term_structure
print(f"Structure: {curve.structure_type}")
for month in curve.months[:5]:
    print(f"  {month.month}: settle={month.settlement}, OI={month.open_interest}")
```

## Using curl

```bash
# Positioning signal
curl -H "X-API-Key: oil_sk_live_..." \
  https://api.openinterestlens.com/v1/signals/positioning/ES

# Roll pressure
curl -H "X-API-Key: oil_sk_live_..." \
  https://api.openinterestlens.com/v1/roll-pressure/CL

# Term structure
curl -H "X-API-Key: oil_sk_live_..." \
  https://api.openinterestlens.com/v1/term-structure/GC

# Health check (no API key needed)
curl https://api.openinterestlens.com/v1/health
```

## Next Steps

- [API Reference](api-reference.md) — Full endpoint documentation
- [SDK Guide](sdk-guide.md) — Advanced SDK usage
- [Deployment Guide](deployment.md) — Production setup

## Troubleshooting

**401 Unauthorized:** Check your API key is set correctly.

**403 Forbidden:** Your tier may not have access to this endpoint or contract. Free tier is limited to ES, NQ, CL.

**429 Too Many Requests:** You've hit your rate limit. Free: 60/hr, Pro: 600/hr, Enterprise: 6000/hr.

**503 Service Unavailable:** No data available for the requested contract. Try ingesting data first.
