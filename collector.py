"""Async REST pollers, one task per venue.

Wide-and-shallow: batch endpoints fetched concurrently across venues, normalized
at the boundary, isolated failure domain per venue.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt_async

from config import EPOCHS_PER_YEAR, VENUES
from normalize import normalize

log = logging.getLogger(__name__)


def _f(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_target_market(m: dict) -> bool:
    """USDT-margined linear perpetual swaps.

    Accepts `quote == 'USDT'` or `settle == 'USDT'` (covers BTC/USD:USDT-style
    contracts that are still USDT-margined linear perps). `active` is treated
    permissively: only `False` excludes — `None` means the venue (e.g. coinex)
    simply doesn't populate the field.
    """
    if not (m.get("swap") and m.get("linear")):
        return False
    if m.get("active") is False:
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


async def _native_bitmart(client, cycle_ts: datetime, canonical: str) -> list[dict]:
    """Single GET to /contract/public/details — replaces tickers + per-symbol funding + OI.

    Bitmart's batch contract endpoint returns funding_rate, expected_funding_rate,
    open_interest_value (USD), turnover_24h (USD), last_price, funding_interval_hours,
    next_funding_rate_timestamp — everything we need in one round-trip.
    """
    raw = await client.fetch("https://api-cloud.bitmart.com/contract/public/details", "GET")
    rows: list[dict] = []
    payload = (raw or {}).get("data") or {}
    for s in payload.get("symbols", []) or []:
        if s.get("product_type") != 1:                # 1 = perpetual
            continue
        if s.get("quote_currency") != "USDT":
            continue
        venue_symbol = s.get("symbol")
        markets_for_id = client.markets_by_id.get(venue_symbol) or []
        ccxt_symbol = (markets_for_id[0].get("symbol") if markets_for_id else None)
        if not ccxt_symbol:
            continue
        rate = _f(s.get("funding_rate"))
        if rate is None:
            continue
        interval_h = int(s.get("funding_interval_hours") or 8)
        rows.append({
            "ts_utc":             cycle_ts,
            "exchange":           canonical,
            "symbol_canonical":   ccxt_symbol,
            "funding_rate":       rate,
            "funding_interval_h": interval_h,
            "predicted_rate":     _f(s.get("expected_funding_rate")),
            "next_funding_ts":    _ms_to_dt(s.get("next_funding_rate_timestamp")),
            "mark_price":         _f(s.get("last_price")),
            "index_price":        _f(s.get("index_price")),
            "open_interest_usd":  _f(s.get("open_interest_value")),
            "volume_24h_usd":     _f(s.get("turnover_24h")),
            "apy_norm":           rate * (EPOCHS_PER_YEAR / interval_h) if interval_h else None,
        })
    return rows


# Per-venue native batch fetchers — each one collapses tickers + funding + OI
# into a single round-trip, replacing slow CCXT per-symbol fan-out paths.
NATIVE_FETCHERS = {
    "bitmart": _native_bitmart,
}


async def fetch_venue(canonical: str, cfg: dict, cycle_ts: datetime) -> list[dict]:
    cls = getattr(ccxt_async, cfg["ccxt_id"])
    client = cls({"options": cfg.get("options", {}), "enableRateLimit": True})
    t0 = time.monotonic()
    try:
        await client.load_markets()

        # Try the native batch path first; fall back to the standard CCXT path on failure.
        native = NATIVE_FETCHERS.get(canonical)
        if native is not None:
            try:
                rows = await native(client, cycle_ts, canonical)
                log.info("venue %s (native): %d obs in %.1fs",
                         canonical, len(rows), time.monotonic() - t0)
                return rows
            except Exception as e:
                log.warning("%s native batch failed (%s); falling back to CCXT path", canonical, e)

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
            row = normalize(
                canonical, cycle_ts, fr,
                tickers.get(symbol),
                client.markets.get(symbol),
                oi_map.get(symbol),
            )
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
