"""Append-only Parquet writer + DuckDB reader over hive partitions.

Layout: data/funding/year=YYYY/month=MM/day=DD/{cycle_ts_utc}.parquet
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from config import FUNDING_DIR

SCHEMA = pa.schema([
    pa.field("ts_utc",             pa.timestamp("ms", tz="UTC")),
    pa.field("exchange",           pa.string()),
    pa.field("symbol_canonical",   pa.string()),
    pa.field("funding_rate",       pa.float64()),
    pa.field("funding_interval_h", pa.int8()),
    pa.field("predicted_rate",     pa.float64()),
    pa.field("next_funding_ts",    pa.timestamp("ms", tz="UTC")),
    pa.field("mark_price",         pa.float64()),
    pa.field("index_price",        pa.float64()),
    pa.field("open_interest_usd",  pa.float64()),
    pa.field("volume_24h_usd",     pa.float64()),
    pa.field("apy_norm",           pa.float64()),
])


def write_observations(rows: list[dict]) -> Path | None:
    if not rows:
        return None

    df = pd.DataFrame(rows, columns=[f.name for f in SCHEMA])
    # pyarrow needs explicit datetime64[ms, UTC] to match the schema cleanly
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True).astype("datetime64[ms, UTC]")
    df["next_funding_ts"] = pd.to_datetime(df["next_funding_ts"], utc=True).astype("datetime64[ms, UTC]")
    table = pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)

    cycle = df["ts_utc"].iloc[0].to_pydatetime()
    partition = FUNDING_DIR / f"year={cycle:%Y}" / f"month={cycle:%m}" / f"day={cycle:%d}"
    partition.mkdir(parents=True, exist_ok=True)
    path = partition / f"{cycle:%Y%m%dT%H%M%SZ}.parquet"
    pq.write_table(table, path, compression="zstd")
    return path


def _glob_pattern() -> str:
    # DuckDB accepts forward slashes on Windows.
    return (FUNDING_DIR / "**" / "*.parquet").as_posix()


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Execute SQL against a `funding` view bound to the parquet partitions.

    Returns an empty DataFrame if no parquet has been written yet.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            f"CREATE VIEW funding AS "
            f"SELECT * FROM read_parquet('{_glob_pattern()}', hive_partitioning=true, union_by_name=true)"
        )
    except duckdb.IOException:
        return pd.DataFrame()
    if params:
        return con.execute(sql, params).fetchdf()
    return con.execute(sql).fetchdf()


def list_distinct(column: str) -> list:
    df = query(f"SELECT DISTINCT {column} AS v FROM funding ORDER BY v")
    return [] if df.empty else df["v"].dropna().tolist()
