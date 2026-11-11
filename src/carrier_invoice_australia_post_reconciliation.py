"""Reconcile Australia Post eParcel charges against Stage 3 model outputs."""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


INPUT_PATH = Path("outputs/carrier_invoices/australia_post_consignments.csv")
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
STAGE3_PATHS = {
    "MOOREBANK": Path("outputs/stage3_moorebank_cost_estimates_full.csv"),
    "TRUGANINA": Path("outputs/stage3_truganina_cost_estimates_full.csv"),
}
MIN_FUEL_NORM_SAMPLE_COUNT = 30
DIMENSION_ALIGNMENT_THRESHOLD_PCT = 15.0
DIMENSION_MATERIAL_THRESHOLD_PCT = 25.0
WEIGHT_MATERIAL_THRESHOLD_KG = 0.5


@dataclass(frozen=True)
class FuelNorm:
    sample_count: int
    p50_pct: float
    source: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consignments", type=Path, default=INPUT_PATH)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument("--moorebank-stage3", type=Path, default=STAGE3_PATHS["MOOREBANK"])
    parser.add_argument("--truganina-stage3", type=Path, default=STAGE3_PATHS["TRUGANINA"])
    args = parser.parse_args()
    stage3_paths = {"MOOREBANK": args.moorebank_stage3, "TRUGANINA": args.truganina_stage3}
    missing = [path for path in [args.consignments, *stage3_paths.values()] if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing input files: " + ", ".join(str(path) for path in missing))

    args.output_directory.mkdir(parents=True, exist_ok=True)
    consignments = list(_read_csv(args.consignments))
    norms, fallback_norm = _build_fuel_norms(consignments, args.output_directory / "australia_post_fuel_norms.csv")
    by_tracking, by_invoice, by_parent_order = _load_stage3(stage3_paths)
    detail = [
        _build_result(actual, *_find_stage3_match(actual, by_tracking, by_invoice, by_parent_order), norms, fallback_norm)
        for actual in consignments
    ]
    _write_csv(args.output_directory / "australia_post_reconciliation.csv", _detail_headers(), detail)
    _write_summary(args.output_directory / "australia_post_reconciliation_summary.csv", detail)
    _write_dimension_diagnostics(args.output_directory / "australia_post_dimension_diagnostics_summary.csv", detail)
    _write_cost_bridge(args.output_directory / "australia_post_reconciliation_cost_bridge.csv", detail)
    print(f"Wrote {args.output_directory / 'australia_post_fuel_norms.csv'}")
    print(f"Wrote {args.output_directory / 'australia_post_reconciliation.csv'}")
    print(f"Wrote {args.output_directory / 'australia_post_reconciliation_summary.csv'}")
    print(f"Wrote {args.output_directory / 'australia_post_dimension_diagnostics_summary.csv'}")
    print(f"Wrote {args.output_directory / 'australia_post_reconciliation_cost_bridge.csv'}")


def _build_fuel_norms(rows: list[dict[str, str]], path: Path) -> tuple[dict[tuple[str, ...], FuelNorm], FuelNorm]:
    samples: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for row in rows:
        if row.get("owner_type") != "TRACKING_NUMBER":
            continue
        base = _number(row.get("actual_base_cost_ex_gst"))
        if base <= 0:
            continue
        fuel_pct = _number(row.get("actual_fuel_surcharge_ex_gst")) / base * 100.0
        service = _service_bucket(row)
        origin_zone = _normalise(row.get("lodgement_zone_pattern"))
        destination_zone = _normalise(row.get("destination_zone_pattern"))
        service_date = _normalise(row.get("service_date"))
        samples[("LANE_DATE", service, origin_zone, destination_zone, service_date)].append(fuel_pct)
        samples[("DATE", service, "", "", service_date)].append(fuel_pct)
        samples[("LANE", service, origin_zone, destination_zone, "")].append(fuel_pct)
        samples[("GLOBAL", service, "", "", "")].append(fuel_pct)

    norms = {key: _fuel_norm(values, _norm_source(key[0])) for key, values in samples.items()}
    fallback = norms.get(("GLOBAL", "AP_DOMESTIC", "", "", ""), FuelNorm(0, 0.0, "NO_FUEL_NORM"))
    rows_out = []
    for (level, service, origin_zone, destination_zone, service_date), norm in sorted(norms.items()):
        rows_out.append(
            {
                "norm_level": level,
                "service_material": service,
                "lodgement_zone": origin_zone,
                "destination_zone": destination_zone,
                "service_date": service_date,
                "sample_count": str(norm.sample_count),
                "p50_fuel_surcharge_pct": _fmt(norm.p50_pct),
                "norm_source": norm.source,
            }
        )
    _write_csv(path, ["norm_level", "service_material", "lodgement_zone", "destination_zone", "service_date", "sample_count", "p50_fuel_surcharge_pct", "norm_source"], rows_out)
    return norms, fallback


def _fuel_norm(samples: list[float], source: str) -> FuelNorm:
    return FuelNorm(len(samples), _percentile(samples, 0.5), source) if samples else FuelNorm(0, 0.0, "NO_FUEL_NORM")


def _select_fuel_norm(row: dict[str, str], norms: dict[tuple[str, ...], FuelNorm], fallback: FuelNorm) -> FuelNorm:
    service = _service_bucket(row)
    origin_zone = _normalise(row.get("lodgement_zone_pattern"))
    destination_zone = _normalise(row.get("destination_zone_pattern"))
    service_date = _normalise(row.get("service_date"))
    candidates = (
        ("LANE_DATE", service, origin_zone, destination_zone, service_date),
        ("DATE", service, "", "", service_date),
        ("LANE", service, origin_zone, destination_zone, ""),
        ("GLOBAL", service, "", "", ""),
    )
    for key in candidates:
        norm = norms.get(key)
        if norm and norm.sample_count >= MIN_FUEL_NORM_SAMPLE_COUNT:
            return norm
    return fallback


def _load_stage3(paths: dict[str, Path]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    by_tracking: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_invoice: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_parent_order: dict[str, list[dict[str, str]]] = defaultdict(list)
    accepted = {"EPARCEL", "EPARCEL METRO", "AUSTRALIA POST", "AUSTRALIA POST EPARCEL", "AUSTRALIA POST / EPARCEL"}
    for site, path in paths.items():
        for row in _read_csv(path):
            carrier = _normalise(row.get("actual_carrier"))
            service = _normalise(row.get("actual_service"))
            if carrier not in accepted and service not in accepted and not service.startswith("EPARCEL"):
                continue
            row["site"] = site
            if tracking := _normalise(row.get("carrier_tracking_number")):
                by_tracking[tracking].append(row)
            if invoice := _normalise(row.get("invoice_number")):
                by_invoice[invoice].append(row)
                if parent := _parent_order_id(invoice):
                    by_parent_order[parent].append(row)
    return by_tracking, by_invoice, by_parent_order


def _find_stage3_match(
    actual: dict[str, str],
    by_tracking: dict[str, list[dict[str, str]]],
    by_invoice: dict[str, list[dict[str, str]]],
    by_parent_order: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, str] | None, str]:
    tracking = _normalise(actual.get("carrier_tracking_number"))
    candidates = by_tracking.get(tracking, []) if tracking else []
    candidates = _filter_candidates(actual, candidates)
    if len(candidates) == 1:
        return candidates[0], "TRACKING_NUMBER"
    if len(candidates) > 1:
        return None, "AMBIGUOUS_TRACKING_NUMBER"

    for reference in (actual.get("customer_ref_1"), actual.get("sales_order"), actual.get("customer_ref_2")):
        order_id = _extract_order_id(reference)
        if not order_id:
            continue
        exact = _filter_candidates(actual, by_invoice.get(order_id, []))
        if len(exact) == 1:
            return exact[0], "KMART_INVOICE_NUMBER"
        if len(exact) > 1:
            return None, "AMBIGUOUS_KMART_INVOICE_NUMBER"
        parent = _filter_candidates(actual, by_parent_order.get(order_id, []))
        if len(parent) == 1:
            return parent[0], "PARENT_ORDER_ID"
        if len(parent) > 1:
            return None, "AMBIGUOUS_PARENT_ORDER_ID"
    return None, "UNMATCHED"


def _filter_candidates(actual: dict[str, str], candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(candidates) <= 1:
        return candidates
    service_date = _normalise(actual.get("service_date"))
    postcode = _digits(actual.get("destination_postcode"))
    filtered = [
        row
        for row in candidates
        if (not service_date or _normalise(row.get("despatch_date")) == service_date)
        and (not postcode or _digits(row.get("dest_postcode")) == postcode)
    ]
    return filtered or candidates


def _build_result(
    actual: dict[str, str],
    model: dict[str, str] | None,
    match_method: str,
    norms: dict[tuple[str, ...], FuelNorm],
    fallback_norm: FuelNorm,
) -> dict[str, str]:
    model = model or {}
    actual_base = _number(actual.get("actual_base_cost_ex_gst"))
    actual_fuel = _number(actual.get("actual_fuel_surcharge_ex_gst"))
    actual_other = _number(actual.get("actual_other_surcharge_ex_gst"))
    actual_total = _number(actual.get("actual_invoice_cost_ex_gst"))
    model_base = _model_base_cost(model)
    configured_fuel_norm = _configured_fuel_norm(model)
    fuel_norm = configured_fuel_norm or _select_fuel_norm(actual, norms, fallback_norm)
    predicted_fuel = model_base * fuel_norm.p50_pct / 100.0 if model else 0.0
    model_total = model_base + predicted_fuel if model else 0.0
    actual_cube = _number(actual.get("billed_cube_cm3"))
    estimator_cube = _model_number(model, "est_parcel_cube_total_cm3", "est_total_parcel_cube_cm3")
    shippit_cube = _model_number(model, "shippit_chargeable_cube_cm3")
    actual_weight = _number(actual.get("actual_chargeable_weight_kg"))
    model_weight = _model_number(model, "cost_weight_basis_kg", "ap_adjusted_parcel_weight_total_kg")
    shippit_weight = _model_number(model, "shippit_chargeable_weight_kg")
    estimator_status, shippit_status = _dimension_statuses(actual_cube, estimator_cube, shippit_cube)
    zone_status = _zone_comparison(actual.get("destination_zone_pattern"), model.get("ap_zone"))
    primary, root_cause, note = _classify_root_cause(
        actual,
        model,
        match_method,
        actual_cube,
        estimator_cube,
        shippit_cube,
        actual_weight,
        model_weight,
        shippit_weight,
        actual_base,
        model_base,
        actual_fuel,
        predicted_fuel,
        actual_other,
        zone_status,
        estimator_status,
        shippit_status,
    )
    return {
        "source_table": actual.get("source_table", ""),
        "invoice_no": actual.get("invoice_no", ""),
        "billing_date": actual.get("billing_date", ""),
        "service_date": actual.get("service_date", ""),
        "owner_type": actual.get("owner_type", ""),
        "charge_link_method": actual.get("charge_link_method", ""),
        "assignment_no": actual.get("assignment_no", ""),
        "carrier_tracking_number": actual.get("carrier_tracking_number", ""),
        "assignment_consignment_count": actual.get("assignment_consignment_count", ""),
        "customer_ref_1": actual.get("customer_ref_1", ""),
        "sales_order": actual.get("sales_order", ""),
        "source_line_count": actual.get("source_line_count", ""),
        "material_pattern": actual.get("material_pattern", ""),
        "description_pattern": actual.get("description_pattern", ""),
        "charge_code_pattern": actual.get("charge_code_pattern", ""),
        "actual_origin_city": actual.get("origin_city", ""),
        "actual_origin_state": actual.get("origin_state", ""),
        "actual_origin_postcode": actual.get("origin_postcode", ""),
        "actual_destination_city": actual.get("destination_city", ""),
        "actual_destination_state": actual.get("destination_state", ""),
        "actual_destination_postcode": actual.get("destination_postcode", ""),
        "actual_lodgement_zone": actual.get("lodgement_zone_pattern", ""),
        "actual_destination_zone": actual.get("destination_zone_pattern", ""),
        "actual_service_material": _service_bucket(actual),
        "actual_weight_kg": actual.get("actual_weight_kg", ""),
        "actual_declared_weight_kg": actual.get("declared_weight_kg", ""),
        "actual_billed_weight_kg": actual.get("billed_weight_kg", ""),
        "actual_cubic_weight_kg": actual.get("cubic_weight_kg", ""),
        "actual_chargeable_weight_kg": actual.get("actual_chargeable_weight_kg", ""),
        "actual_rounded_chargeable_weight_kg": _fmt(math.ceil(max(actual_weight, 0.0))) if actual else "",
        "actual_billed_cube_cm3": _fmt(actual_cube),
        "actual_billed_cube_m3": _fmt_m3(actual_cube),
        "actual_declared_cube_cm3": actual.get("declared_cube_cm3", ""),
        "physical_article_row_count": actual.get("physical_article_row_count", ""),
        "physical_distinct_article_count": actual.get("physical_distinct_article_count", ""),
        "physical_article_duplicate_row_count": actual.get("physical_article_duplicate_row_count", ""),
        "physical_anonymous_row_count": actual.get("physical_anonymous_row_count", ""),
        "physical_aggregation_method": actual.get("physical_aggregation_method", ""),
        "physical_article_cube_sum_cm3": actual.get("physical_article_cube_sum_cm3", ""),
        "physical_article_cube_max_cm3": actual.get("physical_article_cube_max_cm3", ""),
        "physical_article_billed_weight_sum_kg": actual.get("physical_article_billed_weight_sum_kg", ""),
        "physical_article_billed_weight_max_kg": actual.get("physical_article_billed_weight_max_kg", ""),
        "physical_article_cubic_weight_sum_kg": actual.get("physical_article_cubic_weight_sum_kg", ""),
        "physical_article_cubic_weight_max_kg": actual.get("physical_article_cubic_weight_max_kg", ""),
        "actual_base_cost_ex_gst": actual.get("actual_base_cost_ex_gst", ""),
        "actual_fuel_surcharge_ex_gst": actual.get("actual_fuel_surcharge_ex_gst", ""),
        "fuel_surcharge_line_ex_gst": actual.get("fuel_surcharge_line_ex_gst", ""),
        "fuel_surcharge_field_ex_gst": actual.get("fuel_surcharge_field_ex_gst", ""),
        "fuel_allocation_status": actual.get("fuel_allocation_status", ""),
        "actual_other_surcharge_ex_gst": actual.get("actual_other_surcharge_ex_gst", ""),
        "actual_invoice_cost_ex_gst": actual.get("actual_invoice_cost_ex_gst", ""),
        "actual_fuel_rate_pct": actual.get("actual_fuel_rate_pct", ""),
        "actual_tax_amount": actual.get("actual_tax_amount", ""),
        "match_method": match_method,
        "site": model.get("site", ""),
        "invoice_number": model.get("invoice_number", ""),
        "stage3_tracking_number": model.get("carrier_tracking_number", ""),
        "stage3_despatch_date": model.get("despatch_date", ""),
        "kmart_accounting_year": model.get("kmart_accounting_year", ""),
        "kmart_accounting_week_number_in_year": model.get("kmart_accounting_week_number_in_year", ""),
        "stage3_service": model.get("actual_service", ""),
        "modelled_destination_postcode": model.get("dest_postcode", ""),
        "modelled_ap_zone": model.get("ap_zone", ""),
        "modelled_ap_pricing_zone": model.get("ap_pricing_zone", ""),
        "ap_zone_comparison": zone_status,
        "modelled_ap_ratecard_id": model.get("ap_pricing_ratecard_id", ""),
        "modelled_ap_chargeable_weight_kg": _fmt(model_weight),
        "modelled_ap_base_cost_ex_gst": _fmt(model_base),
        "estimator_parcel_cube_cm3": _fmt(estimator_cube),
        "shippit_chargeable_cube_cm3": _fmt(shippit_cube),
        "shippit_chargeable_cube_m3": _fmt_m3(shippit_cube),
        "shippit_chargeable_weight_kg": model.get("shippit_chargeable_weight_kg", ""),
        "fuel_norm_sample_count": "" if configured_fuel_norm else (str(fuel_norm.sample_count) if model else ""),
        "fuel_norm_p50_pct": _fmt(fuel_norm.p50_pct) if model else "",
        "fuel_norm_source": fuel_norm.source if model else "",
        "predicted_fuel_surcharge_ex_gst": _fmt(predicted_fuel),
        "modelled_ap_cost_including_fuel_ex_gst": _fmt(model_total),
        "actual_less_modelled_base_cost_ex_gst": _fmt(actual_base - model_base) if model else "",
        "actual_less_predicted_fuel_ex_gst": _fmt(actual_fuel - predicted_fuel) if model else "",
        "actual_less_modelled_other_surcharge_ex_gst": _fmt(actual_other) if model else "",
        "actual_less_modelled_cost_including_fuel_ex_gst": _fmt(actual_total - model_total) if model else "",
        "actual_less_modelled_cost_including_fuel_pct": _percentage(actual_total - model_total, model_total) if model else "",
        "actual_less_modelled_chargeable_weight_kg": _fmt(actual_weight - model_weight) if model else "",
        "actual_less_shippit_chargeable_weight_kg": _fmt(actual_weight - _number(model.get("shippit_chargeable_weight_kg"))) if model else "",
        "actual_less_estimator_cube_cm3": _fmt(actual_cube - estimator_cube) if model else "",
        "actual_less_shippit_cube_cm3": _fmt(actual_cube - shippit_cube) if model else "",
        "estimator_dimension_diagnostic": estimator_status,
        "shippit_dimension_diagnostic": shippit_status,
        "primary_cost_driver": primary,
        "root_cause": root_cause,
        "reconciliation_note": note,
    }


def _classify_root_cause(
    actual: dict[str, str], model: dict[str, str], match_method: str, actual_cube: float, estimator_cube: float,
    shippit_cube: float, actual_weight: float, model_weight: float, shippit_weight: float, actual_base: float, model_base: float,
    actual_fuel: float, predicted_fuel: float, actual_other: float, zone_status: str, estimator_status: str,
    shippit_status: str,
) -> tuple[str, str, str]:
    if not model or match_method.startswith("UNMATCHED") or match_method.startswith("AMBIGUOUS"):
        return "MATCHING_GAP", "MATCHING_GAP", "No unique Stage 3 match is available, so AP and model cost differences cannot yet be attributed."
    causes: list[str] = []
    notes: list[str] = []
    physical_causes: list[str] = []
    if shippit_status in {"SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER", "SHIPPIT_CUBE_OVERSTATED_VS_CARRIER"}:
        physical_causes.append(shippit_status.replace("_VS_CARRIER", ""))
        notes.append(
            f"AP billed cube {_fmt_m3(actual_cube)} m3 versus Shippit cube {_fmt_m3(shippit_cube)} m3 "
            f"(AP less Shippit {_fmt_m3(actual_cube - shippit_cube)} m3); Shippit is the current model scaling basis."
        )
    if zone_status == "COMPARABLE_MISMATCH" and abs(actual_base - model_base) > 0.05:
        causes.append("AP_DESTINATION_ZONE_MISMATCH")
        notes.append(f"AP invoiced destination zone {actual.get('destination_zone_pattern') or 'blank'} differs from model zone {model.get('ap_zone') or 'blank'}.")
    if "B5R" in _normalise(actual.get("material_pattern")):
        causes.append("AP_SERVICE_MISMATCH")
        notes.append("The AP line is an eParcel return-to-sender service and is not directly equivalent to the standard eParcel model service.")
    comparison_weight = shippit_weight or model_weight
    if actual_weight > 0 and comparison_weight > 0 and abs(actual_weight - comparison_weight) > WEIGHT_MATERIAL_THRESHOLD_KG:
        physical_causes.append("SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH")
        notes.append(
            f"AP chargeable weight {_fmt(actual_weight)} kg versus Shippit chargeable weight {_fmt(comparison_weight)} kg "
            f"(AP less Shippit {_fmt(actual_weight - comparison_weight)} kg)."
        )
    causes.extend(physical_causes)
    if abs(actual_fuel - predicted_fuel) > 0.05:
        causes.append("AP_FUEL_SURCHARGE_VARIANCE")
        notes.append(f"AP fuel ${actual_fuel:.2f} versus modelled fuel assumption ${predicted_fuel:.2f}; the fuel rate is an assumption, not an AP invoice rule.")
    if actual_other > 0.05:
        causes.append("AP_UNMODELLED_SURCHARGE")
        notes.append(f"AP other charges of ${actual_other:.2f} are not included in the current Stage 3 AP base model.")
    if abs(actual_base - model_base) > 0.05:
        causes.append("AP_RATE_OR_CHARGE_MISMATCH")
        notes.append(f"AP base ${actual_base:.2f} versus modelled AP base ${model_base:.2f} after stronger physical and service checks.")
    if not causes:
        causes.append("NO_MATERIAL_VARIANCE_DRIVER")
    priority = (
        "AP_DESTINATION_ZONE_MISMATCH", "AP_SERVICE_MISMATCH", "AP_FUEL_SURCHARGE_VARIANCE", "AP_UNMODELLED_SURCHARGE",
        "AP_RATE_OR_CHARGE_MISMATCH",
    )
    primary = "SHIPPIT_PHYSICAL_BASIS_MISMATCH" if physical_causes else next((cause for cause in priority if cause in causes), "NO_MATERIAL_VARIANCE_DRIVER")
    if estimator_status == "ESTIMATOR_CUBE_ALIGNS_WITH_CARRIER":
        notes.append("The independent parcel estimator cube aligns with the AP billed cube within the diagnostic tolerance.")
    return primary, " | ".join(causes), " ".join(notes) or "No material AP variance driver was identified."


def _dimension_statuses(actual_cube: float, estimator_cube: float, shippit_cube: float) -> tuple[str, str]:
    if actual_cube <= 0:
        return "CARRIER_CUBE_UNAVAILABLE", "CARRIER_CUBE_UNAVAILABLE"
    return _dimension_status(estimator_cube, actual_cube, "ESTIMATOR"), _dimension_status(shippit_cube, actual_cube, "SHIPPIT")


def _dimension_status(estimate: float, actual: float, label: str) -> str:
    if estimate <= 0:
        return f"{label}_CUBE_UNAVAILABLE"
    gap_pct = (estimate - actual) / actual * 100.0
    if abs(gap_pct) <= DIMENSION_ALIGNMENT_THRESHOLD_PCT:
        return f"{label}_CUBE_ALIGNS_WITH_CARRIER"
    if gap_pct <= -DIMENSION_MATERIAL_THRESHOLD_PCT:
        return f"{label}_CUBE_UNDERSTATED_VS_CARRIER"
    if gap_pct >= DIMENSION_MATERIAL_THRESHOLD_PCT:
        return f"{label}_CUBE_OVERSTATED_VS_CARRIER"
    return f"{label}_MODERATE_CARRIER_GAP"


def _zone_comparison(actual_zone: object, model_zone: object) -> str:
    actual_text = _normalise(actual_zone)
    model_text = _normalise(model_zone)
    if not actual_text or not model_text:
        return "NOT_AVAILABLE"
    if actual_text == model_text:
        return "COMPARABLE_MATCH"
    if _looks_like_zone_code(actual_text) != _looks_like_zone_code(model_text):
        return "NOT_COMPARABLE_SOURCE_CODE_VS_MODEL_AREA"
    if _looks_like_zone_code(actual_text) and _looks_like_zone_code(model_text):
        return "COMPARABLE_MISMATCH"
    return "NOT_COMPARABLE_SOURCE_CODE_VS_MODEL_AREA"


def _looks_like_zone_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{1,2}\d{1,2}", value))


def _write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str, str, str], dict[str, float | int]] = defaultdict(lambda: {"count": 0, "matched_count": 0, "actual_base": 0.0, "actual_fuel": 0.0, "actual_other": 0.0, "actual_total": 0.0, "model_base": 0.0, "model_fuel": 0.0, "model_total": 0.0, "estimator_cube": 0.0, "shippit_cube": 0.0})
    for row in rows:
        key = (row.get("invoice_no", ""), row.get("match_method", ""), row.get("site", "") or "Unmatched", row.get("primary_cost_driver", ""))
        group = groups[key]
        group["count"] += 1
        if _is_matched(row):
            group["matched_count"] += 1
        for target, column in (("actual_base", "actual_base_cost_ex_gst"), ("actual_fuel", "actual_fuel_surcharge_ex_gst"), ("actual_other", "actual_other_surcharge_ex_gst"), ("actual_total", "actual_invoice_cost_ex_gst"), ("model_base", "modelled_ap_base_cost_ex_gst"), ("model_fuel", "predicted_fuel_surcharge_ex_gst"), ("model_total", "modelled_ap_cost_including_fuel_ex_gst"), ("estimator_cube", "estimator_parcel_cube_cm3"), ("shippit_cube", "shippit_chargeable_cube_cm3")):
            group[target] += _number(row.get(column))
    output = []
    for (invoice_no, match_method, site, driver), group in sorted(groups.items()):
        variance = _fmt(group["actual_total"] - group["model_total"]) if group["matched_count"] else ""
        variance_pct = _percentage(group["actual_total"] - group["model_total"], group["model_total"]) if group["matched_count"] else ""
        output.append({"source_table": "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST", "invoice_no": invoice_no, "match_method": match_method, "site": site, "primary_cost_driver": driver, "billed_consignment_groups": str(group["count"]), "matched_consignment_groups": str(group["matched_count"]), "actual_base_cost_ex_gst": _fmt(group["actual_base"]), "actual_fuel_surcharge_ex_gst": _fmt(group["actual_fuel"]), "actual_other_surcharge_ex_gst": _fmt(group["actual_other"]), "actual_invoice_cost_ex_gst": _fmt(group["actual_total"]), "modelled_ap_base_cost_ex_gst": _fmt(group["model_base"]), "predicted_fuel_surcharge_ex_gst": _fmt(group["model_fuel"]), "modelled_ap_cost_including_fuel_ex_gst": _fmt(group["model_total"]), "actual_less_modelled_cost_including_fuel_ex_gst": variance, "actual_less_modelled_cost_pct": variance_pct, "estimated_cube_cm3": _fmt(group["estimator_cube"]), "shippit_cube_cm3": _fmt(group["shippit_cube"])} )
    _write_csv(path, _summary_headers(), output)


