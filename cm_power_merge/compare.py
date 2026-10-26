"""Row/column level comparison between two runs (golden-file and shadow-mode checks)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .cleaning import drop_unnamed, strip_output_rows
from .columns import detect_service_asset_column, normalize_key_series, to_number
from .logging_setup import get_logger
from .sources import load_excel

log = get_logger("compare")


@dataclass
class CompareResult:
    summary: dict
    diffs: pd.DataFrame
    only_new: list = field(default_factory=list)
    only_ref: list = field(default_factory=list)


def load_output_rows(path: Path) -> pd.DataFrame:
    """Read a merged workbook and keep data rows only (no subtotals/totals/blank spacers)."""
    df = strip_output_rows(drop_unnamed(load_excel(path)))
    if "_SAN_" not in df.columns:
        df["_SAN_"] = normalize_key_series(df[detect_service_asset_column(df)])
    return df


def _is_numeric(a: pd.Series, b: pd.Series) -> bool:
    for s in (a, b):
        nn = s.notna().sum()
        if nn == 0 or to_number(s).notna().sum() < 0.9 * nn:
            return False
    return True


def compare_frames(new: pd.DataFrame, ref: pd.DataFrame, key: str = "_SAN_", tol: float = 0.005,
                   ignore=("Row #", "Reporting Month", "MRR Rank")) -> CompareResult:
    def prep(df):
        df = df.copy()
        df[key] = normalize_key_series(df[key])
        return df[df[key].notna()].drop_duplicates(key, keep="last").set_index(key)

    n, r = prep(new), prep(ref)
    only_new = sorted(n.index.difference(r.index))
    only_ref = sorted(r.index.difference(n.index))
    common = n.index.intersection(r.index)
    cols = [c for c in n.columns if c in r.columns and c not in ignore]
    parts, per_col = [], {}
    for c in cols:
        a, b = n.loc[common, c], r.loc[common, c]
        if _is_numeric(a, b):
            an, bn = to_number(a), to_number(b)
            neq = ~(((an - bn).abs() <= tol) | (an.isna() & bn.isna()))
        else:
            sa = a.astype(object).where(a.notna(), "").astype(str).str.strip()
            sb = b.astype(object).where(b.notna(), "").astype(str).str.strip()
            neq = sa != sb
        if neq.any():
            per_col[c] = int(neq.sum())
            parts.append(pd.DataFrame({key: common[neq], "column": c, "new": a[neq].to_numpy(),
                                       "reference": b[neq].to_numpy()}))
    diffs = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=[key, "column", "new", "reference"])
    summary = {
        "rows_new": len(n), "rows_reference": len(r), "rows_only_in_new": len(only_new),
        "rows_only_in_reference": len(only_ref), "columns_compared": len(cols),
        "columns_only_in_new": [c for c in n.columns if c not in r.columns],
        "columns_only_in_reference": [c for c in r.columns if c not in n.columns],
        "columns_with_differences": dict(sorted(per_col.items(), key=lambda kv: -kv[1])),
        "cells_different": int(sum(per_col.values())),
    }
    log.info("Compare: %s rows vs %s, %s only in new, %s only in reference, %s differing cells in %s columns",
             f"{len(n):,}", f"{len(r):,}", len(only_new), len(only_ref), summary["cells_different"], len(per_col))
    return CompareResult(summary, diffs, only_new, only_ref)
