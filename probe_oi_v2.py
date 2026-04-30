"""Definitive OI-accessibility probe for the 4 currently-NULL venues.

For each of (binance, bingx, blofin, xt), reports:
  1. ccxt's `has` flags for OI-related methods
  2. Whether `fetch_open_interest(symbol)` actually works on a BTC perp
  3. Full ticker.info dump with values — exposes cryptic field names
     (xt's single-letter keys, blofin's volCurrency24h-style names).

Run once, paste output back. Used to decide whether NULL-OI is truly the
right outcome for these venues, or whether there's a reachable surface we
missed.
"""

import asyncio

import ccxt.async_support as ccxt_async


VENUES = {
    "binance": ("binance", {"defaultType": "swap"}),
    "bingx":   ("bingx",   {"defaultType": "swap"}),
    "blofin":  ("blofin",  {"defaultType": "swap"}),
    "xt":      ("xt",      {"defaultType": "swap"}),
}


def _pick_btc_swap(client) -> str | None:
    for s, m in client.markets.items():
        if not (m.get("swap") and m.get("linear")):
            continue
        if m.get("active") is False:
            continue
        if not (m.get("settle") == "USDT" or m.get("quote") == "USDT"):
            continue
        if "BTC" in s.upper() and "/" in s:
            return s
    return None


async def probe(canonical: str) -> None:
    ccxt_id, options = VENUES[canonical]
    cls = getattr(ccxt_async, ccxt_id)
    client = cls({"options": options, "enableRateLimit": True})
    try:
        await client.load_markets()
        print(f"\n========== {canonical} ==========")

        # 1. Surface OI-related has-flags.
        oi_flags = {k: v for k, v in client.has.items() if "openinterest" in k.lower()}
        print(f"has flags: {oi_flags}")

        target = _pick_btc_swap(client)
        if not target:
            print("no BTC USDT-margined swap found")
            return
        print(f"target symbol: {target}")

        # 2. Try the single-symbol fetchOpenInterest.
        if oi_flags.get("fetchOpenInterest"):
            try:
                oi = await client.fetch_open_interest(target)
                if oi:
                    keys = list(oi.keys())
                    print(f"fetch_open_interest OK -- keys: {keys}")
                    for k in ("openInterestAmount", "openInterestValue", "timestamp"):
                        if k in oi:
                            print(f"  {k} = {oi[k]}")
                    info = oi.get("info") or {}
                    if isinstance(info, dict):
                        print(f"  .info keys ({len(info)}): {list(info.keys())}")
                else:
                    print("fetch_open_interest returned empty")
            except Exception as e:
                print(f"fetch_open_interest ERROR: {type(e).__name__}: {e}")
        else:
            print("fetch_open_interest NOT advertised in client.has")

        # 3. Full ticker.info dump with values, to expose cryptic fields.
        try:
            t = await client.fetch_ticker(target)
            info = t.get("info") or {}
            print(f"fetch_ticker.info ({len(info)} keys, full dump):")
            for k, v in info.items():
                sv = str(v)
                if len(sv) > 60:
                    sv = sv[:57] + "..."
                print(f"  {k:24s} = {sv}")
        except Exception as e:
            print(f"fetch_ticker ERROR: {type(e).__name__}: {e}")

    finally:
        await client.close()


async def main() -> None:
    for venue in VENUES:
        await probe(venue)


if __name__ == "__main__":
    asyncio.run(main())
