# OpenInterest Lens — Project Plan

> Real-time futures market structure API: OI + COT + term structure as developer-ready signals

---

## 1. Product Vision & Scope

### Vision
OpenInterest Lens is the **Stripe of futures positioning data** — a single API that transforms raw CFTC, CME, and exchange data into normalized, actionable signals that quant devs can consume in minutes, not days.

### Core Value Proposition
- **One API call** replaces hours of manual data wrangling across CME PDFs, CFTC CSVs, and broker feeds
- **Pre-computed signals** — smart money positioning, roll pressure, contango/backwardation — not raw data dumps
- **Developer-first** — clean REST + WebSocket API, Python SDK, sensible defaults

### MVP Scope (Weeks 1–10)
| In MVP | Post-MVP |
|--------|-----------|
| 4 contracts: ES, NQ, CL, GC | Expand to 50+ contracts (rates, grains, metals, crypto futures) |
| COT-based positioning signals | Custom composite signals |
| Basic term structure curves | Full term structure with IV surface |
| Roll pressure index | Roll yield optimization signals |
| Daily + 15-min updates | Real-time tick-level updates |
| REST API + basic WebSocket | Full WebSocket with subscription management |
| Python SDK | TypeScript SDK, REST helpers |
| Tiered auth (free/pro/enterprise) | Team management, audit logs |

---

## 2. Signal Schema

### 2.1 PositioningSignal

Smart money vs retail positioning derived from COT data.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "PositioningSignal",
  "type": "object",
  "required": ["contract", "timestamp", "net_position", "smart_money", "retail", "signal"],
  "properties": {
    "contract": {
      "type": "string",
      "description": "Root symbol, e.g. 'ES'",
      "examples": ["ES", "NQ", "CL", "GC"]
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "COT report reference date (Tuesday as-of)"
    },
    "as_of_friday": {
      "type": "string",
      "format": "date",
      "description": "Date the COT report was published (Friday)"
    },
    "net_position": {
      "type": "object",
      "properties": {
        "commercial": { "type": "integer", "description": "Net long contracts (commercial hedgers)" },
        "non_commercial": { "type": "integer", "description": "Net long contracts (managed money / specs)" },
        "non_reportable": { "type": "integer", "description": "Net long contracts (small traders / retail)" }
      }
    },
    "smart_money": {
      "type": "object",
      "properties": {
        "z_score": { "type": "number", "description": "Z-score of commercial net position vs 52-week range" },
        "percentile": { "type": "number", "description": "Percentile rank of current position (0-100)" },
        "direction": { "type": "string", "enum": ["long", "short", "neutral"], "description": "Net direction of commercial positioning" },
        "conviction": { "type": "string", "enum": ["low", "medium", "high"], "description": "Position extremity classification" }
      }
    },
    "retail": {
      "type": "object",
      "properties": {
        "z_score": { "type": "number", "description": "Z-score of non-reportable net position" },
        "percentile": { "type": "number", "description": "Percentile rank (0-100)" },
        "direction": { "type": "string", "enum": ["long", "short", "neutral"] },
        "contrarian_signal": { "type": "string", "enum": ["fade_long", "fade_short", "none"], "description": "Contrarian fade signal based on extreme retail positioning" }
      }
    },
    "signal": {
      "type": "object",
      "properties": {
        "overall": { "type": "string", "enum": ["bullish", "bearish", "neutral"], "description": "Composite signal from smart money vs retail divergence" },
        "strength": { "type": "number", "minimum": 0, "maximum": 1, "description": "Signal confidence (0=weak, 1=strong)" },
        "divergence": { "type": "boolean", "description": "True when smart money and retail are on opposite sides" }
      }
    },
    "week_over_week_change": {
      "type": "object",
      "properties": {
        "commercial": { "type": "integer" },
        "non_commercial": { "type": "integer" },
        "non_reportable": { "type": "integer" }
      },
      "description": "Change in net positions from prior week"
    }
  }
}
```

### 2.2 RollPressureIndex

Quantifies pressure to roll nearby positions to deferred contracts.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "RollPressureIndex",
  "type": "object",
  "required": ["contract", "timestamp", "nearby", "deferred", "roll_pressure"],
  "properties": {
    "contract": { "type": "string" },
    "timestamp": { "type": "string", "format": "date-time" },
    "nearby": {
      "type": "object",
      "properties": {
        "month": { "type": "string", "description": "e.g. 'Jun 26'" },
        "open_interest": { "type": "integer" },
        "volume": { "type": "integer" },
        "settlement_price": { "type": "number" }
      }
    },
    "deferred": {
      "type": "object",
      "properties": {
        "month": { "type": "string" },
        "open_interest": { "type": "integer" },
        "volume": { "type": "integer" },
        "settlement_price": { "type": "number" }
      }
    },
    "roll_pressure": {
      "type": "object",
      "properties": {
        "index": { "type": "number", "description": "0-100 roll pressure score (higher = more pressure to roll)" },
        "oi_decay_pct": { "type": "number", "description": "Nearby OI decline as % of total OI in last 5 sessions" },
        "spread_basis": { "type": "number", "description": "Deferred - nearby price spread (positive = contango)" },
        "days_to_expiry": { "type": "integer" },
        "roll_window": { "type": "string", "enum": ["pre_roll", "active_roll", "post_roll"], "description": "Phase of the roll cycle" }
      }
    }
  }
}
```

