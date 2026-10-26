"""The monthly merge, as small named stages that each log what they did."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .cleaning import add_san_key, clean_cm, clean_pm
from .cmh import assign_cmh_for_new_rows, blank_helper
from .columns import (coerce_numeric_columns, detect_customer_number_column, detect_datacenter_column,
                      detect_service_asset_column, ensure_object, find_column, normalize_key_series, to_number)
from .commercial_model import assign_commercial_model_for_new_rows
from .config import ALIASES, CM_RENAMES, TARGET_FIELDS, AppConfig
from .kw_rules import apply_adjusted_kw
from .layout import Layout, resolve_layout
from .logging_setup import get_logger

log = get_logger("pipeline")

INTERNAL_COLS = ["_SOURCE_", "_AMT_NUM_", "_CUSTOMER_KEY_", "_DEPLOY_KEY_", "_PERKW_QTY_", "_PKW_KW_", "__DC__"]
PM_COMPARE_COLS = ["PM MRR", "PM MRR Var", "PM Adjusted kW", "PM Adjusted kW Var", "COLA/Price Increase Check"]


@dataclass
class PipelineResult:
    data: pd.DataFrame
    layout: Layout
    pm: pd.DataFrame
    cm: pd.DataFrame
    churned: pd.DataFrame
    added: pd.DataFrame
    dup_cm: pd.DataFrame
    dup_pm: pd.DataFrame
    null_san: pd.DataFrame
    kw_diffs: pd.DataFrame
    stats: dict = field(default_factory=dict)


# ------------------------------------------------------------------ stages
def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if "Commerical Model Helper" in df.columns and "Commercial Model Helper" not in df.columns:
        df = df.rename(columns={"Commerical Model Helper": "Commercial Model Helper"})
    return df


def align_cm_columns(cm: pd.DataFrame, pm: pd.DataFrame) -> pd.DataFrame:
    rename = {s: d for s, d in CM_RENAMES.items() if s in cm.columns and d in pm.columns and d not in cm.columns}
    return cm.rename(columns=rename) if rename else cm


def build_merge_base(pm: pd.DataFrame, cm: pd.DataFrame):
    """Keep PM rows whose SAN is in CM; add CM rows for SANs missing from PM."""
    cm_sans = set(cm["_SAN_"])
    kept = pm[pm["_SAN_"].isin(cm_sans)].copy()
    kept["_SOURCE_"] = "f1"
    churned = pm[~pm["_SAN_"].isin(cm_sans)].copy()
    missing = cm_sans - set(kept["_SAN_"])
    added_raw = cm[cm["_SAN_"].isin(missing)].copy()
    added = added_raw.reindex(columns=list(pm.columns))
    added["_SOURCE_"] = "f2_add"
    merged = pd.concat([kept, added], ignore_index=True)
    dropped_cols = [c for c in cm.columns if c not in pm.columns]
    return merged, churned, added_raw, dropped_cols


def overlay_cm_targets(merged: pd.DataFrame, pm: pd.DataFrame, cm: pd.DataFrame, stats: dict):
    """Overwrite the five financial fields from CM. Missing CM columns are reported, never guessed."""
    merged = merged.copy()
    cm_idx = cm.drop_duplicates("_SAN_").set_index("_SAN_")
    is_f1 = merged["_SOURCE_"].eq("f1")
    is_add = merged["_SOURCE_"].eq("f2_add")
    targets, missing = {}, []
    for t in TARGET_FIELDS:
        c2 = find_column(cm, t, ALIASES.get(t, []))
        c1 = find_column(pm, t, ALIASES.get(t, [])) or t
        targets[t] = c1
        if c2 is None:
            missing.append(t)
            log.warning("CM has no column for %r; PM values for it are left unchanged", t)
            continue
        ensure_object(merged, c1)
        new = merged["_SAN_"].map(cm_idx[c2])
        take1 = is_f1 & new.notna()
        merged.loc[take1, c1] = new[take1]
        take2 = is_add & merged[c1].isna() & new.notna()
        merged.loc[take2, c1] = new[take2]
        stale = int((is_f1 & new.isna() & merged[c1].notna()).sum())
        if stale:
            log.warning("%r: CM is blank for %s PM rows; the prior-month value was kept", t, stale)
        log.info("Overlay %r from CM column %r: %s PM rows updated, %s new rows filled",
                 t, c2, f"{int(take1.sum()):,}", f"{int(take2.sum()):,}")
    stats["missing_cm_targets"] = missing
    return merged, targets


def add_pm_comparison_fields(merged: pd.DataFrame, pm: pd.DataFrame) -> pd.DataFrame:
    for need in ("Gross Power MRR", "Adjusted kW"):
        if need not in pm.columns:
            raise KeyError(f"PM workbook has no {need!r} column, cannot build the PM comparison")
    merged = merged.drop(columns=[c for c in PM_COMPARE_COLS if c in merged.columns]).copy()
    lookup = pm.drop_duplicates("_SAN_", keep="last").set_index("_SAN_")
    merged["PM MRR"] = merged["_SAN_"].map(to_number(lookup["Gross Power MRR"])).astype(float).fillna(0.0)
    merged["PM Adjusted kW"] = merged["_SAN_"].map(to_number(lookup["Adjusted kW"])).astype(float).fillna(0.0)
    merged["PM MRR Var"] = np.nan
    merged["PM Adjusted kW Var"] = np.nan
    merged["COLA/Price Increase Check"] = np.nan
    return merged


def add_deployment_validation(merged: pd.DataFrame, min_ratio: float):
    """Per customer + data center: SUM(Per kW Bill Qty) / SUM(PKW Bill Qty x Circuit kW)."""
    merged = merged.copy()
    cust_col = detect_customer_number_column(merged)
    dc_col = detect_datacenter_column(merged)
    if dc_col is None:
        merged["__DC__"] = ""
        dc_col = "__DC__"
    bill_col = find_column(merged, "Bill Qty", ALIASES["Bill Qty"]) or "Bill Qty"
    bill = to_number(merged[bill_col]) if bill_col in merged.columns else pd.Series(np.nan, index=merged.index)
    ckw = to_number(merged["Circuit kW"]) if "Circuit kW" in merged.columns else pd.Series(np.nan, index=merged.index)
    helper = merged["Commercial Model Helper"].astype(str).str.strip().str.lower() \
        if "Commercial Model Helper" in merged.columns else pd.Series("", index=merged.index)
    is_perkw = helper.eq("per kwh") | helper.eq("per kw") | helper.str.contains(r"\bper\s*k\s*w\b", regex=True, na=False)
    is_pkw = helper.eq("pkw")

    key = normalize_key_series(merged[cust_col]).fillna("") + "||" + merged[dc_col].astype(str).str.strip()
    perkw_qty = pd.Series(np.where(is_perkw, bill.fillna(0.0), 0.0), index=merged.index)
    pkw_kw = pd.Series(np.where(is_pkw, (bill * ckw).fillna(0.0), 0.0), index=merged.index)
    merged["Deployment Per kW Qty Total"] = key.map(perkw_qty.groupby(key).sum())
    merged["Deployment PKW kW Total"] = key.map(pkw_kw.groupby(key).sum())
    ratio = (merged["Deployment Per kW Qty Total"] / merged["Deployment PKW kW Total"]
             .where(merged["Deployment PKW kW Total"] > 0))
    merged["Per kW vs PKW %"] = ratio
    # A deployment with no PKW is N/A (blank), not a failure.
    merged["Per kW >60%±10% OK"] = (ratio >= min_ratio).astype("boolean").where(ratio.notna(), pd.NA)
    return merged, cust_col


def populate_helper_columns(merged: pd.DataFrame, cust_col: str) -> pd.DataFrame:
    merged = merged.copy()
    sort_col = "Assest Sort" if "Assest Sort" in merged.columns or "Asset Sort" not in merged.columns else "Asset Sort"
    name = merged["Customer Name"].fillna("").astype(str) if "Customer Name" in merged.columns \
        else pd.Series("", index=merged.index)
    dc = merged["Data Center"].fillna("").astype(str) if "Data Center" in merged.columns \
        else pd.Series("", index=merged.index)
    merged[sort_col] = name.str.strip() + dc.str.strip()

    ckey = normalize_key_series(merged[cust_col]).fillna("__MISSING_CUSTOMER__")
    m_col = find_column(merged, "Monthly Amount", ALIASES["Monthly Amount"]) or "Monthly Amount"
    amt = to_number(merged[m_col]).fillna(0.0)
    sums = amt.groupby(ckey).sum()
    merged["Total Customer MRR"] = ckey.map(sums)
    merged["MRR Rank"] = ckey.map(sums.rank(method="dense", ascending=False).astype(int))
    if "Service Asset Number" in merged.columns:
        merged["SA Lookup"] = merged["Service Asset Number"]
    for c in ("Row #",):
        if c not in merged.columns:
            merged[c] = np.nan
    return merged


def add_cabinet_columns(merged: pd.DataFrame, cust_col: str) -> pd.DataFrame:
    merged = merged.copy()
    sku = merged["Base SKU"].fillna("").astype(str) if "Base SKU" in merged.columns else pd.Series("", index=merged.index)
    bill = to_number(merged["Bill Qty"]).fillna(0.0) if "Bill Qty" in merged.columns else pd.Series(0.0, index=merged.index)
    merged["Cabinet Count"] = np.where(sku.str.contains("CAB", case=False, na=False), bill, 0.0)
    ckey = normalize_key_series(merged[cust_col]).fillna("__MISSING__")
    has = ckey.map(merged["Cabinet Count"].groupby(ckey).sum()) > 0
    merged["Customer Has Cabinet"] = np.where(has, "Yes", "No")
    return merged


def order_by_customer_mrr(merged: pd.DataFrame, cust_col: str) -> pd.DataFrame:
    """Customers by total Monthly Amount descending; ties broken by customer key so runs are repeatable."""
    m_col = find_column(merged, "Monthly Amount", ALIASES["Monthly Amount"]) or "Monthly Amount"
    amt = to_number(merged[m_col]).fillna(0.0)
    ckey = normalize_key_series(merged[cust_col]).fillna("__MISSING_CUSTOMER__")
    sums = amt.groupby(ckey).sum().rename("s").rename_axis("k").reset_index()
    order = sums.sort_values(["s", "k"], ascending=[False, True], kind="mergesort")["k"].tolist()
    rank = {k: i for i, k in enumerate(order)}
    out = merged.assign(_ord=ckey.map(rank)).sort_values("_ord", kind="mergesort").drop(columns="_ord")
    return out.reset_index(drop=True)


def place_pm_comparison_columns(df: pd.DataFrame) -> pd.DataFrame:
    present = [c for c in PM_COMPARE_COLS if c in df.columns]
    rest = [c for c in df.columns if c not in present]
    at = rest.index("Cabinet Count") if "Cabinet Count" in rest else len(rest)
    return df[rest[:at] + present + rest[at:]]


def apply_derived(df: pd.DataFrame, layout: Layout) -> pd.DataFrame:
    """Compute variance/ratio columns in pandas so the data has real values (not only Excel formulas)."""
    df = df.copy()
    for d in layout.derived:
        a = to_number(df[d.a]).fillna(0.0)
        b = to_number(df[d.b]).fillna(0.0)
        df[d.out] = (a / b.where(b != 0)).fillna(0.0) if d.kind == "ratio" else a - b
    return df


# ---------------------------------------------------------------- driver
def run_pipeline(pm_raw: pd.DataFrame, cm_raw: pd.DataFrame, cfg: AppConfig, month: date,
                 model_ref_loader: Callable[[], pd.DataFrame]) -> PipelineResult:
    stats: dict = {"month": str(month)}
    pm = standardize_columns(clean_pm(pm_raw))
    cm = standardize_columns(clean_cm(cm_raw))
    stats.update(pm_rows_loaded=len(pm_raw), pm_rows=len(pm), cm_rows_loaded=len(cm_raw), cm_rows=len(cm))
    cm = align_cm_columns(cm, pm)

    pm_san = cfg.rules.san_column if cfg.rules.san_column in pm.columns else detect_service_asset_column(pm)
    cm_san = cfg.rules.san_column if cfg.rules.san_column in cm.columns else detect_service_asset_column(cm)
    pm = add_san_key(pm, pm_san, cfg.rules.san_key_index)
    cm = add_san_key(cm, cm_san, -1)

    null_pm, null_cm = pm[pm["_SAN_"].isna()], cm[cm["_SAN_"].isna()]
    null_san = pd.concat([null_pm.assign(_side_="PM"), null_cm.assign(_side_="CM")], ignore_index=True)
    if len(null_san):
        log.warning("Rows without a SAN are excluded: PM %s, CM %s", len(null_pm), len(null_cm))
    pm, cm = pm[pm["_SAN_"].notna()].reset_index(drop=True), cm[cm["_SAN_"].notna()].reset_index(drop=True)

    dup_cm = cm[cm["_SAN_"].duplicated(keep=False)]
    dup_pm = pm[pm["_SAN_"].duplicated(keep=False)]
    if len(dup_cm):
        log.warning("CM has %s rows sharing %s SANs; the last row per SAN is used",
                    len(dup_cm), dup_cm["_SAN_"].nunique())
        cm = cm.drop_duplicates("_SAN_", keep="last").reset_index(drop=True)
    if len(dup_pm):
        log.warning("PM has %s rows sharing %s SANs (kept as-is)", len(dup_pm), dup_pm["_SAN_"].nunique())
    stats.update(null_san_rows=len(null_san), dup_cm_rows=len(dup_cm), dup_cm_sans=int(dup_cm["_SAN_"].nunique()),
                 dup_pm_rows=len(dup_pm), dup_pm_sans=int(dup_pm["_SAN_"].nunique()))

    merged, churned, added, dropped_cols = build_merge_base(pm, cm)
    stats.update(kept_from_pm=int(merged["_SOURCE_"].eq("f1").sum()), added_from_cm=len(added),
                 churned=len(churned), cm_columns_not_carried=dropped_cols)
    log.info("Merge: kept %s PM rows, added %s CM-only SANs, dropped %s PM SANs absent from CM",
             f"{stats['kept_from_pm']:,}", f"{len(added):,}", f"{len(churned):,}")
    if dropped_cols:
        log.info("CM columns not in the PM layout (not carried into the output): %s", dropped_cols)

    merged, targets = overlay_cm_targets(merged, pm, cm, stats)
    numeric = [targets.get(t, t) for t in TARGET_FIELDS] + ["Circuit kW", "Final kW for $/kW", "Adjusted kW"]
    merged = coerce_numeric_columns(merged, numeric)

    new_rows = merged["_SOURCE_"].eq("f2_add")
    cmh_now = merged["Commercial Model Helper"] if "Commercial Model Helper" in merged.columns \
        else pd.Series(np.nan, index=merged.index)
    blank_new = new_rows & blank_helper(cmh_now)
    ref = None
    if blank_new.any():                       # the reference is only needed for rows that lack a helper
        try:
            ref = model_ref_loader()
        except FileNotFoundError as exc:
            if cfg.rules.model_ref_required:
                raise
            log.warning("%s (continuing: rules.model_ref_required = false)", exc)
    merged = assign_cmh_for_new_rows(merged, ref, require_ref=cfg.rules.model_ref_required)
    cmh_after = merged["Commercial Model Helper"] if "Commercial Model Helper" in merged.columns \
        else pd.Series(np.nan, index=merged.index)
    stats["new_rows_without_cmh"] = int((new_rows & blank_helper(cmh_after)).sum())

    gpm_col = targets.get("Gross Power MRR") or "Gross Power MRR"
    ensure_object(merged, gpm_col)
    merged["Adjusted Power MRR"] = merged[gpm_col]
    merged, kw_diffs = apply_adjusted_kw(merged, cfg.rules)
    stats["kw_source_counts"] = merged["Adjusted kW Source"].value_counts().to_dict()
    stats["kw_prior_diffs"] = len(kw_diffs)

    merged = assign_commercial_model_for_new_rows(merged)
    merged = add_pm_comparison_fields(merged, pm)
    merged, cust_col = add_deployment_validation(merged, cfg.rules.deploy_ratio_min)
    merged = populate_helper_columns(merged, cust_col)
    merged = add_cabinet_columns(merged, cust_col)
    merged = order_by_customer_mrr(merged, cust_col)
    merged["Row #"] = np.arange(1, len(merged) + 1)
    merged = merged.drop(columns=[c for c in INTERNAL_COLS if c in merged.columns])
    merged = place_pm_comparison_columns(merged)
    merged.insert(0, "Reporting Month", month)

    layout = resolve_layout(list(merged.columns), cfg.excel, cust_col)
    data = apply_derived(merged, layout)

    mrr = to_number(data[gpm_col]).sum()
    kw = to_number(data["Adjusted kW"]).sum()
    stats.update(out_rows=len(data), total_mrr=float(mrr), total_kw=float(kw),
                 customers=int(normalize_key_series(data[cust_col]).nunique()), gpm_col=gpm_col)
    log.info("Pipeline done: %s rows, %s customers, total Gross Power MRR %s, total Adjusted kW %s",
             f"{len(data):,}", f"{stats['customers']:,}", f"{mrr:,.2f}", f"{kw:,.2f}")
    return PipelineResult(data=data, layout=layout, pm=pm, cm=cm, churned=churned, added=added,
                          dup_cm=dup_cm, dup_pm=dup_pm, null_san=null_san, kw_diffs=kw_diffs, stats=stats)
