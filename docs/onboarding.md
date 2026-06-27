# Developer Onboarding: OpenInterest Lens

Welcome to OpenInterest Lens. This guide will get you from zero to your first institutional-grade positioning signal in under 5 minutes.

## 🚀 Quickstart

### 1. Get Your API Key
Sign up at [OpenInterest Lens Landing Page](https://api.openinterestlens.com) to receive your `X-API-Key` via email. This key is required for all requests.

### 2. Install the SDK
The fastest way to integrate is via our official Python SDK.

```bash
pip install openinterest-lens
```

### 3. Your First Request
Run this snippet to fetch the current positioning state for the S&P 500 E-mini (ES).

```python
from openinterest_lens import LensClient

client = LensClient(api_key="YOUR_API_KEY")
signal = client.get_positioning("ES")

print(f"Smart Money Direction: {signal.direction}")
print(f"Conviction Score: {signal.conviction}")
```

---

## 🎓 5-Minute Tutorial: Interpreting Signals

The core of OpenInterest Lens is the `PositioningSignal`. Here is how to interpret the data for a contract like **ES**.

### Fetching the Signal
```python
signal = client.get_positioning("ES")
```

### Interpreting the Data
- **Direction (`Bullish` | `Bearish` | `Neutral`)**: 
  Calculated by fusing Open Interest changes with price action and volume. 
  - `Bullish` + Increasing OI $\rightarrow$ Strong long buildup.
  - `Bearish` + Increasing OI $\rightarrow$ Strong short buildup.
- **Conviction (`0.0` to `1.0`)**: 
  Represents the statistical strength of the signal. A score $> 0.7$ typically indicates a high-probability institutional trend.
- **Smart Money Flow**: 
  Tracks the delta between institutional-sized orders and retail-sized orders.

---

## 🛠 Common Patterns

### Polling vs. WebSockets
- **Polling**: Ideal for dashboards or low-frequency strategies. Use the REST API endpoints.
- **WebSockets**: Mandatory for HFT or real-time alerts. Subscribe to `positioning.updates` to receive pushes every time a signal shifts.

### Signal Thresholds
We recommend the following baseline thresholds for algorithmic triggers:
- **High Conviction Entry**: `conviction > 0.8`
- **Trend Reversal Warning**: `conviction < 0.3` while price is at a key level.

### Rate Limits
- **Free Tier**: 1,000 requests/day.
- **Pro Tier**: 50,000 requests/day.
- *Note: Rate limits are enforced per API key. Exceeding limits will return a `429 Too Many Requests` error.*

---

## ⚠️ Error Handling

The API uses standard HTTP status codes. Always wrap your calls in a try-except block using the SDK's built-in exceptions.

### Example Implementation
```python
from openinterest_lens import LensClient, LensAPIError

client = LensClient(api_key="YOUR_API_KEY")

try:
    data = client.get_positioning("ES")
except LensAPIError as e:
    if e.status_code == 401:
        print("Authentication failed. Check your API key.")
    elif e.status_code == 429:
        print("Rate limit exceeded. Switching to backoff mode...")
    else:
        print(f"An unexpected error occurred: {e}")
```

## 📚 Further Reading
- **Full API Specification**: See [README.md](../README.md)
- **SDK Documentation**: Check the `/docs/sdk` folder in the repository.
- **Support**: Contact `support@openinterestlens.com`
