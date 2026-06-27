#!/usr/bin/env python3
"""Quickstart — sync client basics: connect, get signals, term structure, roll pressure."""

from sdk import OpenInterestLensClient

# Replace with your API key (get one at https://openinterest.lens)
API_KEY = "oil_sk_live_your_api_key_here"

with OpenInterestLensClient(api_key=API_KEY) as client:

    print("=" * 60)
    print("OpenInterest Lens — Quickstart")
    print("=" * 60)

    # Health check
    health = client.get_health()
    print(f"\n✓ API Status: {health.status}")
    print(f"  Version: {health.version}")

    # Get available contracts
    contracts = client.get_contracts()
    print(f"\n✓ {len(contracts.contracts)} contracts available:")
    for c in contracts.contracts[:5]:
        print(f"  • {c.symbol}: {c.full_name}")

    # Get positioning signal for E-mini S&P 500
    signal_resp = client.get_signals("ES")
    sig = signal_resp.signal
    print(f"\n✓ ES Positioning Signal:")
    print(f"  Smart money z-score: {sig.smart_money.z_score:.2f}")
    print(f"  Commercial net:     {sig.net_position.commercial:,}")
    print(f"  Signal direction:   {sig.signal.overall}")
    print(f"  As of:              {sig.timestamp}")

    # Get term structure
    ts = client.get_term_structure("CL")
    curve = ts.term_structure
    if curve:
        print(f"\n✓ CL Term Structure ({curve.structure_type}):")
        for point in curve.months[:5]:
            print(f"  {point.month:>10s}  OI: {point.open_interest:>12,}  Price: {point.settlement:>10.2f}")

    # Get roll pressure for ES
    roll = client.get_roll_pressure("ES")
    if roll.roll_pressure:
        print(f"\n✓ ES Roll Pressure:")
        print(f"  Current contract: {roll.roll_calendar.nearby_month if roll.roll_calendar else 'N/A'}")
        print(f"  Roll pressure:    {roll.roll_pressure.index:.2f}")
        print(f"  Days to expiry:   {roll.roll_pressure.days_to_expiry}")

    print("\n" + "=" * 60)
    print("Quickstart complete! 🐺")
