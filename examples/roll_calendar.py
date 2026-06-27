#!/usr/bin/env python3
"""Roll Calendar — show roll pressure across all contracts, highlight upcoming rolls."""

from sdk import OpenInterestLensClient

API_KEY = "oil_sk_live_your_api_key_here"

# Threshold: contracts expiring within N days get flagged
ROLL_URGENCY_DAYS = 10

SYMBOLS = ["ES", "NQ", "CL", "GC"]


def main():
    client = OpenInterestLensClient(api_key=API_KEY)

    print("=" * 72)
    print("Roll Calendar — Upcoming Roll Pressure")
    print("=" * 72)

    urgent_rolls = []
    all_rolls = []

    for symbol in SYMBOLS:
        try:
            roll = client.get_roll_pressure(symbol)

            if roll.roll_pressure:
                days_left = roll.roll_pressure.days_to_expiry
                pressure = roll.roll_pressure.index
            else:
                days_left = 999
                pressure = 0.0

            nearby = roll.roll_calendar.nearby_month if roll.roll_calendar else "?"

            # Urgency indicator
            if days_left <= ROLL_URGENCY_DAYS:
                urgency = "🔴 URGENT"
                urgent_rolls.append((symbol, days_left, pressure))
            elif days_left <= ROLL_URGENCY_DAYS * 2:
                urgency = "🟡 Watch"
            else:
                urgency = "🟢 Clear"

            all_rolls.append((symbol, days_left, pressure, urgency))

            print(
                f"\n  {symbol:>4s} │ {nearby:>8s} │ "
                f"Days: {days_left:>3d} │ Pressure: {pressure:>6.2f} │ {urgency}"
            )

        except Exception as e:
            print(f"\n  {symbol:>4s} │ ⚠️  {e}")

    # Summary of urgent rolls
    if urgent_rolls:
        print("\n" + "─" * 72)
        print("⚡ URGENT ROLLS (≤10 days to expiry):")
        for symbol, days, pressure in sorted(urgent_rolls, key=lambda x: x[1]):
            print(f"  • {symbol}: {days} days left, pressure index {pressure:.2f}")
    else:
        print("\n✓ No urgent rolls detected.")

    print("\n" + "=" * 72)
    client.close()


if __name__ == "__main__":
    main()
