"""Live radar dashboard.

Run:
    streamlit run dashboard.py

Single-operator, localhost-bound. Three views over the same DuckDB substrate:
cross-exchange delta, anomaly candidates, per-symbol cross-exchange chart.
"""

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


# ---------- sidebar ----------
with st.sidebar:
    st.header("Filters")
    min_vol = st.number_input(
        "Min 24h volume (USD)",
        min_value=0, value=DEFAULT_MIN_VOLUME_24H_USD, step=100_000, format="%d",
    )
    min_abs_apy = st.number_input(
        "Min |APY norm| (%)",
        min_value=0.0, value=DEFAULT_MIN_ABS_APY_PCT, step=10.0,
    )
    persistence = st.slider(
        "Persistence (consecutive obs)",
        min_value=1, max_value=20, value=DEFAULT_MIN_PERSISTENCE,
    )
    st.divider()
    st.caption("Breakeven Epochs assumptions")
    basis_cost_bps = st.number_input("Basis cost (bps, round-trip)", value=DEFAULT_BASIS_COST_BPS, step=1.0)
    taker_fee_bps = st.number_input("Taker fee (bps, per leg per side)", value=DEFAULT_TAKER_FEE_BPS, step=0.5)

    st.divider()
    summary_sql, _ = analytics.latest_summary()
    summary = cached_query(summary_sql, ())
    st.caption(f"{len(summary)} venues with data")
    if not summary.empty:
        st.dataframe(summary, hide_index=True, use_container_width=True)

# ---------- tabs ----------
tab_delta, tab_anom, tab_be, tab_chart = st.tabs(
    ["Cross-Exchange Delta", "Anomaly Candidates", "Breakeven Epochs", "Symbol Chart"]
)

with tab_delta:
    sql, params = analytics.cross_exchange_delta(min_vol)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No data yet — run the collector.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)

with tab_anom:
    sql, params = analytics.anomaly_candidates(min_abs_apy, min_vol, persistence)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No anomalies above the floor with current persistence — relax filters or wait for more cycles.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)

with tab_be:
    sql, params = analytics.breakeven_epochs(min_abs_apy, min_vol, basis_cost_bps, taker_fee_bps)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No venue-pair candidates above the spread floor.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)

with tab_chart:
    symbols = list_distinct("symbol_canonical")
    if not symbols:
        st.info("No data yet.")
    else:
        col1, col2 = st.columns([2, 3])
        with col1:
            default_idx = symbols.index("BTC/USDT:USDT") if "BTC/USDT:USDT" in symbols else 0
            symbol = st.selectbox("Symbol", symbols, index=default_idx)
        with col2:
            venues = st.multiselect(
                "Exchanges",
                list(VENUES.keys()),
                default=list(VENUES.keys()),
            )
        if symbol and venues:
            sql, params = analytics.historical_funding(symbol, venues)
            df = cached_query(sql, tuple(params))
            if df.empty:
                st.info("No observations for this symbol on the selected venues.")
            else:
                df["apy_pct"] = df["apy_norm"] * 100
                fig = px.line(
                    df, x="ts_utc", y="apy_pct", color="exchange",
                    title=f"{symbol} — APY norm (%) by exchange",
                )
                fig.update_layout(hovermode="x unified")
                st.plotly_chart(fig, use_container_width=True)

                fig2 = px.line(
                    df, x="ts_utc", y="funding_rate", color="exchange",
                    title=f"{symbol} — raw per-epoch funding rate",
                )
                fig2.update_layout(hovermode="x unified")
                st.plotly_chart(fig2, use_container_width=True)