def _write_dimension_diagnostics(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str, str, str], dict[str, float | int]] = defaultdict(lambda: {"count": 0, "carrier": 0.0, "estimator": 0.0, "shippit": 0.0})
    for row in rows:
        if not _is_matched(row) or _number(row.get("actual_billed_cube_cm3")) <= 0:
            continue
        key = (row.get("invoice_no", ""), row.get("site", ""), row.get("estimator_dimension_diagnostic", ""), row.get("shippit_dimension_diagnostic", ""))
        group = groups[key]
        group["count"] += 1
        group["carrier"] += _number(row.get("actual_billed_cube_cm3"))
        group["estimator"] += _number(row.get("estimator_parcel_cube_cm3"))
        group["shippit"] += _number(row.get("shippit_chargeable_cube_cm3"))
    output = []
    for (invoice_no, site, estimator_status, shippit_status), group in sorted(groups.items()):
        action = _dimension_action(estimator_status, shippit_status)
        output.append({"invoice_no": invoice_no, "site": site, "estimator_dimension_diagnostic": estimator_status, "shippit_dimension_diagnostic": shippit_status, "recommended_action": action, "matched_consignment_groups": str(group["count"]), "ap_billed_cube_m3": _fmt_m3(group["carrier"]), "estimator_cube_m3": _fmt_m3(group["estimator"]), "shippit_cube_m3": _fmt_m3(group["shippit"]), "estimator_vs_ap_billed_cube_pct": _percentage(group["estimator"] - group["carrier"], group["carrier"]), "shippit_vs_ap_billed_cube_pct": _percentage(group["shippit"] - group["carrier"], group["carrier"])} )
    _write_csv(path, _dimension_headers(), output)


