# OpenInterest Lens — Research Notes

## Product Overview
**Category:** Financial Markets & Trading
**Tagline:** Real-time futures market structure API — OI + COT + term structure as developer-ready signals

### Problem
Futures traders and quant devs spend hours manually stitching together CME PDFs, CFTC CSVs, and broker feeds to understand positioning. There's no single API that delivers open interest, COT data, and term-structure curves as actionable, normalized signals.

### Solution
OpenInterest Lens fuses three data sources into one normalized API endpoint:
- **COT (Commitments of Traders) data** — smart money vs retail positioning
- **Open interest shifts** — real-time OI changes across contracts
- **Term-structure curves** — contango/backwardation signals and roll pressure

Developers get pre-computed positioning signals, roll pressure indices, and contango/backwardation alerts — no manual data wrangling needed.

## Competitive Landscape

| Product | Focus | Gap |
|---------|-------|-----|
| Databento | Raw futures market data feeds | No positioning intelligence or signals |
| iTick | Real-time futures tick data | Raw feeds only, no OI/COT analysis |
| Coinglass | Crypto open interest analytics | Crypto only, no TradFi futures or COT |
| Quandl/Nasdaq | Historical futures data | No real-time signals, no term structure |

**Key differentiator:** Nobody serves normalized OI + COT + term structure as developer-ready positioning signals. Clear whitespace in TradFi futures.

## Target Audience
- Quant developers building systematic futures strategies
- Systematic futures traders needing actionable positioning data
- Algo trading teams at prop firms and hedge funds
- Fintech platforms building futures analytics features

## Revenue Model
- **Free:** 3 contracts, daily snapshots
- **Pro $49/mo:** 50 contracts, 15-min updates
- **Enterprise $249/mo:** Unlimited contracts, real-time WebSocket

## Build Timeline
8–10 weeks to MVP

## Why Now
- Crypto perpetuals made OI analysis mainstream — demand for similar tools in TradFi
- Futures positioning data still requires manual wrangling across 3+ sources
- First-mover advantage in a category that quant teams will pay for
- No unified API delivers OI + COT + term structure as actionable signals

## Technical Considerations (Initial)
- Data sources: CFTC COT reports, CME open interest data, exchange term structures
- Core API: REST + WebSocket for real-time updates
- Signal computation: smart money index, roll pressure, contango/backwardation detection
- Data pipeline: scheduled ingestion, normalization, signal computation
- Storage: time-series DB (QuestDB or TimescaleDB) for OI/price/term-structure data
- SDK: Python-first, then TypeScript
- Rate limiting per tier (3 contracts free, 50 pro, unlimited enterprise)

## Data Source Notes
- CFTC COT reports: Published Friday evenings, covers Tuesday data. Free, public.
- CME Open Interest: Available via CME Group API or FTP. Daily settlement data.
- Term structure: Derived from futures chain prices across expirations.

## Next Steps
- [ ] Define signal schema (positioning signal, roll pressure index, contango alert)
- [ ] Data pipeline architecture (CFTC ingestion, CME ingestion, normalization)
- [ ] Core API design (endpoints for signals, contracts, term structure)
- [ ] MVP scope definition (which contracts, which signals first)
- [ ] Landing page