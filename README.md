# CM Power Merge (v2)

Monthly reconciliation of last month's merged workbook (PM) with this month's CM data, rewritten as a
package with logging, a validation gate, and a staged path to automating the SQL load.

```
python -m cm_power_merge --config config.toml run                      # prior month-end, SQL stored proc
python -m cm_power_merge --config config.toml run --month 2026-09 --cm-file CM.xlsx --pm-file PM.xlsx
python -m cm_power_merge --config config.toml run --load-staging       # shadow mode
python -m cm_power_merge --config config.toml promote --run-id ab12cd34 --month 2026-09
python -m cm_power_merge compare --new new.xlsx --ref approved.xlsx --detail diffs.csv
python -m cm_power_merge layout "CM Power Merged 2026-08.xlsx"         # header -> Excel letter
python -m cm_power_merge --config config.toml ddl --from-output "<output>.xlsx"
```

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | Defaults + TOML overrides; typos in keys are rejected; date helpers |
| `logging_setup.py` | Console + rotating file, run id on every line, collector for the Run Info sheet |
| `sources.py` | Stored procedure (`SET NOCOUNT ON`, closed connection), Excel readers, prior-output lookup |
| `columns.py` / `cleaning.py` | Strict column matching, key and number parsing, artifact stripping |
| `kw_rules.py` | Adjusted kW: one function, one precedence order |
| `cmh.py` / `commercial_model.py` | Helper assignment and the pivot port for new assets |
| `pipeline.py` | The merge as named stages, each logging counts |
| `validation.py` | PASS/WARN/FAIL checks, readiness gate |
| `layout.py` / `report.py` | Header-driven Excel formulas with cached values, Run Info sheet, exception CSVs |
| `sql_load.py` | Staging load, atomic promote, table reads |
| `compare.py` | Row/column diff for golden-file and shadow-mode checks |
| `notify.py` / `cli.py` | Teams/email alerts, commands, exit codes |

## Behaviour changes from the original (review these first)

1. **Adjusted kW is recalculated for every row** (`rules.kw_policy = "recalculate"`). The old script kept any
   prior non-null value. Rows whose recalculated value differs from the prior workbook are listed in
   `exceptions/<month>/adjusted_kw_changes.csv`. Set `"preserve_existing"` to reproduce the old behaviour.
2. **Precedence is now one rule set:** `$0 MRR` -> `1U` -> exempt helper -> by model. (The old code had two
   conflicting copies; the outer one decided in practice.)
3. **Column matching is strict.** A missing CM column is logged and, for the three required ones, fails validation.
   The old fuzzy fallback could overwrite from the wrong column.
4. **Duplicate CM SANs:** the last whole row is used (the old code mixed per-column "last non-null" values).
   Reported as a WARN (or FAIL with `duplicate_san_policy = "fail"`).
5. **Deployment flag** `Per kW >60%±10% OK` is blank (N/A) when a deployment has no PKW, instead of False.
6. **Reporting Month** is a real date (`m/d/yyyy` format), defaulting to the prior month-end.
7. **Only the exact `zz Total` subtotal label is stripped**, not any customer starting with "zz ".
8. Customer ties in total MRR sort by customer key, so output is repeatable.
9. Formula cells carry their computed value, so pandas/SQL/preview tools see numbers, not 0.
10. CM columns that do not exist in the PM layout are still not carried into the output; they are now logged.

## Things to verify before trusting it

* **`_SAN_` position.** `san_key_index = 41` is carried over. With Reporting Month in column A that is column AQ,
  while old comments say AP.
* **Not tested here:** anything that needs SQL Server, ODBC, Teams or SMTP (stored procedure call, staging load,
  promote, alerts). The Excel/pandas pipeline, validation, compare and CLI are covered by tests on synthetic data.
* PKW rows still get no calculated kW (as before); validation WARNs when a revenue row has no kW.

## Rollout
[run_monthly.bat](../../../Desktop/run_monthly.bat)
1. **Golden run.** Run last month's two inputs (`--pm-file`, `--cm-file`) and `compare` against the output you
   approved. Differences should be explainable by the behavior changes above, mainly kW. Use
   `kw_policy = "preserve_existing"` first to see only the unintended ones.
2. **Shadow mode.** Create the staging table (`ddl` prints a starting point; review and freeze it), set
   `promote.load_staging = true`, and keep loading production by hand. `compare --new stg:<RunId> --ref prod --month ...`
   shows any row or column that differs. Do this 2-3 cycles.
3. **Manual promote.** Review `Run Info` and the exception CSVs, then run `promote`.
4. **Auto-promote.** Set `promote.mode = "auto"`. It promotes only on an all-PASS run (WARNs block unless
   `allow_warn = true`); any FAIL stops and alerts.

## Scheduling (Windows)

Task Scheduler, running as a service account, action = `scripts\run_monthly.bat`, "Start in" = package folder.
Trigger monthly on a day when CM data is normally ready, with "restart on failure" every few hours: exit code 4
means the readiness gate found an incomplete extract and nothing was written. Exit 2 is a validation FAIL (the
workbook is saved as `... FAILED.xlsx` so it can never become next month's input). Put the service account in
the SQL login with EXEC on the procedure and INSERT/DELETE only on the two tables.

## Tests

`python tests/run_all.py` (or `pytest tests`). Covers number/key parsing, kW rules, helper mapping with
duplicate reference rows, the full merge, validation, Excel formulas and cached values, next-month round trip,
compare, CLI exit codes and config errors.
