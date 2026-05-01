"""Trace a (symbol, venue) pair's full parquet history.

The dashboard shows `WHERE rn = 1` — the latest STORED row, not the latest CYCLE.
If a venue intermittently drops a pair from its batch funding-rate response,
our 'latest' can be stale by hours while the dashboard happily renders it as
current. This probe surfaces those gaps.

Usage:
    python probe_pair_history.py SYMBOL EXCHANGE [SYMBOL EXCHANGE ...]
    (defaults to RLS/USDT:USDT okx and ST/USDT:USDT bitmart)
"""

import sys

import pandas as pd

from storage import query


def trace(symbol: str, exchange: str) -> None:
    print(f"\n========== {symbol} on {exchange} ==========")
    df = query("""
        SELECT ts_utc, funding_rate, funding_interval_h AS h,
               predicted_rate,
               ROUND(100.0 * apy_norm, 2) AS apy_pct,
               mark_price
        FROM funding
        WHERE symbol_canonical = ? AND exchange = ?
        ORDER BY ts_utc
    """, [symbol, exchange])

    if df.empty:
        print("  (no rows for this pair)")
        return

    # Reference: total cycles + venue's overall cycle coverage
    overall = query(f"""
        SELECT COUNT(DISTINCT ts_utc) AS total_cycles,
               (SELECT COUNT(DISTINCT ts_utc) FROM funding
                WHERE exchange = '{exchange}') AS venue_cycles,
               MIN(ts_utc) AS earliest, MAX(ts_utc) AS latest
        FROM funding
    """).iloc[0]
    total = int(overall["total_cycles"])
    venue_total = int(overall["venue_cycles"])

    pair_cycles = len(df)
    print(f"  Cycles in dataset (overall):       {total}")
    print(f"  Cycles in dataset (this venue):    {venue_total}")
    print(f"  Cycles this pair appeared in:      {pair_cycles}  "
          f"({100.0 * pair_cycles / venue_total:.1f}% of {exchange} cycles)")
    print(f"  Range: {df['ts_utc'].min()}  →  {df['ts_utc'].max()}")

    # Gap analysis — between consecutive observations for THIS pair
    df["gap_min"] = df["ts_utc"].diff().dt.total_seconds() / 60
    gaps = df["gap_min"].dropna()
    if len(gaps):
        print(f"\n  Gap between consecutive cycles where this pair appeared:")
        print(f"    median: {gaps.median():>6.1f} min")
        print(f"    p95:    {gaps.quantile(0.95):>6.1f} min")
        print(f"    max:    {gaps.max():>6.1f} min   "
              f"({'⚠ DROPOUT' if gaps.max() > 10 else 'normal cadence'})")

    # Show the rows around the largest gap, and any extreme→normal transitions
    df["abs_apy"] = df["apy_pct"].abs()
    df["was_extreme"] = df["abs_apy"] > 100
    df["transition"] = df["was_extreme"].ne(df["was_extreme"].shift())
    transitions = df[df["transition"]].copy()
    if len(transitions) > 1:  # >1 because the first row is always a "transition"
        print(f"\n  Extreme↔normal transitions ({len(transitions) - 1} flip(s)):")
        for _, row in transitions.iterrows():
            tag = "EXTREME" if row["was_extreme"] else "normal "
            print(f"    {row['ts_utc']}  {tag}  rate={row['funding_rate']}  apy={row['apy_pct']:+.2f}%")

    # Surface the largest gaps
    if len(gaps):
        big_gaps = df[df["gap_min"] > 10].copy()
        if not big_gaps.empty:
            print(f"\n  Suspicious dropouts (>10 min gap, with the values bracketing each gap):")
            for idx, row in big_gaps.iterrows():
                prev_idx = idx - 1
                if prev_idx in df.index:
                    prev = df.loc[prev_idx]
                    print(f"    BEFORE  {prev['ts_utc']}  apy={prev['apy_pct']:+.2f}%")
                print(f"     AFTER  {row['ts_utc']}  apy={row['apy_pct']:+.2f}%   "
                      f"(gap = {row['gap_min']:.1f} min)")
                print()


def main() -> None:
    args = sys.argv[1:]
    if len(args) % 2 != 0 or not args:
        pairs = [("RLS/USDT:USDT", "okx"), ("ST/USDT:USDT", "bitmart")]
    else:
        pairs = list(zip(args[0::2], args[1::2]))

    pd.set_option("display.width", 200)
    for sym, exc in pairs:
        trace(sym, exc)


if __name__ == "__main__":
    main()
