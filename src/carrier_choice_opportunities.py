"""Build fast, consignment-level carrier-choice opportunity views."""

from __future__ import annotations

import pandas as pd

from src.carrier_rules import CARRIER_ELIGIBILITY_LIMITS


SUMMARY_COLUMNS = (
    "ui_site",
    "current_carrier",
    "next_best_carrier",
    "ap_pricing_zone",
    "opportunity_bucket",
    "eligibility_reason",
    "eligible_consignments",
    "eligible_parcels",
    "eligible_units",
    "current_modelled_cost_ex_gst",
    "next_best_modelled_cost_ex_gst",
    "current_average_cost_per_consignment_ex_gst",
    "next_best_average_cost_per_consignment_ex_gst",
    "estimated_saving_ex_gst",
    "average_saving_per_consignment_ex_gst",
    "estimated_saving_pct",
)


DETAIL_COLUMNS = (
    "opportunity_id",
    "ui_site",
    "invoice_number",
    "despatch_date",
    "kmart_accounting_year",
    "kmart_accounting_week_number_in_year",
    "current_carrier",
    "current_service",
    "next_best_carrier",
    "next_best_service",
    "ap_pricing_zone",
    "reporting_weight_band",
    "opportunity_bucket",
    "eligibility_reason",
    "article_count_basis",
    "opportunity_article_count",
    "opportunity_dead_weight_kg",
    "opportunity_volumetric_weight_kg",
    "actual_parcel_count",
    "parcel_units_total",
    "current_modelled_cost_ex_gst",
    "next_best_modelled_cost_ex_gst",
    "estimated_saving_ex_gst",
    "estimated_saving_pct",
)


PROXY_OPTION_TOKENS = ("gobundle",)


def _text_series(frame: pd.DataFrame, *columns: str) -> pd.Series:
    result = pd.Series("", index=frame.index, dtype="string")
    for column in columns:
        if column not in frame:
            continue
        values = frame[column].fillna("").astype(str).str.strip()
        result = result.mask(result.eq(""), values)
    return result


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").fillna(0.0)


def _optional_numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    """Read a numeric field without turning missing evidence into zero."""
    if column not in frame:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _canonical_carrier(value: object) -> str:
    text = str(value or "").strip().casefold()
    if "courier" in text:
        return "Couriers Please"
    if "gopeople" in text or "goexpress" in text or "gobundle" in text:
        return "GoPeople"
    if "australia post" in text or text in {"eparcel", "eparcel metro"}:
        return "Australia Post"
    if "allied" in text:
        return "Allied Express"
    return str(value or "").strip()


def _rule_limit_text(carrier: str, field: str, value: float | int) -> str:
    if field == "max_articles":
        return f"{carrier} maximum {int(value)} articles"
    if field == "max_dead_weight_kg":
        return f"{carrier} dead-weight limit {value:g} kg"
    return f"{carrier} volumetric-weight limit {value:g} kg"


