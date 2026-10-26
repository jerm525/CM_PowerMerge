import tempfile
from pathlib import Path

import pandas as pd

from cm_power_merge.cli import main
from cm_power_merge.config import load_config
from cm_power_merge.sql_load import ddl_for, infer_sql_type, quote_table
from helpers import make_cm, make_pm, make_ref

TOML = """
[paths]
model_ref = "ref.xlsx"
output_dir = "out"
log_dir = "logs"

[rules]
san_key_index = -1

[excel]
strict_layout = true
sum_columns = ["Bill Qty", "Monthly Amount", "Gross Power MRR", "Adjusted kW", "PM MRR", "PM MRR Var",
               "PM Adjusted kW", "PM Adjusted kW Var", "Cabinet Count"]
subtotal_label_column = "Customer Name"
subtotal_total_columns = ["Data Center"]
grand_total_columns = ["Customer Number", "Customer Name", "Data Center"]
copy_down_columns = ["Customer Number", "Customer Has Cabinet"]

[[excel.derived]]
out = "PM MRR Var"
kind = "diff"
a = "Gross Power MRR"
b = "PM MRR"

[[excel.derived]]
out = "PM Adjusted kW Var"
kind = "diff"
a = "Adjusted kW"
b = "PM Adjusted kW"

[[excel.derived]]
out = "COLA/Price Increase Check"
kind = "ratio"
a = "PM MRR Var"
b = "PM MRR"

[validation]
fail_on_warn = false
row_count_pct = 0.25
"""


def _setup(tmp: Path):
    make_pm().to_excel(tmp / "pm.xlsx", index=False)
    make_cm().to_excel(tmp / "cm.xlsx", index=False)
    make_ref().to_excel(tmp / "ref.xlsx", index=False)
    (tmp / "cfg.toml").write_text(TOML)


def test_cli_run_end_to_end_then_compare_and_layout(capsys=None):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _setup(tmp)
        base = ["--config", str(tmp / "cfg.toml"), "--quiet"]
        code = main(base + ["run", "--month", "2026-09", "--pm-file", str(tmp / "pm.xlsx"),
                            "--cm-file", str(tmp / "cm.xlsx")])
        assert code == 0
        out = tmp / "out" / "CM Power Merged 2026-09.xlsx"
        assert out.exists() and (tmp / "out" / "exceptions" / "2026-09" / "churned_sans.csv").exists()
        assert (tmp / "logs" / "cm_power_merge.log").read_text().count("SUMMARY") == 1

        # identical output vs itself -> compare clean; layout command works
        assert main(base + ["compare", "--new", str(out), "--ref", str(out)]) == 0
        assert main(base + ["layout", str(out)]) == 0
        assert main(base + ["ddl", "--from-output", str(out)]) == 0

        # next month: last month's output is found automatically as the PM input
        code = main(base + ["run", "--month", "2026-10", "--cm-file", str(tmp / "cm.xlsx")])
        assert code == 0 and (tmp / "out" / "CM Power Merged 2026-10.xlsx").exists()


def test_cli_readiness_gate_blocks_short_extract():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _setup(tmp)
        make_cm().head(2).to_excel(tmp / "short.xlsx", index=False)       # 2 rows vs 6 prior
        base = ["--config", str(tmp / "cfg.toml"), "--quiet"]
        code = main(base + ["run", "--month", "2026-09", "--pm-file", str(tmp / "pm.xlsx"),
                            "--cm-file", str(tmp / "short.xlsx")])
        assert code == 4 and not (tmp / "out" / "CM Power Merged 2026-09.xlsx").exists()


def test_cli_failed_validation_does_not_occupy_next_months_input_slot():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _setup(tmp)
        make_cm().drop(columns=["Monthly Amount"]).to_excel(tmp / "bad.xlsx", index=False)
        base = ["--config", str(tmp / "cfg.toml"), "--quiet"]
        code = main(base + ["run", "--month", "2026-09", "--pm-file", str(tmp / "pm.xlsx"),
                            "--cm-file", str(tmp / "bad.xlsx"), "--force"])
        assert code == 2
        assert (tmp / "out" / "CM Power Merged 2026-09 FAILED.xlsx").exists()
        assert not (tmp / "out" / "CM Power Merged 2026-09.xlsx").exists()


def test_missing_prior_file_is_an_error_not_a_silent_run():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _setup(tmp)
        base = ["--config", str(tmp / "cfg.toml"), "--quiet"]
        assert main(base + ["run", "--month", "2026-09", "--cm-file", str(tmp / "cm.xlsx")]) == 1


def test_unknown_config_key_is_rejected():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "bad.toml"
        p.write_text("[rules]\nkw_polcy = 'x'\n")
        try:
            load_config(p)
        except ValueError as exc:
            assert "kw_polcy" in str(exc)
        else:
            raise AssertionError("typo in config should be rejected")


def test_sql_helpers():
    df = pd.DataFrame({"a": [1, 2], "b": [1.5, None], "c": ["x", None], "d": [True, False],
                       "Reporting Month": [pd.Timestamp("2026-09-30").date()] * 2})
    assert [infer_sql_type(df[c]) for c in df.columns] == ["BIGINT", "FLOAT", "NVARCHAR(4000)", "BIT", "DATE"]
    ddl = ddl_for(df, "Corp_Dev.stg_ColoPowerMerged")
    assert "[RunId]" in ddl and "[Reporting Month] DATE" in ddl and ddl.startswith("CREATE TABLE [Corp_Dev].")
    try:
        quote_table("x]; DROP TABLE y;--")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe table name must be rejected")
