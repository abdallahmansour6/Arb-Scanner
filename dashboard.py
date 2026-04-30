"""Live radar dashboard.

Run:
    streamlit run dashboard.py

Single-operator, localhost-bound. Four views over the same DuckDB substrate:
cross-exchange APY-norm delta, anomaly persistence, breakeven epochs, and a
per-symbol cross-venue chart.
"""

from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

import analytics
from config import (
    DEFAULT_BASIS_COST_BPS,
    DEFAULT_MIN_ABS_APY_PCT,
    DEFAULT_MIN_PERSISTENCE,
    DEFAULT_MIN_VOLUME_24H_USD,
    DEFAULT_TAKER_FEE_BPS,
    VENUES,
)
from storage import list_distinct, query

st.set_page_config(page_title="Funding Anomaly Scanner", layout="wide")
st.title("Funding Anomaly Scanner")


@st.cache_data(ttl=15)
def cached_query(sql: str, params: tuple) -> pd.DataFrame:
    return query(sql, list(params))


# ---------- freshness banner ----------
freshness = cached_query(
    "SELECT MAX(ts_utc) AS latest, COUNT(DISTINCT ts_utc) AS cycles, COUNT(*) AS rows FROM funding",
    (),
)
if freshness.empty or freshness["latest"].iloc[0] is None:
    st.warning("No data in the substrate yet — start the collector first.")
    st.stop()

latest_ts = freshness["latest"].iloc[0].to_pydatetime()
if latest_ts.tzinfo is None:
    latest_ts = latest_ts.replace(tzinfo=timezone.utc)
age_min = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 60.0
cycles = int(freshness["cycles"].iloc[0])
rows = int(freshness["rows"].iloc[0])

if age_min <= 15:
    dot = ":green[●]"
elif age_min <= 30:
    dot = ":orange[●]"
else:
    dot = ":red[●]"

c1, c2, c3 = st.columns([3, 2, 2])
c1.markdown(f"{dot} **Last cycle:** {latest_ts:%Y-%m-%d %H:%M UTC}  ·  **{age_min:.1f} min ago**")
c2.markdown(f"**{cycles}** cycles in dataset")
c3.markdown(f"**{rows:,}** total observations")

st.divider()

# ---------- sidebar ----------
with st.sidebar:
    st.header("Liquidity & signal filters")

    min_vol = st.number_input(
        "Minimum 24h volume (USD)",
        min_value=0, value=DEFAULT_MIN_VOLUME_24H_USD, step=100_000, format="%d",
        help="Excludes pairs that don't trade enough notional in 24h to be a viable arb leg.",
    )
    min_abs_apy = st.number_input(
        "Minimum |APY-norm| (%)",
        min_value=0.0, value=DEFAULT_MIN_ABS_APY_PCT, step=10.0,
        help=(
            "APY-norm is the per-epoch funding rate annualized to a single comparable scale: "
            "rate × (8760 / interval_hours). Apples-to-apples across 1h, 4h, 8h pairs."
        ),
    )
    persistence = st.slider(
        "Persistence (consecutive cycles)",
        min_value=1, max_value=20, value=DEFAULT_MIN_PERSISTENCE,
        help=(
            "How many recent collection cycles in a row must show |APY-norm| above the floor "
            "for the pair to be flagged. Higher = more confident the anomaly is real and not "
            "a single-cycle data glitch."
        ),
    )

    with st.expander("Breakeven Epochs assumptions (ranking only)"):
        st.caption(
            "These are static estimates used to *rank* candidates against each other — "
            "not the live basis. The engine reads real basis off the order book at execution."
        )
        basis_cost_bps = st.number_input(
            "Estimated entry+exit basis cost (bps, round-trip)",
            value=DEFAULT_BASIS_COST_BPS, step=1.0,
        )
        taker_fee_bps = st.number_input(
            "Taker fee (bps, per leg per side)",
            value=DEFAULT_TAKER_FEE_BPS, step=0.5,
        )

    st.divider()
    st.caption("Per-venue coverage")
    summary_sql, _ = analytics.latest_summary()
    summary = cached_query(summary_sql, ())
    if not summary.empty:
        st.dataframe(
            summary,
            hide_index=True,
            width="stretch",
            column_config={
                "exchange": st.column_config.TextColumn("Exchange"),
                "symbols": st.column_config.NumberColumn("Symbols"),
                "latest_obs": st.column_config.DatetimeColumn("Latest", format="HH:mm:ss"),
                "earliest_obs": None,  # hide
            },
        )

# ---------- shared column configs ----------
COL_USD = lambda label: st.column_config.NumberColumn(label, format="$%.0f")
COL_PCT = lambda label, sign=True: st.column_config.NumberColumn(
    label, format=("%+.1f%%" if sign else "%.1f%%")
)
COL_DT = lambda label: st.column_config.DatetimeColumn(label, format="MM-DD HH:mm")

# ---------- tabs ----------
tab_delta, tab_anom, tab_be, tab_chart = st.tabs([
    "Cross-Exchange Delta",
    "Anomaly Persistence",
    "Breakeven Epochs",
    "Symbol Chart",
])

