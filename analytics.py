"""Parameterized SQL views over the `funding` DuckDB view (see storage.py).

Each builder returns (sql, params) suitable for storage.query(sql, params).
Patterns earn a place here only after proving themselves in the research layer.
"""

from __future__ import annotations

from config import EPOCHS_PER_YEAR


def cross_exchange_delta(min_volume_usd: float, min_oi_usd: float = 0.0):
    """For every symbol listed on >=2 venues, latest cross-venue APY-norm delta.

    The OI filter is NULL-tolerant: rows with NULL open_interest_usd pass through
    (since 4 of our 14 venues don't expose OI). Only non-NULL values are compared
    against the floor. Set min_oi_usd=0 to disable.
    """
    sql = """
    WITH latest AS (
        SELECT symbol_canonical, exchange, ts_utc, funding_rate, apy_norm,
               funding_interval_h, mark_price, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
          AND apy_norm IS NOT NULL
          AND (open_interest_usd IS NULL OR open_interest_usd >= ?)
    )
    SELECT
        symbol_canonical,
        COUNT(*)                                                         AS venues_listed,
        100.0 * (MAX(apy_norm) - MIN(apy_norm))                          AS delta_apy_pct,
        ARG_MAX(exchange, apy_norm)                                      AS short_venue,
        ARG_MIN(exchange, apy_norm)                                      AS long_venue,
        100.0 * MAX(apy_norm)                                            AS short_apy_pct,
        100.0 * MIN(apy_norm)                                            AS long_apy_pct,
        MIN(volume_24h_usd)                                              AS min_volume_24h_usd,
        MAX(ts_utc)                                                      AS latest_obs
    FROM latest
    WHERE rn = 1
    GROUP BY symbol_canonical
    HAVING COUNT(*) >= 2
    ORDER BY delta_apy_pct DESC
    LIMIT 200;
    """
    return sql, [min_volume_usd, min_oi_usd]


def anomaly_candidates(min_abs_apy_pct: float, min_volume_usd: float, min_persistence: int, min_oi_usd: float = 0.0):
    """Symbol-venue pairs where |APY_norm| has held above the threshold for N consecutive cycles.

    OI filter is NULL-tolerant (rows with NULL open_interest_usd pass through).
    """
    sql = """
    WITH recent AS (
        SELECT symbol_canonical, exchange, ts_utc, funding_rate, apy_norm,
               funding_interval_h, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
          AND apy_norm IS NOT NULL
          AND (open_interest_usd IS NULL OR open_interest_usd >= ?)
    )
    SELECT
        symbol_canonical, exchange,
        COUNT(*) FILTER (WHERE 100.0 * ABS(apy_norm) >= ?)               AS persistence_count,
        100.0 * AVG(apy_norm)                                            AS avg_apy_pct,
        100.0 * MAX(ABS(apy_norm))                                       AS max_abs_apy_pct,
        ANY_VALUE(funding_interval_h)                                    AS interval_h,
        MIN(volume_24h_usd)                                              AS volume_24h_usd,
        MIN(open_interest_usd)                                           AS open_interest_usd,
        MAX(ts_utc)                                                      AS latest_obs
    FROM recent
    WHERE rn <= ?
    GROUP BY symbol_canonical, exchange
    HAVING COUNT(*) FILTER (WHERE 100.0 * ABS(apy_norm) >= ?) >= ?
    ORDER BY ABS(avg_apy_pct) DESC
    LIMIT 200;
    """
    p = min_persistence
    return sql, [min_volume_usd, min_oi_usd, min_abs_apy_pct, p, min_abs_apy_pct, p]