### 2.3 ContangoAlert

Signals when a market shifts between contango and backwardation.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ContangoAlert",
  "type": "object",
  "required": ["contract", "timestamp", "structure", "alert_type"],
  "properties": {
    "contract": { "type": "string" },
    "timestamp": { "type": "string", "format": "date-time" },
    "structure": {
      "type": "string",
      "enum": ["contango", "backwardation", "flat"],
      "description": "Current term structure state"
    },
    "alert_type": {
      "type": "string",
      "enum": ["transition", "extreme_contango", "extreme_backwardation", "steepening", "flattening"],
      "description": "What triggered this alert"
    },
    "spread_summary": {
      "type": "object",
      "properties": {
        "front_month_price": { "type": "number" },
        "next_month_price": { "type": "number" },
        "m1_m2_spread": { "type": "number", "description": "Next - front month spread in price units" },
        "m1_m2_annualized": { "type": "number", "description": "Annualized % spread" },
        "z_score": { "type": "number", "description": "Z-score of current spread vs 1-year range" }
      }
    },
    "prior_structure": { "type": "string", "enum": ["contango", "backwardation", "flat"] },
    "days_in_current_state": { "type": "integer" },
    "severity": { "type": "string", "enum": ["info", "warning", "critical"] }
  }
}
```

### 2.4 TermStructureCurve

Full term structure across the futures chain for a given contract.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "TermStructureCurve",
  "type": "object",
  "required": ["contract", "timestamp", "as_of_date", "months"],
  "properties": {
    "contract": { "type": "string" },
    "timestamp": { "type": "string", "format": "date-time" },
    "as_of_date": { "type": "string", "format": "date" },
    "structure_type": {
      "type": "string",
      "enum": ["contango", "backwardation", "mixed", "flat"]
    },
    "months": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["month", "settlement", "open_interest", "volume"],
        "properties": {
          "month": { "type": "string", "description": "e.g. 'Jun 26'" },
          "expiry_date": { "type": "string", "format": "date" },
          "settlement": { "type": "number" },
          "open_interest": { "type": "integer" },
          "volume": { "type": "integer" },
          "spread_to_front": { "type": "number", "description": "Price difference from front month" },
          "annualized_yield": { "type": "number", "description": "Annualized % vs front month" }
        }
      }
    },
    "curve_metrics": {
      "type": "object",
      "properties": {
        "front_month_oi": { "type": "integer" },
        "total_oi": { "type": "integer" },
        "oi_concentration_pct": { "type": "number", "description": "Front month OI as % of total" },
        "avg_daily_volume": { "type": "integer" },
        "steepness": { "type": "number", "description": "Curve steepness metric (slope)" }
      }
    }
  }
}
```

---

## 3. Core API Design

### Base URL
```
https://api.openinterestlens.com/v1
```

### Authentication
- Header: `Authorization: Bearer <api_key>`
- Tier enforced via API key metadata

### 3.1 GET /signals/{contract}

Returns the latest positioning signal for a contract.

**Request:**
```
GET /v1/signals/ES?include_history=false&weeks_back=4
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `include_history` | bool | false | Return weekly history instead of just latest |
| `weeks_back` | int | 4 | Number of weeks for history (max 52) |

**Response (200):**
```json
{
  "contract": "ES",
  "current": { /* PositioningSignal */ },
  "history": [
    { /* PositioningSignal */ },
    { /* PositioningSignal */ }
  ]
}
```

**Errors:** 404 (contract not tracked), 403 (tier limit exceeded)

### 3.2 GET /contracts

List all tracked contracts with metadata.

**Request:**
```
GET /v1/contracts?exchange=CME&asset_class=equity_index
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `exchange` | string | all | Filter by exchange |
| `asset_class` | string | all | equity_index, energy, metal, agriculture, etc. |

**Response (200):**
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
      "data_available_from": "1997-01-01",
      "signals_available": ["positioning", "roll_pressure", "contango_alert", "term_structure"]
    }
  ]
}
```

### 3.3 GET /term-structure/{contract}

Full term structure curve for a contract.

**Request:**
```
GET /v1/term-structure/ES?date=2026-05-13&include_history=false
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `date` | string | latest | As-of date (YYYY-MM-DD) |
| `include_history` | bool | false | Return daily term structure history |
| `days_back` | int | 30 | Days of history (max 365) |

**Response (200):**
```json
{
  "contract": "ES",
  "current": { /* TermStructureCurve */ },
  "contango_alerts": [ /* ContangoAlert — recent alerts */ ],
  "history": [ /* TermStructureCurve — if requested */ ]
}
```

### 3.4 GET /cot/{contract}

Raw COT data with computed z-scores and percentiles.