def _dimension_action(estimator_status: str, shippit_status: str) -> str:
    if shippit_status == "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER" and estimator_status == "ESTIMATOR_CUBE_ALIGNS_WITH_CARRIER":
        return "Prioritise Shippit/CFC dimensional hand-off review; the independent parcel estimator is supported by AP billed dimensions."
    if shippit_status == "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER":
        return "Investigate Shippit/CFC dimensions against AP billed dimensions and the independent parcel estimate."
    if estimator_status == "ESTIMATOR_CUBE_UNDERSTATED_VS_CARRIER":
        return "Review CFC SKU dimensions, parcelisation, and cube assumptions against AP billed dimensions."
    if estimator_status == "ESTIMATOR_CUBE_OVERSTATED_VS_CARRIER":
        return "Review parcel estimator rectangularisation and air assumptions."
    return "Monitor AP billed dimensions against Shippit and the independent parcel estimator."


def _write_cost_bridge(path: Path, rows: list[dict[str, str]]) -> None:
    scopes: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {"count": 0, "model_base": 0.0, "model_fuel": 0.0, "actual_base": 0.0, "actual_fuel": 0.0, "actual_other": 0.0, "carrier_cube": 0.0, "estimator_cube": 0.0, "shippit_cube": 0.0, "buckets": defaultdict(lambda: {"count": 0, "base": 0.0, "fuel": 0.0, "other": 0.0, "carrier_cube": 0.0, "estimator_cube": 0.0, "shippit_cube": 0.0})})
    for row in rows:
        if not _is_matched(row):
            continue
        key = (row.get("invoice_no", ""), row.get("site", ""))
        scope = scopes[key]
        scope["count"] += 1
        scope["model_base"] += _number(row.get("modelled_ap_base_cost_ex_gst")); scope["model_fuel"] += _number(row.get("predicted_fuel_surcharge_ex_gst"))
        scope["actual_base"] += _number(row.get("actual_base_cost_ex_gst")); scope["actual_fuel"] += _number(row.get("actual_fuel_surcharge_ex_gst")); scope["actual_other"] += _number(row.get("actual_other_surcharge_ex_gst"))
        scope["carrier_cube"] += _number(row.get("actual_billed_cube_cm3")); scope["estimator_cube"] += _number(row.get("estimator_parcel_cube_cm3")); scope["shippit_cube"] += _number(row.get("shippit_chargeable_cube_cm3"))
        bucket = scope["buckets"][row.get("primary_cost_driver", "NO_MATERIAL_VARIANCE_DRIVER")]
        bucket["count"] += 1; bucket["base"] += _number(row.get("actual_less_modelled_base_cost_ex_gst")); bucket["fuel"] += _number(row.get("actual_less_predicted_fuel_ex_gst")); bucket["other"] += _number(row.get("actual_less_modelled_other_surcharge_ex_gst")); bucket["carrier_cube"] += _number(row.get("actual_billed_cube_cm3")); bucket["estimator_cube"] += _number(row.get("estimator_parcel_cube_cm3")); bucket["shippit_cube"] += _number(row.get("shippit_chargeable_cube_cm3"))
    output = []
    for (invoice_no, site), scope in sorted(scopes.items()):
        model_base, model_fuel = float(scope["model_base"]), float(scope["model_fuel"])
        model_total = model_base + model_fuel
        actual_base, actual_fuel, actual_other = float(scope["actual_base"]), float(scope["actual_fuel"]), float(scope["actual_other"])
        actual_total = actual_base + actual_fuel + actual_other
        output.append(_bridge_row(invoice_no, site, "MODEL_ESTIMATED_COST", "", int(scope["count"]), model_base, model_fuel, 0.0, model_total, "", None, None, None))
        for driver, bucket in sorted(scope["buckets"].items()):
            base, fuel, other = float(bucket["base"]), float(bucket["fuel"]), float(bucket["other"])
            output.append(_bridge_row(invoice_no, site, "VARIANCE_BUCKET", driver, int(bucket["count"]), base, fuel, other, base + fuel + other, _percentage(base + fuel + other, model_total), float(bucket["carrier_cube"]), float(bucket["estimator_cube"]), float(bucket["shippit_cube"])))
        output.append(_bridge_row(invoice_no, site, "ACTUAL_INVOICED_COST", "", int(scope["count"]), actual_base, actual_fuel, actual_other, actual_total, _percentage(actual_total - model_total, model_total), float(scope["carrier_cube"]), float(scope["estimator_cube"]), float(scope["shippit_cube"])))
    _write_csv(path, _bridge_headers(), output)


