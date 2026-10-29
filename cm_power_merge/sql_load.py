"""Staging load, promotion to production, and table reads.

Nothing here runs unless the CLI is asked to. Promotion is one transaction:
DELETE the month, INSERT from staging, verify the row count, then COMMIT.
"""
from __future__ import annotations

import contextlib
import re
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

from .config import SqlConfig
from .logging_setup import get_logger
from .sources import connect, read_sql_frame

log = get_logger("sql_load")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
AUDIT_COLS = [] #"LoadedAt"


def quote_table(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"Unsafe table name: {name!r}")
    return ".".join(f"[{p}]" for p in name.split("."))


def quote_col(name) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _split(name: str):
    parts = name.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])


def infer_sql_type(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "BIT"
    if pd.api.types.is_integer_dtype(s):
        return "BIGINT"
    if pd.api.types.is_float_dtype(s):
        return "FLOAT"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "DATE"
    vals = s.dropna()
    if vals.empty:
        return "NVARCHAR(4000)"
    if all(isinstance(v, (date, datetime)) for v in vals.head(1000)):
        return "DATE"
    if all(isinstance(v, (bool, np.bool_)) for v in vals.head(1000)):
        return "BIT"
    if all(isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_))
           for v in vals.head(1000)):
        return "FLOAT"
    longest = vals.astype(str).str.len().max()
    return "NVARCHAR(4000)" if longest <= 4000 else "NVARCHAR(MAX)"


def ddl_for(df: pd.DataFrame, table: str) -> str:
    lines = ["    [RunId] NVARCHAR(32) NOT NULL", "    [LoadedAt] DATETIME2 NOT NULL"]
    lines += [f"    {quote_col(c)} {infer_sql_type(df[c])} NULL" for c in df.columns]
    return f"CREATE TABLE {quote_table(table)} (\n" + ",\n".join(lines) + "\n);"


def _convert(v, sql_type: str):
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)) and (np.isnan(v) or np.isinf(v)):
        return None
    if sql_type.startswith("NVARCHAR"):
        return str(v)
    if sql_type == "BIT":
        return bool(v)
    if sql_type == "DATE":
        return v.date() if isinstance(v, datetime) else v
    if sql_type == "BIGINT":
        return int(v)
    return float(v)


