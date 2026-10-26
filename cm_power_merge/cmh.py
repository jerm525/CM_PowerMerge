"""Commercial Model Helper (CMH) assignment for newly added assets."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .columns import ensure_object
from .logging_setup import get_logger

log = get_logger("cmh")
CMH = "Commercial Model Helper"


def blank_helper(s: pd.Series) -> pd.Series:
    """Blank = missing, empty text, or the header text leaked into the column."""
    txt = s.astype(str).str.strip()
    return s.isna() | txt.eq("") | txt.str.casefold().eq("commercial model helper")


def build_ref_maps(ref: pd.DataFrame):
    """Return (base_sku_map, (F2,F3) map). Duplicate keys keep the first row and are logged."""
    base_map, pair_map = {}, {}
    if CMH not in ref.columns:
        raise KeyError(f"Model reference lacks a '{CMH}' column")
    if "Base SKU" in ref.columns:
        sub = ref.dropna(subset=["Base SKU"])
        for sku, helper in zip(sub["Base SKU"].astype(str).str.strip().str.upper(), sub[CMH]):
            if sku in base_map and base_map[sku] != helper:
                log.warning("Model reference: Base SKU %s maps to several helpers; keeping %r", sku, base_map[sku])
                continue
            base_map.setdefault(sku, helper)
    if {"F2", "F3"} <= set(ref.columns):
        sub = ref.dropna(subset=["F2", "F3"], how="all")
        keys = zip(sub["F2"].astype(str).str.strip().str.upper(), sub["F3"].astype(str).str.strip().str.upper())
        for key, helper in zip(keys, sub[CMH]):
            if key in pair_map and pair_map[key] != helper:
                log.warning("Model reference: (F2,F3)=%s maps to several helpers; keeping %r", key, pair_map[key])
                continue
            pair_map.setdefault(key, helper)
    return base_map, pair_map


def assign_cmh_for_new_rows(merged: pd.DataFrame, ref: Optional[pd.DataFrame],
                            require_ref: bool = True) -> pd.DataFrame:
    """Fill a blank CMH on f2_add rows: Base SKU first, then (F2, F3)."""
    df = merged.copy()
    ensure_object(df, CMH)
    new = df["_SOURCE_"].eq("f2_add") & blank_helper(df[CMH])
    if not new.any():
        return df
    if ref is None or ref.empty:
        if not require_ref:
            log.warning("%s new rows have no Commercial Model Helper and no model reference is available; "
                        "left blank", int(new.sum()))
            return df
        raise ValueError(f"{int(new.sum())} new rows need a {CMH} but the model reference is missing or empty")

    base_map, pair_map = build_ref_maps(ref)
    n_new = int(new.sum())

    if "Base SKU" in df.columns and base_map:
        keys = df.loc[new, "Base SKU"].astype(str).str.strip().str.upper()
        mapped = keys.map(base_map).dropna()
        df.loc[mapped.index, CMH] = mapped
    remaining = new & blank_helper(df[CMH])
    by_sku = n_new - int(remaining.sum())

    if remaining.any() and pair_map and {"F2", "F3"} <= set(df.columns):
        f2 = df.loc[remaining, "F2"].astype(str).str.strip().str.upper()
        f3 = df.loc[remaining, "F3"].astype(str).str.strip().str.upper()
        mapped = pd.Series([pair_map.get(k) for k in zip(f2, f3)], index=f2.index, dtype=object).dropna()
        df.loc[mapped.index, CMH] = mapped
    still = new & blank_helper(df[CMH])
    log.info("CMH for %s new rows: %s by Base SKU, %s by (F2,F3), %s unmapped",
             f"{n_new:,}", f"{by_sku:,}", f"{n_new - by_sku - int(still.sum()):,}", f"{int(still.sum()):,}")
    if still.any():
        skus = df.loc[still, "Base SKU"].astype(str).value_counts().head(10).to_dict() \
            if "Base SKU" in df.columns else {}
        log.warning("%s new rows have no Commercial Model Helper. Top Base SKUs: %s", int(still.sum()), skus)
    return df