def _bridge_row(invoice_no: str, site: str, row_type: str, driver: str, count: int, base: float, fuel: float, other: float, total: float, variance_pct: str, carrier_cube: float | None, estimator_cube: float | None, shippit_cube: float | None) -> dict[str, str]:
    return {"invoice_no": invoice_no, "site": site, "carrier": "Australia Post", "bridge_row_type": row_type, "primary_cost_driver": driver, "matched_consignment_groups": str(count), "base_freight_ex_gst": _fmt(base), "fuel_surcharge_ex_gst": _fmt(fuel), "other_surcharges_ex_gst": _fmt(other), "total_ex_gst": _fmt(total), "variance_from_model_pct": variance_pct, "ap_billed_cube_m3": _fmt_m3(carrier_cube), "estimated_parcel_cube_m3": _fmt_m3(estimator_cube), "shippit_cube_m3": _fmt_m3(shippit_cube), "estimator_vs_ap_billed_cube_pct": _percentage((estimator_cube or 0.0) - (carrier_cube or 0.0), carrier_cube or 0.0) if carrier_cube else "", "shippit_vs_ap_billed_cube_pct": _percentage((shippit_cube or 0.0) - (carrier_cube or 0.0), carrier_cube or 0.0) if carrier_cube else ""}


def _detail_headers() -> list[str]:
    return [
        "source_table", "invoice_no", "billing_date", "service_date", "owner_type", "charge_link_method", "assignment_no", "carrier_tracking_number", "assignment_consignment_count", "customer_ref_1", "sales_order", "source_line_count", "material_pattern", "description_pattern", "charge_code_pattern", "actual_origin_city", "actual_origin_state", "actual_origin_postcode", "actual_destination_city", "actual_destination_state", "actual_destination_postcode", "actual_lodgement_zone", "actual_destination_zone", "actual_service_material", "actual_weight_kg", "actual_declared_weight_kg", "actual_billed_weight_kg", "actual_cubic_weight_kg", "actual_chargeable_weight_kg", "actual_rounded_chargeable_weight_kg", "actual_billed_cube_cm3", "actual_billed_cube_m3", "actual_declared_cube_cm3", "physical_article_row_count", "physical_distinct_article_count", "physical_article_duplicate_row_count", "physical_anonymous_row_count", "physical_aggregation_method", "physical_article_cube_sum_cm3", "physical_article_cube_max_cm3", "physical_article_billed_weight_sum_kg", "physical_article_billed_weight_max_kg", "physical_article_cubic_weight_sum_kg", "physical_article_cubic_weight_max_kg", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "fuel_surcharge_line_ex_gst", "fuel_surcharge_field_ex_gst", "fuel_allocation_status", "actual_other_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "actual_fuel_rate_pct", "actual_tax_amount", "match_method", "site", "invoice_number", "stage3_tracking_number", "stage3_despatch_date", "kmart_accounting_year", "kmart_accounting_week_number_in_year", "stage3_service", "modelled_destination_postcode", "modelled_ap_zone", "modelled_ap_pricing_zone", "ap_zone_comparison", "modelled_ap_ratecard_id", "modelled_ap_chargeable_weight_kg", "modelled_ap_base_cost_ex_gst", "estimator_parcel_cube_cm3", "shippit_chargeable_cube_cm3", "shippit_chargeable_cube_m3", "shippit_chargeable_weight_kg", "fuel_norm_sample_count", "fuel_norm_p50_pct", "fuel_norm_source", "predicted_fuel_surcharge_ex_gst", "modelled_ap_cost_including_fuel_ex_gst", "actual_less_modelled_base_cost_ex_gst", "actual_less_predicted_fuel_ex_gst", "actual_less_modelled_other_surcharge_ex_gst", "actual_less_modelled_cost_including_fuel_ex_gst", "actual_less_modelled_cost_including_fuel_pct", "actual_less_modelled_chargeable_weight_kg", "actual_less_shippit_chargeable_weight_kg", "actual_less_estimator_cube_cm3", "actual_less_shippit_cube_cm3", "estimator_dimension_diagnostic", "shippit_dimension_diagnostic", "primary_cost_driver", "root_cause", "reconciliation_note",
    ]


