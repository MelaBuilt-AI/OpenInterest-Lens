"""API examples endpoint — /v1/examples.

Returns sample data for each API endpoint to help developers
understand the response format without making live queries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from app.dependencies import require_api_key
from app.middleware.auth import TierInfo

router = APIRouter(tags=["examples"])


@router.get("/examples")
async def get_examples(
    tier_info: TierInfo = Depends(require_api_key),
):
    """Get sample request/response examples for all API endpoints.

    Returns example data for each endpoint, showing the expected
    request format and response shape. Useful for onboarding
    and integration testing.
    """
    examples = {
        "positioning": {
            "description": "Smart money positioning signals from COT data",
            "endpoints": {
                "all_commodities": {
                    "method": "GET",
                    "path": "/v1/signals/positioning",
                    "params": {
                        "lookback_weeks": "int (4-260, default 52)",
                        "commodity": "Optional filter to a single symbol",
                    },
                    "example_response": {
                        "signals": [
                            {
                                "contract": "ES",
                                "smart_money": {
                                    "z_score": 1.85,
                                    "percentile": 0.82,
                                    "direction": "bullish",
                                    "conviction": "high",
                                },
                                "retail": {
                                    "z_score": -1.2,
                                    "percentile": 0.15,
                                    "direction": "bearish",
                                    "contrarian_signal": "bullish",
                                },
                                "signal": {
                                    "direction": "bullish",
                                    "strength": 0.75,
                                    "divergence": False,
                                },
                                "metadata": {
                                    "commodity": "ES",
                                    "lookback_weeks": 52,
                                    "data_points": 52,
                                    "computed_at": datetime.now(UTC).isoformat(),
                                    "cache_hit": False,
                                },
                            }
                        ],
                        "computed_at": datetime.now(UTC).isoformat(),
                    },
                },
                "single_commodity": {
                    "method": "GET",
                    "path": "/v1/signals/positioning/{commodity}",
                    "params": {
                        "commodity": "Contract symbol (ES, NQ, CL, GC)",
                        "lookback_weeks": "int (4-260, default 52)",
                    },
                    "example_response": {
                        "contract": "CL",
                        "smart_money": {
                            "z_score": -0.5,
                            "percentile": 0.35,
                            "direction": "bearish",
                            "conviction": "moderate",
                        },
                        "retail": {
                            "z_score": 0.8,
                            "percentile": 0.72,
                            "direction": "bullish",
                            "contrarian_signal": "bearish",
                        },
                        "signal": {
                            "direction": "bearish",
                            "strength": 0.4,
                            "divergence": True,
                        },
                        "metadata": {
                            "commodity": "CL",
                            "lookback_weeks": 52,
                            "data_points": 52,
                            "computed_at": datetime.now(UTC).isoformat(),
                            "cache_hit": False,
                        },
                    },
                },
            },
        },
        "term_structure": {
            "description": "Term structure curves with contango/backwardation indicators",
            "endpoints": {
                "single_commodity": {
                    "method": "GET",
                    "path": "/v1/signals/term-structure/{commodity}",
                    "params": {
                        "date": "Optional as-of date (YYYY-MM-DD)",
                    },
                    "example_response": {
                        "contract": "ES",
                        "term_structure": {
                            "structure_type": "contango",
                            "months": [
                                {
                                    "month": "Jun 26",
                                    "expiry_date": "2026-06-19",
                                    "settlement": 5900.0,
                                    "open_interest": 1200000,
                                    "volume": 1500000,
                                    "spread_to_front": 0.0,
                                    "annualized_yield": 0.0,
                                },
                                {
                                    "month": "Sep 26",
                                    "expiry_date": "2026-09-18",
                                    "settlement": 5925.0,
                                    "open_interest": 800000,
                                    "volume": 900000,
                                    "spread_to_front": 25.0,
                                    "annualized_yield": 0.017,
                                },
                            ],
                            "front_month_oi": 1200000,
                            "total_oi": 2700000,
                            "oi_concentration_pct": 44.4,
                            "steepness": 0.004,
                        },
                        "contango_backwardation": {
                            "structure_type": "contango",
                            "m1_m2_spread": 25.0,
                            "m1_m2_annualized": 0.017,
                            "spread_z_score": 0.5,
                            "confidence": "moderate",
                            "slope": "positive",
                        },
                    },
                },
            },
        },
        "roll_pressure": {
            "description": "Roll pressure index and roll calendar",
            "endpoints": {
                "single_commodity": {
                    "method": "GET",
                    "path": "/v1/roll-pressure/{commodity}",
                    "params": {
                        "start_date": "Optional start date (YYYY-MM-DD)",
                        "end_date": "Optional end date (YYYY-MM-DD)",
                        "days_back": "int (1-365, default 30)",
                    },
                    "example_response": {
                        "contract": "ES",
                        "roll_pressure": {
                            "index": 0.65,
                            "oi_decay_pct": 12.5,
                            "spread_basis": 2.5,
                            "days_to_expiry": 8,
                            "roll_window": "active",
                        },
                        "roll_calendar": {
                            "nearby_month": "Jun 26",
                            "nearby_expiry": "2026-06-19",
                            "deferred_month": "Sep 26",
                            "deferred_expiry": "2026-09-18",
                            "days_to_roll": 8,
                            "roll_start_date": "2026-06-05",
                            "roll_end_date": "2026-06-19",
                            "roll_urgency": "high",
                        },
                        "roll_impact": {
                            "impact_score": 0.7,
                            "oi_concentration": 0.44,
                            "volume_shift": 0.3,
                            "expected_slippage": 0.25,
                            "impact_category": "moderate",
                        },
                    },
                },
            },
        },
        "cot": {
            "description": "Raw COT data with computed Z-scores and percentiles",
            "endpoints": {
                "single_contract": {
                    "method": "GET",
                    "path": "/v1/cot/{contract}",
                    "params": {
                        "start_date": "Optional start date (YYYY-MM-DD)",
                        "end_date": "Optional end date (YYYY-MM-DD)",
                        "format": "Optional: 'full' (default) or 'summary'",
                    },
                    "example_response": {
                        "contract": "ES",
                        "reports": [
                            {
                                "as_of_date": "2026-05-12",
                                "published_date": "2026-05-15",
                                "commercial": {
                                    "long": 800000,
                                    "short": 750000,
                                    "net": 50000,
                                    "z_score_52w": 1.2,
                                    "percentile_52w": 0.85,
                                },
                                "non_commercial": {
                                    "long": 600000,
                                    "short": 650000,
                                    "net": -50000,
                                    "z_score_52w": -0.8,
                                    "percentile_52w": 0.25,
                                },
                            }
                        ],
                    },
                },
            },
        },
        "contracts": {
            "description": "List tracked futures contracts",
            "endpoints": {
                "list": {
                    "method": "GET",
                    "path": "/v1/contracts",
                    "params": {
                        "exchange": "Optional filter (CME, NYMEX, COMEX)",
                        "asset_class": "Optional filter (equity_index, energy, metal)",
                    },
                },
            },
        },
        "quality": {
            "description": "Data quality monitoring (staleness, gaps, completeness)",
            "endpoints": {
                "full_report": {
                    "method": "GET",
                    "path": "/v1/quality",
                    "params": {
                        "contract": "Optional specific contract symbol",
                    },
                    "example_response": {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "contracts": ["ES", "NQ", "CL", "GC"],
                        "overall_health": "healthy",
                        "cot_staleness": [
                            {
                                "source": "cot",
                                "contract": "ES",
                                "is_stale": False,
                                "last_data_date": "2026-05-06",
                                "days_since_last": 8,
                                "threshold_days": 14,
                                "warning": None,
                            }
                        ],
                        "warnings": [],
                    },
                },
                "staleness": {
                    "method": "GET",
                    "path": "/v1/quality/staleness?contract=ES",
                },
                "gaps": {
                    "method": "GET",
                    "path": "/v1/quality/gaps?contract=ES",
                },
                "completeness": {
                    "method": "GET",
                    "path": "/v1/quality/completeness?contract=ES",
                },
            },
        },
        "ingestion": {
            "description": "Trigger and monitor data ingestion (Pro+ only)",
            "endpoints": {
                "trigger_cot": {
                    "method": "POST",
                    "path": "/v1/ingestion/cot",
                    "params": {"report_type": "'futures' or 'combined'"},
                    "note": "Requires Pro or Enterprise tier",
                },
                "trigger_settlements": {
                    "method": "POST",
                    "path": "/v1/ingestion/settlements",
                    "params": {"symbols": "Comma-separated contract symbols"},
                    "note": "Requires Pro or Enterprise tier",
                },
                "status": {
                    "method": "GET",
                    "path": "/v1/ingestion/status",
                },
            },
        },
        "websocket": {
            "description": "Real-time signal updates via WebSocket (Pro+ only)",
            "endpoint": "ws://host/ws/v1/signals?api_key=YOUR_KEY",
            "actions": {
                "auth": '{"action": "auth", "api_key": "YOUR_KEY"}',
                "subscribe": '{"action": "subscribe", "signal_types": ["positioning"], "contracts": ["ES"]}',
                "unsubscribe": '{"action": "unsubscribe", "signal_types": ["positioning"], "contracts": ["ES"]}',
                "ping": '{"action": "ping"}',
            },
        },
        "authentication": {
            "description": "All endpoints require X-API-Key header",
            "header": "X-API-Key: oil_sk_live_YOUR_KEY",
            "tiers": {
                "free": "60 req/hr, ES/NQ/CL only, no WebSocket, no ingestion",
                "pro": "600 req/hr, all contracts, WebSocket, ingestion, 104 weeks history",
                "enterprise": "6000 req/hr, all contracts, real-time WebSocket, full history",
            },
        },
    }

    return examples