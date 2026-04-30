"""Async REST pollers, one task per venue.

Wide-and-shallow: batch endpoints fetched concurrently across venues, normalized
at the boundary, isolated failure domain per venue.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt_async

from config import VENUES
from normalize import normalize

log = logging.getLogger(__name__)


def _is_target_market(m: dict) -> bool:
    """USDT-margined linear perpetual swaps.

    Accepts either `quote == 'USDT'` (BTC/USDT:USDT) or `settle == 'USDT'`
    (BTC/USD:USDT, used by venues like coinex for USD-quoted, USDT-settled
    contracts — still a USDT-margined linear perp).
    """
    if not (m.get("swap") and m.get("linear") and m.get("active", True)):
        return False
    return m.get("settle") == "USDT" or m.get("quote") == "USDT"


async def _fetch_funding(client, target_symbols: list[str]) -> dict[str, dict]:
    """Batch where supported; per-symbol async fan-out otherwise."""
    if client.has.get("fetchFundingRates"):
        try:
            return await client.fetch_funding_rates()
        except Exception as e:
            log.warning("%s batch fetch_funding_rates failed (%s); falling back to per-symbol", client.id, e)
    results = await asyncio.gather(
        *(client.fetch_funding_rate(s) for s in target_symbols),
        return_exceptions=True,
    )
    out: dict[str, dict] = {}
    for s, r in zip(target_symbols, results):
        if isinstance(r, Exception):
            continue
        out[s] = r
    return out


async def _fetch_open_interest(client, target_symbols: list[str]) -> dict[str, float]:
    """Best-effort: only venues with batch fetchOpenInterests are pulled.

    Per-symbol fetchOpenInterest fan-out is skipped to stay under rate budgets;
    OI on those venues stays None until iterated on.
    """
    if not client.has.get("fetchOpenInterests"):
        return {}
    try:
        ois = await client.fetch_open_interests()
    except Exception as e:
        log.warning("%s fetch_open_interests failed: %s", client.id, e)
        return {}
    out: dict[str, float] = {}
    for s, oi in (ois or {}).items():
        usd = oi.get("openInterestValue") or oi.get("openInterestAmount")
        if usd:
            out[s] = float(usd)
    return out


async def fetch_venue(canonical: str, cfg: dict, cycle_ts: datetime) -> list[dict]:
    cls = getattr(ccxt_async, cfg["ccxt_id"])
    client = cls({"options": cfg.get("options", {}), "enableRateLimit": True})
    t0 = time.monotonic()
    try:
        await client.load_markets()
        target_symbols = [s for s, m in client.markets.items() if _is_target_market(m)]
        if not target_symbols:
            log.warning("%s: no USDT linear-swap markets discovered", canonical)
            return []

        tickers_task = asyncio.create_task(client.fetch_tickers(target_symbols))
        oi_task = asyncio.create_task(_fetch_open_interest(client, target_symbols))
        funding_task = asyncio.create_task(_fetch_funding(client, target_symbols))

        tickers, oi_map, funding_rates = await asyncio.gather(
            tickers_task, oi_task, funding_task, return_exceptions=False
        )

        rows: list[dict] = []
        for symbol, fr in funding_rates.items():
            row = normalize(canonical, cycle_ts, fr, tickers.get(symbol), oi_map.get(symbol))
            if row is not None:
                rows.append(row)
        log.info("venue %s: %d obs in %.1fs (%d markets)",
                 canonical, len(rows), time.monotonic() - t0, len(target_symbols))
        return rows
    finally:
        await client.close()


async def collect_once() -> list[dict]:
    cycle_ts = datetime.now(timezone.utc)
    tasks = {name: asyncio.create_task(fetch_venue(name, cfg, cycle_ts)) for name, cfg in VENUES.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    all_rows: list[dict] = []
    for name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.warning("venue %s: collection failed: %s", name, result)
            continue
        all_rows.extend(result)
    return all_rows