def table_columns(cfg: SqlConfig, table: str) -> list:
    schema, name = _split(table)
    df = read_sql_frame(cfg, "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                             "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION", (schema, name))
    return df["COLUMN_NAME"].tolist() if not df.empty else []

def get_eda_execution(cur, run_id: str) -> int:
    """
    Temporary implementation.
    Replace with RunId-derived value if approved.
    """
    cur.execute("""
        SELECT COALESCE(MAX(eda_execution), 0)
        FROM eda_lake.xlsx_extract.tbl__xlsx_m_colo_kw_audit
    """)

    return cur.fetchone()[0] + 1

def load_to_staging(df: pd.DataFrame, cfg: SqlConfig, run_id: str) -> int:
    """Insert validated data rows into the (pre-created) staging table. Idempotent per RunId."""
    df['eda_isActive'] = True
    df.rename(columns=cfg.column_map, inplace=True)

    table = cfg.staging_table
    existing = table_columns(cfg, table)
    if not existing:
        raise RuntimeError(f"Staging table {table} does not exist. Review `ddl` output and create it first.")
    missing = [c for c in df.columns if c not in existing]
    if missing:
        raise RuntimeError(f"Staging table {table} lacks column(s) {missing}; schema drift, load refused.")


    with contextlib.closing(connect(cfg, autocommit=False)) as conn:
        cur = conn.cursor()
        eda_execution = get_eda_execution(cur, run_id)
        df['eda_guid'] = run_id
        df['eda_execution'] = eda_execution
        types = {c: infer_sql_type(df[c]) for c in df.columns}
        cols = list(df.columns)
        # now = datetime.now() # To add runId and loadedAt
        # rows = [[run_id, now] + [_convert(v, types[c]) for c, v in zip(cols, rec)]
        #         for rec in df.astype(object).itertuples(index=False, name=None)]
        rows = [ [_convert(v, types[c]) for c, v in zip(cols, rec)]
                for rec in df.astype(object).itertuples(index=False, name=None)]
        sql = (f"INSERT INTO {quote_table(table)} ({', '.join(quote_col(c) for c in AUDIT_COLS + cols)}) "
               f"VALUES ({', '.join('?' * (len(cols) + len(AUDIT_COLS)))})")
        try:

            cur.execute(f"DELETE FROM {quote_table(table)} WHERE [eda_guid] = ?", run_id)
            cur.fast_executemany = True
            cur.executemany(sql, rows)
            cur.execute(f"SELECT COUNT(*) FROM {quote_table(table)} WHERE [eda_guid] = ?", run_id)
            loaded = cur.fetchone()[0]
            if loaded != len(rows):
                raise RuntimeError(f"Staging row count {loaded} != expected {len(rows)}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    log.info("Loaded %s rows into %s (RunId %s), eda_execution: %s", f"{loaded:,}", table, run_id,eda_execution)
    return loaded


def promote(cfg: SqlConfig, run_id: str, month: date) -> int:
    """Replace the month in production with the staged run, atomically."""

    #Replace with mapping
    stg, prod = cfg.staging_table, cfg.production_table
    stg_cols = [c for c in table_columns(cfg, stg) if c not in AUDIT_COLS]
    prod_cols = table_columns(cfg, prod)
    if not prod_cols:
        raise RuntimeError(f"Production table {prod} not found")
    missing = [c for c in stg_cols if c not in prod_cols]
    if missing:
        raise RuntimeError(f"Production table lacks column(s) {missing}; promotion refused.")
    mcol = cfg.month_column
    if mcol not in stg_cols:
        raise RuntimeError(f"Month column {mcol!r} is not in the staging table")


    col_list = ", ".join(quote_col(c) for c in stg_cols)

    with contextlib.closing(connect(cfg, autocommit=False)) as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {quote_table(stg)} WHERE [RunId] = ?", run_id)
            staged = cur.fetchone()[0]
            if staged == 0:
                raise RuntimeError(f"No staged rows for RunId {run_id}")
            cur.execute(f"SELECT COUNT(*) FROM {quote_table(stg)} WHERE [RunId] = ? AND {quote_col(mcol)} <> ?",
                        run_id, month)
            if cur.fetchone()[0]:
                raise RuntimeError(f"Staged RunId {run_id} contains rows for a month other than {month}")
            cur.execute(f"DELETE FROM {quote_table(prod)} WHERE {quote_col(mcol)} = ?", month)
            removed = cur.rowcount
            cur.execute(f"INSERT INTO {cfg.production_database}.{quote_table(prod)} ({col_list}) SELECT {col_list} "
                        f"FROM {quote_table(stg)} WHERE [RunId] = ?", run_id)
            inserted = cur.rowcount
            if inserted != staged:
                raise RuntimeError(f"Inserted {inserted} rows but staged {staged}; rolled back")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    log.info("Promoted RunId %s to %s for %s: replaced %s rows with %s", run_id, prod, month,
             f"{removed:,}", f"{inserted:,}")
    return inserted


def read_month(cfg: SqlConfig, which: str, month: date, run_id: Optional[str] = None) -> pd.DataFrame:
    table = cfg.staging_table if which == "stg" else cfg.production_table
    sql = f"SELECT * FROM {quote_table(table)} WHERE {quote_col(cfg.month_column)} = ?"
    params = [month]
    if which == "stg":
        if not run_id:
            raise ValueError("run id is required to read staging")
        sql += " AND [RunId] = ?"
        params.append(run_id)
    df = read_sql_frame(cfg, sql, params)
    return df.drop(columns=[c for c in AUDIT_COLS if c in df.columns])
