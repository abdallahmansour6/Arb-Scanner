"""Generate synthetic observations for offline dashboard / analytics validation.

Use this when the local machine can't reach the venues (geo-block) but you
still want to exercise the storage, SQL views, and dashboard end-to-end.

    python seed_synthetic.py --cycles 12 --symbols 60

Writes parquet files under data/funding/ exactly like the real collector does.
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from config import EPOCHS_PER_YEAR, VENUES
from storage import write_observations

BASE_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "TRX", "MATIC", "DOT", "LTC", "BCH", "NEAR", "ATOM", "FIL", "APT", "ARB",
    "OP", "SUI", "INJ", "TIA", "SEI", "JUP", "WIF", "PEPE", "BONK", "FLOKI",
    "ORDI", "RNDR", "FET", "AGIX", "RUNE", "ENA", "PYTH", "HBAR", "ICP", "STX",
    "AAVE", "UNI", "MKR", "LDO", "ARKM", "JTO", "MANTA", "ALT", "DYM", "STRK",
    "PIXEL", "PORTAL", "AXL", "ETHFI", "ONDO", "WLD", "SUSHI", "CRV", "GMX", "DYDX",
]


def gen_funding_rate(symbol: str, base_drift: float) -> float:
    """Most pairs near zero; ~10% are anomalies; some extreme."""
    roll = random.random()
    if roll < 0.85:
        return random.gauss(0, 0.0002) + base_drift  # -0.05% to +0.05% typical
    if roll < 0.97:
        return random.choice([-1, 1]) * random.uniform(0.001, 0.005) + base_drift
    return random.choice([-1, 1]) * random.uniform(0.01, 0.03) + base_drift  # extreme


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cycles", type=int, default=8, help="Number of historical cycles to generate.")
    p.add_argument("--interval-s", type=int, default=60, help="Spacing between cycles.")
    p.add_argument("--symbols", type=int, default=50, help="Number of symbols per venue.")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    random.seed(args.seed)
    symbols = BASE_SYMBOLS[: args.symbols]
    venues = list(VENUES.keys())

    # Per-symbol drift makes some symbols look anomalous globally.
    symbol_drift = {s: random.gauss(0, 0.001) for s in symbols}

    end = datetime.now(timezone.utc).replace(microsecond=0)
    total_rows = 0
    for c in range(args.cycles):
        cycle_ts = end - timedelta(seconds=args.interval_s * (args.cycles - 1 - c))
        rows = []
        for venue in venues:
            for base in symbols:
                interval_h = random.choice([8, 8, 8, 4, 4, 1])
                rate = gen_funding_rate(base, symbol_drift[base]) + random.gauss(0, 0.0003)
                next_ts = cycle_ts + timedelta(hours=interval_h)
                price = random.uniform(0.5, 70000)
                vol = random.lognormvariate(15, 2)            # log-normal: heavy tail
                oi = random.lognormvariate(13, 1.5) if random.random() < 0.4 else None
                rows.append({
                    "ts_utc":             cycle_ts,
                    "exchange":           venue,
                    "symbol_canonical":   f"{base}/USDT:USDT",
                    "funding_rate":       rate,
                    "funding_interval_h": interval_h,
                    "predicted_rate":     rate + random.gauss(0, 0.0001),
                    "next_funding_ts":    next_ts,
                    "mark_price":         price,
                    "index_price":        price * (1 + random.gauss(0, 0.0001)),
                    "open_interest_usd":  oi,
                    "volume_24h_usd":     vol,
                    "apy_norm":           rate * (EPOCHS_PER_YEAR / interval_h),
                })
        path = write_observations(rows)
        total_rows += len(rows)
        print(f"cycle {c+1}/{args.cycles} @ {cycle_ts:%Y-%m-%dT%H:%M:%SZ}: {len(rows)} rows -> {path.name}")
    print(f"\nTotal: {total_rows} rows across {args.cycles} cycles x {len(venues)} venues x {len(symbols)} symbols")


if __name__ == "__main__":
    main()