**Request:**
```
GET /v1/cot/ES?weeks_back=12
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `weeks_back` | int | 12 | Number of weeks (max 260 = 5 years) |

**Response (200):**
```json
{
  "contract": "ES",
  "reports": [
    {
      "as_of_date": "2026-05-12",
      "published_date": "2026-05-15",
      "commercial": {
        "long": 850000,
        "short": 1200000,
        "net": -350000,
        "z_score_52w": -1.8,
        "percentile_52w": 12
      },
      "non_commercial": {
        "long": 600000,
        "short": 200000,
        "net": 400000,
        "z_score_52w": 1.5,
        "percentile_52w": 85
      },
      "non_reportable": {
        "long": 150000,
        "short": 50000,
        "net": 100000,
        "z_score_52w": 0.8,
        "percentile_52w": 72
      },
      "total_open_interest": 1600000
    }
  ]
}
```

### 3.5 GET /roll-pressure/{contract}

Roll pressure index for a contract.

**Request:**
```
GET /v1/roll-pressure/ES?include_history=false&days_back=30
```

**Response (200):**
```json
{
  "contract": "ES",
  "current": { /* RollPressureIndex */ },
  "history": [ /* RollPressureIndex */ ]
}
```

### 3.6 WebSocket /ws/v1/signals

Real-time signal updates via WebSocket.

**Connection:**
```
wss://api.openinterestlens.com/ws/v1/signals?api_key=<key>
```

**Client subscribes:**
```json
{
  "action": "subscribe",
  "contracts": ["ES", "NQ"],
  "signal_types": ["positioning", "roll_pressure", "contango_alert"]
}
```

**Server pushes:**
```json
{
  "type": "positioning_update",
  "contract": "ES",
  "timestamp": "2026-05-13T22:00:00Z",
  "data": { /* PositioningSignal */ }
}
```

**Heartbeat:**
```json
{"type": "ping"}
→ {"type": "pong"}
```

**Tier limits:**
- Free: WebSocket not available
- Pro: 15-min update frequency
- Enterprise: Real-time (on data arrival)

### 3.7 Common Error Responses

```json
// 401 Unauthorized
{"error": "invalid_api_key", "message": "Invalid or missing API key"}

// 403 Tier Limit
{"error": "tier_limit_exceeded", "message": "Contract 'CL' not available on free tier. Upgrade to Pro for 50 contracts."}

// 429 Rate Limited
{"error": "rate_limit_exceeded", "message": "Rate limit exceeded. Retry after 60s.", "retry_after": 60}

// 404 Not Found
{"error": "not_found", "message": "Contract 'XX' is not tracked"}

// 503 Data Unavailable
{"error": "data_unavailable", "message": "COT data for 2026-05-12 not yet published"}
```

---

## 4. Data Pipeline Architecture

### 4.1 Data Sources

| Source | Data | Schedule | Format | Latency |
|--------|------|----------|--------|---------|
| CFTC COT | Positioning by trader category | Friday ~7:30 PM ET | CSV/JSON | ~3 days lag (Tue as-of) |
| CME Daily Settlement | Prices + OI per contract month | Daily ~4:30 PM ET | CSV via FTP/API | T+1 |
| CME Real-time Volume | Intraday volume updates | Continuous | FIX/REST | Near real-time |

### 4.2 Ingestion Pipeline

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  CFTC COT    │     │  CME Settle  │     │  CME Volume  │     │ Future:      │
│  (Friday PM) │────▶│  (Daily PM)  │────▶│  (Intraday)  │────▶│ Exchange     │
│              │     │              │     │              │     │ WebSocket    │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
       │                    │                    │                     │
       ▼                    ▼                    ▼                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                        Ingestion Layer (Celery Tasks)                    │
│                                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  ┌─────────────┐ │
│  │ cot_ingest   │  │ settle_ingest│  │ volume_ingest │  │ manual_ingest│ │
│  │ (weekly)     │  │ (daily)      │  │ (15-min)      │  │ (on-demand) │ │
│  └─────────────┘  └──────────────┘  └───────────────┘  └─────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     Raw Storage (PostgreSQL + TimescaleDB)               │
│                                                                          │
│  Tables:                                                                 │
│  - raw_cot_reports (contract, date, commercial_long, commercial_short, │
│    non_commercial_long, non_commercial_short, non_reportable_long, ...)  │
│  - raw_settlements (contract, month, date, settlement, oi, volume)       │
│  - raw_volume_snapshots (contract, timestamp, volume, oi)                │
└──────────────────────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  Normalization & Signal Computation (Celery)             │
│                                                                          │
│  ┌──────────────────┐  ┌───────────────────┐  ┌─────────────────────┐  │
│  │ normalize_cot    │  │ compute_term_      │  │ compute_roll_       │  │
│  │ - z-scores      │  │ structure          │  │ pressure            │  │
│  │ - percentiles   │  │ - spread curves    │  │ - OI decay rate     │  │
│  │ - weekly deltas │  │ - contango detect  │  │ - roll window phase │  │
│  └──────────────────┘  └───────────────────┘  └─────────────────────┘  │
│                                                                          │
│  ┌──────────────────┐  ┌───────────────────┐                            │
│  │ compute_signal   │  │ compute_contango_  │                           │
│  │ - smart money    │  │ alert              │                           │
│  │ - retail contr.  │  │ - transition det.  │                           │
│  │ - divergence     │  │ - severity scoring  │                           │
│  └──────────────────┘  └───────────────────┘                            │
└──────────────────────────────────────────────────────────────────────────┘
       │                    │
       ▼                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                 Signal Storage (TimescaleDB + Redis)                     │
│                                                                          │
│  TimescaleDB hypertables:                                                │
│  - signal_positioning (contract, timestamp, signal_json)                │
│  - signal_roll_pressure (contract, timestamp, signal_json)               │
│  - signal_contango_alert (contract, timestamp, signal_json)             │
│  - term_structure_curves (contract, date, curve_json)                   │
│                                                                          │
│  Redis (cache layer):                                                    │
│  - latest:signal:{contract} → cached latest signal (TTL: 1h)            │
│  - latest:term:{contract} → cached latest term structure (TTL: 1h)      │
│  - ws:subscriptions → pub/sub channels for WebSocket push               │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Ingestion Schedule

| Task | Schedule | Celery Beat | Description |
|------|----------|-------------|-------------|
| `ingest_cot` | Friday 8:00 PM ET | `crontab(minute=0, hour=0, day_of_week=5)` | Fetch latest COT CSV |
| `ingest_settlement` | Daily 5:30 PM ET | `crontab(minute=30, hour=21)` | Fetch CME daily settlement |
| `compute_signals_cot` | After `ingest_cot` | Chained task | Compute positioning signals |
| `compute_signals_daily` | After `ingest_settlement` | Chained task | Compute term structure + roll pressure |
| `cache_warm` | Daily 6:00 PM ET | `crontab(minute=0, hour=22)` | Pre-warm Redis cache |
| `data_quality_check` | Daily 7:00 AM ET | `crontab(minute=0, hour=11)` | Validate completeness + gaps |

### 4.4 Normalization Steps

1. **Download** raw data from source (CFTC CSV, CME FTP/API)
2. **Parse** into standardized internal format (pandas DataFrames)
3. **Validate** — check for missing values, stale data, outlier detection (z-score > 5 on OI)
4. **Map contract codes** — CFTC uses names like "E-MINI S&P 500", CME uses "ES". Maintain a `contract_mapping` table
5. **Store raw** — write to `raw_*` tables with ingestion timestamp
6. **Compute derived metrics** — z-scores, percentiles, weekly deltas, spreads
7. **Generate signals** — apply signal logic (smart money conviction, roll pressure, contango alerts)
8. **Store signals** — write to signal hypertables
9. **Update cache** — write latest signals to Redis with TTL
10. **Push WebSocket** — publish to Redis pub/sub for connected clients

---

## 5. SDK Design (Python)

### Package Structure

```
openinterest_lens/
├── __init__.py
├── client.py          # OpenInterestLensClient (sync + async)
├── models.py          # Pydantic models for all schemas
├── errors.py          # Custom exceptions
├── websocket.py       # WebSocket client
└── config.py          # Configuration dataclass
```

### Key Classes

```python
from openinterest_lens import OpenInterestLensClient
from openinterest_lens.models import PositioningSignal, TermStructureCurve, RollPressureIndex, ContangoAlert

