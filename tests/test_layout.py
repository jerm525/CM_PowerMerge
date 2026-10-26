from cm_power_merge.config import ExcelConfig
from cm_power_merge.layout import resolve_layout


def _wide():
    cols = [f"c{i}" for i in range(56)]
    named = {6: "Customer Number", 7: "Customer Name", 23: "Gross Power MRR", 32: "Adjusted kW",
             47: "PM MRR", 48: "PM MRR Var", 49: "PM Adjusted kW", 50: "PM Adjusted kW Var",
             51: "COLA/Price Increase Check", 53: "Customer Has Cabinet", 52: "Cabinet Count"}
    for i, n in named.items():
        cols[i] = n
    return cols


def test_default_legacy_letters_resolve_and_are_reported():
    lay = resolve_layout(_wide(), ExcelConfig(), "Customer Number")
    assert lay.letter_map["@X"] == "Gross Power MRR" and lay.letter_map["@AV"] == "PM MRR"
    assert lay.sum_cols[0] == "c19" and "Cabinet Count" in lay.sum_cols and "PM Adjusted kW Var" in lay.sum_cols
    assert lay.subtotal_total_cols[0] == "c8" and lay.grand_total_cols[0] == "Customer Number"


def test_strict_layout_refuses_letters_and_unknown_headers():
    cfg = ExcelConfig(strict_layout=True)
    try:
        resolve_layout(_wide(), cfg, "Customer Number")
    except ValueError as exc:
        assert "column letter" in str(exc)
    else:
        raise AssertionError("strict layout must reject letters")
    cfg2 = ExcelConfig(sum_columns=["No Such Header"], subtotal_total_columns=[], grand_total_columns=[],
                       derived=[], strict_layout=True)
    try:
        resolve_layout(_wide(), cfg2, "Customer Number")
    except KeyError as exc:
        assert "No Such Header" in str(exc)
    else:
        raise AssertionError("unknown header must stop the run")


def test_header_name_ranges_and_mixed_ranges():
    from cm_power_merge.layout import _many
    cols = ["A1", "Bill Qty", "Monthly Amount", "Final kW for $/kW", "Odd: Name", "Z9"]
    assert _many(cols, ["Bill Qty:Final kW for $/kW"], True, {}) == ["Bill Qty", "Monthly Amount", "Final kW for $/kW"]
    assert _many(cols, ["Odd: Name"], True, {}) == ["Odd: Name"]                   # colon inside a real header
    assert _many(cols, ["Monthly Amount:Odd: Name"], True, {}) == ["Monthly Amount", "Final kW for $/kW", "Odd: Name"]
    assert _many(cols, ["@B:Final kW for $/kW"], False, {}) == ["Bill Qty", "Monthly Amount", "Final kW for $/kW"]
    for bad, exc in ((["Final kW for $/kW:Bill Qty"], ValueError), (["Bill Qty:Nope"], KeyError)):
        try:
            _many(cols, bad, True, {})
        except exc:
            pass
        else:
            raise AssertionError(bad)
