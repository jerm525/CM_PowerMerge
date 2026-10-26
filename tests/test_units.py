import numpy as np
import pandas as pd

from cm_power_merge.cmh import assign_cmh_for_new_rows
from cm_power_merge.columns import (col_letter, find_column, letter_index, normalize_key_series, to_number)
from cm_power_merge.config import RulesConfig, default_reporting_month, parse_month
from cm_power_merge.kw_rules import compute_adjusted_kw
from datetime import date


def test_to_number_handles_credits_currency_and_small_floats():
    s = pd.Series(["$1,234.50", "(123.45)", "1e-05", None, "abc", 7, "-5"], dtype=object)
    out = to_number(s)
    assert out.iloc[0] == 1234.5 and out.iloc[1] == -123.45
    assert abs(out.iloc[2] - 1e-05) < 1e-12
    assert np.isnan(out.iloc[3]) and np.isnan(out.iloc[4])
    assert out.iloc[5] == 7 and out.iloc[6] == -5


def test_find_column_is_strict():
    df = pd.DataFrame(columns=["Circuit kW", "Monthly Amount", "Bill Quantity"])
    assert find_column(df, "$/kW", ["$/KW"]) is None          # no fuzzy fallback to 'Circuit kW'
    assert find_column(df, "Bill Qty", ["Bill Quantity"]) == "Bill Quantity"
    assert find_column(df, "monthly amount") == "Monthly Amount"


def test_key_normalisation():
    s = normalize_key_series(pd.Series([1234.0, " 55 ", "77.0", "", None, np.nan, "A1"], dtype=object))
    assert s.tolist()[:3] == ["1234", "55", "77"] and s.tolist()[6] == "A1"
    assert s.isna().tolist() == [False, False, False, True, True, True, False]


def test_letters_roundtrip():
    assert col_letter(0) == "A" and col_letter(26) == "AA" and col_letter(51) == "AZ" and col_letter(52) == "BA"
    assert letter_index("AA") == 26 and letter_index("BB") == 53


def _kw_frame(**rows):
    return pd.DataFrame(rows)


def test_kw_precedence_and_models():
    df = pd.DataFrame({
        "Commercial Model Helper": ["Circuit", "Circuit", "Metered", "Other Colo", "Custom Power", "Space", "Per kW", "Circuit"],
        "Base SKU": ["A", "B", "C", "COLO-1U-TRS", "D", "E", "F", "COLO-1U-TRS"],
        "Gross Power MRR": [100, 100, 100, 100, 100, 100, 100, 0],
        "Bill Qty": [2, 3, 7, 1, 2, 1, 4, 1],
        "Final kW for $/kW": [10, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan],
        "Circuit kW": [np.nan, 4, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan],
        "Item Full Name": ["", "", "", "", "30A 208V 3 phase circuit", "", "", ""],
        "Asset Notes": [""] * 8, "Sales Description": [""] * 8,
    })
    kw, src = compute_adjusted_kw(df, RulesConfig())
    assert kw.iloc[0] == 10 and kw.iloc[1] == 12 and kw.iloc[2] == 7
    assert kw.iloc[3] == 0.44                         # 1U beats exempt helper
    expected_custom = 30 * 208 * np.sqrt(3) / 1000 / 0.8 * 2
    assert abs(kw.iloc[4] - expected_custom) < 1e-9
    assert kw.iloc[5] == 0 and src.iloc[5] == "exempt_default_zero"
    assert kw.iloc[6] == 4
    assert kw.iloc[7] == 0 and src.iloc[7] == "override_gpm0"      # $0 MRR beats 1U


def test_pkw_rows_have_no_calculated_kw():
    df = pd.DataFrame({"Commercial Model Helper": ["PKW"], "Gross Power MRR": [100], "Bill Qty": [3],
                       "Base SKU": ["X"]})
    kw, src = compute_adjusted_kw(df, RulesConfig())
    assert np.isnan(kw.iloc[0]) and src.iloc[0] == "missing"       # surfaced by validation, not hidden


def test_cmh_assignment_survives_duplicate_reference_rows():
    merged = pd.DataFrame({"_SOURCE_": ["f2_add", "f2_add", "f1"], "Base SKU": ["PWR-CIRC", "NOPE", "PWR-CIRC"],
                           "Commercial Model Helper": [np.nan, np.nan, "Metered"], "F2": ["a", "b", "c"], "F3": ["x", "y", "z"]})
    ref = pd.DataFrame({"Base SKU": ["PWR-CIRC", "PWR-CIRC"], "Commercial Model Helper": ["Circuit", "Metered"],
                        "F2": ["b", "b"], "F3": ["y", "y"]})
    out = assign_cmh_for_new_rows(merged, ref)
    # row 0: by Base SKU (first reference row wins); row 1: no SKU match, falls back to (F2,F3); row 2 untouched
    assert out["Commercial Model Helper"].tolist() == ["Circuit", "Circuit", "Metered"]


def test_month_helpers():
    assert default_reporting_month(date(2026, 10, 13)) == date(2026, 9, 30)
    assert default_reporting_month(date(2026, 1, 5)) == date(2025, 12, 31)
    assert parse_month("2026-02") == date(2026, 2, 28)
    assert parse_month("3/31/2026") == date(2026, 3, 31)
