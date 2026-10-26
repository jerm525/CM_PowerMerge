"""Column and key helpers. Matching is strict: a missing column is reported,
never replaced by a 'closest' guess."""
from __future__ import annotations

import re
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .logging_setup import get_logger

log = get_logger("columns")


def normalize_colname(col) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", " ", str(col).lower()).strip()
    return re.sub(r"\s+", " ", s)


def token_set(col) -> set:
    return set(normalize_colname(col).split())


def find_column(df_or_cols, target: str, aliases: Iterable[str] = ()) -> Optional[str]:
    """Exact name, then normalized name, then aliases (exact, normalized). None if absent."""
    cols = list(df_or_cols.columns) if hasattr(df_or_cols, "columns") else list(df_or_cols)
    names = [target, *aliases]
    for name in names:
        for c in cols:
            if str(c).strip().lower() == str(name).strip().lower():
                return c
    for name in names:
        n = normalize_colname(name)
        for c in cols:
            if normalize_colname(c) == n:
                return c
    return None


def _score_detect(df, wanted, extra_exact, penalise=()):
    best, best_score = None, 0
    for c in df.columns:
        toks = token_set(c)
        score = 0
        if toks & wanted[0]:
            score += 2
        if toks & wanted[1]:
            score += 2
        if toks & set(penalise):
            score -= 1
        if normalize_colname(c) in extra_exact:
            score += 3
        if score > best_score:
            best, best_score = c, score
    return best


def detect_customer_number_column(df: pd.DataFrame) -> str:
    for alt in ("Customer Number", "CustomerNumber", "Customer #", "Account Number", "Acct Number"):
        if alt in df.columns:
            return alt
    best = _score_detect(df, ({"customer", "cust", "account", "acct"}, {"number", "num", "no", "id"}),
                         {"customer number", "customer no", "customer id", "account number", "acct number"},
                         penalise=("name",))
    if best is None:
        raise KeyError("Cannot identify the customer number column")
    return best


def detect_service_asset_column(df: pd.DataFrame) -> str:
    for alt in ("Service Asset Number", "Service Asset #", "SAN", "ServiceAssetNumber"):
        if alt in df.columns:
            return alt
    best = _score_detect(df, ({"service", "asset", "san"}, {"number", "num", "no", "id"}),
                         {"service asset number", "service asset id", "san"})
    if best is None:
        raise KeyError("Cannot identify the Service Asset Number column")
    return best


def detect_datacenter_column(df: pd.DataFrame) -> Optional[str]:
    for alt in ("Data Center", "Datacenter", "DataCenter", "DC", "Facility"):
        if alt in df.columns:
            return alt
    return None


def _norm_key(v):
    if v is None or v is pd.NA or v is pd.NaT:
        return np.nan
    if isinstance(v, (float, np.floating)):
        if np.isnan(v):
            return np.nan
        if float(v).is_integer():
            return str(int(v))
    s = re.sub(r"\.0$", "", str(v).strip())
    return np.nan if s in ("", "nan", "NaN", "None", "<NA>") else s


def normalize_key_series(series: pd.Series) -> pd.Series:
    """Strip whitespace and a trailing '.0'; blanks become NaN. Result is object dtype."""
    return series.map(_norm_key).astype(object)


def to_number(series: pd.Series) -> pd.Series:
    """Parse numbers from numbers or text. '$1,234.50' -> 1234.5; '(123.45)' -> -123.45."""
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return series.astype(float)
    num = pd.to_numeric(series, errors="coerce").astype(float)
    need = num.isna() & series.notna()
    if need.any():
        txt = series[need].astype(str).str.strip()
        neg = txt.str.match(r"^\(.*\)$")
        cleaned = txt.str.replace(r"[^0-9.\-]", "", regex=True)
        val = pd.to_numeric(cleaned, errors="coerce").astype(float)
        val = val.where(~neg, -val.abs())
        num = num.copy()
        num.loc[need] = val.to_numpy()
    return num


def coerce_numeric_columns(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = to_number(df[c])
    return df


def ensure_object(df: pd.DataFrame, col: str) -> None:
    """Create the column if needed and make it object dtype so strings can be assigned."""
    if col not in df.columns:
        df[col] = pd.Series([np.nan] * len(df), index=df.index, dtype=object)
    elif df[col].dtype != object:
        df[col] = df[col].astype(object)


# ------------------------------------------------------------ Excel letters
def col_letter(idx: int) -> str:
    """Zero-based column index -> Excel letters (0 -> A, 26 -> AA)."""
    letters, n = "", idx
    while True:
        n, r = divmod(n, 26)
        letters = chr(65 + r) + letters
        if n == 0:
            return letters
        n -= 1


def letter_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def swap_header_names(columns, pairs) -> list:
    """Swap the names in each (a, b) pair; every other name is untouched. Applying it twice restores the original."""
    m = {}
    for a, b in pairs or []:
        m[a], m[b] = b, a
    return [m.get(c, c) for c in columns]
