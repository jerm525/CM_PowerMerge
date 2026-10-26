"""Commercial Model for newly added assets.

Python port of the Customer Commercial Model pivot/lookup (Colo kW Audit AL).
Only blank Commercial Model cells on newly added (f2_add) rows are filled;
prior-period values are retained exactly as they came from the PM workbook.

Pivot semantics reproduced here:
  * grouping: customer number/name, data center, asset sales order, blank2
  * only groups containing a power-related helper are in the pivot
  * initial model (Excel AK) per group, then the model with most adjusted kW
    for the customer/data center wins when AK returns N/A
  * lookup priority: sales order + DC, then customer + DC, then customer max,
    then Circuit (the workbook's zero-kW tie result)
  * ties follow workbook order: Circuit, Per-kW, +E
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .columns import ensure_object, normalize_key_series, to_number
from .logging_setup import get_logger

log = get_logger("commercial_model")

NON_POWER_HELPERS = {"space", "pdu", "other colo", "customer hardware", ""}
BUCKET_MAP = {
    "bundled colocation": "bundled", "circuit": "circuit", "circuits": "circuit",
    "custom power": "custom", "infrastructure": "infrastructure", "metered": "metered",
    "per kw": "per_kw", "per kwh": "per_kw", "pkw": "pkw",
}


def assign_commercial_model_for_new_rows(merged: pd.DataFrame) -> pd.DataFrame:
    if "_SOURCE_" not in merged.columns:
        raise ValueError("_SOURCE_ is required to identify new asset rows")
    merged = merged.copy()
    new_rows = merged["_SOURCE_"].eq("f2_add")
    if not new_rows.any():
        log.info("Commercial Model: no new assets, nothing to assign")
        return merged

    required = ["Customer Number", "Customer Name", "Data Center", "Asset Sales Order",
                "Commercial Model Helper", "Adjusted kW"]
    missing = [c for c in required if c not in merged.columns]
    if missing:
        raise ValueError(f"Cannot calculate Commercial Model; missing columns: {missing}")
    mrr_col = "Adjusted Power MRR" if "Adjusted Power MRR" in merged.columns else "Gross Power MRR"
    if mrr_col not in merged.columns:
        raise ValueError("Cannot calculate Commercial Model; power MRR is missing")
    if "Commercial Model" not in merged.columns:
        raise ValueError("Cannot calculate Commercial Model; prior PM extract lacks the Commercial Model column")

    ensure_object(merged, "Commercial Model")
    existing_model = merged["Commercial Model"].fillna("").astype(str).str.strip()
    needs_model = new_rows & existing_model.eq("")
    if not needs_model.any():
        return merged

    def text_key(column: str) -> pd.Series:
        return merged[column].fillna("").astype(str).str.strip()

    keys = pd.DataFrame({
        "customer_number": normalize_key_series(merged["Customer Number"]).fillna(""),
        "customer_name": text_key("Customer Name"),
        "data_center": text_key("Data Center"),
        "sales_order": text_key("Asset Sales Order"),
        "blank2": text_key("blank2") if "blank2" in merged.columns else pd.Series("", index=merged.index),
    }, index=merged.index)
    group_codes, _ = pd.factorize(pd.MultiIndex.from_frame(keys), sort=False)

    helper = (text_key("Commercial Model Helper").str.casefold()
              .str.replace(r"[-_]+", " ", regex=True).str.replace(r"\s+", " ", regex=True))
    buckets = helper.map(BUCKET_MAP).fillna("other")
    unknown = helper[buckets.eq("other") & ~helper.isin(NON_POWER_HELPERS)]
    if len(unknown):
        log.warning("Commercial Model: %s rows have an unrecognised helper and are ignored by the pivot: %s",
                    len(unknown), unknown.value_counts().head(10).to_dict())

    amounts = pd.DataFrame({
        "model_group": group_codes,
        "bucket": buckets.to_numpy(),
        "mrr": to_number(merged[mrr_col]).fillna(0.0).to_numpy(),
        "kw": to_number(merged["Adjusted kW"]).fillna(0.0).to_numpy(),
    })
    group_ids = pd.Index(amounts.loc[amounts["bucket"].ne("other"), "model_group"].unique(),
                         name="model_group")
    if group_ids.empty:
        merged.loc[needs_model, "Commercial Model"] = "Circuit"
        log.warning("Commercial Model: no power-model groups; assigned Circuit to %s new rows",
                    int(needs_model.sum()))
        return merged
    by_bucket = (amounts.groupby(["model_group", "bucket"], sort=False)[["mrr", "kw"]]
                 .sum().unstack(fill_value=0.0))

    def amount(field: str, bucket: str) -> pd.Series:
        column = (field, bucket)
        if column in by_bucket.columns:
            return by_bucket[column].reindex(group_ids, fill_value=0.0)
        return pd.Series(0.0, index=group_ids)

    bundled_mrr = amount("mrr", "bundled")
    circuit_mrr = amount("mrr", "circuit")
    custom_mrr = amount("mrr", "custom")
    infrastructure_mrr = amount("mrr", "infrastructure")
    metered_mrr = amount("mrr", "metered")
    per_kw_mrr = amount("mrr", "per_kw")
    pkw_mrr = amount("mrr", "pkw")
    bundled_kw = amount("kw", "bundled")
    totals = (bundled_mrr + circuit_mrr + custom_mrr + infrastructure_mrr
              + metered_mrr + per_kw_mrr + pkw_mrr)

    allocated_kw = sum(
        amount("kw", bucket).where(amount("mrr", bucket) + bundled_mrr > 0, 0.0)
        for bucket in ("circuit", "custom", "infrastructure", "metered", "per_kw", "pkw"))
    rate = pd.Series(np.divide(totals.to_numpy(), allocated_kw.to_numpy(),
                               out=np.zeros(len(group_ids), dtype=float),
                               where=allocated_kw.to_numpy() != 0), index=group_ids)
    pkw_present = (amounts["bucket"].eq("pkw").groupby(amounts["model_group"]).sum()
                   .reindex(group_ids, fill_value=0).gt(0))

    initial = pd.Series(np.select([
        totals.eq(0) & pkw_present & pkw_mrr.eq(0),
        totals.eq(0) & (bundled_mrr + circuit_mrr).eq(0),
        (bundled_mrr + circuit_mrr).gt(0) & per_kw_mrr.eq(0),
        per_kw_mrr.eq(0)
        & (custom_mrr + infrastructure_mrr + metered_mrr + per_kw_mrr + pkw_mrr + bundled_kw).gt(0)
        & (rate.lt(150) | infrastructure_mrr.gt(0)) & allocated_kw.ne(0),
        per_kw_mrr.gt(0) | rate.ge(150),
    ], ["Per-kW", "Circuit", "Circuit", "+E", "Per-kW"], default="N/A"), index=group_ids)

    group_keys = (keys.assign(model_group=group_codes).drop_duplicates("model_group")
                  .set_index("model_group").reindex(group_ids))
    customer_key = group_keys["customer_number"].where(group_keys["customer_number"].ne(""),
                                                       "name:" + group_keys["customer_name"])
    scores = pd.DataFrame({
        "customer": customer_key.to_numpy(),
        "data_center": group_keys["data_center"].to_numpy(),
        "initial": initial.reindex(group_keys.index).to_numpy(),
        "kw": allocated_kw.reindex(group_keys.index).to_numpy(),
    })
    order = ["Circuit", "Per-kW", "+E"]
    dc_scores = (scores.groupby(["customer", "data_center", "initial"], sort=False)["kw"].sum()
                 .unstack(fill_value=0.0).reindex(columns=order, fill_value=0.0))
    dc_winner = dc_scores.idxmax(axis=1)
    customer_scores = (scores.groupby(["customer", "initial"], sort=False)["kw"].sum()
                       .unstack(fill_value=0.0).reindex(columns=order, fill_value=0.0))
    customer_winner = customer_scores.idxmax(axis=1)
    dc_keys = pd.MultiIndex.from_arrays([customer_key.to_numpy(), group_keys["data_center"].to_numpy()])
    fallback = dc_winner.reindex(dc_keys).to_numpy()
    final_model = pd.Series(np.where(initial.reindex(group_keys.index).eq("N/A"),
                                     fallback, initial.reindex(group_keys.index)),
                            index=group_keys.index)

    lookup = group_keys.assign(final_model=final_model).sort_values(
        ["customer_number", "customer_name", "data_center", "sales_order", "blank2"], kind="mergesort")
    so_lookup = (lookup.loc[lookup["sales_order"].ne("")]
                 .drop_duplicates(["sales_order", "data_center"])
                 .set_index(["sales_order", "data_center"])["final_model"])
    cust_lookup = (lookup.drop_duplicates(["customer_number", "data_center"])
                   .set_index(["customer_number", "data_center"])["final_model"])
    new_keys = keys.loc[needs_model]
    so_keys = pd.MultiIndex.from_frame(new_keys[["sales_order", "data_center"]])
    cust_keys = pd.MultiIndex.from_frame(new_keys[["customer_number", "data_center"]])
    by_order = so_lookup.reindex(so_keys).to_numpy()
    by_customer = cust_lookup.reindex(cust_keys).to_numpy()
    new_customer_key = new_keys["customer_number"].where(new_keys["customer_number"].ne(""),
                                                         "name:" + new_keys["customer_name"])
    by_customer_max = customer_winner.reindex(new_customer_key).to_numpy()
    selected = np.where(pd.notna(by_order), by_order,
                        np.where(pd.notna(by_customer), by_customer, by_customer_max)).astype(object)
    zero_kw = pd.isna(selected)
    if zero_kw.any():
        selected[zero_kw] = "Circuit"
        log.warning("Commercial Model: %s new asset(s) had no power-model match; assigned Circuit "
                    "(zero-kW max rule)", int(zero_kw.sum()))

    ambiguous = (lookup.loc[lookup["sales_order"].ne("")].groupby(["sales_order", "data_center"])["final_model"]
                 .nunique().gt(1))
    review = int(ambiguous.reindex(so_keys, fill_value=False).sum())
    if review:
        log.warning("Commercial Model: %s new assets have a sales order/DC with conflicting pivot models; "
                    "used the first sorted group", review)

    merged.loc[needs_model, "Commercial Model"] = selected
    log.info("Commercial Model: filled %s new rows (%s); kept %s existing rows",
             f"{int(needs_model.sum()):,}", pd.Series(selected).value_counts().to_dict(),
             f"{int((~needs_model).sum()):,}")
    return merged
