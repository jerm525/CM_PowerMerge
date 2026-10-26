"""Resolve Excel layout references (header names or legacy '@AB' letters) to headers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .columns import col_letter, letter_index
from .config import ExcelConfig
from .logging_setup import get_logger

log = get_logger("layout")


@dataclass
class ResolvedDerived:
    out: str
    kind: str
    a: str
    b: str


@dataclass
class Layout:
    columns: list
    sum_cols: list
    derived: list
    label_col: Optional[str]
    subtotal_total_cols: list
    grand_total_cols: list
    copy_down_cols: list
    cust_col: str
    number_cols: list = field(default_factory=list)
    letter_map: dict = field(default_factory=dict)     # '@T' -> header it resolved to


def _one(columns, ref: str, strict: bool, letter_map: dict) -> str:
    if ref.startswith("@"):
        if strict:
            raise ValueError(f"strict_layout is on but {ref!r} is a column letter; use the header name")
        idx = letter_index(ref[1:])
        if idx >= len(columns):
            raise KeyError(f"{ref} is beyond the last column ({col_letter(len(columns) - 1)})")
        letter_map[ref] = columns[idx]
        return columns[idx]
    if ref not in columns:
        raise KeyError(f"Column {ref!r} not found in the output layout. Run `layout` to list headers.")
    return ref


def _range_endpoints(columns, ref: str):
    """Return (lo, hi) if ref is 'start:end' where each end is a header name or '@Letter', else None.

    An exact header match always wins, so a header that itself contains ':' still works as a single
    column. Otherwise every ':' is tried as the split point until both sides resolve.
    """
    if ref in columns or ":" not in ref:
        return None
    for pos in (i for i, ch in enumerate(ref) if ch == ":"):
        lo, hi = ref[:pos].strip(), ref[pos + 1:].strip()
        if all(side.startswith("@") or side in columns for side in (lo, hi)):
            return lo, hi
    return None


def _many(columns, refs, strict: bool, letter_map: dict) -> list:
    out: list = []
    for ref in refs:
        ends = _range_endpoints(columns, ref)
        if ends:
            first = _one(columns, ends[0], strict, letter_map)
            last = _one(columns, ends[1], strict, letter_map)
            i, j = columns.index(first), columns.index(last)
            if i > j:
                raise ValueError(f"Range {ref!r} runs backwards: {first!r} is after {last!r} in the layout")
            if ends[0].startswith("@") or ends[1].startswith("@"):
                for k in range(i, j + 1):
                    letter_map.setdefault(f"@{col_letter(k)}", columns[k])
            out.extend(columns[i:j + 1])
        else:
            out.append(_one(columns, ref, strict, letter_map))
    return list(dict.fromkeys(out))


def resolve_layout(columns: list, ex: ExcelConfig, cust_col: str) -> Layout:
    columns = list(columns)
    lm: dict = {}
    s = ex.strict_layout
    derived = [ResolvedDerived(_one(columns, d.out, s, lm), d.kind,
                               _one(columns, d.a, s, lm), _one(columns, d.b, s, lm))
               for d in ex.derived]
    layout = Layout(
        columns=columns,
        sum_cols=_many(columns, ex.sum_columns, s, lm),
        derived=derived,
        label_col=_one(columns, ex.subtotal_label_column, s, lm) if ex.subtotal_label_column else None,
        subtotal_total_cols=_many(columns, ex.subtotal_total_columns, s, lm),
        grand_total_cols=_many(columns, ex.grand_total_columns, s, lm),
        copy_down_cols=[c for c in (_one(columns, r, s, lm) for r in ex.copy_down_columns)],
        cust_col=cust_col,
        number_cols=_many(columns, ex.number_columns, s, lm),
        letter_map=lm,
    )
    if lm:
        log.info("Layout letters resolved: %s", {k: v for k, v in lm.items()})
        log.warning("Layout uses %s letter reference(s); replace with header names and set "
                    "excel.strict_layout = true once verified", len(lm))
    return layout
