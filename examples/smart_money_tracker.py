#!/usr/bin/env python3
"""Smart Money Tracker — monitor positioning z-scores and detect threshold crossings."""

from datetime import date, timedelta

from sdk import OpenInterestLensClient

API_KEY = "oil_sk_live_your_api_key_here"

# Thresholds for alerts
ZSCORE_EXTREME = 2.0  # Very strong signal
ZSCORE_MODERATE = 1.5  # Notable signal

SYMBOLS = ["ES", "NQ", "CL", "GC"]


def classify_zscore(z: float) -> str:
    """Classify z-score magnitude."""
    az = abs(z)
    if az >= ZSCORE_EXTREME:
        return "🔴 EXTREME"
    elif az >= ZSCORE_MODERATE:
        return "🟡 MODERATE"
    else:
        return "⚪ Normal"


def main():
    client = OpenInterestLensClient(api_key=API_KEY)

    print("=" * 60)
    print("Smart Money Tracker")
    print("=" * 60)

    end_date = date.today()
    start_date = end_date - timedelta(weeks=4)

    for symbol in SYMBOLS:
        try:
            resp = client.get_signals(symbol)
            sig = resp.signal

            zscore = sig.smart_money.z_score
            classification = classify_zscore(zscore)

            print(f"\n{symbol:>4s} | {classification}")
            print(f"     z-score: {zscore:+.3f}")
            print(f"     direction: {sig.signal.overall}")
            print(f"     commercial net: {sig.net_position.commercial:,}")
            print(f"     as of: {sig.timestamp}")

            # Alert on extreme readings
            if abs(zscore) >= ZSCORE_EXTREME:
                direction = "BULLISH" if zscore > 0 else "BEARISH"
                print(f"  ⚡ ALERT: {symbol} extreme smart money positioning ({direction})!")

        except Exception as e:
            print(f"\n{symbol:>4s} | ⚠️  Error: {e}")

    client.close()

    print("\n" + "=" * 60)
    print("Scan complete")


if __name__ == "__main__":
    main()
