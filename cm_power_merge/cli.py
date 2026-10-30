"""Command line: run | layout | ddl | compare | promote.

Exit codes: 0 ok, 1 error, 2 validation FAIL, 3 WARN with fail_on_warn,
4 CM data not ready (readiness gate).
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import __version__
from .cleaning import clean_pm, unswap_pm_headers
from .columns import col_letter, swap_header_names
from .compare import compare_frames, load_output_rows
from .config import AppConfig, default_reporting_month, load_config, name_for_month, parse_month
from .logging_setup import get_logger, setup_logging, shutdown_logging
from .notify import notify
from .pipeline import run_pipeline
from .report import write_exceptions, write_workbook
from .sources import (load_cm_from_sql, load_excel, load_model_ref, prior_output_path)
from .validation import overall_status, readiness_checks, run_validations

log = get_logger("cli")

EXIT_OK, EXIT_ERROR, EXIT_FAIL, EXIT_WARN, EXIT_NOT_READY = 0, 1, 2, 3, 4


def _month(args) -> date:
    return parse_month(args.month) if getattr(args, "month", None) else default_reporting_month()


def cmd_run(args, cfg: AppConfig, run_id: str, collector) -> int:
    month = _month(args)
    started = datetime.now()
    log.info("cm_power_merge %s starting; reporting month %s", __version__, month)

    pm_path = Path(args.pm_file) if args.pm_file else (
        cfg.paths.pm_file or prior_output_path(cfg.paths.output_dir, cfg.paths.output_name_pattern, month))
    if not pm_path.exists():
        raise FileNotFoundError(f"Prior-month workbook not found: {pm_path} (use --pm-file)")
    pm_raw = unswap_pm_headers(load_excel(pm_path, cfg.paths.sheet_pm), cfg.excel.swap_headers, cfg.rules.san_column)

    cm_file = Path(args.cm_file) if args.cm_file else cfg.paths.cm_file
    cm_raw = load_excel(cm_file, cfg.paths.sheet_cm) if cm_file else load_cm_from_sql(cfg.sql, month)
    cm_source = str(cm_file) if cm_file else f"{cfg.sql.stored_proc} @ {month}"

    ready = readiness_checks(len(cm_raw), len(clean_pm(pm_raw)), cfg)   # prior *data* rows, not subtotals
    for c in ready:
        (log.info if c.status == "PASS" else log.error)("readiness %s %s: %s", c.name, c.status, c.detail)
    if overall_status(ready) == "FAIL" and not args.force:
        msg = "; ".join(c.detail for c in ready if c.status == "FAIL")
        notify(cfg.notify, "fail", f"CM Power Merge {month}: data not ready", msg)
        log.error("Readiness gate failed; nothing was written. Re-run later or use --force.")
        return EXIT_NOT_READY

    res = run_pipeline(pm_raw, cm_raw, cfg, month, lambda: load_model_ref(cfg.paths.model_ref))
    checks = ready + run_validations(res, cfg)
    status = overall_status(checks)

    out_name = name_for_month(cfg.paths.output_name_pattern, month)
    out_path = cfg.paths.output_dir / out_name
    if status == "FAIL":      # keep failed output out of next month's input slot
        out_path = out_path.with_name(out_path.stem + " FAILED" + out_path.suffix)
    run_info = {
        "Run ID": run_id, "Version": __version__, "Reporting month": month,
        "Started": started.strftime("%Y-%m-%d %H:%M:%S"), "PM file": pm_path, "CM source": cm_source,
        "Adjusted kW policy": cfg.rules.kw_policy, "Validation status": status,
        "Rows out": res.stats["out_rows"], "Customers": res.stats["customers"],
        "Total Gross Power MRR": f"{res.stats['total_mrr']:,.2f}", "Total Adjusted kW": f"{res.stats['total_kw']:,.2f}",
        "PM rows": res.stats["pm_rows"], "CM rows": res.stats["cm_rows"],
        "Kept / added / churned": f"{res.stats['kept_from_pm']} / {res.stats['added_from_cm']} / {res.stats['churned']}",
    }
    write_workbook(out_path, res, checks, run_info, list(collector.records), cfg)
    write_exceptions(cfg.paths.output_dir / "exceptions" / f"{month:%Y-%m}", res)

    promoted = False
    if cfg.promote.load_staging or args.load_staging:
        if status == "FAIL":
            log.error("Validation FAILED: staging load skipped")
        else:
            from .sql_load import load_to_staging, promote
            stage_df = res.data.set_axis(swap_header_names(list(res.data.columns), cfg.excel.swap_headers), axis=1)
            load_to_staging(stage_df, cfg.sql, run_id)
            can_auto = cfg.promote.mode == "auto" and (status == "PASS" or cfg.promote.allow_warn)
            if args.promote or can_auto:
                promote(cfg.sql, run_id, month)
                promoted = True
            else:
                log.info("Staged only. After review: python -m cm_power_merge promote --run-id %s --month %s",
                         run_id, month)

    summary = (f"month={month} status={status} rows={res.stats['out_rows']:,} customers={res.stats['customers']:,} "
               f"mrr={res.stats['total_mrr']:,.2f} kw={res.stats['total_kw']:,.2f} promoted={promoted} output={out_path}")
    log.info("SUMMARY %s", summary)
    problems = "\n".join(f"{c.status} {c.name}: {c.detail}" for c in checks if c.status != "PASS")
    notify(cfg.notify, {"PASS": "success", "WARN": "warn", "FAIL": "fail"}[status],
           f"CM Power Merge {month}: {status}", summary + ("\n\n" + problems if problems else ""))
    if status == "FAIL":
        return EXIT_FAIL
    if status == "WARN" and cfg.validation.fail_on_warn:
        return EXIT_WARN
    return EXIT_OK


def cmd_layout(args, cfg, run_id, collector) -> int:
    df = pd.read_excel(args.file, dtype=object, nrows=0)
    print("letter  header")
    for i, c in enumerate(df.columns):
        print(f"{col_letter(i):<7} {c}")
    return EXIT_OK


def cmd_ddl(args, cfg, run_id, collector) -> int:
    from .sql_load import ddl_for
    df = load_output_rows(Path(args.from_output))
    df = df.drop(columns=[c for c in df.columns if str(c).startswith("Unnamed")])
    print(ddl_for(df, cfg.sql.staging_table))
    return EXIT_OK


def _load_side(spec: str, cfg: AppConfig, month: date, run_id: str):
    if spec == "prod":
        from .sql_load import read_month
        df = read_month(cfg.sql, "prod", month)
    elif spec.startswith("stg:"):
        from .sql_load import read_month
        df = read_month(cfg.sql, "stg", month, spec.split(":", 1)[1])
    else:
        df = load_output_rows(Path(spec))
    # workbooks and tables carry the swapped header names; compare on logical names
    return df.set_axis(swap_header_names(list(df.columns), cfg.excel.swap_headers), axis=1)


def cmd_compare(args, cfg, run_id, collector) -> int:
    month = _month(args)
    new = _load_side(args.new, cfg, month, run_id)
    ref = _load_side(args.ref, cfg, month, run_id)
    result = compare_frames(new, ref, key=args.key, tol=args.tol)
    for k, v in result.summary.items():
        print(f"{k}: {v}")
    if args.detail:
        result.diffs.to_csv(args.detail, index=False)
        print(f"detail written to {args.detail}")
    clean = (not result.summary["cells_different"] and not result.only_new and not result.only_ref)
    return EXIT_OK if clean else EXIT_FAIL


def cmd_promote(args, cfg, run_id, collector) -> int:
    from .sql_load import promote
    promote(cfg.sql, args.run_id, _month(args))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cm_power_merge", description="Monthly CM Power merge")
    ap.add_argument("--config", type=Path, default=None, help="TOML config (see config.example.toml)")
    ap.add_argument("--run-id", dest="run_id_override", default=None)
    ap.add_argument("--quiet", action="store_true", help="no console logging (file log only)")
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the merge")
    r.add_argument("--month", help="reporting month-end, e.g. 2026-09-30 or 2026-09 (default: prior month-end)")
    r.add_argument("--pm-file", help="prior-month workbook (default: last month's output)")
    r.add_argument("--cm-file", help="CM Excel extract instead of the stored procedure")
    r.add_argument("--load-staging", action="store_true", help="load validated rows to the staging table")
    r.add_argument("--promote", action="store_true", help="after staging, promote to production")
    r.add_argument("--force", action="store_true", help="ignore the readiness gate")
    r.set_defaults(func=cmd_run)

    l = sub.add_parser("layout", help="list header -> Excel letter for a workbook")
    l.add_argument("file", type=Path)
    l.set_defaults(func=cmd_layout)

    d = sub.add_parser("ddl", help="print CREATE TABLE for the staging table from an output workbook")
    d.add_argument("--from-output", required=True)
    d.set_defaults(func=cmd_ddl)

    c = sub.add_parser("compare", help="diff two runs by SAN: workbook paths, 'prod', or 'stg:<RunId>'")
    c.add_argument("--new", required=True)
    c.add_argument("--ref", required=True)
    c.add_argument("--month", help="needed when a side is a SQL table")
    c.add_argument("--key", default="_SAN_")
    c.add_argument("--tol", type=float, default=0.005)
    c.add_argument("--detail", help="CSV path for cell-level differences")
    c.set_defaults(func=cmd_compare)

    p = sub.add_parser("promote", help="promote a staged run to production (single transaction)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--month", required=True)
    p.set_defaults(func=cmd_promote)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.config is not None:
        log.info("loading config from %s", args.config.name)
        print(args.config.name)
    else:
        log.info("no config file specified, using default config")
    log.info("Stage table: %s", cfg.sql.staging_table)
    log.info("Target table: %s", cfg.sql.production_table)
    run_id, collector = setup_logging(cfg.paths.log_dir, args.run_id_override, console=not args.quiet)
    try:
        return args.func(args, cfg, run_id, collector)
    except Exception as exc:
        log.exception("Run failed: %s", exc)
        if args.command == "run":
            notify(cfg.notify, "fail", "CM Power Merge: run failed",
                   f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-1500:]}")
        return EXIT_ERROR
    finally:
        shutdown_logging()                    # release the log file as soon as the run ends

if __name__ == "__main__":
    sys.exit(main())