def _summary_headers() -> list[str]:
    return ["source_table", "invoice_no", "match_method", "site", "primary_cost_driver", "billed_consignment_groups", "matched_consignment_groups", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_other_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "modelled_ap_base_cost_ex_gst", "predicted_fuel_surcharge_ex_gst", "modelled_ap_cost_including_fuel_ex_gst", "actual_less_modelled_cost_including_fuel_ex_gst", "actual_less_modelled_cost_pct", "estimated_cube_cm3", "shippit_cube_cm3"]


def _dimension_headers() -> list[str]:
    return ["invoice_no", "site", "estimator_dimension_diagnostic", "shippit_dimension_diagnostic", "recommended_action", "matched_consignment_groups", "ap_billed_cube_m3", "estimator_cube_m3", "shippit_cube_m3", "estimator_vs_ap_billed_cube_pct", "shippit_vs_ap_billed_cube_pct"]


def _bridge_headers() -> list[str]:
    return ["invoice_no", "site", "carrier", "bridge_row_type", "primary_cost_driver", "matched_consignment_groups", "base_freight_ex_gst", "fuel_surcharge_ex_gst", "other_surcharges_ex_gst", "total_ex_gst", "variance_from_model_pct", "ap_billed_cube_m3", "estimated_parcel_cube_m3", "shippit_cube_m3", "estimator_vs_ap_billed_cube_pct", "shippit_vs_ap_billed_cube_pct"]


