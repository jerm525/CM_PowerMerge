"""Data sources: PM workbook, CM stored procedure / Excel, model reference."""
from __future__ import annotations

import contextlib
import os
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import SqlConfig, name_for_month, prior_month_end
from .logging_setup import get_logger
from decimal import Decimal

log = get_logger("sources")


# ------------------------------------------------------------------ SQL
def build_connection_string(cfg: SqlConfig) -> str:
    full = os.environ.get(cfg.conn_str_env)
    if full:
        return full
    if not cfg.server:
        raise ValueError(f"sql.server is not set and ${cfg.conn_str_env} is empty")
    parts = [f"DRIVER={{{cfg.driver}}}", f"SERVER={cfg.server}", f"DATABASE={cfg.database}"]
    if cfg.encrypt is not None:
        parts.append(f"Encrypt={'yes' if cfg.encrypt else 'no'}")
    if cfg.trust_server_certificate is not None:
        parts.append(f"TrustServerCertificate={'yes' if cfg.trust_server_certificate else 'no'}")
    if cfg.trusted_connection:
        parts.append("Trusted_Connection=yes")
    else:
        user, pwd = os.environ.get(cfg.user_env), os.environ.get(cfg.password_env)
        if not user or not pwd:
            raise ValueError(f"SQL auth needs ${cfg.user_env} and ${cfg.password_env}")
        parts += [f"UID={user}", f"PWD={pwd}"]
    return ";".join(parts) + ";"


def connect(cfg: SqlConfig, autocommit: bool = True):
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover
        raise ImportError("pyodbc is required for SQL access: pip install pyodbc") from exc
    conn = pyodbc.connect(build_connection_string(cfg), autocommit=autocommit,
                          timeout=30)
    conn.timeout = cfg.timeout_seconds
    return conn


def _rows_to_frame(cur) -> pd.DataFrame:
    """Return the first result set that has columns (skips row-count messages)."""
    while cur.description is None:
        if not cur.nextset():
            return pd.DataFrame()
    cols = [str(d[0]).strip() for d in cur.description]
    return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)


def load_cm_from_sql(cfg: SqlConfig, month: date) -> pd.DataFrame:
    log.info("Executing %s @ReportingMonth=%s", cfg.stored_proc, month)
    with contextlib.closing(connect(cfg)) as conn:
        cur = conn.cursor()
        cur.execute(f"SET NOCOUNT ON; EXEC {cfg.stored_proc} @ReportingMonth = ?", month)
        df = _rows_to_frame(cur)
    log.info("Stored procedure returned %s rows, %s columns", f"{len(df):,}", df.shape[1])
    print(df['Unit Price'].iloc[0])
    print(type(df['Unit Price'].iloc[0]))
    df['Unit Price'] = df['Unit Price'].astype(float)
    return df


def read_sql_frame(cfg: SqlConfig, sql: str, params=()) -> pd.DataFrame:
    with contextlib.closing(connect(cfg)) as conn:
        cur = conn.cursor()
        cur.execute(sql, *params)
        return _rows_to_frame(cur)


# ---------------------------------------------------------------- files
def load_excel(path: Path, sheet: int = 0) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_excel(path, sheet_name=sheet, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    log.info("Read %s rows x %s columns from %s", f"{len(df):,}", df.shape[1], path.name)
    return df


def prior_output_path(output_dir: Path, pattern: str, month: date) -> Path:
    """Last month's validated output is this month's PM input."""
    return Path(output_dir) / name_for_month(pattern, prior_month_end(month))


def load_model_ref(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Commercial Model Reference not found: {path}")
    ref = pd.read_excel(path, dtype=object)
    ref.columns = [str(c).strip() for c in ref.columns]
    if "Commerical Model Helper" in ref.columns and "Commercial Model Helper" not in ref.columns:
        ref = ref.rename(columns={"Commerical Model Helper": "Commercial Model Helper"})
    return ref
