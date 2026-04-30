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
    sql = f"""
    WITH recent AS (
        SELECT symbol_canonical, exchange, ts_utc, funding_rate, apy_norm,
               predicted_rate, funding_interval_h, volume_24h_usd, open_interest_usd,
               predicted_rate * ({EPOCHS_PER_YEAR}.0 / NULLIF(funding_interval_h, 0))
                                                                          AS predicted_apy_norm,
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
        100.0 * ARG_MAX(predicted_apy_norm, ts_utc)                      AS predicted_apy_pct,
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
    max_abs_entry_basis_bps: float = 0.0,   # 0 = filter disabled
    basis_history_hours: int = 1,
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
    # If user passes 0, bypass the basis filter by setting threshold to a value
    # nothing realistic will exceed. Don't filter unless the user opted in.
    basis_threshold = max_abs_entry_basis_bps if max_abs_entry_basis_bps > 0 else 1e18

    sql = f"""
    WITH latest AS (
        SELECT symbol_canonical, exchange, ts_utc, apy_norm, funding_rate, predicted_rate,
               funding_interval_h, mark_price, volume_24h_usd, open_interest_usd,
               ROW_NUMBER() OVER (PARTITION BY symbol_canonical, exchange ORDER BY ts_utc DESC) AS rn
        FROM funding
        WHERE volume_24h_usd >= ?
          AND apy_norm IS NOT NULL
          AND mark_price IS NOT NULL
          AND (open_interest_usd IS NULL OR open_interest_usd >= ?)
    ),
    snap AS (SELECT * FROM latest WHERE rn = 1),
    -- Basis volatility: stddev of cross-venue mark spread over the trailing window.
    -- Stable spread = scanner snapshot is a good proxy for what you'll fill at.
    -- Volatile spread = expect slippage between scan and execute.
    basis_history AS (
        SELECT a.symbol_canonical,
               LEAST(a.exchange, b.exchange)    AS venue_lo,
               GREATEST(a.exchange, b.exchange) AS venue_hi,
               STDDEV_SAMP(10000.0 * (a.mark_price - b.mark_price)
                           / ((a.mark_price + b.mark_price) / 2.0))   AS basis_stddev_bps,
               COUNT(*)                                                 AS basis_samples
        FROM funding a
        JOIN funding b
          ON a.symbol_canonical = b.symbol_canonical
         AND a.ts_utc = b.ts_utc
         AND a.exchange < b.exchange
        WHERE a.ts_utc >= (SELECT MAX(ts_utc) FROM funding) - INTERVAL {int(basis_history_hours)} HOUR
          AND a.mark_price IS NOT NULL
          AND b.mark_price IS NOT NULL
        GROUP BY a.symbol_canonical, LEAST(a.exchange, b.exchange), GREATEST(a.exchange, b.exchange)
    ),
    pairs AS (
        SELECT
            s.symbol_canonical,
            l.exchange       AS long_venue,
            s.exchange       AS short_venue,
            l.apy_norm       AS long_apy,
            s.apy_norm       AS short_apy,
            -- Predicted next-epoch APY (annualized) per leg; NULL if venue
            -- doesn't expose predicted_rate.
            l.predicted_rate * ({EPOCHS_PER_YEAR}.0 / NULLIF(l.funding_interval_h, 0))
                              AS long_predicted_apy_norm,
            s.predicted_rate * ({EPOCHS_PER_YEAR}.0 / NULLIF(s.funding_interval_h, 0))
                              AS short_predicted_apy_norm,
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
        p.symbol_canonical,
        p.long_venue,
        p.short_venue,
        100.0 * (p.short_apy - p.long_apy)                                  AS spread_apy_pct,
        100.0 * (p.short_predicted_apy_norm - p.long_predicted_apy_norm)    AS predicted_spread_apy_pct,
        p.short_interval_h,
        p.long_interval_h,
        (p.short_interval_h <> p.long_interval_h)                           AS interval_mismatch,
        p.long_mark,
        p.short_mark,
        10000.0 * (p.short_mark - p.long_mark) / ((p.short_mark + p.long_mark) / 2.0)
                                                                            AS entry_basis_bps,
        bh.basis_stddev_bps,
        bh.basis_samples,
        (
            ({exit_basis_bps}
             - 10000.0 * (p.short_mark - p.long_mark) / ((p.short_mark + p.long_mark) / 2.0))
            + 4.0 * {taker_fee_bps}
        ) / NULLIF(10000.0 * (p.short_apy - p.long_apy) * p.short_interval_h / {EPOCHS_PER_YEAR}, 0)
                                                                            AS breakeven_epochs,
        p.min_volume_24h_usd,
        p.latest_obs
    FROM pairs p
    LEFT JOIN basis_history bh
      ON p.symbol_canonical = bh.symbol_canonical
     AND LEAST(p.long_venue, p.short_venue) = bh.venue_lo
     AND GREATEST(p.long_venue, p.short_venue) = bh.venue_hi
    WHERE 100.0 * (p.short_apy - p.long_apy) >= ?
      AND ABS(10000.0 * (p.short_mark - p.long_mark) / ((p.short_mark + p.long_mark) / 2.0)) <= ?
    ORDER BY breakeven_epochs ASC NULLS LAST
    LIMIT 200;
    """
    return sql, [min_volume_usd, min_oi_usd, min_spread_apy_pct, basis_threshold]


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
