"""Async REST pollers, one task per venue.

Wide-and-shallow: batch endpoints fetched concurrently across venues, normalized
at the boundary, isolated failure domain per venue.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp
import ccxt.async_support as ccxt_async

from config import EPOCHS_PER_YEAR, VENUES
from normalize import normalize

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ArbScanner/0.1)"}


async def _http_get_json(url: str) -> dict:
    """Single-shot GET with a fresh session; returns parsed JSON.

    Bypasses ccxt's `fetch()` to avoid the 4.5.49 header-handling edge case
    that surfaced as `'NoneType' object has no attribute 'lower'` on bitmart.
    """
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


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


async def _native_phemex_funding(client) -> dict[str, dict]:
    """Funding-only native fetch for phemex.

    One GET to /md/v3/ticker/24hr/all gives us funding rate + predicted rate
    for every USDT-linear perp. We hand the result back in ccxt-funding-rate
    shape so normalize() consumes it identically. Mark/OI/volume continue to
    come from fetch_tickers (which handles phemex's scaled-integer encoding).
    """
    raw = await _http_get_json("https://api.phemex.com/md/v3/ticker/24hr/all")
    result = raw.get("result") or (raw.get("data") or {}).get("result") or []
    if not isinstance(result, list):
        raise RuntimeError(f"phemex unexpected response: keys={list(raw.keys())}")

    out: dict[str, dict] = {}
    for s in result:
        ccxt_symbol = _ccxt_symbol_for(client, s.get("symbol"))
        if not ccxt_symbol:
            continue
        market = client.markets.get(ccxt_symbol) or {}
        if not (market.get("linear") and (market.get("settle") == "USDT" or market.get("quote") == "USDT")):
            continue
        rate = _f(s.get("fundingRateRr"))
        if rate is None:
            rate = _f(s.get("fundingRate"))
        if rate is None:
            continue
        out[ccxt_symbol] = {
            "symbol":               ccxt_symbol,
            "fundingRate":          rate,
            "nextFundingRate":      _f(s.get("predFundingRateRr")) or _f(s.get("predFundingRate")),
            "fundingTimestamp":     None,
            "nextFundingTimestamp": None,
            "interval":             "8h",
            "info":                 s,
        }
    return out


async def _native_mexc_funding(client) -> dict[str, dict]:
    """Funding-only native fetch for mexc.

    One GET to /api/v1/contract/funding_rate returns funding rate, collect cycle
    (interval), and next settle time for every contract. Tickers/OI keep coming
    from fetch_tickers + the existing info extractor.
    """
    raw = await _http_get_json("https://contract.mexc.com/api/v1/contract/funding_rate")
    if raw.get("success") is False or (raw.get("code") not in (0, None)):
        raise RuntimeError(f"mexc non-zero code: {raw.get('code')}")
    data = raw.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"mexc unexpected data shape: {type(data).__name__}")

    out: dict[str, dict] = {}
    for s in data:
        ccxt_symbol = _ccxt_symbol_for(client, s.get("symbol"))
        if not ccxt_symbol:
            continue
        rate = _f(s.get("fundingRate"))
        if rate is None:
            continue
        cycle_h = s.get("collectCycle")
        try:
            cycle_h = int(cycle_h) if cycle_h is not None else 8
        except (TypeError, ValueError):
            cycle_h = 8
        next_ts = s.get("nextSettleTime")
        try:
            next_ts = int(next_ts) if next_ts is not None else None
        except (TypeError, ValueError):
            next_ts = None
        out[ccxt_symbol] = {
            "symbol":               ccxt_symbol,
            "fundingRate":          rate,
            "fundingTimestamp":     None,
            "nextFundingTimestamp": next_ts,
            "interval":             f"{cycle_h}h",
            "info":                 s,
        }
    return out


# Funding-only native fetchers: replace slow per-symbol fan-out with a single
# batch call, but keep tickers/OI flowing through the standard ccxt path.
# Use this pattern when the venue's all-tickers endpoint is structurally
# painful to parse (phemex scaled values) but a clean funding-only endpoint
# exists.
NATIVE_FUNDING_FETCHERS = {
    "phemex": _native_phemex_funding,
    "mexc":   _native_mexc_funding,
}


async def _fetch_funding(client, target_symbols: list[str], canonical: str | None = None) -> dict[str, dict]:
    """Native funding-only path > ccxt batch > ccxt per-symbol fan-out."""
    if canonical and canonical in NATIVE_FUNDING_FETCHERS:
        try:
            rows = await NATIVE_FUNDING_FETCHERS[canonical](client)
            min_acceptable = max(50, len(target_symbols) // 2)
            if len(rows) >= min_acceptable:
                return rows
            log.warning("%s native funding returned %d/%d rows; falling back",
                        canonical, len(rows), len(target_symbols))
        except Exception as e:
            log.warning("%s native funding failed (%s); falling back", canonical, e)

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


def _ccxt_symbol_for(client, venue_symbol: str | None) -> str | None:
    """Map a venue-native symbol id to ccxt's unified symbol."""
    if not venue_symbol:
        return None
    markets = client.markets_by_id.get(venue_symbol) or []
    return markets[0].get("symbol") if markets else None


_BITMART_URLS = (
    # v2 host is the documented home for Bitmart Futures v2 since 2024;
    # v1 host is kept as a fallback in case of routing changes.
    "https://api-cloud-v2.bitmart.com/contract/public/details",
    "https://api-cloud.bitmart.com/contract/public/details",
)


async def _native_bitmart(client, cycle_ts: datetime, canonical: str) -> list[dict]:
    """Single GET to /contract/public/details — replaces tickers + per-symbol funding + OI.

    Returns funding_rate, expected_funding_rate, open_interest_value (USD),
    turnover_24h (USD), last_price, funding_interval_hours, and
    next_funding_rate_timestamp in one round-trip.
    """
    raw = None
    last_err: Exception | None = None
    for url in _BITMART_URLS:
        try:
            raw = await _http_get_json(url)
            break
        except Exception as e:
            last_err = e
    if raw is None:
        raise RuntimeError(f"all bitmart endpoints failed: {last_err}")
    rows: list[dict] = []
    payload = (raw or {}).get("data") or {}
    for s in payload.get("symbols") or []:
        if s.get("product_type") != 1:                # 1 = perpetual
            continue
        if s.get("quote_currency") != "USDT":
            continue
        ccxt_symbol = _ccxt_symbol_for(client, s.get("symbol"))
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


# All-in-one native batch fetchers — the venue's response is rich enough that
# we replace tickers + funding + OI in a single round-trip. Use this only when
# the response shape is clean to parse end-to-end (bitmart's v2 contract API).
# For venues where only funding is cleanly batchable but mark/OI need the
# standard ccxt path (e.g. phemex's scaled-integer encoding), use
# NATIVE_FUNDING_FETCHERS above instead.
NATIVE_FETCHERS = {
    "bitmart": _native_bitmart,
}


async def fetch_venue(canonical: str, cfg: dict, cycle_ts: datetime) -> list[dict]:
    cls = getattr(ccxt_async, cfg["ccxt_id"])
    client = cls({"options": cfg.get("options", {}), "enableRateLimit": True})
    t0 = time.monotonic()
    try:
        await client.load_markets()

        target_symbols = [s for s, m in client.markets.items() if _is_target_market(m)]

        # Try the native batch path first; fall back to the standard CCXT path on
        # failure OR if the response yielded suspiciously few rows (wrong field names).
        native = NATIVE_FETCHERS.get(canonical)
        if native is not None:
            try:
                rows = await native(client, cycle_ts, canonical)
                min_acceptable = max(50, len(target_symbols) // 2)
                if len(rows) >= min_acceptable:
                    log.info("venue %s (native): %d obs in %.1fs",
                             canonical, len(rows), time.monotonic() - t0)
                    return rows
                log.warning("%s native returned only %d/%d rows; falling back to CCXT path",
                            canonical, len(rows), len(target_symbols))
            except Exception as e:
                log.warning("%s native batch failed (%s); falling back to CCXT path", canonical, e)

        if not target_symbols:
            log.warning("%s: no USDT linear-swap markets discovered", canonical)
            return []

        tickers_task = asyncio.create_task(client.fetch_tickers(target_symbols))
        oi_task = asyncio.create_task(_fetch_open_interest(client, target_symbols))
        funding_task = asyncio.create_task(_fetch_funding(client, target_symbols, canonical))

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
