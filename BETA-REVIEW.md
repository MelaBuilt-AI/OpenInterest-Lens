# 🐺 OpenInterest Lens — Beta Launch Review

**Date:** 2026-06-03 | **Reviewer:** Mela (Autonomous Agent)
**Tests:** 576 ✅ passed, 2 ⏭️ skipped, 22.4s | **Warnings:** 2,618

---

## Executive Summary

The OpenInterest Lens project is **structurally complete** — the server is well-architected, feature-rich, and the test suite is comprehensive. However, there are **critical inconsistencies** between the actual SDK/API surface and both the docs and example scripts. These must be resolved before beta launch. The good news: none of the issues are architectural — they're all documentation/code alignment problems.

---

## 🔴 Critical Issues (Must Fix Before Beta)

### 1. All 5 Example Scripts Are Broken
Every script in `examples/` references methods and attributes that don't exist on the current SDK.

**File: `examples/quickstart.py`**
- `client.health()` → should be `client.get_health()`
- `client.list_contracts()` → should be `client.get_contracts()`
- `client.get_signal("ES")` → should be `client.get_signals("ES")`
- `signal.smart_money_zscore` → should be `signal.smart_money.z_score`
- `signal.commercial_net` → should be `signal.net_position.commercial`
- `signal.direction` → should be `signal.signal.overall`
- `signal.date` → no such field; use `signal.timestamp`
- `ts.curve[0].contract, .open_interest` → should be `ts.term_structure.months[0].month`
- `roll.current_contract, .pressure_index, .days_to_expiry` → should be `roll.roll_pressure.index`, `roll.roll_calendar.days_to_roll`

**File: `examples/smart_money_tracker.py`**
- `client.get_signal(symbol)` → `client.get_signals(symbol)`
- Same nested attribute mismatch as quickstart
- References symbols `["ES", "NQ", "CL", "GC", "ZN"]` — `ZN` not in the 4 MVP contracts

**File: `examples/roll_calendar.py`**
- `client.get_roll_pressure(symbol)` is correct (exists on SDK) ✓
- `roll.days_to_expiry` → should be `roll.roll_pressure.days_to_expiry`
- `roll.pressure_index` → should be `roll.roll_pressure.index`
- `roll.current_contract` → should be `roll.contract` (top-level)

**File: `examples/async_streaming.py`**
- `client.stream_signals(contracts)` → doesn't exist on `AsyncOpenInterestLensClient`
- `update.smart_money_zscore, .direction, .open_interest` → wrong attribute depth

**File: `examples/term_structure_demo.py`**
- Imports from `openinterest_lens` → should be from `sdk`
- Uses `OpenInterestLensClient` which is correct (sync client) ✓
- `response.data` → `get_term_structure()` returns `TermStructureResponse` directly, no `.data` wrapper
- `ts.curve` → `ts.term_structure.months`
- `ts.front_month` → no such field
- `ts.m1_m2_spread` → `ts.contango_backwardation.m1_m2_spread`
- `ts.contango_pct` → no such field

### 2. All SDK Docs Reference a Non-Existent API

Every doc file references `OILClient` and a sub-client pattern that doesn't exist:

| Doc File | Incorrect Pattern | Actual SDK |
|---|---|---|
| `docs/quickstart.md`, `docs/sdk-guide.md`, `docs/README.md` | `from openinterest_lens import OILClient` | `from sdk import OpenInterestLensClient` |
| All docs | `client.signals.positioning("ES")` | `client.get_signals("ES")` |
| All docs | `client.cot("CL")` | `client.get_cot("CL")` |
| All docs | `client.roll_pressure("ES")` | `client.get_roll_pressure("ES")` |
| All docs | `client.term_structure("GC")` | `client.get_term_structure("GC")` |
| All docs | `client.quality()` | `client.get_quality()` — doesn't exist |
| `docs/sdk-guide.md` | `AsyncOILClient` | `AsyncOpenInterestLensClient` |
| `docs/sdk-guide.md` | `OILClientBuilder` | `ClientBuilder` |
| `docs/sdk-guide.md` | `OILError` | `OpenInterestLensError` |

**Effect:** Developers following the docs will get `ImportError` and `AttributeError` on every single line.

### 3. SDK Pub/Namespace Mismatch

The SDK is structured as a flat `sdk/` package (import as `from sdk import ...`), but:
- Root `pyproject.toml` correctly packages this as `packages = ["sdk", "admin"]` → works for local dev
- `sdk/pyproject.toml` has `packages = ["src/openinterest_lens"]` → **cannot publish to PyPI** (no `src/openinterest_lens/` dir exists)
- `sdk/README.md` and all docs reference `from openinterest_lens import ...` → wrong namespace

**Fix:** Either restructure SDK to `src/openinterest_lens/` layout, or change `sdk/pyproject.toml` packaging config to match the flat layout, and update all imports/docs to use the correct namespace.

### 4. `app/database.py` Reads Wrong Env Var

```python
# database.py (line ~23)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./openinterest_lens.db")

# config.py uses:
OIL_DATABASE_URL  # (via pydantic-settings env_prefix="OIL_")
```

When running with Docker Compose (which sets `OIL_DATABASE_URL`), the database module will silently fall back to SQLite because it reads the unprefixed `DATABASE_URL`. The application will appear to run but persist to a local SQLite file instead of TimescaleDB.