def breakeven_epochs(
    min_spread_apy_pct: float,
    min_volume_usd: float,
    exit_basis_bps: float,
    taker_fee_bps: float,
    min_oi_usd: float = 0.0,
):
    """Rank candidate venue-pairs by E_BE = (basis_cost + fee_cost) / yield_per_epoch.

    Entry basis is computed live from the mark-price spread between venues:
        entry_basis_bps = 10000 * (mark_short - mark_long) / mid_mark
    Positive entry basis = favorable entry (short price > long price means you
    sell high and buy low, pocketing the spread on convergence).

    Round-trip costs:
        basis_cost_bps = exit_basis_bps - entry_basis_bps  (negative if favorable)
        fee_cost_bps   = 4 * taker_fee_bps                  (entry + exit, both legs)

    Yield-per-epoch uses the SHORT leg's interval (the high-collecting side).
    Interval mismatch is surfaced as a column; refine in the research layer if needed.
    """
    sql = f"""
    WITH latest AS (
        SELECT symbol_canonical, exchange, ts_utc, apy_norm, funding_rate,
               funding_interval_h, mark_price, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
          AND apy_norm IS NOT NULL
          AND mark_price IS NOT NULL
          AND (open_interest_usd IS NULL OR open_interest_usd >= ?)
    ),
    snap AS (SELECT * FROM latest WHERE rn = 1),
    pairs AS (
        SELECT
            s.symbol_canonical,
            l.exchange       AS long_venue,
            s.exchange       AS short_venue,
            l.apy_norm       AS long_apy,
            s.apy_norm       AS short_apy,
            l.funding_interval_h AS long_interval_h,
            s.funding_interval_h AS short_interval_h,
            l.mark_price     AS long_mark,
            s.mark_price     AS short_mark,
            LEAST(s.volume_24h_usd, l.volume_24h_usd) AS min_volume_24h_usd,
            LEAST(s.ts_utc, l.ts_utc)                 AS latest_obs
        FROM snap s
        JOIN snap l
          ON s.symbol_canonical = l.symbol_canonical
         AND s.exchange <> l.exchange
         AND s.apy_norm > l.apy_norm
    )
    SELECT
        symbol_canonical,
        long_venue,
        short_venue,
        100.0 * (short_apy - long_apy)                                    AS spread_apy_pct,
        short_interval_h,
        long_interval_h,
        (short_interval_h <> long_interval_h)                             AS interval_mismatch,
        long_mark,
        short_mark,
        10000.0 * (short_mark - long_mark) / ((short_mark + long_mark) / 2.0)
                                                                          AS entry_basis_bps,
        (
            ({exit_basis_bps}
             - 10000.0 * (short_mark - long_mark) / ((short_mark + long_mark) / 2.0))
            + 4.0 * {taker_fee_bps}
        ) / NULLIF(10000.0 * (short_apy - long_apy) * short_interval_h / {EPOCHS_PER_YEAR}, 0)
                                                                          AS breakeven_epochs,
        min_volume_24h_usd,
        latest_obs
    FROM pairs
    WHERE 100.0 * (short_apy - long_apy) >= ?
    ORDER BY breakeven_epochs ASC NULLS LAST
    LIMIT 200;
    """
    return sql, [min_volume_usd, min_oi_usd, min_spread_apy_pct]


def historical_funding(symbol: str, exchanges: list[str], hours_back: int | None = None):
    """If hours_back is None, returns the full history; otherwise restricts to
    the trailing window relative to the dataset's most recent timestamp."""
    placeholders = ",".join(["?"] * len(exchanges))
    if hours_back is not None:
        time_clause = (
            f"AND ts_utc >= (SELECT MAX(ts_utc) FROM funding) - INTERVAL {int(hours_back)} HOUR"
        )
    else:
        time_clause = ""
    sql = f"""
    SELECT ts_utc, exchange, funding_rate, apy_norm, funding_interval_h,
           mark_price, volume_24h_usd, open_interest_usd
    FROM funding
    WHERE symbol_canonical = ?
      AND exchange IN ({placeholders})
      {time_clause}
    ORDER BY ts_utc;
    """
    return sql, [symbol, *exchanges]


def latest_summary():
    sql = """
    SELECT
        exchange,
        COUNT(DISTINCT symbol_canonical) AS symbols,
        MAX(ts_utc)                      AS latest_obs,
        MIN(ts_utc)                      AS earliest_obs
    FROM funding
    GROUP BY exchange
    ORDER BY exchange;
    """
    return sql, []