# Sync client
client = OpenInterestLensClient(api_key="oil_sk_...")

# Async client
async with OpenInterestLensClient(api_key="oil_sk_...", async_mode=True) as client:
    signal = await client.signals.get("ES")

# --- Core Methods ---

# Get positioning signal
signal: PositioningSignal = client.signals.get("ES", include_history=True, weeks_back=12)
signal.smart_money.z_score          # -1.8
signal.smart_money.direction        # "short"
signal.retail.contrarian_signal     # "fade_long"

# Get term structure
curve: TermStructureCurve = client.term_structure.get("ES", date="2026-05-13")
curve.structure_type                # "contango"
curve.months[0].settlement          # 5900.25

# Get roll pressure
roll: RollPressureIndex = client.roll_pressure.get("CL")
roll.roll_pressure.index            # 72.3
roll.roll_pressure.roll_window      # "active_roll"

# Get COT data
cot = client.cot.get("ES", weeks_back=26)

# List contracts
contracts = client.contracts.list(exchange="CME")
for c in contracts:
    print(c.symbol, c.full_name)

# --- WebSocket (async only) ---
async with client.websocket(contracts=["ES", "CL"], signal_types=["positioning"]) as ws:
    async for update in ws:
        print(update.type, update.contract, update.data)
```

### Models (Pydantic)

```python
from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional

class NetPosition(BaseModel):
    commercial: int
    non_commercial: int
    non_reportable: int

class SmartMoney(BaseModel):
    z_score: float
    percentile: float
    direction: str  # "long" | "short" | "neutral"
    conviction: str  # "low" | "medium" | "high"

class Retail(BaseModel):
    z_score: float
    percentile: float
    direction: str
    contrarian_signal: str  # "fade_long" | "fade_short" | "none"

class SignalScore(BaseModel):
    overall: str  # "bullish" | "bearish" | "neutral"
    strength: float  # 0.0 - 1.0
    divergence: bool

class PositioningSignal(BaseModel):
    contract: str
    timestamp: datetime
    as_of_friday: Optional[date]
    net_position: NetPosition
    smart_money: SmartMoney
    retail: Retail
    signal: SignalScore
    week_over_week_change: Optional[NetPosition]
