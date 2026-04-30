"""Parameterized SQL views over the `funding` DuckDB view (see storage.py).

Each builder returns (sql, params) suitable for storage.query(sql, params).
Patterns earn a place here only after proving themselves in the research layer.
"""

from __future__ import annotations

from config import EPOCHS_PER_YEAR


def cross_exchange_delta(min_volume_usd: float):
    """For every symbol listed on >=2 venues, latest cross-venue APY-norm delta."""
    sql = """
    WITH latest AS (
        SELECT symbol_canonical, exchange, ts_utc, funding_rate, apy_norm,
               funding_interval_h, mark_price, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
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
    return sql, [min_volume_usd]


def anomaly_candidates(min_abs_apy_pct: float, min_volume_usd: float, min_persistence: int):
    """Symbol-venue pairs where |APY_norm| has held above the floor for N consecutive obs."""
    sql = """
    WITH recent AS (
        SELECT symbol_canonical, exchange, ts_utc, funding_rate, apy_norm,
               funding_interval_h, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
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
    return sql, [min_volume_usd, min_abs_apy_pct, p, min_abs_apy_pct, p]


def breakeven_epochs(
    min_abs_delta_apy_pct: float,
    min_volume_usd: float,
    basis_cost_bps: float,
    taker_fee_bps: float,
):
    """Rank candidate venue-pairs by E_BE = (basis_cost + 2*taker_fee) / yield_per_epoch.

    Yield-per-epoch uses the SHORT leg's interval (the high-collecting side).
    Mismatched intervals are flagged via interval_mismatch but math assumes
    the short-leg cadence; refine in research notebook if needed.
    """
    sql = f"""
    WITH latest AS (
        SELECT symbol_canonical, exchange, apy_norm, funding_rate,
               funding_interval_h, volume_24h_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
    ),
    snap AS (SELECT * FROM latest WHERE rn = 1)
    SELECT
        s.symbol_canonical,
        l.exchange                                                       AS long_venue,
        s.exchange                                                       AS short_venue,
        100.0 * (s.apy_norm - l.apy_norm)                                AS spread_apy_pct,
        s.funding_interval_h                                             AS short_interval_h,
        l.funding_interval_h                                             AS long_interval_h,
        (s.funding_interval_h <> l.funding_interval_h)                   AS interval_mismatch,
        ({basis_cost_bps} + 2 * {taker_fee_bps})
          / NULLIF(10000.0 * (s.apy_norm - l.apy_norm) * s.funding_interval_h / {EPOCHS_PER_YEAR}, 0)
                                                                         AS breakeven_epochs,
        LEAST(s.volume_24h_usd, l.volume_24h_usd)                        AS min_volume_24h_usd
    FROM snap s
    JOIN snap l
      ON s.symbol_canonical = l.symbol_canonical
     AND s.exchange <> l.exchange
     AND s.apy_norm > l.apy_norm                  -- s = short the high, l = long the low
    WHERE 100.0 * (s.apy_norm - l.apy_norm) >= ?
    ORDER BY breakeven_epochs ASC NULLS LAST
    LIMIT 200;
    """
    return sql, [min_volume_usd, min_abs_delta_apy_pct]


def historical_funding(symbol: str, exchanges: list[str]):
    placeholders = ",".join(["?"] * len(exchanges))
    sql = f"""
    SELECT ts_utc, exchange, funding_rate, apy_norm, mark_price,
           volume_24h_usd, open_interest_usd
    FROM funding
    WHERE symbol_canonical = ?
      AND exchange IN ({placeholders})
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