# ============== Cross-Exchange Delta ==============
with tab_delta:
    st.markdown(
        "Latest snapshot per (symbol, venue), grouped by symbol. "
        "**Delta APY** is the gap between the highest and lowest APY-norm across venues — "
        "the raw size of a potential arb opportunity. Long the venue with low/negative APY, short the high one."
    )
    sql, params = analytics.cross_exchange_delta(min_vol)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No symbols meet the volume floor yet.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":   st.column_config.TextColumn("Symbol"),
                "venues_listed":      st.column_config.NumberColumn("Venues"),
                "delta_apy_pct":      COL_PCT("Δ APY", sign=False),
                "short_venue":        st.column_config.TextColumn("Short on"),
                "long_venue":         st.column_config.TextColumn("Long on"),
                "short_apy_pct":      COL_PCT("Short APY"),
                "long_apy_pct":       COL_PCT("Long APY"),
                "min_volume_24h_usd": COL_USD("Min 24h Volume"),
                "latest_obs":         COL_DT("Last update"),
            },
        )

# ============== Anomaly Persistence ==============
with tab_anom:
    st.markdown(
        f"(Symbol, Venue) pairs whose **|APY-norm| has stayed above {min_abs_apy:.0f}% for "
        f"the last {persistence} consecutive cycles**. Filters out single-cycle spikes "
        "(usually data glitches or already-decayed transients) so you only see anomalies "
        "with enough lifespan to be deployable."
    )
    sql, params = analytics.anomaly_candidates(min_abs_apy, min_vol, persistence)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No anomalies meet the persistence + APY floor — relax filters or wait for more cycles.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":    st.column_config.TextColumn("Symbol"),
                "exchange":            st.column_config.TextColumn("Venue"),
                "persistence_count":   st.column_config.NumberColumn("Cycles held"),
                "avg_apy_pct":         COL_PCT("Avg APY"),
                "max_abs_apy_pct":     COL_PCT("Peak |APY|", sign=False),
                "interval_h":          st.column_config.NumberColumn("Interval (h)"),
                "volume_24h_usd":      COL_USD("24h Volume"),
                "open_interest_usd":   COL_USD("Open Interest"),
                "latest_obs":          COL_DT("Last update"),
            },
        )

# ============== Breakeven Epochs ==============
with tab_be:
    st.markdown(
        "For each candidate venue-pair, **Breakeven Epochs** = how many funding cycles you'd "
        "need to sit on the position before the captured yield covers your round-trip cost. "
        "Lower = better. Cost = `entry+exit basis cost + 2 × taker fee` (from the sidebar's "
        "ranking assumptions). Use this to compare opportunities apples-to-apples — *not* to "
        "predict actual entry conditions."
    )
    sql, params = analytics.breakeven_epochs(min_abs_apy, min_vol, basis_cost_bps, taker_fee_bps)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No venue-pair candidates meet the spread floor.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":    st.column_config.TextColumn("Symbol"),
                "long_venue":          st.column_config.TextColumn("Long on"),
                "short_venue":         st.column_config.TextColumn("Short on"),
                "spread_apy_pct":      COL_PCT("Spread APY", sign=False),
                "short_interval_h":    st.column_config.NumberColumn("Short int. (h)"),
                "long_interval_h":     st.column_config.NumberColumn("Long int. (h)"),
                "interval_mismatch":   st.column_config.CheckboxColumn("Interval mismatch"),
                "breakeven_epochs":    st.column_config.NumberColumn("Breakeven epochs", format="%.2f"),
                "min_volume_24h_usd":  COL_USD("Min 24h Volume"),
            },
        )

# ============== Symbol Chart ==============
with tab_chart:
    st.markdown(
        "Per-symbol, per-venue history. APY-norm makes 1h/4h/8h pairs comparable on the "
        "same axis — the bottom chart shows the raw per-epoch rate for sanity-checking."
    )
    symbols = list_distinct("symbol_canonical")
    if not symbols:
        st.info("No data yet.")
    else:
        c_sym, c_period = st.columns([2, 3])
        with c_sym:
            default_idx = symbols.index("BTC/USDT:USDT") if "BTC/USDT:USDT" in symbols else 0
            symbol = st.selectbox("Symbol", symbols, index=default_idx)
        with c_period:
            period_options = {"1h": 1, "6h": 6, "24h": 24, "3 days": 72, "All": None}
            period_label = st.radio(
                "Time window", list(period_options.keys()),
                index=2, horizontal=True,
                help="Restricts the chart to the trailing window. Use plotly's drag-to-zoom for finer slices.",
            )
            hours_back = period_options[period_label]

        venues = st.multiselect(
            "Exchanges",
            list(VENUES.keys()),
            default=list(VENUES.keys()),
        )

        if symbol and venues:
            sql, params = analytics.historical_funding(symbol, venues, hours_back)
            df = cached_query(sql, tuple(params))
            if df.empty:
                st.info("No observations for this symbol/venue/window combination.")
            else:
                df["apy_pct"] = df["apy_norm"] * 100

                fig = px.line(
                    df, x="ts_utc", y="apy_pct", color="exchange",
                    title=f"{symbol} — APY-norm (%) by venue",
                    labels={"ts_utc": "Time (UTC)", "apy_pct": "APY-norm (%)", "exchange": "Venue"},
                )
                fig.update_layout(
                    hovermode="x unified",
                    xaxis=dict(rangeslider=dict(visible=True), type="date"),
                )
                st.plotly_chart(fig, width="stretch")

                fig2 = px.line(
                    df, x="ts_utc", y="funding_rate", color="exchange",
                    title=f"{symbol} — raw per-epoch funding rate",
                    labels={"ts_utc": "Time (UTC)", "funding_rate": "Per-epoch rate", "exchange": "Venue"},
                )
                fig2.update_layout(hovermode="x unified")
                st.plotly_chart(fig2, width="stretch")