```

---

## 6. Tech Stack

### Recommended Stack

| Component | Technology | Justification |
|-----------|-----------|---------------|
| **API Framework** | FastAPI | Async-native, auto OpenAPI docs, Pydantic validation, broad ecosystem. Perfect for financial APIs that need both REST + WebSocket |
| **Time-Series DB** | TimescaleDB | PostgreSQL extension — gives us relational queries + time-series optimizations (hypertables, continuous aggregates, retention policies). Better than QuestDB for complex joins across COT + OI tables |
| **Cache** | Redis | Latest-signal caching, WebSocket pub/sub, rate limit counters. Industry standard, well-understood |
| **Task Queue** | Celery + Redis broker | Scheduled ingestion (CFTC weekly, CME daily), signal computation, cache warming. Celery beat for cron-like scheduling, Redis as broker |
| **Database Layer** | SQLAlchemy 2.0 + Alembic | ORM with TimescaleDB support, Alembic for schema migrations |
| **HTTP Client** | httpx | Async-capable HTTP client for CFTC/CME data fetching |
| **Data Processing** | pandas | COT parsing, z-score computation, percentile calculations. Standard for financial data |
| **WebSocket** | FastAPI WebSocket + Redis Pub/Sub | Native FastAPI WS support + Redis pub/sub for multi-process message distribution |
| **Auth** | API keys via FastAPI dependency | Simple API key auth with tier metadata. JWT overkill for a data API |
| **Rate Limiting** | slowapi + Redis | Tiered rate limiting per API key |
| **Monitoring** | Prometheus + Grafana | Standard observability stack |
| **Deployment** | Docker + Docker Compose (MVP), K8s (scale) | Simple for MVP, portable for scale |
| **CI/CD** | GitHub Actions | Test, lint, build, deploy pipeline |

### Why TimescaleDB over QuestDB

- TimescaleDB is a PostgreSQL extension — we get full SQL, joins across COT + settlement tables, and mature tooling
- QuestDB is faster for pure time-series inserts, but our signal computation needs relational joins
- TimescaleDB continuous aggregates are perfect for pre-computed z-scores and percentiles
- Alembic migrations work natively

### Why Celery over alternatives

- Celery + Redis is battle-tested for scheduled financial data pipelines
- Beat scheduler handles our weekly COT + daily settlement + 15-min update schedules
- Task chaining (ingest → normalize → compute → cache → push) is clean
- Alternatives (Dramatiq, Huey) are simpler but less proven for this pattern

---

## 7. MVP Milestones

### Week 1: Foundation
- [ ] Project scaffolding: FastAPI app, Docker Compose, TimescaleDB, Redis
- [ ] Database schema: `raw_cot_reports`, `raw_settlements`, contract mapping table
- [ ] Alembic migrations
- [ ] API key auth middleware with tier enforcement
- [ ] Health check + `/v1/contracts` endpoint
- [ ] CI pipeline (GitHub Actions): lint, test, build

**Deliverable:** Running API with auth, contracts endpoint, database seeded

### Week 2: Data Ingestion
- [ ] CFTC COT ingestion task (Celery): download CSV, parse, validate, store
- [ ] CME settlement ingestion task: fetch daily settlements via API/FTP
- [ ] Contract mapping table (ES, NQ, CL, GC)
- [ ] Data validation: missing values, staleness detection, outlier flagging
- [ ] Ingestion monitoring: logging, error alerts, retry logic

**Deliverable:** Automated ingestion pipeline running on schedule

### Week 3: Signal Computation — Positioning
- [ ] Z-score + percentile computation for COT positions (52-week rolling window)
- [ ] Smart money classification: direction, conviction, z-score thresholds
- [ ] Retail contrarian signal logic: extreme positioning → fade signal
- [ ] Divergence detection: smart money vs retail on opposite sides
- [ ] `signal_positioning` hypertable + continuous aggregates

**Deliverable:** Positioning signals computed and stored for all 4 contracts

### Week 4: Signal Computation — Term Structure + Roll Pressure
- [ ] Term structure curve builder: fetch settlement prices across contract months
- [ ] Contango/backwardation detection + classification
- [ ] Spread computation: M1/M2, annualized yield, curve steepness
- [ ] Roll pressure index: OI decay rate, roll window phase detection
- [ ] Contango alert generation: transition detection, severity scoring

**Deliverable:** Term structure + roll pressure signals computed and stored

### Week 5: API Endpoints
- [x] `GET /v1/signals/positioning/{contract}` — positioning signals (canonical route)
- [x] `GET /v1/term-structure/{contract}` — term structure + alerts
- [x] `GET /v1/cot/{contract}` — raw COT with computed metrics
- [x] `GET /v1/roll-pressure/{contract}` — roll pressure index
- [x] `GET /v1/contracts` — contract listing with metadata
- [x] Response caching with Redis (1h TTL for latest, no cache for history)
- [x] Tier-based access control (3 contracts free, 50 pro, unlimited enterprise)
- [x] Rate limiting per tier

**Deliverable:** All REST endpoints functional with tier enforcement

### Week 6: WebSocket + Real-time
- [ ] WebSocket endpoint: `/ws/v1/signals`
- [ ] Redis pub/sub integration for signal push
- [ ] Subscription management: subscribe/unsubscribe contracts + signal types
- [ ] Heartbeat + reconnection logic
- [ ] Tier enforcement on WebSocket (free=no WS, pro=15min, enterprise=realtime)
- [ ] Connection management and cleanup

**Deliverable:** Working WebSocket with tier-gated update frequency

### Week 7: Python SDK
- [x] `openinterest_lens` package structure
- [x] `OpenInterestLensClient` — sync + async modes
- [x] Models: `PositioningSignal`, `TermStructureCurve`, `RollPressureIndex`, `ContangoAlert`
- [x] WebSocket client with async generator pattern
- [x] Error handling: custom exceptions, retries, rate limit handling
- [x] Docs: README + examples

**Deliverable:** Published Python SDK on PyPI

### Week 8: Testing + Hardening
- [ ] Integration tests: full pipeline (ingest → compute → API → SDK)
- [ ] Load testing: simulate concurrent API + WebSocket connections
- [ ] Error handling: CFTC delays, CME outages, malformed data
- [ ] Data quality dashboard: staleness, gap detection, completeness metrics
- [ ] API documentation: OpenAPI spec, examples, tutorials
- [ ] Landing page with API key signup

**Deliverable:** Tested, documented, production-ready MVP

### Week 9–10: Polish + Launch
- [ ] Monitoring: Prometheus metrics, Grafana dashboards, PagerDuty alerts
- [ ] Rate limit tuning based on load test results
- [ ] Security review: API key rotation, input validation, CORS
- [ ] Landing page: pricing, docs, interactive API explorer
- [ ] Developer onboarding: quickstart guide, 5-minute tutorial
- [ ] Beta launch: invite 5–10 quant devs, collect feedback

**Deliverable:** Launched MVP with landing page and onboarding docs

---

## 8. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐                  │
│  │ CFTC COT │  │ CME Settle│  │ CME Volume   │                  │
│  │ (weekly) │  │ (daily)   │  │ (intraday)   │                  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘                  │
└───────┼──────────────┼───────────────┼──────────────────────────┘
        │              │               │
        ▼              ▼               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   INGESTION PIPELINE (Celery)                    │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Download │─▶│ Parse &  │─▶│ Validate │─▶│ Store Raw    │   │
│  │ (httpx)  │  │ Normalize│  │ & QA     │  │ (TimescaleDB)│   │
│  └──────────┘  └──────────┘  └──────────┘  └──────┬───────┘   │
│                                                       │          │
│  ┌────────────────────────────────────────────────────▼───────┐ │
│  │              SIGNAL COMPUTATION (Celery Tasks)              │ │
│  │                                                             │ │
│  │  ┌─────────────┐ ┌───────────────┐ ┌────────────────────┐ │ │
│  │  │ Positioning │ │ Term Structure│ │ Roll Pressure &    │ │ │
│  │  │ Signals     │ │ Curves        │ │ Contango Alerts    │ │ │
│  │  └──────┬──────┘ └──────┬───────┘ └─────────┬──────────┘ │ │
│  │         └───────────────┬┴──────────────────┘             │ │
│  └─────────────────────────┼─────────────────────────────────┘ │
└────────────────────────────┼──────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    STORAGE LAYER                                 │
│  ┌──────────────────────┐  ┌────────────────────────────────┐ │
│  │ TimescaleDB          │  │ Redis                           │ │
│  │ - raw_cot_reports    │  │ - latest:signal:{contract}      │ │
│  │ - raw_settlements    │  │ - latest:term:{contract}        │ │
│  │ - signal_* tables    │  │ - ws:subscriptions (pub/sub)    │ │
│  │ - term_structure_*   │  │ - rate_limit:{api_key}         │ │
│  └──────────┬───────────┘  └──────────────┬─────────────────┘ │
└─────────────┼──────────────────────────────┼───────────────────┘
              │                              │
              ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      API LAYER (FastAPI)                         │
│                                                                  │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                │
│  │ REST API   │  │ WebSocket  │  │ Auth &     │                │
│  │ Endpoints  │  │ Handler   │  │ Rate Limit │                │
│  └──────┬─────┘  └──────┬─────┘  └────────────┘                │
│         │               │                                       │
│  ┌──────▼─────┐  ┌──────▼─────┐                                │
│  │ Response   │  │ Redis      │                                │
│  │ Cache      │  │ Pub/Sub    │                                │
│  └────────────┘  └────────────┘                                │
└─────────┬──────────────┬───────────────────────────────────────┘
          │              │
          ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    CONSUMERS                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ Python SDK   │  │ TypeScript   │  │ Direct HTTP  │         │
│  │ (PyPI)       │  │ SDK (future) │  │ Clients       │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 9. Monetization Implementation

### Tier Definitions

| Feature | Free | Pro ($49/mo) | Enterprise ($249/mo) |
|---------|------|---------------|----------------------|
| **Contracts** | 3 (ES, NQ, CL) | 50 | Unlimited |
| **Update frequency** | Daily snapshot | 15-min updates | Real-time (on data arrival) |
| **Historical data** | 4 weeks | 2 years | Full history |
| **WebSocket** | ❌ | ✅ (15-min push) | ✅ (real-time push) |
| **Rate limit** | 60 req/hr | 600 req/hr | 6000 req/hr |
| **Signal types** | Positioning only | All signals | All signals + custom |
| **Term structure** | Current only | Historical | Historical + futures |
| **Support** | Community | Email | Slack + phone |
| **SLA** | Best effort | 99.5% | 99.9% |

### Implementation in Code

```python
# Tier enforcement middleware
TIER_LIMITS = {
    "free": {
        "max_contracts": ["ES", "NQ", "CL"],
        "update_frequency": "daily",
        "history_weeks": 4,
        "websocket": False,
        "rate_limit": 60,  # requests per hour
        "signals": ["positioning"],
        "term_structure": "current_only",
    },
    "pro": {
        "max_contracts": 50,  # numeric = count limit, not list
        "update_frequency": "15min",
        "history_weeks": 104,
        "websocket": True,
        "rate_limit": 600,
        "signals": "all",
        "term_structure": "historical",
    },
    "enterprise": {
        "max_contracts": float("inf"),
        "update_frequency": "realtime",
        "history_weeks": 260,
        "websocket": True,
        "rate_limit": 6000,
        "signals": "all",
        "term_structure": "historical_and_futures",
    },
}
```

### API Key Model

```python
class APIKey(BaseModel):
    key: str  # oil_sk_live_xxxx or oil_sk_test_xxxx
    tier: Literal["free", "pro", "enterprise"]
    user_id: str
    contracts_allowed: list[str] | None  # None = all within tier
    created_at: datetime
    rate_limit_reset_hourly: bool = True
