"""Single source of truth for venue map, paths, and thresholds."""

from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
FUNDING_DIR = DATA_DIR / "funding"

POLL_INTERVAL_S = 60

EPOCHS_PER_YEAR = 8760

# Canonical slug -> CCXT instantiation config.
# `ccxt_id` is the class name in ccxt.async_support.
# `options` is forwarded to the constructor; defaultType=swap pins to USDT-margined linear perps.
VENUES: dict[str, dict] = {
    "binance":  {"ccxt_id": "binance",       "options": {"defaultType": "swap"}},
    "bingx":    {"ccxt_id": "bingx",         "options": {"defaultType": "swap"}},
    "bitget":   {"ccxt_id": "bitget",        "options": {"defaultType": "swap"}},
    "bitmart":  {"ccxt_id": "bitmart",       "options": {"defaultType": "swap"}},
    "blofin":   {"ccxt_id": "blofin",        "options": {"defaultType": "swap"}},
    "bybit":    {"ccxt_id": "bybit",         "options": {"defaultType": "swap"}},
    "coinex":   {"ccxt_id": "coinex",        "options": {"defaultType": "swap"}},
    "gate":     {"ccxt_id": "gate",          "options": {"defaultType": "swap"}},
    "htx":      {"ccxt_id": "htx",           "options": {"defaultType": "swap"}},
    "kucoin":   {"ccxt_id": "kucoinfutures", "options": {}},
    "mexc":     {"ccxt_id": "mexc",          "options": {"defaultType": "swap"}},
    "okx":      {"ccxt_id": "okx",           "options": {"defaultType": "swap"}},
    "phemex":   {"ccxt_id": "phemex",        "options": {"defaultType": "swap"}},
    "xt":       {"ccxt_id": "xt",            "options": {"defaultType": "swap"}},
}

# Default analytics gates (operator overrides these in the dashboard sidebar).
DEFAULT_MIN_ABS_APY_PCT = 100.0          # |APY_norm| >= 100%
DEFAULT_MIN_VOLUME_24H_USD = 1_000_000   # $1M 24h notional
DEFAULT_MIN_PERSISTENCE = 3              # consecutive observations
DEFAULT_EXIT_BASIS_BPS = 0.0             # assume price convergence at unwind
DEFAULT_TAKER_FEE_BPS = 5.0              # per leg per side
DEFAULT_MIN_SPREAD_APY_PCT = 50.0        # min cross-venue annualized spread for breakeven view
