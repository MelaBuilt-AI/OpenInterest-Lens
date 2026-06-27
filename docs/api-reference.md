# API Reference — OpenInterest Lens

Base URL: `https://api.openinterestlens.com/v1`

All endpoints require the `X-API-Key` header unless noted.

## Authentication

Pass your API key in the `X-API-Key` header:

```
X-API-Key: oil_sk_live_your_key_here
```

Invalid or missing keys return `401 Unauthorized`. Tier mismatches return `403 Forbidden`.

## Rate Limits

| Tier | Requests/Hour | COT/Settlements | Roll Pressure | Term Structure |
|---|---|---|---|---|
| Free | 60 | 20 | 30 | 30 |
| Pro | 600 | 200 | 300 | 300 |
| Enterprise | 6000 | 3000 | 4000 | 4000 |

Rate limit headers on every response:
- `X-RateLimit-Limit`: Maximum requests per window
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Seconds until window resets
- `Retry-After`: Seconds until retry (on 429 only)

---

## Signals

### GET /v1/signals/positioning

Get positioning signals for all accessible commodities.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `lookback_weeks` | int | 52 | Lookback window for Z-scores (4–260) |
| `commodity` | string | all | Filter to a single commodity symbol |

**Response:** `MultiCommoditySignalResponse`

```json
{
  "signals": [
    {
      "commodity": "ES",
      "smart_money": { "direction": "bullish", "z_score": 2.34, "percentile": 98.1, "net_position": -42520 },
      "retail_contrarian": { "direction": "bearish", "z_score": -1.87, "percentile": 12.3 },
      "composite": { "direction": "bullish", "strength": 0.87, "confidence": "high" },
      "metadata": { "lookback_weeks": 52, "cache_hit": false, "computed_at": "2026-05-14T12:00:00Z" }
    }
  ],
  "computed_at": "2026-05-14T12:00:00Z"
}
```

### GET /v1/signals/positioning/{commodity}

Get positioning signal for a specific commodity.

**Path Parameters:**

| Name | Type | Description |
|---|---|---|
| `commodity` | string | Commodity symbol (e.g., `ES`, `NQ`, `CL`, `GC`) |

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `lookback_weeks` | int | 52 | Lookback window (4–260) |

**Response:** `PositioningSignalResponse`

**Tier Access:** Free (ES, NQ, CL only), Pro/Enterprise (all)

**Errors:**
- `400` — Invalid commodity symbol format
- `403` — Contract not available on your tier
- `404` — Contract not tracked
- `503` — No COT data available

---

## Term Structure

### GET /v1/term-structure/{contract}

Get term structure curve with contango/backwardation alerts.

**Path Parameters:**

| Name | Type | Description |
|---|---|---|
| `contract` | string | Contract symbol |

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `start_date` | string | — | Start date (YYYY-MM-DD) |
| `end_date` | string | — | End date (YYYY-MM-DD) |

**Response:**

```json
{
  "contract": "GC",
  "term_structure": {
    "structure_type": "contango",
    "months": [
      {
        "month": "Jun25",
        "expiry_date": "2026-06-26",
        "settlement": 2341.5,
        "open_interest": 312400,
        "volume": 185200,
        "spread_to_front": 0.0,
        "annualized_yield": 4.2
      }
    ],
    "front_month_oi": 312400,
    "total_oi": 892100,
    "oi_concentration_pct": 35.0,
    "steepness": 12.5
  },
  "contango_backwardation": { ... },
  "slope_metrics": { ... },
  "calendar_spread_ratios": { ... },
  "metadata": { "commodity": "GC", "as_of_date": "2026-05-14", "data_points": 12, "computed_at": "..." }
}
```

**Tier Access:** Free (ES, NQ, CL current only), Pro/Enterprise (all, historical)

---

## COT Data

### GET /v1/cot/{contract}

Raw COT data with computed Z-scores and percentile rankings.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `start_date` | string | — | Start date (YYYY-MM-DD) |
| `end_date` | string | — | End date (YYYY-MM-DD) |
| `format` | string | `full` | Response format: `full` or `summary` |

**Response:**

```json
{
  "contract": "ES",
  "reports": [
    {
      "as_of_date": "2026-05-10",
      "commercial": { "long": 850000, "short": 920000, "net": -70000, "z_score_52w": 2.34, "percentile_52w": 98.1 },
      "non_commercial": { "long": 420000, "short": 380000, "net": 40000, "z_score_52w": 1.12, "percentile_52w": 72.3 },
      "non_reportable": { "long": 15000, "short": 20000, "net": -5000, "z_score_52w": -0.45, "percentile_52w": 32.1 },
      "total_open_interest": 1780000
    }
  ],
  "metadata": { "total_reports": 52, "computed_at": "..." }
}
```