```

### Rate Limiting

- **Redis-based sliding window**: `INCR` + `EXPIRE` per `{api_key}:{window}`
- Free: 60 req/hr, Pro: 600 req/hr, Enterprise: 6000 req/hr
- WebSocket messages count separately: Free=0, Pro=100/hr, Enterprise=unlimited
- Return `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers

---

## 10. Risk Assessment

### High Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **CFTC data delays** | Signals stale by days | Medium | Build stale-data detection, surface `as_of_date` clearly, provide last-known-good fallback |
| **CME API changes/instability** | Pipeline breaks | Medium | Abstract data source behind adapter pattern, maintain manual ingestion fallback, monitor CME status page |
| **Signal accuracy concerns** | User trust loss | Medium | Backtest signals against historical data, publish methodology, label signals as "informational not advice" |
| **Low market demand** | Revenue below projections | Medium | Start with free tier, validate demand before heavy spend, track API usage metrics closely |

### Medium Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Contract mapping errors** | Wrong signals served | Low-Medium | Maintain authoritative mapping table, automated cross-checks against CFTC + CME data |
| **TimescaleDB operational complexity** | Outages, data loss | Low | Use managed TimescaleDB cloud for MVP, automated backups, connection pooling |
| **WebSocket scaling** | Connection limits under load | Low | Redis pub/sub fans out naturally, horizontal scaling via multiple workers |
| **Rate limit abuse on free tier** | API cost overrun | Low | Aggressive free tier limits, bot detection, abuse monitoring |

