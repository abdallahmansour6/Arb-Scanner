"""Live radar dashboard.

Run:
    streamlit run dashboard.py

Single-operator, localhost-bound. Each tab owns the filters that affect it.
"""

from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

import analytics
from config import (
    DEFAULT_EXIT_BASIS_BPS,
    DEFAULT_MIN_ABS_APY_PCT,
    DEFAULT_MIN_PERSISTENCE,
    DEFAULT_MIN_SPREAD_APY_PCT,
    DEFAULT_MIN_VOLUME_24H_USD,
    DEFAULT_TAKER_FEE_BPS,
    VENUES,
)
from storage import list_distinct, query

st.set_page_config(page_title="Funding Anomaly Scanner", layout="wide")
st.title("Funding Anomaly Scanner")

APY_TOOLTIP = (
    "**Annualized APY** = per-epoch funding rate × (8760 / interval_hours). "
    "This puts 1h, 4h, and 8h pairs on the same yield axis so they can be "
    "compared directly. A pair paying 0.1% per 8-hour epoch shows as 109.5% APY; "
    "the same 0.1% per 1h epoch shows as 876% APY."
)


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

# ---------- sidebar: reference only, no filters ----------
with st.sidebar:
    st.header("Reference")
    with st.expander("What is Annualized APY?", expanded=False):
        st.markdown(APY_TOOLTIP)
    with st.expander("Per-venue coverage", expanded=True):
        summary_sql, _ = analytics.latest_summary()
        summary = cached_query(summary_sql, ())
        if not summary.empty:
            st.dataframe(
                summary,
                hide_index=True,
                width="stretch",
                column_config={
                    "exchange":     st.column_config.TextColumn("Venue"),
                    "symbols":      st.column_config.NumberColumn("Symbols"),
                    "latest_obs":   st.column_config.DatetimeColumn("Latest", format="HH:mm:ss"),
                    "earliest_obs": None,
                },
            )

# ---------- shared column configs ----------
COL_USD = lambda label: st.column_config.NumberColumn(label, format="$%.0f")
COL_PCT_SIGNED = lambda label: st.column_config.NumberColumn(label, format="%+.1f%%")
COL_PCT_UNSIGNED = lambda label: st.column_config.NumberColumn(label, format="%.1f%%")
COL_BPS_SIGNED = lambda label: st.column_config.NumberColumn(label, format="%+.1f bps")
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
        "Latest snapshot of every symbol listed on **2 or more venues**. "
        "**Δ APY** is the gap between the highest and lowest annualized APY across those venues — "
        "the raw size of a potential arb opportunity. Long the venue with low/negative APY, short the high one."
    )
    min_vol_delta = st.number_input(
        "Minimum 24h volume on each leg (USD)",
        min_value=0, value=DEFAULT_MIN_VOLUME_24H_USD, step=100_000, format="%d",
        help="Excludes pairs where either leg lacks the notional liquidity to be a viable arb leg.",
        key="delta_min_vol",
    )
    sql, params = analytics.cross_exchange_delta(min_vol_delta)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No symbols meet the volume threshold yet.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":   st.column_config.TextColumn("Symbol"),
                "venues_listed":      st.column_config.NumberColumn("Venues"),
                "delta_apy_pct":      COL_PCT_UNSIGNED("Δ Annualized APY"),
                "short_venue":        st.column_config.TextColumn("Short on"),
                "long_venue":         st.column_config.TextColumn("Long on"),
                "short_apy_pct":      COL_PCT_SIGNED("Short APY"),
                "long_apy_pct":       COL_PCT_SIGNED("Long APY"),
                "min_volume_24h_usd": COL_USD("Min 24h Volume"),
                "latest_obs":         COL_DT("Last update"),
            },
        )

