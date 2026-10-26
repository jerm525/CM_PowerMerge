"""Excel report built from the clean DataFrame.

Formulas are generated from header names (never hard-coded letters) and every
formula is written with its computed value cached, so pandas/SQL/preview tools
read real numbers rather than the 0 xlsxwriter stores by default.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xlsxwriter

from .columns import col_letter, normalize_key_series, swap_header_names, to_number
from .config import AppConfig
from .logging_setup import get_logger
from .pipeline import PipelineResult

log = get_logger("report")


class _Styles:
    """Cached cell formats: header / subtotal / total rows get colour, any kind can carry a number format."""

    def __init__(self, wb, ex):
        self.wb, self.ex, self._cache = wb, ex, {}

    def get(self, kind, num_format=None):
        key = (kind, num_format)
        if key not in self._cache:
            props = {}
            if kind == "header":
                props = {"bold": True, "font_color": self.ex.header_font, "bg_color": self.ex.header_bg,
                         "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True}
            elif kind == "subtotal":
                props = {"bold": True, "bg_color": self.ex.subtotal_bg, "top": 1}
            elif kind == "total":
                props = {"bold": True, "bg_color": self.ex.grand_total_bg, "top": 1, "bottom": 6}
            if num_format:
                props["num_format"] = num_format
            self._cache[key] = self.wb.add_format(props) if props else None
        return self._cache[key]


def _write_cell(ws, r, c, v, datefmt, fmt=None):
    if v is None or v is pd.NA or v is pd.NaT:
        return
    if isinstance(v, (np.bool_, bool)):
        ws.write_boolean(r, c, bool(v), fmt)
        return
    if isinstance(v, (np.integer, int)):
        ws.write_number(r, c, int(v), fmt)
        return
    if isinstance(v, (np.floating, float)):
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return
        ws.write_number(r, c, f, fmt)
        return
    if isinstance(v, datetime):
        ws.write_datetime(r, c, v.to_pydatetime() if isinstance(v, pd.Timestamp) else v, datefmt)
        return
    if isinstance(v, date):
        ws.write_datetime(r, c, datetime(v.year, v.month, v.day), datefmt)
        return
    ws.write_string(r, c, str(v), fmt)


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if np.isnan(f) or np.isinf(f) else f
    except (TypeError, ValueError):
        return 0.0


def write_workbook(path: Path, res: PipelineResult, checks: list, run_info: dict,
                   log_messages: list, cfg: AppConfig) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")          # written aside, renamed when complete
    data, lay, ex = res.data, res.layout, cfg.excel
    cols = list(data.columns)
    ix = {c: i for i, c in enumerate(cols)}
    values = data.astype(object).to_numpy()
    derived = {d.out: d for d in lay.derived}
    numeric = {c: to_number(data[c]).fillna(0.0).to_numpy() for c in lay.sum_cols}

    wb = xlsxwriter.Workbook(str(tmp))
    try:
        ws = wb.add_worksheet("Merged")
        st = _Styles(wb, ex)
        datefmt = st.get("data", "m/d/yyyy")
        colfmt = {c: ex.number_format for c in lay.number_cols}
        colfmt.update({name: f for name, f in ex.formats.items() if name in ix})

        for i, c in enumerate(swap_header_names(cols, ex.swap_headers)):     # display names only
            ws.write_string(0, i, str(c), st.get("header"))
        ws.set_row(0, 32)
        for name, f in colfmt.items():
            ws.set_column(ix[name], ix[name], 18 if "%" in f else 16)

        L = {c: col_letter(i) for c, i in ix.items()}

        def formula_for(d, r1):               # r1 = 1-based Excel row
            a, b = f"{L[d.a]}{r1}", f"{L[d.b]}{r1}"
            return f"=IFERROR({a}/{b},0)" if d.kind == "ratio" else f"=IFERROR({a}-{b},{a})"

        def fill_row(row, kind):              # colour the whole row, including blank cells
            for c, name in enumerate(cols):
                ws.write_blank(row, c, None, st.get(kind, colfmt.get(name)))

        ckey = normalize_key_series(data[lay.cust_col]).fillna("__MISSING_CUSTOMER__")
        block = (ckey != ckey.shift()).cumsum().to_numpy()
        n = len(data)
        row, i = 1, 0
        while i < n:
            j = i
            while j + 1 < n and block[j + 1] == block[i]:
                j += 1
            first_row = row
            for k in range(i, j + 1):
                for c, v in enumerate(values[k]):
                    name = cols[c]
                    fmt = st.get("data", colfmt.get(name))
                    if name in derived:
                        ws.write_formula(row, c, formula_for(derived[name], row + 1), fmt, _num(v))
                    else:
                        _write_cell(ws, row, c, v, datefmt, fmt)
                row += 1
            # ---- customer subtotal (one row, directly under the customer's data)
            r1 = row + 1
            fill_row(row, "subtotal")
            if lay.label_col:
                ws.write_string(row, ix[lay.label_col], ex.subtotal_label, st.get("subtotal"))
            for c in lay.subtotal_total_cols:
                ws.write_string(row, ix[c], "Total", st.get("subtotal"))
            for c in lay.copy_down_cols:
                ws.write_formula(row, ix[c], f"={L[c]}{r1 - 1}", st.get("subtotal", colfmt.get(c)),
                                 "" if values[j][ix[c]] is None else values[j][ix[c]])
            sub = {}
            for c in lay.sum_cols:
                if c in derived and derived[c].kind == "ratio":
                    continue
                sub[c] = float(numeric[c][i:j + 1].sum())
                ws.write_formula(row, ix[c],
                                 f"=_xlfn.AGGREGATE(9,6,{L[c]}{first_row + 1}:{L[c]}{row})",
                                 st.get("subtotal", colfmt.get(c)), sub[c])
            for d in lay.derived:
                if d.kind == "ratio":
                    b = sub.get(d.b, 0.0)
                    ws.write_formula(row, ix[d.out], formula_for(d, r1), st.get("subtotal", colfmt.get(d.out)),
                                     sub.get(d.a, 0.0) / b if b else 0.0)
            row += 1
            i = j + 1

        # ---- grand total: one blank spacer row, then totals over data rows only
        last_excel = row                                   # last written row, 1-based
        row += 1
        r1 = row + 1
        fill_row(row, "total")
        for c in lay.grand_total_cols:
            ws.write_string(row, ix[c], "Total", st.get("total"))
        tot = {}
        for c in lay.sum_cols:
            if c in derived and derived[c].kind == "ratio":
                continue
            tot[c] = float(numeric[c].sum())
            ws.write_formula(row, ix[c], f"=_xlfn.AGGREGATE(9,3,{L[c]}2:{L[c]}{last_excel})",
                             st.get("total", colfmt.get(c)), tot[c])
        for d in lay.derived:
            if d.kind == "ratio":
                b = tot.get(d.b, 0.0)
                ws.write_formula(row, ix[d.out], formula_for(d, r1), st.get("total", colfmt.get(d.out)),
                                 tot.get(d.a, 0.0) / b if b else 0.0)

        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, last_excel - 1, len(cols) - 1)
        _write_run_info(wb, st, run_info, checks, log_messages, lay)
        wb.close()
    except Exception:
        try:
            wb.close()
        finally:
            if tmp.exists():
                tmp.unlink()
        raise
    os.replace(tmp, path)
    log.info("Workbook written: %s (%s data rows)", path, f"{n:,}")
    return path


def _write_run_info(wb, st, run_info, checks, log_messages, lay):
    ws = wb.add_worksheet("Run Info")
    head = st.get("header")
    ws.set_column(0, 0, 34)
    ws.set_column(1, 1, 24)
    ws.set_column(2, 2, 120)
    r = 0
    ws.write(r, 0, "Run information", head)
    ws.write(r, 1, "", head)
    r += 1
    for k, v in run_info.items():
        ws.write(r, 0, str(k))
        ws.write(r, 1, str(v))
        r += 1
    r += 1
    for c, label in enumerate(("Validation check", "Status", "Detail")):
        ws.write(r, c, label, head)
    r += 1
    for c in checks:
        ws.write(r, 0, c.name)
        ws.write(r, 1, c.status)
        ws.write(r, 2, c.detail)
        r += 1
    r += 1
    ws.write(r, 0, "Warnings and errors from the log", head)
    ws.write(r, 1, "", head)
    ws.write(r, 2, "", head)
    r += 1
    for level, msg in log_messages:
        ws.write(r, 0, level)
        ws.write(r, 2, msg)
        r += 1
    if lay.letter_map:
        r += 1
        ws.write(r, 0, "Layout letters resolved this run", head)
        ws.write(r, 1, "", head)
        r += 1
        for k, v in lay.letter_map.items():
            ws.write(r, 0, k)
            ws.write(r, 1, str(v))
            r += 1


def write_exceptions(out_dir: Path, res: PipelineResult) -> list:
    """Review lists as CSV: churned/added SANs, duplicates, null SANs, kW changes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = [c for c in ("_SAN_", "Customer Number", "Customer Name", "Base SKU", "Data Center",
                       "Commercial Model Helper", "Commercial Model", "Gross Power MRR", "Adjusted kW")]
    items = {
        "churned_sans.csv": res.churned, "added_sans.csv": res.added,
        "duplicate_sans_cm.csv": res.dup_cm, "duplicate_sans_pm.csv": res.dup_pm,
        "null_san_rows.csv": res.null_san, "adjusted_kw_changes.csv": res.kw_diffs,
    }
    written = []
    for name, df in items.items():
        if df is None or df.empty:
            continue
        cols = [c for c in df.columns if c in key or name == "adjusted_kw_changes.csv"] or list(df.columns)[:12]
        df[cols].to_csv(out_dir / name, index=False)
        written.append(out_dir / name)
    if written:
        log.info("Exception lists written to %s: %s", out_dir, [p.name for p in written])
    return written