**Fix:** Change `os.getenv("DATABASE_URL", ...)` to `os.getenv("OIL_DATABASE_URL", ...)` in `database.py`.

---

## 🟡 High Priority

### 5. 2,618 Deprecation Warnings

All from `datetime.datetime.utcnow()` usage in SQLAlchemy ORM models and Pydantic validators. Python 3.14 deprecated `utcnow()` in favor of timezone-aware `datetime.now(timezone.utc)`.

**Affected files:**
- `server/app/models/db.py` — `default=datetime.utcnow` on every timestamp column
- All Pydantic models with default datetimes

**Impact:** Not breaking now, but will become warnings in future Python versions. Silences legitimate test output.

### 6. CI MyPy Check is Vacuous

```yaml
- name: MyPy type check
  run: mypy server/app/ --ignore-missing-imports || true
```

The `|| true` means the step **never fails** regardless of type errors. Mypy is running but unreported.

**Fix:** Remove `|| true` or output violations as annotations.

### 7. `docs/onboarding.md` is Vestigial

This file references `LensClient`, `LensAPIError`, and `get_positioning()` — names from an earlier product version. It's completely disconnected from the current codebase and will confuse new developers.

### 8. Duplicate Landing Pages

Two landing page files exist:
- `landing/landing.html` — the polished beta landing (served by FastAPI)
- `landing/index.html` — an earlier/alternative version (not served, dead file)

The server explicitly references `landing/landing.html` (correct), but having a dead `index.html` will confuse future maintainers.

### 9. SDK `get_quality()` Method Does Not Exist

The docs reference `client.quality()` / `client.get_quality()` but this method is not implemented in the sync or async SDK clients. The server has `GET /v1/quality` but the SDK never routes to it.

---

## 🟢 Low Priority / Nice to Have

### 10. Signal Cache Import Redundancy

`server/app/routers/signals.py` imports both `get_signal_cache` and has inline cache logic. The `api.py` (canonical API) uses `get_cache_service` instead. The two cache services may have diverging behavior.

### 11. Worker Redundancy in Docker

The worker container and API container both use the same `Dockerfile`. The worker runs `python -m app.ingestion.scheduler` which is designed as a standalone asyncio scheduler. For production, consider separating the Dockerfiles or using Celery worker as stated in the README.

### 12. SDK Model vs Server Response Alignment

The SDK's `TermStructureResponse` model expects `term_structure: Optional[TermStructureCurve]` but the server's `/v1/term-structure/{contract}` endpoint returns a flat dict with `term_structure` containing inline data (not a `TermStructureCurve` model). The SDK's `model_validate()` may fail depending on field presence.

Similarly, `RollPressureResponse` SDK model expects `RollPressureMetrics` in `roll_pressure` but the server returns a dict. This works if Pydantic accepts dicts (it does for v2), but response format drift could break silently.

---

## ✅ What Looks Great

- **576 passing tests** — comprehensive coverage across API, auth, ingestion, signals, WebSocket, security, and SDK
- **Well-structured server** — clean FastAPI app factory, async everything, proper layering (routers → services → signals)
- **Solid auth system** — tiered API keys, demo keys, rotation with grace periods, rate limiting
- **Production Docker setup** — multi-stage build, non-root user, health checks, resource limits, Prometheus metrics
- **Full WebSocket implementation** — tier-gated update frequencies, heartbeat, reconnection, pub/sub
- **Landing page** — polished dark theme, responsive, feature-complete
- **SDK builder pattern** — `ClientBuilder` fluent API is well-designed
- **Comprehensive error hierarchy** — typed exceptions for every HTTP error code

---

## Recommended Pre-Beta Checklist

| # | Item | Effort | Impact |
|---|------|--------|--------|
| 1 | **Fix 5 example scripts** to match actual SDK API | 2h | 🔴 Drop-dead |
| 2 | **Rewrite docs** (`quickstart.md`, `sdk-guide.md`, `docs/README.md`) to match actual `OpenInterestLensClient` API | 3h | 🔴 Drop-dead |
| 3 | **Fix `app/database.py`** to read `OIL_DATABASE_URL` | 5min | 🔴 Production bug |
| 4 | **Fix SDK packaging** — restructure to `src/openinterest_lens/` and verify `pip install -e .` | 1h | 🔴 PyPI publish |
| 5 | **Update landing page** CTA link if beta is live | 15min | 🟡 User flow |
| 6 | **Remove or update** `docs/onboarding.md` | 15min | 🟡 Clarity |
| 7 | **Fix CI mypy** — remove `|| true` | 5min | 🟡 Quality gate |
| 8 | **Remove dead `landing/index.html`** | 5min | 🟢 Housekeeping |
| 9 | **Add `get_quality()` to both SDK clients** | 30min | 🟢 Completeness |
| 10 | **Fix utcnow deprecations** across all models | 1h | 🟢 Cleanup |

**Estimated total effort:** 8-9 hours (critical items ~6h, nice-to-haves ~3h)

---

## Verdict

**✅ Ready for beta AFTER fixing items #1–4 from the checklist above.** The server and SDK are functionally complete and well-tested. The issues are entirely in the documentation/example layer, where the code drifted from an earlier API design. Once those are realigned, the project is solidly launchable.

> 🐺 *"Good code with broken docs is like a husky who knows the route but can't read the map."*