# ============== Anomaly Persistence ==============
with tab_anom:
    st.markdown(
        "(Symbol, Venue) pairs whose **|Annualized APY| has stayed above the threshold "
        "for the last N consecutive collection cycles**. Filters out single-cycle spikes "
        "(usually data glitches or already-decayed transients) so you only see anomalies "
        "with enough lifespan to be deployable."
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        min_vol_anom = st.number_input(
            "Minimum 24h volume (USD)",
            min_value=0, value=DEFAULT_MIN_VOLUME_24H_USD, step=100_000, format="%d",
            help="Excludes pairs that don't trade enough notional to be a viable arb leg.",
            key="anom_min_vol",
        )
    with c2:
        min_abs_apy = st.number_input(
            "Minimum |Annualized APY| (%)",
            min_value=0.0, value=DEFAULT_MIN_ABS_APY_PCT, step=10.0,
            help=APY_TOOLTIP,
            key="anom_min_apy",
        )
    with c3:
        persistence = st.slider(
            "Must hold for N consecutive cycles",
            min_value=1, max_value=20, value=DEFAULT_MIN_PERSISTENCE,
            help=(
                "How many recent collection cycles in a row must all show "
                "|Annualized APY| above the threshold for the pair to be flagged. "
                "Higher = more confident the anomaly is real, not a single-cycle glitch."
            ),
            key="anom_persistence",
        )

    sql, params = analytics.anomaly_candidates(min_abs_apy, min_vol_anom, persistence)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No anomalies meet both thresholds and persistence — relax filters or wait for more cycles.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":    st.column_config.TextColumn("Symbol"),
                "exchange":            st.column_config.TextColumn("Venue"),
                "persistence_count":   st.column_config.NumberColumn("Cycles held"),
                "avg_apy_pct":         COL_PCT_SIGNED("Avg Annualized APY"),
                "max_abs_apy_pct":     COL_PCT_UNSIGNED("Peak |APY|"),
                "interval_h":          st.column_config.NumberColumn("Interval (h)"),
                "volume_24h_usd":      COL_USD("24h Volume"),
                "open_interest_usd":   COL_USD("Open Interest"),
                "latest_obs":          COL_DT("Last update"),
            },
        )

# ============== Breakeven Epochs ==============
with tab_be:
    st.markdown(
        "For each candidate venue-pair, **Breakeven Epochs** = how many funding cycles "
        "you'd need to hold the position before the captured yield covers your round-trip cost."
    )
    with st.expander("How this is calculated", expanded=False):
        st.markdown(
            "**Entry basis** is computed live from the mark-price spread between the two venues: "
            "`entry_basis_bps = 10000 × (mark_short − mark_long) / mid_mark`. "
            "Positive = favorable entry (you sell the high-priced venue and buy the low-priced one). "
            "Negative = you pay the spread on entry.\n\n"
            "**Exit basis** defaults to 0 — the standard delta-neutral assumption is that "
            "the inter-venue price gap converges by unwind. Use the slider below to stress-test "
            "what happens if convergence is incomplete.\n\n"
            "**Round-trip cost** = `(exit_basis − entry_basis) + 4 × taker_fee_bps`. "
            "(4 = entry + exit, both legs.) **Yield per epoch** = `spread_APY_% × interval_h / 8760`. "
            "**E_BE** = round-trip cost / yield per epoch. **Negative E_BE** means the entry is "
            "favorable enough that you profit instantly — no holding required."
        )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        min_vol_be = st.number_input(
            "Minimum 24h volume on each leg (USD)",
            min_value=0, value=DEFAULT_MIN_VOLUME_24H_USD, step=100_000, format="%d",
            key="be_min_vol",
        )
    with c2:
        min_spread_apy = st.number_input(
            "Minimum spread APY (%)",
            min_value=0.0, value=DEFAULT_MIN_SPREAD_APY_PCT, step=10.0,
            help="Cross-venue annualized APY gap required to even consider the pair.",
            key="be_min_spread",
        )
    with c3:
        taker_fee_bps = st.number_input(
            "Taker fee per side (bps)",
            value=DEFAULT_TAKER_FEE_BPS, step=0.5, min_value=0.0,
            help="Used 4× in the cost (entry + exit, both legs).",
            key="be_taker_fee",
        )
    with c4:
        exit_basis_bps = st.number_input(
            "Residual exit basis assumption (bps)",
            value=DEFAULT_EXIT_BASIS_BPS, step=1.0,
            help=(
                "Assumed bps of basis at unwind. 0 = full price convergence (default). "
                "Increase to stress-test imperfect convergence."
            ),
            key="be_exit_basis",
        )

    sql, params = analytics.breakeven_epochs(min_spread_apy, min_vol_be, exit_basis_bps, taker_fee_bps)
    df = cached_query(sql, tuple(params))
    if df.empty:
        st.info("No venue-pair candidates meet the spread threshold.")
    else:
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "symbol_canonical":    st.column_config.TextColumn("Symbol"),
                "long_venue":          st.column_config.TextColumn("Long on"),
                "short_venue":         st.column_config.TextColumn("Short on"),
                "spread_apy_pct":      COL_PCT_UNSIGNED("Spread APY"),
                "short_interval_h":    st.column_config.NumberColumn("Short int. (h)"),
                "long_interval_h":     st.column_config.NumberColumn("Long int. (h)"),
                "interval_mismatch":   st.column_config.CheckboxColumn("Interval mismatch"),
                "long_mark":           st.column_config.NumberColumn("Long mark", format="%.6f"),
                "short_mark":          st.column_config.NumberColumn("Short mark", format="%.6f"),
                "entry_basis_bps":     COL_BPS_SIGNED("Entry basis (live)"),
                "breakeven_epochs":    st.column_config.NumberColumn(
                    "Breakeven epochs", format="%.2f",
                    help="Negative values mean entry is favorable enough that you profit instantly.",
                ),
                "min_volume_24h_usd":  COL_USD("Min 24h Volume"),
            },
        )

