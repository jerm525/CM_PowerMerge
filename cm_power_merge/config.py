"""Configuration: defaults live here, overrides come from a TOML file.

Secrets (SQL password, webhook URLs) are never stored in the TOML file; the
config names the *environment variable* that holds them.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

TARGET_FIELDS = ["Bill Qty", "Monthly Amount", "Gross Power MRR", "$/kW", "Per-kW $/kW Margin"]
ALIASES = {
    "Bill Qty": ["Bill Quantity", "Billing Quantity", "Billed Qty", "BillQty"],
    "Monthly Amount": ["MRR", "Monthly Recurring Revenue", "MonthlyAmount"],
    "Gross Power MRR": ["Power MRR", "Gross Power MRR ($)"],
    "$/kW": ["$/KW", "Per kW Cost", "Rate per kW"],
    "Per-kW $/kW Margin": ["Per kW Margin", "Margin $/kW", "Per-kW Margin",
                           "All-in $/kW Margin", "All-In $/kW Margin"],
}
# CM/stored-proc column names -> PM workbook column names (applied only when
# the destination exists in the PM layout).
CM_RENAMES = {
    "CustomerNumber": "Customer Number",
    "CustomerName": "Customer Name",
    "BaseSKU": "Base SKU",
    "ServiceAssetNumber": "Service Asset Number",
    "Asset_Sales_Order": "Asset Sales Order",
    "DataCenter": "Data Center",
    "MonthlyAmount": "Monthly Amount",
    "BillQty": "Bill Qty",
}


@dataclass
class PathsConfig:
    pm_file: Optional[Path] = None            # explicit prior-month workbook (optional)
    cm_file: Optional[Path] = None            # CM Excel extract (optional; SQL is the default source)
    model_ref: Path = Path("Commercial Model Reference.xlsx")
    output_dir: Path = Path("output")
    log_dir: Path = Path("logs")
    output_name_pattern: str = "CM Power Merged {yyyy}-{mm}.xlsx"
    sheet_pm: int = 0
    sheet_cm: int = 0


@dataclass
class SqlConfig:
    server: Optional[str] = None
    database: str = "eda_repository"
    driver: str = "ODBC Driver 17 for SQL Server"
    trusted_connection: bool = True
    encrypt: Optional[bool] = None            # needed explicitly for ODBC Driver 18
    trust_server_certificate: Optional[bool] = None
    conn_str_env: str = "EDA_REPOSITORY"   # full connection string, wins if set
    user_env: str = "CM_POWER_SQL_USER"
    password_env: str = "CM_POWER_SQL_PASSWORD"
    stored_proc: str = "Corp_Dev.usp_ColoPowerExport"
    staging_table: str = "Corp_Dev.tbl__stg_ColoKwAudit"
    production_database: str = "eda_lake"
    production_table: str = "xlsx_extract.tbl__xlsx_m_colo_kw_audit" #not used for connection only insert promote
    month_column: str = "reporting_month"     # SQL column holding the reporting month
    timeout_seconds: int = 600
    column_map: dict[str, str] = field(default_factory=dict)




@dataclass
class RulesConfig:
    san_column: str = "Service Asset Number"
    san_key_index: int = 41                   # legacy position of _SAN_ in the PM layout; -1 = append
    commit_divisor: float = 0.8
    amp_patterns: list = field(default_factory=lambda: [r"(\d+)\s*A\b", r"(\d+)\s*amp(s)?\b"])
    volt_patterns: list = field(default_factory=lambda: [r"(\d+)\s*V\b", r"(\d+)\s*VAC\b", r"(\d+)\s*VDC\b"])
    one_u_sku: str = "COLO-1U-TRS"
    one_u_kw: float = 0.44
    exempt_helpers: list = field(default_factory=lambda: ["space", "pdu", "other colo", "customer hardware"])
    deploy_ratio_min: float = 0.54            # '>60% +/-10%' read as >= 54%
    # "recalculate": Adjusted kW is recomputed for every row (prior value is shown only as PM Adjusted kW).
    # "preserve_existing": legacy behaviour, a non-null prior value always wins.
    kw_policy: str = "recalculate"
    commercial_model_allowed: list = field(default_factory=lambda: ["Circuit", "Per-kW", "+E"])
    duplicate_san_policy: str = "warn"        # "warn" or "fail"
    # true: stop if new assets need a helper and the reference file is missing.
    # false: carry on (like the old script); validation still flags the unmapped rows.
    model_ref_required: bool = True


@dataclass
class DerivedSpec:
    out: str
    kind: str          # "ratio": IFERROR(a/b,0)   "diff": IFERROR(a-b,a)
    a: str
    b: str


def _default_derived():
    return [
        DerivedSpec("PM MRR Var", "diff", "Gross Power MRR", "PM MRR"),
        DerivedSpec("PM Adjusted kW Var", "diff", "Adjusted kW", "PM Adjusted kW"),
        DerivedSpec("COLA/Price Increase Check", "ratio", "PM MRR Var", "PM MRR"),
        DerivedSpec("@AA", "ratio", "@X", "@Z"),
        DerivedSpec("@AH", "ratio", "@AD", "@AG"),
        DerivedSpec("@AK", "ratio", "@AD", "@AJ"),
    ]


@dataclass
class ExcelConfig:
    # References are a header name or an Excel letter written "@AB" (ranges "@T:@Z").
    # Letters reproduce the legacy layout; replace them with header names after
    # running `layout`, then set strict_layout = true.
    sum_columns: list = field(default_factory=lambda: ["@T:@Z", "@AD:@AG", "@AI:@AJ", "@AV:@AY", "@BA"])
    subtotal_label_column: str = "Customer Name"
    subtotal_label: str = "zz Total"
    subtotal_total_columns: list = field(default_factory=lambda: ["@I:@S"])
    grand_total_columns: list = field(default_factory=lambda: ["@G:@S"])
    copy_down_columns: list = field(default_factory=lambda: ["Customer Number", "Customer Has Cabinet"])
    derived: list = field(default_factory=_default_derived)
    formats: dict = field(default_factory=lambda: {
        "PM MRR Var": "0.00", "PM Adjusted kW Var": "0.00",
        "COLA/Price Increase Check": "0.00%"})
    strict_layout: bool = False
    # Styling. Colours are "#RRGGBB". number_columns get number_format on data, subtotal and total rows;
    # `formats` (header -> format) overrides per column and also covers percent/other formats.
    header_bg: str = "#1F3864"
    header_font: str = "#FFFFFF"
    subtotal_bg: str = "#DDEBF7"
    grand_total_bg: str = "#BDD7EE"
    number_format: str = "#,##0.00"
    number_columns: list = field(default_factory=list)
    # Pairs of header names to swap in the written workbook/staging load, e.g. [["_SAN_", "Adjusted kW Source"]].
    swap_headers: list = field(default_factory=list)


@dataclass
class ValidationConfig:
    min_cm_rows: int = 1
    row_count_pct: float = 0.10               # readiness gate + WARN vs prior month
    total_mrr_pct: float = 0.15
    total_kw_pct: float = 0.15
    customer_swing_pct: float = 0.50
    customer_swing_min_abs: float = 1000.0
    churn_pct: float = 0.05
    recon_tolerance: float = 0.01
    required_cm_columns: list = field(default_factory=lambda: ["Bill Qty", "Monthly Amount", "Gross Power MRR"])
    fail_on_warn: bool = False
    # Per-check override, e.g. { kw_nonnegative = "WARN", churned_sans = "OFF" }
    severity: dict = field(default_factory=dict)


@dataclass
class NotifyConfig:
    teams_webhook_env: str = "CM_POWER_TEAMS_WEBHOOK"
    smtp_host: Optional[str] = None
    smtp_port: int = 25
    mail_from: Optional[str] = None
    mail_to: list = field(default_factory=list)
    on: list = field(default_factory=lambda: ["fail", "warn"])   # any of fail / warn / success


@dataclass
class PromoteConfig:
    load_staging: bool = False                # run: write validated rows to the staging table
    mode: str = "manual"                         # off | manual | auto  (auto = promote on clean run)
    allow_warn: bool = False                  # auto mode: promote even when WARNs exist


@dataclass
class AppConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    sql: SqlConfig = field(default_factory=SqlConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    excel: ExcelConfig = field(default_factory=ExcelConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    promote: PromoteConfig = field(default_factory=PromoteConfig)
    base_dir: Path = Path(".")


def _build(obj, data: dict, where: str):
    names = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"Unknown config key [{where}] {key!r}")
        current = getattr(obj, key)
        if is_dataclass(current):
            _build(current, value, f"{where}.{key}" if where else key)
        elif key == "derived":
            setattr(obj, key, [DerivedSpec(**d) for d in value])
        elif isinstance(current, Path) or key in ("pm_file", "cm_file"):
            setattr(obj, key, Path(value) if value else None)
        else:
            setattr(obj, key, value)


def load_config(path: Optional[Path] = None) -> AppConfig:
    cfg = AppConfig()
    if path is not None:
        path = Path(path).resolve()
        with open(path, "rb") as fh:
            _build(cfg, tomllib.load(fh), "")
        cfg.base_dir = path.parent
    # Relative paths resolve against the config file, not the scheduler's cwd.
    for name in ("pm_file", "cm_file", "model_ref", "output_dir", "log_dir"):
        value = getattr(cfg.paths, name)
        if value is not None and not Path(value).is_absolute():
            setattr(cfg.paths, name, (cfg.base_dir / value).resolve())
    if cfg.rules.kw_policy not in ("recalculate", "preserve_existing"):
        raise ValueError("rules.kw_policy must be 'recalculate' or 'preserve_existing'")
    if cfg.promote.mode not in ("off", "manual", "auto"):
        raise ValueError("promote.mode must be off, manual or auto")
    return cfg


# ----------------------------------------------------------------- dates
def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def prior_month_end(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def default_reporting_month(today: Optional[date] = None) -> date:
    """Last day of the previous calendar month."""
    return prior_month_end(today or date.today())


def parse_month(value: str) -> date:
    from datetime import datetime
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}", value):
        y, m = map(int, value.split("-"))
        return month_end(date(y, m, 1))
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("month must look like 2026-03-31 or 2026-03")


def name_for_month(pattern: str, d: date) -> str:
    return pattern.format(yyyy=f"{d.year:04d}", mm=f"{d.month:02d}", dd=f"{d.day:02d}")
