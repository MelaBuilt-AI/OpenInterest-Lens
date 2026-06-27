#!/usr/bin/env python3
"""
OpenInterest Lens — Term Structure & Roll Calendar Notebook
=============================================================

Demonstrates fetching term structure curves, contango/backwardation analysis,
and roll calendar data for futures contracts.

Requirements:
    pip install openinterest-lens matplotlib pandas
"""

import os

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY = os.environ.get("OIL_API_KEY", "oil_sk_test_development_key")
BASE_URL = os.environ.get("OIL_BASE_URL", "http://localhost:8000")
SYMBOLS = ["CL", "ES"]  # Crude Oil, S&P 500


def fetch_term_structure():
    """Fetch and display term structure curves."""
    from sdk import OpenInterestLensClient

    client = OpenInterestLensClient(api_key=API_KEY, base_url=BASE_URL)

    results = {}
    for symbol in SYMBOLS:
        try:
            response = client.get_term_structure(symbol)
            curve = response.term_structure
            cb = response.contango_backwardation
            results[symbol] = response

            print(f"\n{'='*60}")
            print(f"  {symbol} — Term Structure")
            print(f"{'='*60}")
            if curve:
                front_month = curve.months[0].month if curve.months else "N/A"
                print(f"  Structure Type    : {curve.structure_type}")
                print(f"  Front Month       : {front_month}")
            if cb:
                print(f"  M1-M2 Spread      : {cb.m1_m2_spread:.2f}")
                print(f"  M1-M2 Annualized  : {cb.m1_m2_annualized:.2f}%")
                print(f"  Confidence        : {cb.confidence:.2f}")

            if curve:
                print(f"\n  Curve Points:")
                for point in curve.months[:5]:  # Show first 5
                    print(f"    {point.month}: OI={point.open_interest:,} | "
                          f"Price={point.settlement:.2f} | "
                          f"Volume={point.volume:,}")
                if len(curve.months) > 5:
                    print(f"    ... and {len(curve.months) - 5} more months")

        except Exception as e:
            print(f"  {symbol}: Error — {e}")

    client.close()
    return results


def fetch_roll_pressure():
    """Fetch roll pressure index for contracts."""
    from sdk import OpenInterestLensClient

    client = OpenInterestLensClient(api_key=API_KEY, base_url=BASE_URL)

    results = {}
    for symbol in SYMBOLS:
        try:
            response = client.get_roll_pressure(symbol)
            rp = response.roll_pressure
            cal = response.roll_calendar
            impact = response.roll_impact
            results[symbol] = response

            print(f"\n{'='*60}")
            print(f"  {symbol} — Roll Pressure")
            print(f"{'='*60}")
            if rp:
                print(f"  Roll Pressure Index : {rp.index:.2f}")
                print(f"  Days to Expiry     : {rp.days_to_expiry}")
                print(f"  OI Decay Rate      : {rp.oi_decay_pct:.2f}%")
            if cal:
                print(f"  Days to Roll       : {cal.days_to_roll}")
                print(f"  Roll Urgency       : {cal.roll_urgency}")
            if impact:
                print(f"  Impact Score       : {impact.impact_score:.2f}")
                print(f"  Impact Category    : {impact.impact_category}")

        except Exception as e:
            print(f"  {symbol}: Error — {e}")

    client.close()
    return results


def visualize_term_structure(results: dict):
    """Visualize term structure curves."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        print("\n[INFO] matplotlib not installed — skipping visualization")
        return

    curves = {}
    for symbol, resp in results.items():
        if resp.term_structure:
            curves[symbol] = resp.term_structure

    if not curves:
        print("\n[WARN] No data to visualize")
        return

    fig, axes = plt.subplots(1, len(curves), figsize=(6 * len(curves), 5))
    if len(curves) == 1:
        axes = [axes]

    for ax, (symbol, curve) in zip(axes, curves.items()):
        months = [p.month for p in curve.months]
        oi_values = [p.open_interest for p in curve.months]
        prices = [p.settlement for p in curve.months]

        color = "#3b82f6" if curve.structure_type == "contango" else "#ef4444"

        ax2 = ax.twinx()
        ax.bar(months, oi_values, alpha=0.3, color=color, label="Open Interest")
        ax2.plot(months, prices, "o-", color="#f59e0b", linewidth=2, markersize=6, label="Price")

        ax.set_title(f"{symbol} — {curve.structure_type.title()}", fontsize=14, fontweight="bold")
        ax.set_xlabel("Month")
        ax.set_ylabel("Open Interest", color=color)
        ax2.set_ylabel("Price ($)", color="#f59e0b")
        ax.tick_params(axis="x", rotation=45)

    plt.tight_layout()
    plt.savefig("term_structure.png", dpi=150, bbox_inches="tight")
    print(f"\n📊 Chart saved to term_structure.png")


if __name__ == "__main__":
    print("OpenInterest Lens — Term Structure & Roll Calendar Demo")
    print("=" * 60)
    ts_results = fetch_term_structure()
    rp_results = fetch_roll_pressure()
    visualize_term_structure(ts_results)