# ============== Symbol Chart ==============
with tab_chart:
    st.markdown(
        "Per-symbol, per-venue history. The **top chart** shows Annualized APY — comparable "
        "across all venues regardless of their funding interval. The **bottom chart** shows the "
        "raw per-epoch funding rate, where each point is the rate paid in that specific funding "
        "cycle (so a 1h-interval venue's points represent 1h yields and an 8h-interval venue's "
        "points represent 8h yields — values are *not* comparable across intervals)."
    )
    symbols = list_distinct("symbol_canonical")
    if not symbols:
        st.info("No data yet.")
    else:
        c_sym, c_period = st.columns([2, 3])
        with c_sym:
            default_idx = symbols.index("BTC/USDT:USDT") if "BTC/USDT:USDT" in symbols else 0
            symbol = st.selectbox("Symbol", symbols, index=default_idx, key="chart_symbol")
        with c_period:
            period_options = {"1h": 1, "6h": 6, "24h": 24, "3 days": 72, "All": None}
            period_label = st.radio(
                "Time window", list(period_options.keys()),
                index=2, horizontal=True,
                help="Restricts the chart to the trailing window. Drag-zoom in plotly for finer slices.",
                key="chart_period",
            )
            hours_back = period_options[period_label]

        venues = st.multiselect(
            "Venues",
            list(VENUES.keys()),
            default=list(VENUES.keys()),
            key="chart_venues",
        )

        if symbol and venues:
            sql, params = analytics.historical_funding(symbol, venues, hours_back)
            df = cached_query(sql, tuple(params))
            if df.empty:
                st.info("No observations for this symbol/venue/window combination.")
            else:
                df["apy_pct"] = df["apy_norm"] * 100
                # Annotate venue with its funding interval (mode within the window)
                interval_per_venue = (
                    df.dropna(subset=["funding_interval_h"])
                      .groupby("exchange")["funding_interval_h"]
                      .agg(lambda s: int(s.mode().iloc[0]) if len(s.mode()) else None)
                      .to_dict()
                )
                df["venue_label"] = df["exchange"].map(
                    lambda e: f"{e} ({interval_per_venue.get(e)}h)" if interval_per_venue.get(e) else e
                )

                fig = px.line(
                    df, x="ts_utc", y="apy_pct", color="venue_label",
                    title=f"{symbol} — Annualized APY (%) — comparable across all venues",
                    labels={"ts_utc": "Time (UTC)", "apy_pct": "Annualized APY (%)", "venue_label": "Venue"},
                )
                fig.update_layout(
                    hovermode="x unified",
                    xaxis=dict(rangeslider=dict(visible=True), type="date"),
                )
                st.plotly_chart(fig, width="stretch")

                fig2 = px.line(
                    df, x="ts_utc", y="funding_rate", color="venue_label",
                    title=f"{symbol} — Raw per-epoch funding rate (NOT comparable across intervals)",
                    labels={"ts_utc": "Time (UTC)", "funding_rate": "Per-epoch rate", "venue_label": "Venue"},
                )
                fig2.update_layout(hovermode="x unified")
                st.plotly_chart(fig2, width="stretch")