### Low Risks

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| **Competitor launches similar product** | Market share pressure | Low | First-mover advantage, focus on signal quality over breadth |
| **Regulatory concerns** | Legal issues | Low | No trading advice, clear disclaimers, signals are derived from public data |
| **SDK adoption slow** | Lower than expected reach | Medium | Invest in docs, examples, and community; make REST API primary |

### Assumptions

1. **CFTC COT reports remain free and publicly accessible** — no authentication changes or paywall
2. **CME provides settlement data at no/low cost** — for MVP, daily settlement CSV is sufficient
3. **4 contracts (ES, NQ, CL, GC) are enough to validate product-market fit** — expand post-launch
4. **Developers will pay $49/mo for pre-computed positioning signals** — validated by analogous APIs (Databento pricing, Coinglass success)
5. **TimescaleDB is sufficient for MVP scale** — can migrate to dedicated time-series DB if needed
6. **Weekly COT + daily settlement frequency is sufficient for MVP** — intraday OI is future scope

---

## 11. Directory Structure

```
openinterest-lens/
├── README.md
├── PLAN.md                    # This file
├── RESEARCH.md                # Market research notes
├── PROJECTS.md                # Project tracker (if shared workspace)
│
├── api/                       # FastAPI application
│   ├── __init__.py
│   ├── main.py                # FastAPI app factory
│   ├── config.py              # Settings (env vars, tier config)
│   ├── dependencies.py        # Auth, rate limiting, DB session deps
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── signals.py         # GET /signals/{contract}
│   │   ├── contracts.py       # GET /contracts
│   │   ├── term_structure.py  # GET /term-structure/{contract}
│   │   ├── cot.py             # GET /cot/{contract}
│   │   ├── roll_pressure.py   # GET /roll-pressure/{contract}
│   │   └── websocket.py       # WebSocket /ws/v1/signals
│   ├── models/
│   │   ├── __init__.py
│   │   ├── schemas.py          # Pydantic request/response models
│   │   ├── db_models.py        # SQLAlchemy ORM models
│   │   └── signals.py          # Signal schema models (Section 2)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── signal_computer.py  # Signal computation logic
│   │   ├── cot_processor.py    # COT normalization + z-scores
│   │   ├── term_structure.py   # Term structure builder
│   │   └── roll_pressure.py    # Roll pressure computation
│   └── middleware/
│       ├── __init__.py
│       ├── auth.py             # API key validation + tier checks
│       └── rate_limit.py       # Redis-based rate limiting
│
├── pipeline/                  # Data ingestion pipeline
│   ├── __init__.py
│   ├── celery_app.py          # Celery config + beat schedule
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── ingest_cot.py      # CFTC COT ingestion task
│   │   ├── ingest_settlement.py  # CME settlement ingestion
│   │   ├── compute_signals.py # Signal computation orchestration
│   │   ├── cache_warm.py      # Redis cache warming
│   │   └── data_quality.py    # Data quality checks
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── cftc.py            # CFTC download + parse
│   │   ├── cme.py             # CME API/FTP client
│   │   └── base.py            # Abstract data source adapter
│   └── normalization/
│       ├── __init__.py
│       ├── contract_mapping.py  # Symbol mapping (ES → E-MINI S&P 500)
│       └── validators.py        # Data validation + outlier detection
│
├── db/                        # Database
│   ├── migrations/            # Alembic migrations
│   │   └── versions/
│   ├── seed/                  # Seed data (contract definitions, initial mappings)
│   │   └── contracts.json
│   └── alembic.ini
│
├── sdk/                       # Python SDK
│   ├── openinterest_lens/
│   │   ├── __init__.py
│   │   ├── client.py          # OpenInterestLensClient
│   │   ├── models.py          # Pydantic SDK models
│   │   ├── errors.py          # Custom exceptions
│   │   ├── websocket.py       # Async WebSocket client
│   │   └── config.py          # SDK configuration
│   ├── tests/
│   ├── pyproject.toml
│   └── README.md
│
├── tests/                     # Integration + E2E tests
│   ├── __init__.py
│   ├── conftest.py            # Fixtures, test DB, mock API keys
│   ├── test_api/
│   │   ├── test_signals.py
│   │   ├── test_contracts.py
│   │   ├── test_term_structure.py
│   │   ├── test_cot.py
│   │   ├── test_roll_pressure.py
│   │   └── test_websocket.py
│   ├── test_pipeline/
│   │   ├── test_cot_ingestion.py
│   │   ├── test_settlement_ingestion.py
│   │   └── test_signal_computation.py
│   └── test_sdk/
│       └── test_client.py
│
├── scripts/                   # Utility scripts
│   ├── seed_contracts.py      # Seed contract definitions
│   ├── backfill_cot.py        # Historical COT data backfill
│   └── validate_signals.py    # Signal accuracy validation
│
├── docker/                    # Docker configs
│   ├── Dockerfile.api
│   ├── Dockerfile.worker
│   └── docker-compose.yml
│
├── monitoring/                # Observability
│   ├── prometheus.yml
│   ├── grafana/
│   │   └── dashboards/
│   └── alerting.yml
│
├── docs/                      # Developer documentation
│   ├── api-reference.md
│   ├── quickstart.md
│   ├── signal-methodology.md
│   └── sdk-guide.md
│
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── deploy.yml
│
├── .env.example
├── pyproject.toml              # Root project config
├── requirements.txt
└── Makefile                    # Common tasks (run, test, migrate, seed)
```

