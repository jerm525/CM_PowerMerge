"""Remove artifacts from the PM workbook and totals from the CM extract."""
from __future__ import annotations

import pandas as pd

from .columns import (detect_customer_number_column, detect_service_asset_column, normalize_key_series,
                      swap_header_names)
from .logging_setup import get_logger

log = get_logger("cleaning")

PM_WORKING_COLUMNS = [
    "Reporting Month", "_SAN_",
    "PM MRR", "PM MRR Var", "PM Adjusted kW", "PM Adjusted kW Var",
    "COLA/Price Increase Check",
    "PM MRR_x", "PM MRR_y", "PM Adjusted kW_x", "PM Adjusted kW_y",
]


def drop_unnamed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop 'Unnamed: n' columns that appear when an Excel index is re-imported."""
    return df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed", case=False, na=False)]


def strip_output_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep real data rows only: drops subtotal rows, the grand-total row and blank spacers."""
    df = df.copy()
    before = len(df)
    if "Customer Name" in df.columns:
        name = df["Customer Name"].astype(str).str.strip().str.casefold()
        df = df[~name.str.startswith("zz total")]          # only the subtotal label, not any 'zz ...' customer
    marker = df.drop(columns=["Reporting Month"], errors="ignore")   # spacer rows may still carry a month
    df = df[~marker.isna().all(axis=1)]
    if "Customer Number" in df.columns:
        df = df[~df["Customer Number"].astype(str).str.strip().str.casefold().eq("total")]
    df = df.reset_index(drop=True)
    log.info("Output-row cleanup removed %s subtotal/total/blank rows (%s -> %s)",
             f"{before - len(df):,}", f"{before:,}", f"{len(df):,}")
    return df


def clean_pm(pm_raw: pd.DataFrame) -> pd.DataFrame:
    """Prior month's workbook -> clean source frame (no totals, no stale working columns)."""
    pm = drop_unnamed(pm_raw)
    pm = pm.drop(columns=[c for c in PM_WORKING_COLUMNS if c in pm.columns], errors="ignore")
    pm = strip_output_rows(pm)
    log.info("PM file: %s data rows after artifact strip (raw %s)", f"{len(pm):,}", f"{len(pm_raw):,}")
    return pm


def clean_cm(cm_raw: pd.DataFrame) -> pd.DataFrame:
    """CM extract -> drop Unnamed columns, blank rows and any 'Total' rows (by key column, not position)."""
    cm = drop_unnamed(cm_raw)
    cm.columns = [str(c).strip() for c in cm.columns]
    before = len(cm)
    cm = cm.dropna(how="all")
    key_cols = []
    for fn in (detect_service_asset_column, detect_customer_number_column):
        try:
            key_cols.append(fn(cm))
        except KeyError:
            pass
    is_total = pd.Series(False, index=cm.index)
    for c in key_cols:
        is_total |= cm[c].astype(str).str.strip().str.casefold().eq("total")
    cm = cm[~is_total].reset_index(drop=True)
    log.info("CM extract: %s rows after removing %s blank/total rows", f"{len(cm):,}", f"{before - len(cm):,}")
    return cm


def add_san_key(df: pd.DataFrame, san_col: str, insert_at: int) -> pd.DataFrame:
    df = df.copy()
    df["_SAN_"] = normalize_key_series(df[san_col])
    if insert_at >= 0:
        cols = [c for c in df.columns if c != "_SAN_"]
        cols.insert(min(insert_at, len(cols)), "_SAN_")
        df = df[cols]
    return df


def unswap_pm_headers(pm_raw: pd.DataFrame, pairs, san_col: str) -> pd.DataFrame:
    """Put a workbook written with swapped headers back to logical names before merging.

    Only acts when the '_SAN_' column does not actually hold the SANs, so a workbook that was written
    without the swap (or already unswapped) is left alone.
    """
    if not pairs or "_SAN_" not in pm_raw.columns or san_col not in pm_raw.columns:
        return pm_raw
    same = normalize_key_series(pm_raw["_SAN_"]).eq(normalize_key_series(pm_raw[san_col]))
    if same.mean() >= 0.5:
        return pm_raw
    log.info("PM workbook has swapped headers %s; restoring logical names for the merge", list(pairs))
    return pm_raw.set_axis(swap_header_names(list(pm_raw.columns), pairs), axis=1)