def _classify_opportunity_rules(
    work: pd.DataFrame,
    next_carrier: pd.Series,
) -> pd.DataFrame:
    """Classify the persisted cheapest alternative without recalculating Stage 3.

    The fast screen uses the physical evidence already persisted in Stage 3.
    It intentionally does not claim that per-item or maximum-length limits have
    passed because those measurements are not currently aggregate Stage 3 fields.
    """
    estimated_articles = _optional_numeric_series(work, "estimated_parcel_count")
    actual_articles = _optional_numeric_series(work, "actual_parcel_count")
    article_count = estimated_articles.where(estimated_articles.notna(), actual_articles)
    dead_weight = _optional_numeric_series(work, "shippit_dead_weight_kg")
    volumetric_weight = _optional_numeric_series(work, "shippit_volumetric_weight_kg")
    evidence_columns_present = any(
        column in work
        for column in (
            "estimated_parcel_count",
            "actual_parcel_count",
            "shippit_dead_weight_kg",
            "shippit_volumetric_weight_kg",
        )
    )

    buckets: list[str] = []
    reasons: list[str] = []
    article_basis: list[str] = []
    for index in work.index:
        carrier = _canonical_carrier(next_carrier.loc[index])
        limits = CARRIER_ELIGIBILITY_LIMITS.get(carrier)
        if not evidence_columns_present:
            buckets.append("Realizable today")
            reasons.append("No physical-limit fields were supplied; legacy/test input")
            article_basis.append("Not available")
            continue
        if estimated_articles.loc[index] == estimated_articles.loc[index]:
            article_basis.append("Stage 3 estimated article count")
        elif actual_articles.loc[index] == actual_articles.loc[index]:
            article_basis.append("Stage 3 actual parcel count fallback")
        else:
            article_basis.append("Unavailable")

        if limits is None:
            buckets.append("Needs validation")
            reasons.append("Carrier eligibility limits are not configured in the fast screen")
            continue

        missing: list[str] = []
        blockers: list[str] = []
        checks = (
            ("max_articles", article_count.loc[index]),
            ("max_dead_weight_kg", dead_weight.loc[index]),
            ("max_volumetric_weight_kg", volumetric_weight.loc[index]),
        )
        for field, observed in checks:
            limit = limits[field]
            if observed != observed:
                missing.append(field.replace("_", " "))
            elif float(observed) > float(limit):
                blockers.append(_rule_limit_text(carrier, field, float(limit)))

        if blockers:
            buckets.append("Conditional if rule changed")
            reasons.append("; ".join(blockers))
        elif missing:
            buckets.append("Needs validation")
            reasons.append("Missing physical evidence: " + ", ".join(missing))
        else:
            buckets.append("Realizable today")
            reasons.append("Article, dead-weight and volumetric-weight limits passed")

    return pd.DataFrame(
        {
            "opportunity_bucket": buckets,
            "eligibility_reason": reasons,
            "article_count_basis": article_basis,
            "opportunity_article_count": article_count,
            "opportunity_dead_weight_kg": dead_weight,
            "opportunity_volumetric_weight_kg": volumetric_weight,
        },
        index=work.index,
    )


def _empty_detail() -> pd.DataFrame:
    return pd.DataFrame(columns=DETAIL_COLUMNS)


