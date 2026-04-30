"""Canonical schema enforcement at the collector boundary.

Every observation downstream of this module conforms to the locked row shape
defined by CANONICAL_FIELDS. Exchange-specific idiosyncrasies do not leak.
"""

import re
from datetime import datetime, timezone

from config import EPOCHS_PER_YEAR

# All target venues currently default to 8h funding cycles for vanilla USDT
# linear perps. A handful of pairs run on 4h or 1h schedules; those are picked
# up explicitly when the response carries timestamps or an `interval` field.
DEFAULT_INTERVAL_H = 8

CANONICAL_FIELDS = [
    "ts_utc",                # cycle timestamp (datetime, UTC)
    "exchange",              # canonical slug
    "symbol_canonical",      # CCXT unified symbol, e.g. BTC/USDT:USDT
    "funding_rate",          # raw per-epoch rate (decimal, not %)
    "funding_interval_h",    # 1 | 4 | 8
    "predicted_rate",        # next epoch predicted rate, if exposed
    "next_funding_ts",       # datetime, UTC
    "mark_price",
    "index_price",
    "open_interest_usd",
    "volume_24h_usd",
    "apy_norm",              # funding_rate * (8760 / interval_h)  -- decimal
]


_INTERVAL_PAT = re.compile(r"(\d+)\s*h", re.IGNORECASE)


def _snap(delta_h: float) -> int | None:
    for known in (1, 4, 8):
        if abs(delta_h - known) < 0.5:
            return known
    return None


def detect_interval_h(fr: dict) -> int | None:
    """Resolve the funding interval in hours, in order of decreasing reliability.

    1) Explicit `interval` string from CCXT (e.g. "8h", "4h").
    2) Diff of any two of (previous, current, next) funding timestamps.
    3) `DEFAULT_INTERVAL_H` — most target venues run 8h.
    """
    interval_str = fr.get("interval")
    if isinstance(interval_str, str):
        m = _INTERVAL_PAT.search(interval_str)
        if m:
            snapped = _snap(int(m.group(1)))
            if snapped is not None:
                return snapped

    fts = fr.get("fundingTimestamp")
    nfts = fr.get("nextFundingTimestamp")
    pfts = fr.get("previousFundingTimestamp")

    if fts and nfts and nfts > fts:
        return _snap((nfts - fts) / 3_600_000) or DEFAULT_INTERVAL_H
    if fts and pfts and fts > pfts:
        return _snap((fts - pfts) / 3_600_000) or DEFAULT_INTERVAL_H
    if pfts and nfts and nfts > pfts:
        return _snap((nfts - pfts) / 3_600_000 / 2) or DEFAULT_INTERVAL_H

    return DEFAULT_INTERVAL_H


def compute_apy_norm(rate: float | None, interval_h: int | None) -> float | None:
    if rate is None or interval_h is None or interval_h == 0:
        return None
    return rate * (EPOCHS_PER_YEAR / interval_h)


def _to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def normalize(
    exchange: str,
    cycle_ts: datetime,
    fr: dict,
    ticker: dict | None,
    open_interest_usd: float | None,
) -> dict | None:
    """Return a canonical row, or None if the observation is too sparse to keep."""
    symbol = fr.get("symbol")
    rate = fr.get("fundingRate")
    if symbol is None or rate is None:
        return None

    interval_h = detect_interval_h(fr)
    mark = fr.get("markPrice") or (ticker or {}).get("last")
    quote_vol = (ticker or {}).get("quoteVolume")  # USDT-quoted -> USD-equivalent

    return {
        "ts_utc":             cycle_ts,
        "exchange":           exchange,
        "symbol_canonical":   symbol,
        "funding_rate":       float(rate),
        "funding_interval_h": interval_h,
        "predicted_rate":     _f(fr.get("nextFundingRate")),
        "next_funding_ts":    _to_dt(fr.get("nextFundingTimestamp")),
        "mark_price":         _f(mark),
        "index_price":        _f(fr.get("indexPrice")),
        "open_interest_usd":  _f(open_interest_usd),
        "volume_24h_usd":     _f(quote_vol),
        "apy_norm":           compute_apy_norm(float(rate), interval_h),
    }


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
