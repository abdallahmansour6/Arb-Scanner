"""Sanity-check: parquet-stored values vs live venue APIs for specific symbols.

For each symbol, dumps:
  1. What we have stored per venue (latest row, with its ts_utc — so we can spot
     stale rows from delisted/halted pairs).
  2. A fresh `fetch_funding_rate(symbol)` from each of the 14 venues, with
     enough fields to manually compare against the venue website (rate,
     predicted, interval, next-funding timestamp, market.active flag).

Usage:
    python probe_data_sanity.py [SYMBOL ...]
Default symbols: ST/USDT:USDT, RLS/USDT:USDT.
"""

import asyncio
import sys
from datetime import datetime, timezone

import ccxt.async_support as ccxt_async
import pandas as pd

from config import VENUES
from storage import query


def show_stored(symbols: list[str]) -> None:
    print("=== STORED (latest row per venue in parquet) ===")
    placeholders = ",".join(["?"] * len(symbols))
    sql = f"""
        WITH latest AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange
                                      ORDER BY ts_utc DESC) AS rn
            FROM funding
            WHERE symbol_canonical IN ({placeholders})
        )
        SELECT symbol_canonical, exchange, ts_utc,
               funding_rate, funding_interval_h AS h, predicted_rate,
               ROUND(100.0 * apy_norm, 2) AS apy_pct,
               mark_price
        FROM latest WHERE rn = 1
        ORDER BY symbol_canonical, exchange
    """
    df = query(sql, symbols)
    if df.empty:
        print("  (no stored rows)")
        return
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)
    print(df.to_string(index=False))

    overall = query("SELECT MAX(ts_utc) AS latest FROM funding")
    if not overall.empty and overall["latest"].iloc[0] is not None:
        latest = overall["latest"].iloc[0].to_pydatetime()
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        print(f"\nLatest cycle in parquet: {latest:%Y-%m-%d %H:%M:%S UTC}")
        print("(any row above whose ts_utc is much older = stale; pair likely delisted/halted)")


async def _probe_one(canonical: str, cfg: dict, symbol: str) -> str | None:
    cls = getattr(ccxt_async, cfg["ccxt_id"])
    client = cls({"options": cfg.get("options", {}), "enableRateLimit": True})
    try:
        await client.load_markets()
        if symbol not in client.markets:
            return None
        active = client.markets[symbol].get("active")
        try:
            fr = await client.fetch_funding_rate(symbol)
        except Exception as e:
            return f"  {canonical:10} listed but fetch ERROR — {type(e).__name__}: {str(e)[:80]}"
        rate = fr.get("fundingRate")
        pred = fr.get("nextFundingRate")
        interval_str = fr.get("interval")
        fts = fr.get("fundingTimestamp")
        nfts = fr.get("nextFundingTimestamp")
        pfts = fr.get("previousFundingTimestamp")
        interval_h: float | None = None
        if fts and nfts and nfts > fts:
            interval_h = round((nfts - fts) / 3_600_000, 2)
        elif pfts and nfts and nfts > pfts:
            interval_h = round((nfts - pfts) / 3_600_000 / 2, 2)
        next_str = (datetime.fromtimestamp(nfts / 1000, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
                    if nfts else "None")
        apy = (float(rate) * 8760 / interval_h * 100) if (rate is not None and interval_h) else None
        apy_str = f"{apy:+8.2f}%" if apy is not None else "    n/a"
        return (f"  {canonical:10} active={active!s:5}  rate={rate}  predicted={pred}  "
                f"interval={interval_h}h (str={interval_str!r})  "
                f"nextFunding@{next_str}  apy={apy_str}")
    except Exception as e:
        return f"  {canonical:10} load_markets ERROR — {type(e).__name__}: {str(e)[:80]}"
    finally:
        await client.close()


async def show_live(symbol: str) -> None:
    print(f"\n=== LIVE from venue APIs — {symbol} ===")
    results = await asyncio.gather(*(
        _probe_one(name, cfg, symbol) for name, cfg in VENUES.items()
    ))
    for r in results:
        if r:
            print(r)


async def main() -> None:
    symbols = sys.argv[1:] or ["ST/USDT:USDT", "RLS/USDT:USDT"]
    show_stored(symbols)
    for s in symbols:
        await show_live(s)


if __name__ == "__main__":
    asyncio.run(main())