def build_carrier_choice_detail(frame: pd.DataFrame) -> pd.DataFrame:
    """Return one primary next-best option per Shippit consignment.

    Stage 3 has already applied carrier ratecard coverage, lane and weight
    validity checks. A row is a candidate when both current and alternative
    modelled costs are positive and the alternative is genuinely cheaper. The
    returned detail then separates candidates that pass known carrier limits
    from conditional or unvalidated candidates.
    """
    if frame.empty:
        return _empty_detail()

    work = frame.copy()
    invoice = _text_series(work, "invoice_number", "shippit_invoice_number")
    current_carrier = _text_series(work, "actual_carrier", "shippit_carrier")
    current_service = _text_series(work, "actual_service", "shippit_service")
    next_carrier = _text_series(work, "cheapest_alt_carrier")
    next_service = _text_series(work, "cheapest_alt_service")
    current_cost = _numeric_series(work, "actual_est_cost_ex_gst")
    next_cost = _numeric_series(work, "cheapest_alt_cost_ex_gst")
    saving = current_cost - next_cost

    option_text = (
        current_carrier
        .str.cat(current_service, sep=" ")
        .str.cat(next_carrier, sep=" ")
        .str.cat(next_service, sep=" ")
        .str.casefold()
    )
    proxy_option = option_text.str.contains(
        "|".join(PROXY_OPTION_TOKENS), regex=True, na=False
    )

    same_option = (
        current_carrier.str.casefold().eq(next_carrier.str.casefold())
        & current_service.str.casefold().eq(next_service.str.casefold())
    )
    eligible = (
        invoice.ne("")
        & current_carrier.ne("")
        & next_carrier.ne("")
        & next_service.ne("")
        & current_cost.gt(0)
        & next_cost.gt(0)
        & saving.gt(0.01)
        & ~same_option
        & ~proxy_option
    )
    if not eligible.any():
        return _empty_detail()

    rule_classification = _classify_opportunity_rules(work, next_carrier)

    detail = pd.DataFrame(index=work.index)
    detail["opportunity_id"] = (
        "CARRIER_CHOICE|"
        + _text_series(work, "ui_site").replace("", "ALL")
        + "|"
        + invoice
    )
    detail["ui_site"] = _text_series(work, "ui_site").replace("", "ALL")
    detail["invoice_number"] = invoice
    for column in (
        "despatch_date",
        "kmart_accounting_year",
        "kmart_accounting_week_number_in_year",
        "reporting_weight_band",
    ):
        detail[column] = work[column] if column in work else ""
    detail["current_carrier"] = current_carrier
    detail["current_service"] = current_service
    detail["next_best_carrier"] = next_carrier
    detail["next_best_service"] = next_service
    detail["ap_pricing_zone"] = _text_series(work, "ap_pricing_zone").replace(
        "", "Not mapped / not applicable"
    )
    for column in (
        "opportunity_bucket",
        "eligibility_reason",
        "article_count_basis",
        "opportunity_article_count",
        "opportunity_dead_weight_kg",
        "opportunity_volumetric_weight_kg",
    ):
        detail[column] = rule_classification[column]
    detail["actual_parcel_count"] = _numeric_series(work, "actual_parcel_count")
    detail["parcel_units_total"] = _numeric_series(work, "parcel_units_total")
    detail["current_modelled_cost_ex_gst"] = current_cost
    detail["next_best_modelled_cost_ex_gst"] = next_cost
    detail["estimated_saving_ex_gst"] = saving
    detail["estimated_saving_pct"] = saving.div(current_cost).mul(100).where(current_cost.gt(0))

    detail = detail.loc[eligible, list(DETAIL_COLUMNS)].copy()
    # Stage 3 is expected to be one row per consignment. This guard prevents
    # duplicate rows from inflating opportunity totals if an older export is not.
    detail = detail.sort_values(
        ["estimated_saving_ex_gst", "opportunity_id"], ascending=[False, True]
    ).drop_duplicates("opportunity_id", keep="first")
    return detail.reset_index(drop=True)


def build_carrier_choice_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Aggregate eligible carrier-choice opportunities for dashboard display."""
    if detail.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    detail = detail.copy()
    if "opportunity_bucket" not in detail:
        detail["opportunity_bucket"] = "Realizable today"
    if "eligibility_reason" not in detail:
        detail["eligibility_reason"] = ""
    group_columns = (
        "ui_site",
        "current_carrier",
        "next_best_carrier",
        "ap_pricing_zone",
        "opportunity_bucket",
        "eligibility_reason",
    )
    summary = (
        detail.groupby(list(group_columns), dropna=False, as_index=False)
        .agg(
            eligible_consignments=("opportunity_id", "nunique"),
            eligible_parcels=("actual_parcel_count", "sum"),
            eligible_units=("parcel_units_total", "sum"),
            current_modelled_cost_ex_gst=("current_modelled_cost_ex_gst", "sum"),
            next_best_modelled_cost_ex_gst=("next_best_modelled_cost_ex_gst", "sum"),
            estimated_saving_ex_gst=("estimated_saving_ex_gst", "sum"),
        )
    )
    summary["average_saving_per_consignment_ex_gst"] = summary[
        "estimated_saving_ex_gst"
    ].div(summary["eligible_consignments"].replace(0, pd.NA))
    summary["current_average_cost_per_consignment_ex_gst"] = summary[
        "current_modelled_cost_ex_gst"
    ].div(summary["eligible_consignments"].replace(0, pd.NA))
    summary["next_best_average_cost_per_consignment_ex_gst"] = summary[
        "next_best_modelled_cost_ex_gst"
    ].div(summary["eligible_consignments"].replace(0, pd.NA))
    summary["estimated_saving_pct"] = summary[
        "estimated_saving_ex_gst"
    ].div(summary["current_modelled_cost_ex_gst"].replace(0, pd.NA)).mul(100)
    return summary.loc[:, list(SUMMARY_COLUMNS)].sort_values(
        ["estimated_saving_ex_gst", "eligible_consignments"], ascending=[False, False]
    ).reset_index(drop=True)
