"""Compact health digest for the scanner's parquet substrate.

Designed to produce a single-paste output: storage state, per-venue coverage
of the latest cycle, schema integrity, funding distribution, and the top
analytics signals. Append `--log <path>` to also tail the collector log.

    python diagnose.py
    python diagnose.py --log collector.log
"""

import argparse
from pathlib import Path

import analytics
from config import FUNDING_DIR, VENUES
from storage import query


def _h(title: str) -> None:
    print(f"\n=== {title} ===")


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({(100 * n / total):.1f}%)" if total else f"{n}/0"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log", type=Path, help="Optional collector log file to tail.")
    p.add_argument("--tail", type=int, default=15)
    args = p.parse_args()

    files = sorted(FUNDING_DIR.rglob("*.parquet")) if FUNDING_DIR.exists() else []
    if not files:
        print("No parquet files yet. Start with: python run_collector.py --once --print")
        return

    overview = query("""
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT ts_utc) AS cycles,
               COUNT(DISTINCT symbol_canonical) AS symbols,
               MIN(ts_utc) AS earliest, MAX(ts_utc) AS latest
        FROM funding
    """).iloc[0]

    _h("Storage")
    print(f"Files:    {len(files)}")
    print(f"Rows:     {int(overview['rows']):,}")
    print(f"Cycles:   {int(overview['cycles'])}")
    print(f"Symbols:  {int(overview['symbols'])}")
    print(f"Range:    {overview['earliest']}  ->  {overview['latest']}")

    _h("Per-venue coverage (latest cycle)")
    latest_df = query("""
        SELECT exchange, COUNT(*) AS rows
        FROM funding
        WHERE ts_utc = (SELECT MAX(ts_utc) FROM funding)
        GROUP BY exchange
    """)
    seen = dict(zip(latest_df["exchange"], latest_df["rows"]))
    for venue in sorted(VENUES):
        n = int(seen.get(venue, 0))
        status = "OK  " if n > 0 else "FAIL"
        print(f"  {status}  {venue:10s}  {n:>4d} symbols")

    _h("Schema integrity")
    integ = query("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN funding_rate IS NULL THEN 1 ELSE 0 END)        AS null_rate,
               SUM(CASE WHEN apy_norm IS NULL THEN 1 ELSE 0 END)             AS null_apy,
               SUM(CASE WHEN funding_interval_h IS NULL THEN 1 ELSE 0 END)   AS null_interval,
               SUM(CASE WHEN volume_24h_usd IS NULL THEN 1 ELSE 0 END)       AS null_vol,
               SUM(CASE WHEN open_interest_usd IS NULL THEN 1 ELSE 0 END)    AS null_oi,
               SUM(CASE WHEN mark_price IS NULL THEN 1 ELSE 0 END)           AS null_mark
        FROM funding
    """).iloc[0]
    total = int(integ["total"])
    print(f"Total rows:        {total:,}")
    print(f"NULL funding_rate: {_pct(int(integ['null_rate']), total)}")
    print(f"NULL apy_norm:     {_pct(int(integ['null_apy']), total)}")
    print(f"NULL interval_h:   {_pct(int(integ['null_interval']), total)}")
    print(f"NULL volume_24h:   {_pct(int(integ['null_vol']), total)}")
    print(f"NULL open_int:     {_pct(int(integ['null_oi']), total)}")
    print(f"NULL mark_price:   {_pct(int(integ['null_mark']), total)}")

    _h("Funding by interval")
    dist = query("""
        SELECT funding_interval_h AS interval_h,
               COUNT(*) AS rows,
               ROUND(MIN(100*apy_norm), 1) AS min_apy_pct,
               ROUND(APPROX_QUANTILE(100*apy_norm, 0.5), 2) AS p50_apy_pct,
               ROUND(MAX(100*apy_norm), 1) AS max_apy_pct,
               ROUND(APPROX_QUANTILE(100*ABS(apy_norm), 0.99), 1) AS p99_abs_apy_pct
        FROM funding
        GROUP BY 1 ORDER BY 1
    """)
    print(dist.to_string(index=False))

    _h("Top 5 cross-exchange APY-norm deltas (vol >= $500k)")
    sql, params = analytics.cross_exchange_delta(min_volume_usd=500_000)
    df = query(sql, params).head(5)
    if df.empty:
        print("(none)")
    else:
        for _, r in df.iterrows():
            print(
                f"  {r['symbol_canonical']:24s} "
                f"delta={float(r['delta_apy_pct']):+9.1f}%  "
                f"short={r['short_venue']:9s} ({float(r['short_apy_pct']):+8.1f}%)  "
                f"long={r['long_venue']:9s} ({float(r['long_apy_pct']):+8.1f}%)"
            )

    _h("Top 5 anomalies (|APY|>=100%, persistence>=2)")
    sql, params = analytics.anomaly_candidates(
        min_abs_apy_pct=100, min_volume_usd=500_000, min_persistence=2
    )
    df = query(sql, params).head(5)
    if df.empty:
        print("(none)")
    else:
        for _, r in df.iterrows():
            print(
                f"  {r['symbol_canonical']:24s} {r['exchange']:10s}  "
                f"avg={float(r['avg_apy_pct']):+8.1f}%  "
                f"max_abs={float(r['max_abs_apy_pct']):8.1f}%  "
                f"persist={int(r['persistence_count'])}"
            )

    if args.log and args.log.exists():
        _h(f"Log tail ({args.tail} lines from {args.log.name})")
        # Filter to lines that are interesting: WARNING, ERROR, or cycle summaries.
        text = args.log.read_text(errors="ignore").splitlines()
        interesting = [
            ln for ln in text
            if ("WARNING" in ln) or ("ERROR" in ln) or ("cycle:" in ln)
        ]
        for line in (interesting or text)[-args.tail:]:
            print(line)


if __name__ == "__main__":
    main()