def _service_bucket(row: dict[str, str]) -> str:
    material = _normalise(row.get("material_pattern"))
    if "B5R" in material and "B5S" not in material and "B48" not in material:
        return "AP_RETURN"
    return "AP_DOMESTIC"


def _model_base_cost(model: dict[str, str]) -> float:
    explicit_base = _number(model.get("actual_est_base_cost_ex_gst"))
    if explicit_base > 0:
        return explicit_base
    total = _number(model.get("actual_est_cost_ex_gst"))
    embedded_fuel = _number(model.get("actual_est_fuel_surcharge_ex_gst"))
    return total - embedded_fuel if embedded_fuel and total >= embedded_fuel else total


def _configured_fuel_norm(model: dict[str, str]) -> FuelNorm | None:
    rate = model.get("actual_est_fsc_rate_pct")
    if rate in (None, ""):
        return None
    return FuelNorm(0, _number(rate), model.get("actual_est_fsc_source") or "STAGE3_FSC_RULE")


def _model_number(model: dict[str, str], *keys: str) -> float:
    for key in keys:
        value = _number(model.get(key))
        if value:
            return value
    return 0.0


def _parent_order_id(value: object) -> str:
    digits = _extract_order_id(value)
    return digits[:9] if len(digits) >= 10 else digits


def _extract_order_id(value: object) -> str:
    text = str(value or "")
    match = re.search(r"(?:ORDER\s*)?(\d{9,})", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _is_matched(row: dict[str, str]) -> bool:
    return bool(row.get("site")) and not row.get("match_method", "").startswith(("UNMATCHED", "AMBIGUOUS"))


def _read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as input_file:
        yield from csv.DictReader(input_file)


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _normalise(value: object) -> str:
    return str(value or "").strip().upper()


def _digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _number(value: object) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _fmt_m3(value: float | None) -> str:
    return _fmt((value or 0.0) / 1_000_000.0) if value is not None else ""


def _percentage(numerator: float, denominator: float) -> str:
    return _fmt(numerator / denominator * 100.0) if denominator else ""


def _percentile(values: list[float], quantile: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _norm_source(level: str) -> str:
    return {"LANE_DATE": "AP_INVOICE_FUEL_P50_LANE_DATE", "DATE": "AP_INVOICE_FUEL_P50_DATE", "LANE": "AP_INVOICE_FUEL_P50_LANE", "GLOBAL": "AP_INVOICE_FUEL_P50_GLOBAL"}.get(level, "AP_INVOICE_FUEL_P50")


if __name__ == "__main__":
    main()