**Per-endpoint rate limits:** Free 20/hr, Pro 200/hr, Enterprise 3000/hr

---

## Roll Pressure

### GET /v1/roll-pressure/{contract}

Roll pressure index, roll calendar, and impact estimation.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `start_date` | string | — | Start date (YYYY-MM-DD) |
| `end_date` | string | — | End date (YYYY-MM-DD) |
| `days_back` | int | 30 | OI decay lookback (1–365) |

**Response:**

```json
{
  "contract": "ES",
  "roll_pressure": { "index": 0.72, "oi_decay_pct": -12.4, "spread_basis": -0.38, "days_to_expiry": 8 },
  "roll_calendar": { "nearby_month": "M", "nearby_expiry": "2026-06-19", "days_to_roll": 8, "roll_urgency": "high" },
  "roll_impact": { "impact_score": 0.65, "oi_concentration": 0.35, "expected_slippage": 0.12 }
}
```

**Per-endpoint rate limits:** Free 30/hr, Pro 300/hr, Enterprise 4000/hr

---

## Contracts

### GET /v1/contracts

List all tracked contracts with metadata.

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `exchange` | string | — | Filter by exchange (CME, NYMEX, COMEX) |
| `asset_class` | string | — | Filter by asset class |

**Response:**

```json
{
  "contracts": [
    {
      "symbol": "ES",
      "exchange": "CME",
      "asset_class": "equity_index",
      "full_name": "E-mini S&P 500",
      "tick_size": 0.25,
      "contract_size": 50,
      "months_traded": ["H", "M", "U", "Z"],
      "signals_available": ["positioning", "roll_pressure", "contango_alert", "term_structure"]
    }
  ]
}
```

---

## Data Quality

### GET /v1/quality

Data quality scores for all tracked contracts.

**Response:**

```json
{
  "contracts": [
    {
      "symbol": "ES",
      "quality_score": 0.95,
      "freshness": { "last_update": "2026-05-14", "stale": false, "hours_since_update": 2 },
      "completeness": { "pct": 0.98, "missing_fields": [] }
    }
  ],
  "computed_at": "2026-05-14T12:00:00Z"
}
```

---

## Key Management

### POST /v1/keys/rotate

Rotate your API key. The old key remains valid for a configurable grace period.

**Request:**

```json
{
  "grace_period_hours": 1
}
```

**Response:**

```json
{
  "new_api_key": "oil_sk_live_...",
  "new_key_hash": "sha256...",
  "old_key_expires_at": "2026-05-14T13:00:00Z",
  "grace_period_hours": 1
}
```

**Important:** Store `new_api_key` securely — it won't be shown again.

### GET /v1/keys/me

Get info about your current API key.

**Response:**

```json
{
  "tier": "pro",
  "user_id": "demo_pro",
  "key_prefix": "oil_sk_live",
  "contracts_accessible": "all",
  "rate_limit": 600
}
```

---

## Health

### GET /v1/health

Service health check. No authentication required.

**Response:**

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 86400
}
```

---

## WebSocket

### ws://api.openinterestlens.com/ws/v1/signals

Real-time signal streaming. Requires API key in query parameter or message.

**Connection:**
```javascript
const ws = new WebSocket('wss://api.openinterestlens.com/ws/v1/signals?api_key=oil_sk_live_...');
ws.onmessage = (event) => console.log(JSON.parse(event.data));
```

**Subscription message:**
```json
{ "action": "subscribe", "contracts": ["ES", "NQ", "CL"] }
```

**Signal update:**
```json
{ "type": "signal_update", "contract": "ES", "data": { ... }, "timestamp": "2026-05-14T12:00:00Z" }
```

**Tier access:** Pro and Enterprise only.

---

## Error Codes

| Code | Error | Description |
|---|---|---|
| 400 | `invalid_symbol` | Invalid commodity symbol format |
| 400 | `invalid_date` | Invalid date format (use YYYY-MM-DD) |
| 401 | `invalid_api_key` | Missing or invalid API key |
| 401 | `api_key_revoked` | Key has been revoked |
| 403 | `tier_limit_exceeded` | Contract or feature not available on your tier |
| 404 | `not_found` | Contract or resource not found |
| 413 | `request_too_large` | Request body exceeds 256KB |
| 422 | `validation_error` | Pydantic validation failed |
| 429 | `rate_limit_exceeded` | Rate limit exceeded, retry after `Retry-After` |
| 500 | `signal_error` | Internal signal computation error |
| 503 | `data_unavailable` | No data available for the requested contract |