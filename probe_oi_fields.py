"""One-shot probe: surface open-interest field names from each venue's ticker `info`.

Run once on the VPS, paste output back. Used to build the per-venue OI
extractor table without making per-symbol fan-out calls.
"""

import asyncio
import ccxt.async_support as ccxt_async

from config import VENUES

# Substrings that hint at OI in info-blob keys (case-insensitive).
OI_HINTS = (
    "openinterest", "open_interest",
    "holding", "holdvol", "hold_vol",
    "totalsize", "total_size",
    "positions", "position_amt",
    "oi",
)


async def probe(canonical: str, cfg: dict) -> None:
    cls = getattr(ccxt_async, cfg["ccxt_id"])
    client = cls({"options": cfg.get("options", {}), "enableRateLimit": True})
    try:
        await client.load_markets()
        target = None
        for s, m in client.markets.items():
            if not (m.get("swap") and m.get("linear")):
                continue
            if m.get("active") is False:
                continue
            if (m.get("settle") == "USDT" or m.get("quote") == "USDT") and "BTC" in s.upper():
                target = s
                break
        if target is None:
            print(f"\n{canonical}: no USDT-margined BTC swap found")
            return
        t = await client.fetch_ticker(target)
        info = t.get("info") or {}
        last = t.get("last")

        oi_like = {k: v for k, v in info.items() if any(h in k.lower() for h in OI_HINTS)}
        print(f"\n{canonical} | symbol={target} | last={last}")
        if oi_like:
            for k, v in oi_like.items():
                print(f"  HIT  {k} = {v}")
        else:
            keys = list(info.keys())
            print(f"  no oi-named keys. all info keys ({len(keys)}): {keys}")
    except Exception as e:
        print(f"\n{canonical}: ERROR {type(e).__name__}: {e}")
    finally:
        await client.close()


async def main() -> None:
    # Serial so output stays grouped per-venue (probing is fast: 14 single calls).
    for canonical, cfg in VENUES.items():
        await probe(canonical, cfg)


if __name__ == "__main__":
    asyncio.run(main())
