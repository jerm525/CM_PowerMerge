"""Small synthetic PM / CM data and a test config that uses header names (no letters)."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from cm_power_merge.config import AppConfig, DerivedSpec

MONTH = date(2026, 9, 30)


def make_config() -> AppConfig:
    cfg = AppConfig()
    cfg.rules.san_key_index = -1
    ex = cfg.excel
    ex.strict_layout = True
    ex.sum_columns = ["Bill Qty", "Monthly Amount", "Gross Power MRR", "Adjusted kW",
                      "PM MRR", "PM MRR Var", "PM Adjusted kW", "PM Adjusted kW Var", "Cabinet Count"]
    ex.subtotal_label_column = "Customer Name"
    ex.subtotal_total_columns = ["Data Center"]
    ex.grand_total_columns = ["Customer Number", "Customer Name", "Data Center"]
    ex.copy_down_columns = ["Customer Number", "Customer Has Cabinet"]
    ex.derived = [
        DerivedSpec("PM MRR Var", "diff", "Gross Power MRR", "PM MRR"),
        DerivedSpec("PM Adjusted kW Var", "diff", "Adjusted kW", "PM Adjusted kW"),
        DerivedSpec("COLA/Price Increase Check", "ratio", "PM MRR Var", "PM MRR"),
        DerivedSpec("Adj $/kW", "ratio", "Adjusted Power MRR", "Adjusted kW"),
    ]
    return cfg


PM_COLS = ["Reporting Month", "Customer Number", "Customer Name", "Data Center", "Asset Sales Order",
           "Service Asset Number", "Base SKU", "Commercial Model Helper", "Commercial Model", "Bill Qty",
           "Monthly Amount", "Gross Power MRR", "$/kW", "Per-kW $/kW Margin", "Circuit kW",
           "Final kW for $/kW", "Adjusted kW", "Adjusted Power MRR", "Adjusted kW Source", "Adj $/kW",
           "F2", "F3", "Item Full Name", "Asset Notes", "Sales Description"]


def _pm_row(**kw):
    row = {c: np.nan for c in PM_COLS}
    row.update(kw)
    return row


def make_pm() -> pd.DataFrame:
    rows = [
        _pm_row(**{"Customer Number": 100, "Customer Name": "Acme", "Data Center": "DC1", "Asset Sales Order": "SO1",
                   "Service Asset Number": "S1", "Base SKU": "PWR-CIRC", "Commercial Model Helper": "Circuit",
                   "Commercial Model": "Circuit", "Bill Qty": 2, "Monthly Amount": 1000, "Gross Power MRR": 1000,
                   "Final kW for $/kW": 10, "Adjusted kW": 10, "Adjusted Power MRR": 1000}),
        _pm_row(**{"Customer Number": 100, "Customer Name": "Acme", "Data Center": "DC1", "Asset Sales Order": "SO1",
                   "Service Asset Number": "S2", "Base SKU": "SPACE-1", "Commercial Model Helper": "Space",
                   "Commercial Model": "Circuit", "Bill Qty": 1, "Monthly Amount": 200, "Gross Power MRR": 200,
                   "Adjusted kW": 99.0, "Adjusted Power MRR": 200}),          # stale kW on an exempt row
        _pm_row(**{"Customer Number": 200, "Customer Name": "Beta", "Data Center": "DC1", "Asset Sales Order": "SO2",
                   "Service Asset Number": "S3", "Base SKU": "PWR-MTR", "Commercial Model Helper": "Metered",
                   "Commercial Model": "+E", "Bill Qty": 5, "Monthly Amount": 500, "Gross Power MRR": 500,
                   "Adjusted kW": 5, "Adjusted Power MRR": 500}),
        _pm_row(**{"Customer Number": 200, "Customer Name": "Beta", "Data Center": "DC1", "Asset Sales Order": "SO2",
                   "Service Asset Number": "S4", "Base SKU": "COLO-1U-TRS", "Commercial Model Helper": "Other Colo",
                   "Commercial Model": "+E", "Bill Qty": 1, "Monthly Amount": 50, "Gross Power MRR": 50,
                   "Adjusted kW": 0.44, "Adjusted Power MRR": 50}),
        _pm_row(**{"Customer Number": 300, "Customer Name": "Gamma", "Data Center": "DC2", "Asset Sales Order": "SO3",
                   "Service Asset Number": "S5", "Base SKU": "PWR-CIRC", "Commercial Model Helper": "Circuit",
                   "Commercial Model": "Circuit", "Bill Qty": 1, "Monthly Amount": 300, "Gross Power MRR": 300,
                   "Adjusted kW": 3, "Adjusted Power MRR": 300}),                # churned
        _pm_row(**{"Customer Number": 300, "Customer Name": "Gamma", "Data Center": "DC2", "Asset Sales Order": "SO3",
                   "Service Asset Number": "S6", "Base SKU": "PWR-PKW", "Commercial Model Helper": "Per kW",
                   "Commercial Model": "Per-kW", "Bill Qty": 4, "Monthly Amount": 800, "Gross Power MRR": 800,
                   "Adjusted kW": 4, "Adjusted Power MRR": 800}),
    ]
    df = pd.DataFrame(rows, columns=PM_COLS)
    df["Reporting Month"] = "8/31/2026"
    # artifacts that a prior output contains and must be stripped
    sub = {c: np.nan for c in PM_COLS}
    sub.update({"Customer Name": "zz Total", "Data Center": "Total", "Gross Power MRR": 1234})
    tot = {c: np.nan for c in PM_COLS}
    tot.update({"Customer Number": "Total", "Customer Name": "Total", "Gross Power MRR": 99999})
    blank = {c: np.nan for c in PM_COLS}
    blank["Reporting Month"] = "8/31/2026"
    return pd.concat([df, pd.DataFrame([sub, blank, tot])], ignore_index=True)


CM_COLS = ["Service Asset Number", "Customer Number", "Customer Name", "Data Center", "Asset Sales Order",
           "Base SKU", "Bill Qty", "Monthly Amount", "Gross Power MRR", "$/kW", "Per-kW $/kW Margin",
           "Circuit kW", "F2", "F3", "Brand New Column"]


def _cm_row(**kw):
    row = {c: np.nan for c in CM_COLS}
    row.update(kw)
    return row


def make_cm() -> pd.DataFrame:
    rows = [
        _cm_row(**{"Service Asset Number": "S1", "Customer Number": 100, "Customer Name": "Acme", "Data Center": "DC1",
                   "Asset Sales Order": "SO1", "Base SKU": "PWR-CIRC", "Bill Qty": 2, "Monthly Amount": 1100,
                   "Gross Power MRR": 1100, "$/kW": 110}),
        _cm_row(**{"Service Asset Number": "S2", "Customer Number": 100, "Customer Name": "Acme", "Data Center": "DC1",
                   "Asset Sales Order": "SO1", "Base SKU": "SPACE-1", "Bill Qty": 1, "Monthly Amount": 200,
                   "Gross Power MRR": 200}),
        _cm_row(**{"Service Asset Number": "S3", "Customer Number": 200, "Customer Name": "Beta", "Data Center": "DC1",
                   "Asset Sales Order": "SO2", "Base SKU": "PWR-MTR", "Bill Qty": 6, "Monthly Amount": 600,
                   "Gross Power MRR": 600}),
        _cm_row(**{"Service Asset Number": "S4", "Customer Number": 200, "Customer Name": "Beta", "Data Center": "DC1",
                   "Asset Sales Order": "SO2", "Base SKU": "COLO-1U-TRS", "Bill Qty": 1, "Monthly Amount": 50,
                   "Gross Power MRR": 50}),
        _cm_row(**{"Service Asset Number": "S6", "Customer Number": 300, "Customer Name": "Gamma", "Data Center": "DC2",
                   "Asset Sales Order": "SO3", "Base SKU": "PWR-PKW", "Bill Qty": 4, "Monthly Amount": 800,
                   "Gross Power MRR": 800}),
        # new assets
        _cm_row(**{"Service Asset Number": "S7", "Customer Number": 400, "Customer Name": "Delta", "Data Center": "DC3",
                   "Asset Sales Order": "SO7", "Base SKU": "PWR-CIRC", "Bill Qty": 2, "Monthly Amount": 700,
                   "Gross Power MRR": 700, "Circuit kW": 5, "Brand New Column": "x"}),
        _cm_row(**{"Service Asset Number": "S8", "Customer Number": 100, "Customer Name": "Acme", "Data Center": "DC1",
                   "Asset Sales Order": "SO1", "Base SKU": "CAB-42U", "Bill Qty": 1, "Monthly Amount": 150,
                   "Gross Power MRR": 150}),
    ]
    return pd.DataFrame(rows, columns=CM_COLS)


def make_ref() -> pd.DataFrame:
    return pd.DataFrame({
        "Base SKU": ["PWR-CIRC", "CAB-42U", "PWR-CIRC"],            # duplicate key on purpose
        "Commercial Model Helper": ["Circuit", "Space", "Metered"],
        "F2": [None, None, None], "F3": [None, None, None],
    })
