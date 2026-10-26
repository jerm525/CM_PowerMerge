"""Adjusted kW: one function, one precedence order, no duplicated rules."""
from __future__ import annotations

import re
from typing import Optional

import numpy as np
import pandas as pd

from .columns import to_number
from .config import RulesConfig
from .logging_setup import get_logger

log = get_logger("kw_rules")


def _extract_first_match(series: pd.Series, patterns: list) -> pd.Series:
    """First numeric regex match per row, trying patterns in order."""
    result = pd.Series(np.nan, index=series.index, dtype=float)
    for pat in patterns or []:
        todo = result.isna()
        if not todo.any():
            break
        extracted = series[todo].str.extract(pat, flags=re.IGNORECASE, expand=True)
        first = extracted.iloc[:, 0]                      # group 1 is always the number
        result.loc[todo] = pd.to_numeric(first, errors="coerce").to_numpy()
    return result


def compute_adjusted_kw(df: pd.DataFrame, rules: RulesConfig) -> tuple[pd.Series, pd.Series]:
    """Return (Adjusted kW, source label) for every row.

    Precedence, highest first (first match wins):
      1. Gross Power MRR == 0        -> 0
      2. Base SKU == one-U SKU       -> one_u_kw
      3. exempt helper               -> 0
      4. by model: circuit / metered+infrastructure+per-kW / custom power
    """
    def col(name):
        return df[name] if name in df.columns else pd.Series(np.nan, index=df.index)

    exempt = {h.lower() for h in rules.exempt_helpers}
    model = col("Commercial Model Helper").astype(str).str.strip().str.lower()
    gross = to_number(col("Gross Power MRR"))
    bill = to_number(col("Bill Qty"))
    final = to_number(col("Final kW for $/kW"))
    ckw = to_number(col("Circuit kW"))
    base = col("Base SKU").astype(str).str.strip().str.upper()

    revenue_ok = gross.isna() | (gross > 0)
    gpm_zero = gross.notna() & (gross == 0.0)
    is_1u = base == rules.one_u_sku.upper()
    is_exempt = model.isin(exempt)
    is_circuit = model.str.contains("circuit", na=False) & revenue_ok
    is_metered = model.isin(["metered", "infrastructure"]) & revenue_ok
    is_perkw = model.str.contains(r"per.*kw|kw.*per", na=False, regex=True) & revenue_ok
    is_custom = model.str.contains("custom", na=False) & ~gpm_zero & ~is_1u & ~is_exempt

    val = pd.Series(np.nan, index=df.index, dtype=float)

    circuit_val = np.where(final.notna(), final, np.where(ckw.notna() & bill.notna(), ckw * bill, np.nan))
    val = val.where(~is_circuit, circuit_val)
    val = val.where(~(is_metered | is_perkw), bill)

    if is_custom.any():
        text = (col("Base SKU").fillna("").astype(str) + " | " + col("Item Full Name").fillna("").astype(str)
                + " | " + col("Asset Notes").fillna("").astype(str) + " | "
                + col("Sales Description").fillna("").astype(str))
        amps = _extract_first_match(text, rules.amp_patterns)
        volts = _extract_first_match(text, rules.volt_patterns)
        three_phase = text.str.contains(r"\b3\s*phase\b|\b3p\b|\bthree\s*phase\b", flags=re.IGNORECASE,
                                        na=False, regex=True)
        dc_48v = text.str.lower().str.contains("48v", na=False)
        phase = np.where(three_phase & ~dc_48v, 3.0, 1.0)
        rating_kw = (amps * volts * np.sqrt(phase)) / 1000.0
        qty = bill.where(bill.notna(), 1.0)
        custom_val = np.where(amps.notna() & volts.notna() & (rules.commit_divisor != 0),
                              (rating_kw / rules.commit_divisor) * qty, np.nan)
        val = val.where(~is_custom, custom_val)

    source = pd.Series("missing", index=df.index, dtype=object)
    source = source.mask(val.notna(), "calculated")
    # Apply overrides lowest priority first so the highest-priority rule is written last.
    for mask, v, label in ((is_exempt, 0.0, "exempt_default_zero"),
                           (is_1u, rules.one_u_kw, "override_1u_trs"),
                           (gpm_zero, 0.0, "override_gpm0")):
        val = val.mask(mask, v)
        source = source.mask(mask, label)
    return val.rename("Adjusted kW"), source.rename("Adjusted kW Source")


def apply_adjusted_kw(merged: pd.DataFrame, rules: RulesConfig):
    """Write Adjusted kW and Adjusted kW Source onto merged.

    Returns (merged, diffs) where diffs lists prior-month rows whose recalculated
    kW differs from the value carried in the PM workbook (informational under the
    'recalculate' policy, the entire point of the review under 'preserve_existing').
    """
    merged = merged.copy()
    existing = to_number(merged["Adjusted kW"]) if "Adjusted kW" in merged.columns \
        else pd.Series(np.nan, index=merged.index)
    calc, source = compute_adjusted_kw(merged, rules)

    from_pm = merged["_SOURCE_"].eq("f1") if "_SOURCE_" in merged.columns else existing.notna()
    both = from_pm & existing.notna() & calc.notna()
    changed = both & ((existing - calc).abs() > 1e-6)
    changed |= from_pm & existing.notna() & calc.isna()

    if rules.kw_policy == "preserve_existing":
        final = existing.where(existing.notna(), calc)
        source = source.mask(existing.notna(), "existing")
    else:
        final = calc

    merged["Adjusted kW"] = final
    merged["Adjusted kW Source"] = source
    keep = [c for c in ("_SAN_", "Customer Number", "Customer Name", "Base SKU", "Commercial Model Helper",
                        "Gross Power MRR", "Bill Qty") if c in merged.columns]
    diffs = merged.loc[changed, keep].copy()
    diffs["Prior Adjusted kW"] = existing[changed]
    diffs["Calculated Adjusted kW"] = calc[changed]
    log.info("Adjusted kW (%s): %s rows; source counts: %s", rules.kw_policy, f"{len(merged):,}",
             source.value_counts().to_dict())
    log.info("Adjusted kW: %s prior-month rows differ from the recalculated value",
             f"{int(changed.sum()):,}")
    return merged, diffs