---

*Plan created: 2026-05-13 | Status: Ready for development*
## Weeks 9–10 Completion Summary (2026-05-14)

### Monitoring — Prometheus Metrics
- Created `server/app/monitoring/__init__.py`, `metrics.py`, `middleware.py`
- 7 Prometheus metrics: http_requests_total, http_request_duration_seconds, active_websocket_connections, signals_computed_total, data_ingestion_events, data_quality_score, api_key_usage_total
- PrometheusMiddleware captures request duration + status
- `/metrics` endpoint serves Prometheus format
- Added `prometheus-client>=0.20.0` to server dependencies

### Security Review
- API key rotation: `POST /v1/keys/rotate` with configurable grace period (0–72 hours)
- `GET /v1/keys/me` for current key info
- Key revocation support in APIKeyAuth
- Rotated key grace period validation
- CORS hardening: subdomain wildcard matching (`https://*.example.com`)
- Per-endpoint rate limits: stricter for COT (20/hr free), roll pressure (30/hr), settlements
- Input validation: all endpoints use Pydantic models, commodity symbol regex validation
- 37 security tests: key rotation, CORS, rate limits, input validation, expired key rejection

### Landing Page
- `landing/landing.html` — dark-themed single-page landing
- Sections: Hero with stats, Features (5 signal types), API Explorer (4 tabs), Quickstart (5 steps), Pricing (Free/Pro/Enterprise), Footer
- Mobile-responsive, no external JS frameworks
- Served at `/` from FastAPI when `landing/` directory exists

### Developer Onboarding Docs
- `docs/README.md` — Complete project README with architecture, quick start, API overview, SDK usage
- `docs/quickstart.md` — 5-minute tutorial
- `docs/api-reference.md` — Full API reference with all endpoints, parameters, responses, error codes
- `docs/sdk-guide.md` — Python SDK guide: sync, async, WebSocket, builder pattern
- `docs/deployment.md` — Deployment guide: Docker, env vars, Redis, production checklist

### Test Results
- **558 tests passing, 2 skipped** (was 521)
- 37 new security tests added
