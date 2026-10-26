"""Validation gate: every check returns PASS / WARN / FAIL and is logged.

Severity can be overridden per check in config ([validation.severity]):
  name = "WARN" | "FAIL" | "OFF"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .columns import normalize_key_series, to_number
from .config import AppConfig
from .logging_setup import get_logger
from .pipeline import PipelineResult

log = get_logger("validation")


@dataclass
class Check:
    name: str
    status: str          # PASS | WARN | FAIL
    detail: str


def overall_status(checks: list) -> str:
    if any(c.status == "FAIL" for c in checks):
        return "FAIL"
    if any(c.status == "WARN" for c in checks):
        return "WARN"
    return "PASS"


def _pct(new: float, old: float) -> Optional[float]:
    return None if not old else (new - old) / abs(old)


def _sample(values, n=8) -> str:
    vals = [str(v) for v in list(values)[:n]]
    extra = len(list(values)) - len(vals)
    return ", ".join(vals) + (f" (+{extra} more)" if extra > 0 else "")


def readiness_checks(cm_rows: int, pm_rows: int, cfg: AppConfig) -> list:
    """Run before the merge: is the CM extract plausibly complete?"""
    v = cfg.validation
    checks = []
    if cm_rows < v.min_cm_rows:
        checks.append(Check("cm_ready_rows", "FAIL", f"CM extract has {cm_rows} rows (minimum {v.min_cm_rows})"))
        return checks
    checks.append(Check("cm_ready_rows", "PASS", f"CM extract has {cm_rows:,} rows"))
    change = _pct(cm_rows, pm_rows)
    if change is not None and abs(change) > v.row_count_pct:
        checks.append(Check("cm_ready_row_count", "FAIL",
                            f"CM rows {cm_rows:,} vs prior month {pm_rows:,} ({change:+.1%}); "
                            f"limit ±{v.row_count_pct:.0%}. The source data may not be fully loaded."))
    else:
        checks.append(Check("cm_ready_row_count", "PASS", f"CM rows {cm_rows:,} vs prior {pm_rows:,}"))
    return checks


def run_validations(res: PipelineResult, cfg: AppConfig) -> list:
    v, r = cfg.validation, cfg.rules
    d, st = res.data, res.stats
    gpm = st["gpm_col"]
    checks: list = []

    def add(name, ok, detail_ok, detail_bad, bad="FAIL"):
        checks.append(Check(name, "PASS" if ok else bad, detail_ok if ok else detail_bad))

    # ---- inputs
    missing = [t for t in st.get("missing_cm_targets", []) if t in v.required_cm_columns]
    add("required_cm_columns", not missing, "all required CM columns present",
        f"CM extract lacks required column(s): {missing}")

    add("null_san", st["null_san_rows"] == 0, "no rows without a SAN",
        f"{st['null_san_rows']} rows without a SAN were excluded", "WARN")
    dup_bad = "FAIL" if r.duplicate_san_policy == "fail" else "WARN"
    add("cm_san_unique", st["dup_cm_rows"] == 0, "CM SANs are unique",
        f"{st['dup_cm_sans']} SANs repeat in CM ({st['dup_cm_rows']} rows); last row used", dup_bad)
    add("pm_san_unique", st["dup_pm_rows"] == 0, "PM SANs are unique",
        f"{st['dup_pm_sans']} SANs repeat in the PM workbook ({st['dup_pm_rows']} rows)", dup_bad)

    # ---- completeness / reconciliation
    cm_sans, out_sans = set(res.cm["_SAN_"].dropna()), set(d["_SAN_"].dropna())
    add("completeness", cm_sans == out_sans, f"output has all {len(cm_sans):,} CM SANs",
        f"CM-only {_sample(sorted(cm_sans - out_sans))}; output-only {_sample(sorted(out_sans - cm_sans))}")
    dups_out = d["_SAN_"].duplicated().sum()
    add("output_san_unique", dups_out == 0, "output SANs are unique", f"{dups_out} duplicate SANs in output",
        dup_bad)

    m_col = next((c for c in ("Monthly Amount",) if c in d.columns and c in res.cm.columns), None)
    if m_col:
        cm_total = to_number(res.cm[m_col]).sum()
        out_total = to_number(d[m_col]).sum()
        ok = abs(cm_total - out_total) <= v.recon_tolerance
        add("mrr_reconciles_to_cm", ok, f"Monthly Amount ties to CM ({out_total:,.2f})",
            f"Monthly Amount output {out_total:,.2f} vs CM {cm_total:,.2f} (diff {out_total - cm_total:,.2f})")

    # ---- classification
    helper = d["Commercial Model Helper"].astype(str).str.strip() if "Commercial Model Helper" in d.columns \
        else pd.Series("", index=d.index)
    null_helper = d["Commercial Model Helper"].isna() | helper.eq("") if "Commercial Model Helper" in d.columns \
        else pd.Series(True, index=d.index)
    add("new_rows_have_helper", st["new_rows_without_cmh"] == 0, "every new asset has a Commercial Model Helper",
        f"{st['new_rows_without_cmh']} new assets have no Commercial Model Helper")
    add("helper_not_null", int(null_helper.sum()) == 0, "no blank Commercial Model Helper",
        f"{int(null_helper.sum())} rows have a blank Commercial Model Helper", "WARN")
    if "Commercial Model" in d.columns:
        allowed = set(r.commercial_model_allowed)
        model = d["Commercial Model"].astype(str).str.strip()
        bad = d.loc[d["Commercial Model"].isna() | ~model.isin(allowed), "_SAN_"]
        add("commercial_model_values", bad.empty, f"Commercial Model is always one of {sorted(allowed)}",
            f"{len(bad)} rows have a blank/unexpected Commercial Model, e.g. {_sample(bad)}")

    # ---- kW rules
    kw = to_number(d["Adjusted kW"])
    mrr = to_number(d[gpm])
    neg = d.loc[kw < 0, "_SAN_"]
    add("kw_nonnegative", neg.empty, "Adjusted kW is never negative", f"{len(neg)} negative Adjusted kW, e.g. {_sample(neg)}")
    exempt = {h.lower() for h in r.exempt_helpers}
    is_exempt = helper.str.lower().isin(exempt)
    missing_kw = d.loc[kw.isna() & (mrr > 0) & ~is_exempt, "_SAN_"]
    by_helper = helper[kw.isna() & (mrr > 0) & ~is_exempt].value_counts().head(5).to_dict()
    add("kw_populated_for_revenue_rows", missing_kw.empty, "every revenue row has an Adjusted kW",
        f"{len(missing_kw)} revenue rows have no Adjusted kW (helpers: {by_helper}), e.g. {_sample(missing_kw)}", "WARN")

    source = (
        d["Adjusted kW Source"].astype(str)
        if "Adjusted kW Source" in d.columns
        else pd.Series("", index=d.index)
    )

    validate_kw = source != "existing"

    zero_bad = d.loc[
        validate_kw &
        (mrr == 0) &
        (kw.fillna(0) != 0),
        "_SAN_"
    ]
    
    add("kw_zero_when_mrr_zero", zero_bad.empty, "Adjusted kW is 0 wherever MRR is 0",
        f"{len(zero_bad)} rows have $0 MRR but non-zero kW, e.g. {_sample(zero_bad)}")
    if "Base SKU" in d.columns:
        one_u = (
                d["Base SKU"]
                .astype(str)
                .str.strip()
                .str.upper()
                .eq(r.one_u_sku.upper())
                & (mrr != 0)
        )
        one_u_bad = d.loc[
            validate_kw &
            one_u &
            ((kw - r.one_u_kw).abs() > 1e-9),
            "_SAN_"
        ]
        add("one_u_kw", one_u_bad.empty, f"{r.one_u_sku} rows are {r.one_u_kw} kW",
            f"{len(one_u_bad)} {r.one_u_sku} rows are not {r.one_u_kw} kW")

    # ---- deployment ratio (informational unless the ratio is below the limit)
    flag_col = "Per kW >60%±10% OK"
    if flag_col in d.columns:
        low = d[d[flag_col].eq(False)]
        deployments = low[[c for c in ("Customer Number", "Data Center") if c in low.columns]].drop_duplicates()
        add("deployment_per_kw_ratio", deployments.empty, "all deployments with PKW meet the per-kW ratio",
            f"{len(deployments)} deployments below {r.deploy_ratio_min:.0%}: "
            f"{_sample([tuple(x) for x in deployments.itertuples(index=False)])}", "WARN")

    # ---- month over month
    pm_mrr, pm_kw = to_number(res.pm[gpm]).sum() if gpm in res.pm.columns else 0.0, \
        to_number(res.pm["Adjusted kW"]).sum() if "Adjusted kW" in res.pm.columns else 0.0
    for name, new, old, limit in (("mom_total_mrr", mrr.sum(), pm_mrr, v.total_mrr_pct),
                                  ("mom_total_kw", kw.sum(), pm_kw, v.total_kw_pct)):
        change = _pct(new, old)
        ok = change is None or abs(change) <= limit
        add(name, ok, f"{new:,.2f} vs prior {old:,.2f}" + ("" if change is None else f" ({change:+.1%})"),
            f"{new:,.2f} vs prior {old:,.2f} ({change:+.1%}), limit ±{limit:.0%}", "WARN")
    row_change = _pct(len(d), len(res.pm))
    add("mom_row_count", row_change is None or abs(row_change) <= v.row_count_pct,
        f"{len(d):,} rows vs prior {len(res.pm):,}", f"{len(d):,} rows vs prior {len(res.pm):,} ({row_change:+.1%})", "WARN")

    cust = normalize_key_series(d[res.layout.cust_col]).fillna("__MISSING__")
    now = mrr.groupby(cust).sum()
    before = to_number(d["PM MRR"]).groupby(cust).sum()
    delta = (now - before)
    pct = delta.abs() / before.abs().replace(0, np.nan)
    swing = delta[(delta.abs() >= v.customer_swing_min_abs) & ((pct >= v.customer_swing_pct) | pct.isna())]
    top = swing.reindex(swing.abs().sort_values(ascending=False).index)
    add("customer_mrr_swings", top.empty, "no customer swings above threshold",
        f"{len(top)} customers moved ≥{v.customer_swing_min_abs:,.0f} and ≥{v.customer_swing_pct:.0%}: "
        + _sample([f"{k} ({x:+,.0f})" for k, x in top.items()]), "WARN")

    churn_pct = len(res.churned) / len(res.pm) if len(res.pm) else 0.0
    add("churned_sans", churn_pct <= v.churn_pct,
        f"{len(res.churned):,} prior-month SANs absent from CM ({churn_pct:.1%})",
        f"{len(res.churned):,} prior-month SANs absent from CM ({churn_pct:.1%}), limit {v.churn_pct:.0%}", "WARN")

    # ---- apply per-check severity overrides
    final = []
    for c in checks:
        override = str(v.severity.get(c.name, "")).upper()
        if override == "OFF":
            continue
        if override in ("WARN", "FAIL") and c.status != "PASS":
            c = Check(c.name, override, c.detail)
        final.append(c)
    for c in final:
        level = {"PASS": log.info, "WARN": log.warning, "FAIL": log.error}[c.status]
        level("check %-32s %-4s %s", c.name, c.status, c.detail)
    log.info("Validation result: %s (%s checks)", overall_status(final), len(final))
    return final
