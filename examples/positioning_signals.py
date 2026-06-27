#!/usr/bin/env python3
"""
OpenInterest Lens — Positioning Signals Notebook
=================================================

Demonstrates how to fetch and visualize smart money positioning signals
for futures contracts using the OpenInterest Lens SDK.

Requirements:
    pip install openinterest-lens matplotlib pandas
"""

import os

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY = os.environ.get("OIL_API_KEY", "oil_sk_test_development_key")
BASE_URL = os.environ.get("OIL_BASE_URL", "http://localhost:8000")
SYMBOLS = ["ES", "NQ", "CL", "GC"]  # S&P 500, Nasdaq, Crude Oil, Gold


def fetch_positioning_signals():
    """Fetch positioning signals for multiple contracts."""
    from sdk import OpenInterestLensClient

    client = OpenInterestLensClient(api_key=API_KEY, base_url=BASE_URL)

    results = {}
    for symbol in SYMBOLS:
        try:
            response = client.get_signals(symbol)
            sig = response.signal
            results[symbol] = sig
            print(f"\n{'='*60}")
            print(f"  {symbol} — Positioning Signal")
            print(f"{'='*60}")
            print(f"  Smart Money Direction : {sig.smart_money.direction}")
            print(f"  Conviction Level      : {sig.smart_money.conviction}")
            print(f"  Z-Score               : {sig.smart_money.z_score:.2f}")
            print(f"  Percentile            : {sig.smart_money.percentile:.1f}%")
            print(f"  Commercial Net        : {sig.net_position.commercial:,}")
            print(f"  Non-Commercial Net    : {sig.net_position.non_commercial:,}")
            print(f"  Non-Reportable Net    : {sig.net_position.non_reportable:,}")
            print(f"  Retail Contrarian     : {sig.retail.contrarian_signal}")
            print(f"  Composite Overall     : {sig.signal.overall}")
            print(f"  Composite Strength    : {sig.signal.strength:.2f}")
        except Exception as e:
            print(f"  {symbol}: Error — {e}")

    client.close()
    return results


def visualize_signals(results: dict):
    """Visualize positioning signals as a bar chart."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend
    except ImportError:
        print("\n[INFO] matplotlib not installed — skipping visualization")
        print("       Install with: pip install matplotlib")
        return

    if not results:
        print("\n[WARN] No data to visualize")
        return

    symbols = list(results.keys())
    z_scores = [results[s].smart_money.z_score for s in symbols]
    directions = [results[s].smart_money.direction for s in symbols]
    colors = ["#22c55e" if d == "long" else "#ef4444" if d == "short" else "#94a3b8" for d in directions]

    fig, ax = plt.subplots(figsize=(10, 6))
    bars = ax.bar(symbols, z_scores, color=colors, edgecolor="white", linewidth=1.5)

    ax.set_title("Smart Money Z-Score by Contract", fontsize=16, fontweight="bold")
    ax.set_xlabel("Contract", fontsize=12)
    ax.set_ylabel("Z-Score", fontsize=12)
    ax.axhline(y=0, color="#64748b", linestyle="--", linewidth=0.8)
    ax.axhline(y=2, color="#22c55e", linestyle=":", linewidth=0.8, alpha=0.5)
    ax.axhline(y=-2, color="#ef4444", linestyle=":", linewidth=0.8, alpha=0.5)

    for bar, score in zip(bars, z_scores):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.1,
                f"{score:.2f}", ha="center", va="bottom", fontsize=10, fontweight="bold")

    plt.tight_layout()
    plt.savefig("positioning_signals.png", dpi=150, bbox_inches="tight")
    print(f"\n📊 Chart saved to positioning_signals.png")


if __name__ == "__main__":
    print("OpenInterest Lens — Positioning Signals Demo")
    print("=" * 60)
    results = fetch_positioning_signals()
    visualize_signals(results)
