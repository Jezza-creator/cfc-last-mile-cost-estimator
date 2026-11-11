"""Reconcile Allied billed jobs against Stage 3 cost-estimator outputs."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


ALLIED_JOBS_PATH = Path("outputs/carrier_invoices/allied_jobs.csv")
STAGE3_PATHS = {
    "MOOREBANK": Path("outputs/stage3_moorebank_cost_estimates_full.csv"),
    "TRUGANINA": Path("outputs/stage3_truganina_cost_estimates_full.csv"),
}
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, default=ALLIED_JOBS_PATH)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument("--moorebank-stage3", type=Path, default=STAGE3_PATHS["MOOREBANK"])
    parser.add_argument("--truganina-stage3", type=Path, default=STAGE3_PATHS["TRUGANINA"])
    args = parser.parse_args()

    stage3_paths = {"MOOREBANK": args.moorebank_stage3, "TRUGANINA": args.truganina_stage3}
    missing = [path for path in [args.jobs, *stage3_paths.values()] if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing input files: " + ", ".join(str(path) for path in missing))

    args.output_directory.mkdir(parents=True, exist_ok=True)
    reconciliation_path = args.output_directory / "allied_reconciliation.csv"
    summary_path = args.output_directory / "allied_reconciliation_summary.csv"
    bridge_path = args.output_directory / "allied_reconciliation_cost_bridge.csv"
    fuel_norms_path = args.output_directory / "allied_fuel_norms.csv"
    tracking, parent_order = _load_stage3(stage3_paths)
    results = [_build_result(job, *_find_match(job, tracking, parent_order)) for job in _read_csv(args.jobs)]
    _apply_fuel_norms(results, fuel_norms_path)
    _write_csv(reconciliation_path, _detail_headers(), results)
    _write_summary(summary_path, results)
    _write_bridge(bridge_path, results)
    print(f"Wrote {reconciliation_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {bridge_path}")
    print(f"Wrote {fuel_norms_path}")


def _load_stage3(paths: dict[str, Path]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    by_tracking: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_parent_order: dict[str, list[dict[str, str]]] = defaultdict(list)
    for site, path in paths.items():
        for row in _read_csv(path):
            if _normalise(row.get("actual_carrier")) != "ALLIED EXPRESS":
                continue
            row["site"] = site
            if tracking_number := _normalise(row.get("carrier_tracking_number")):
                by_tracking[tracking_number].append(row)
            if invoice_number := _digits(row.get("invoice_number")):
                # Parent-order matching is deliberately only a fallback; a fulfilment suffix is not fixed.
                for length in range(8, len(invoice_number)):
                    by_parent_order[invoice_number[:length]].append(row)
    return by_tracking, by_parent_order


def _find_match(
    actual: dict[str, str],
    by_tracking: dict[str, list[dict[str, str]]],
    by_parent_order: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, str] | None, str]:
    matches = by_tracking.get(_normalise(actual.get("docket")), [])
    if len(matches) == 1:
        return matches[0], "TRACKING_NUMBER"
    if len(matches) > 1:
        return None, "AMBIGUOUS_TRACKING_NUMBER"

    candidates = by_parent_order.get(_digits(actual.get("external_order_id")), [])
    postcode = _digits(actual.get("destination_postcode"))
    candidates = [row for row in candidates if not postcode or _digits(row.get("dest_postcode")) == postcode]
    if len(candidates) == 1:
        return candidates[0], "PARENT_EXTERNAL_ORDER_ID"
    if len(candidates) > 1:
        return None, "AMBIGUOUS_PARENT_EXTERNAL_ORDER_ID"
    return None, "UNMATCHED"


def _build_result(actual: dict[str, str], model: dict[str, str] | None, match_method: str) -> dict[str, str]:
    model = model or {}
    actual_total = _number(actual.get("actual_invoice_cost_ex_gst"))
    actual_base = _number(actual.get("actual_base_cost_ex_gst"))
    actual_fuel = _number(actual.get("actual_fuel_surcharge_ex_gst"))
    actual_other = actual_total - actual_base - actual_fuel
    model_base = _number(model.get("allied_total_cost_ex_gst"))
    actual_weight = _number(actual.get("charged_weight_kg"))
    model_weight = _number(model.get("cost_weight_basis_kg"))
    actual_cube = _number(actual.get("declared_volume_m3")) * 1_000_000
    estimator_cube = _number(model.get("est_parcel_cube_total_cm3"))
    shippit_cube = _number(model.get("shippit_chargeable_cube_cm3"))
    driver, note = _classify(actual, model, match_method, actual_weight, model_weight, actual_cube, estimator_cube, shippit_cube)
    return {
        "source_file": actual.get("source_file", ""), "source_sheet": actual.get("source_sheet", ""),
        "allied_week_number": actual.get("allied_week_number", ""), "docket": actual.get("docket", ""), "job_number": actual.get("job_number", ""),
        "reference": actual.get("reference", ""), "external_order_id": actual.get("external_order_id", ""),
        "actual_origin_suburb": actual.get("origin_suburb", ""), "actual_origin_postcode": actual.get("origin_postcode", ""),
        "actual_destination_suburb": actual.get("destination_suburb", ""), "actual_destination_postcode": actual.get("destination_postcode", ""),
        "actual_service_code": actual.get("service_code", ""), "actual_charge_code_pattern": actual.get("charge_code_pattern", ""),
        "actual_chargeable_weight_kg": actual.get("charged_weight_kg", ""), "actual_declared_weight_kg": actual.get("declared_weight_kg", ""),
        "actual_declared_cube_m3": actual.get("declared_volume_m3", ""), "actual_declared_cube_cm3": _format_number(actual_cube),
        "actual_base_cost_ex_gst": actual.get("actual_base_cost_ex_gst", ""), "actual_excess_weight_surcharge_ex_gst": actual.get("actual_excess_weight_surcharge_ex_gst", ""),
        "actual_delivery_surcharge_ex_gst": actual.get("actual_delivery_surcharge_ex_gst", ""), "actual_fuel_surcharge_ex_gst": actual.get("actual_fuel_surcharge_ex_gst", ""),
        "actual_other_surcharge_ex_gst": actual.get("actual_other_surcharge_ex_gst", ""), "actual_invoice_cost_ex_gst": actual.get("actual_invoice_cost_ex_gst", ""),
        "match_method": match_method, "site": model.get("site", ""), "invoice_number": model.get("invoice_number", ""),
        "stage3_tracking_number": model.get("carrier_tracking_number", ""), "stage3_despatch_date": model.get("despatch_date", ""),
        "kmart_accounting_year": model.get("kmart_accounting_year", ""), "kmart_accounting_week_number_in_year": model.get("kmart_accounting_week_number_in_year", ""),
        "modelled_destination_postcode": model.get("dest_postcode", ""), "modelled_allied_zone": model.get("allied_zone", ""),
        "modelled_allied_cost_weight_basis_kg": model.get("cost_weight_basis_kg", ""), "modelled_allied_base_cost_ex_gst": model.get("allied_total_cost_ex_gst", ""),
        "modelled_allied_component_pattern": _modelled_components(model), "estimator_parcel_cube_cm3": model.get("est_parcel_cube_total_cm3", ""),
        "shippit_chargeable_cube_cm3": model.get("shippit_chargeable_cube_cm3", ""), "shippit_chargeable_weight_kg": model.get("shippit_chargeable_weight_kg", ""),
        "actual_less_modelled_base_cost_ex_gst": _optional_difference(actual_base, model_base, bool(model)),
        "actual_less_modelled_cost_ex_gst": _optional_difference(actual_total, model_base, bool(model)),
        "actual_less_modelled_cost_pct": _percentage(actual_total - model_base, model_base) if model else "",
        "actual_less_modelled_chargeable_weight_kg": _optional_difference(actual_weight, model_weight, bool(model)),
        "actual_less_shippit_chargeable_weight_kg": _optional_difference(actual_weight, _number(model.get("shippit_chargeable_weight_kg")), bool(model)),
        "actual_less_estimator_cube_cm3": _optional_difference(actual_cube, estimator_cube, bool(model)),
        "actual_less_shippit_cube_cm3": _optional_difference(actual_cube, shippit_cube, bool(model)),
        "primary_cost_driver": driver, "reconciliation_note": note,
    }


def _classify(actual: dict[str, str], model: dict[str, str], match_method: str, actual_weight: float, model_weight: float, actual_cube: float, estimator_cube: float, shippit_cube: float) -> tuple[str, str]:
    if not model:
        return match_method, "No unique Stage 3 match is available; this job is retained for follow-up."
    if _digits(actual.get("destination_postcode")) != _digits(model.get("dest_postcode")):
        return "ALLIED_DESTINATION_MISMATCH", "Allied and Stage 3 destination postcodes differ."
    if abs(actual_weight - model_weight) > 0.5:
        return "ALLIED_CHARGEABLE_WEIGHT_MISMATCH", "Allied billed charged weight differs materially from the Stage 3 costing weight basis."
    if actual_cube > 0 and shippit_cube > 0 and actual_cube > shippit_cube * 1.15:
        return "SHIPPIT_CUBE_UNDERSTATED", "Allied declared cube exceeds Shippit cube; the invoice supports investigating Shippit dimensions."
    if actual_cube > 0 and estimator_cube > 0 and abs(actual_cube - estimator_cube) / actual_cube <= 0.15:
        return "ALLIED_RATE_OR_CHARGE_MISMATCH", "Weights and dimensions broadly align; review Allied ratecard and charge-code treatment."
    if _number(actual.get("actual_delivery_surcharge_ex_gst")) > 0 or _number(actual.get("actual_excess_weight_surcharge_ex_gst")) > 0:
        return "ALLIED_UNMODELLED_SURCHARGE", "Allied invoice includes delivery or excess-weight charge codes not separately represented in the current model."
    return "NO_MATERIAL_VARIANCE_DRIVER", "No single material driver was identified from the available invoice and Stage 3 fields."


def _modelled_components(model: dict[str, str]) -> str:
    components = [
        ("BASE", "allied_base_cost_ex_gst"), ("REMOTE", "allied_remote_area_surcharge_ex_gst"),
        ("OVERWEIGHT", "allied_overweight_surcharge_ex_gst"), ("OVERSIZED", "allied_oversized_item_surcharge_ex_gst"),
        ("MANUAL", "allied_manual_measurement_fee_ex_gst"), ("REVERSE", "allied_reverse_logistics_surcharge_ex_gst"),
        ("ADDITIONAL", "allied_additional_services_ex_gst"),
    ]
    return "|".join(name for name, field in components if _number(model.get(field)) != 0)


def _apply_fuel_norms(rows: list[dict[str, str]], fuel_norms_path: Path) -> None:
    samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        pre_fuel_cost = _number(row["actual_invoice_cost_ex_gst"]) - _number(row["actual_fuel_surcharge_ex_gst"])
        if row["site"] and pre_fuel_cost > 0:
            samples[_fuel_key(row)].append(_number(row["actual_fuel_surcharge_ex_gst"]) / pre_fuel_cost * 100.0)
    all_samples = [sample for values in samples.values() for sample in values]
    fallback_p50 = statistics.median(all_samples) if all_samples else 0.0
    norm_rows = []
    for (site, zone, service), values in sorted(samples.items()):
        norm_rows.append({"site": site, "allied_zone": zone, "actual_service_code": service, "sample_count": len(values), "p50_fuel_surcharge_pct": _format_number(statistics.median(values)), "norm_source": "MATCHED_SITE_ZONE_SERVICE_P50"})
    norm_rows.append({"site": "ALL", "allied_zone": "ALL", "actual_service_code": "ALL", "sample_count": len(all_samples), "p50_fuel_surcharge_pct": _format_number(fallback_p50), "norm_source": "MATCHED_ALLIED_P50_FALLBACK"})
    _write_csv(fuel_norms_path, ["site", "allied_zone", "actual_service_code", "sample_count", "p50_fuel_surcharge_pct", "norm_source"], norm_rows)
    for row in rows:
        values = samples.get(_fuel_key(row), [])
        p50, count, source = (statistics.median(values), len(values), "MATCHED_SITE_ZONE_SERVICE_P50") if len(values) >= 30 else (fallback_p50, len(all_samples), "MATCHED_ALLIED_P50_FALLBACK")
        model_base = _number(row["modelled_allied_base_cost_ex_gst"])
        predicted_fuel = model_base * p50 / 100.0 if row["site"] else 0.0
        model_total = model_base + predicted_fuel if row["site"] else 0.0
        row["fuel_norm_sample_count"] = str(count) if row["site"] else ""
        row["fuel_norm_p50_pct"] = _format_number(p50) if row["site"] else ""
        row["fuel_norm_source"] = source if row["site"] else ""
        row["predicted_fuel_surcharge_ex_gst"] = _format_number(predicted_fuel) if row["site"] else ""
        row["modelled_allied_cost_including_fuel_ex_gst"] = _format_number(model_total) if row["site"] else ""
        row["actual_less_modelled_cost_ex_gst"] = _format_number(_number(row["actual_invoice_cost_ex_gst"]) - model_total) if row["site"] else ""
        row["actual_less_modelled_cost_pct"] = _percentage(_number(row["actual_invoice_cost_ex_gst"]) - model_total, model_total) if row["site"] else ""


def _fuel_key(row: dict[str, str]) -> tuple[str, str, str]:
    return row.get("site", ""), row.get("modelled_allied_zone", ""), _normalise(row.get("actual_service_code"))


def _write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str, str, str, str], dict[str, float | int]] = defaultdict(lambda: {"count": 0, "actual": 0.0, "model": 0.0, "fuel": 0.0, "other": 0.0})
    for row in rows:
        group = groups[(row["source_file"], row["source_sheet"], row["match_method"], row["site"] or "Unmatched", row["primary_cost_driver"])]
        group["count"] += 1; group["actual"] += _number(row["actual_invoice_cost_ex_gst"]); group["model"] += _number(row["modelled_allied_cost_including_fuel_ex_gst"]); group["fuel"] += _number(row["actual_fuel_surcharge_ex_gst"]); group["other"] += _number(row["actual_excess_weight_surcharge_ex_gst"]) + _number(row["actual_delivery_surcharge_ex_gst"]) + _number(row["actual_other_surcharge_ex_gst"])
    output = []
    for key, value in sorted(groups.items()):
        source_file, source_sheet, method, site, driver = key
        output.append({"source_file": source_file, "source_sheet": source_sheet, "match_method": method, "site": site, "primary_cost_driver": driver, "billed_jobs": value["count"], "actual_invoice_cost_ex_gst": _format_number(value["actual"]), "modelled_allied_cost_ex_gst": _format_number(value["model"]), "actual_fuel_surcharge_ex_gst": _format_number(value["fuel"]), "actual_other_surcharges_ex_gst": _format_number(value["other"]), "actual_less_modelled_cost_ex_gst": _format_number(value["actual"] - value["model"]), "actual_less_modelled_cost_pct": _percentage(value["actual"] - value["model"], value["model"])})
    _write_csv(path, ["source_file", "source_sheet", "match_method", "site", "primary_cost_driver", "billed_jobs", "actual_invoice_cost_ex_gst", "modelled_allied_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_other_surcharges_ex_gst", "actual_less_modelled_cost_ex_gst", "actual_less_modelled_cost_pct"], output)


def _write_bridge(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str, str], dict[str, float | int]] = defaultdict(lambda: {"count": 0, "actual_base": 0.0, "actual_fuel": 0.0, "actual_other": 0.0, "actual_total": 0.0, "model_base": 0.0, "model_fuel": 0.0})
    for row in rows:
        if row["match_method"] not in {"TRACKING_NUMBER", "PARENT_EXTERNAL_ORDER_ID"}:
            continue
        group = groups[(row["source_file"], row["site"], row["primary_cost_driver"])]
        group["count"] += 1; group["actual_base"] += _number(row["actual_base_cost_ex_gst"]); group["actual_fuel"] += _number(row["actual_fuel_surcharge_ex_gst"]); group["actual_other"] += _number(row["actual_excess_weight_surcharge_ex_gst"]) + _number(row["actual_delivery_surcharge_ex_gst"]) + _number(row["actual_other_surcharge_ex_gst"]); group["actual_total"] += _number(row["actual_invoice_cost_ex_gst"]); group["model_base"] += _number(row["modelled_allied_base_cost_ex_gst"]); group["model_fuel"] += _number(row["predicted_fuel_surcharge_ex_gst"])
    output: list[dict[str, str]] = []
    by_scope: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"actual": 0.0, "model_base": 0.0, "model_fuel": 0.0})
    for (source_file, site, _), group in groups.items(): by_scope[(source_file, site)]["actual"] += group["actual_total"]; by_scope[(source_file, site)]["model_base"] += group["model_base"]; by_scope[(source_file, site)]["model_fuel"] += group["model_fuel"]
    for (source_file, site), totals in sorted(by_scope.items()):
        model_total = totals["model_base"] + totals["model_fuel"]
        output.append({"source_file": source_file, "site": site, "carrier": "Allied Express", "bridge_row_type": "MODEL_ESTIMATED_COST", "primary_cost_driver": "", "matched_jobs": "", "base_freight_ex_gst": _format_number(totals["model_base"]), "fuel_surcharge_ex_gst": _format_number(totals["model_fuel"]), "other_surcharges_ex_gst": "0", "total_ex_gst": _format_number(model_total), "variance_from_model_pct": ""})
        for (group_file, group_site, driver), group in sorted(groups.items()):
            if (group_file, group_site) != (source_file, site): continue
            variance = group["actual_total"] - group["model_base"] - group["model_fuel"]
            output.append({"source_file": source_file, "site": site, "carrier": "Allied Express", "bridge_row_type": "VARIANCE_BUCKET", "primary_cost_driver": driver, "matched_jobs": group["count"], "base_freight_ex_gst": _format_number(group["actual_base"] - group["model_base"]), "fuel_surcharge_ex_gst": _format_number(group["actual_fuel"] - group["model_fuel"]), "other_surcharges_ex_gst": _format_number(group["actual_other"]), "total_ex_gst": _format_number(variance), "variance_from_model_pct": _percentage(variance, model_total)})
        output.append({"source_file": source_file, "site": site, "carrier": "Allied Express", "bridge_row_type": "ACTUAL_INVOICED_COST", "primary_cost_driver": "", "matched_jobs": "", "base_freight_ex_gst": _format_number(sum(g["actual_base"] for (f, s, _), g in groups.items() if (f, s) == (source_file, site))), "fuel_surcharge_ex_gst": _format_number(sum(g["actual_fuel"] for (f, s, _), g in groups.items() if (f, s) == (source_file, site))), "other_surcharges_ex_gst": _format_number(sum(g["actual_other"] for (f, s, _), g in groups.items() if (f, s) == (source_file, site))), "total_ex_gst": _format_number(totals["actual"]), "variance_from_model_pct": _percentage(totals["actual"] - model_total, model_total)})
    _write_csv(path, ["source_file", "site", "carrier", "bridge_row_type", "primary_cost_driver", "matched_jobs", "base_freight_ex_gst", "fuel_surcharge_ex_gst", "other_surcharges_ex_gst", "total_ex_gst", "variance_from_model_pct"], output)


def _detail_headers() -> list[str]:
    return ["source_file", "source_sheet", "allied_week_number", "docket", "job_number", "reference", "external_order_id", "actual_origin_suburb", "actual_origin_postcode", "actual_destination_suburb", "actual_destination_postcode", "actual_service_code", "actual_charge_code_pattern", "actual_chargeable_weight_kg", "actual_declared_weight_kg", "actual_declared_cube_m3", "actual_declared_cube_cm3", "actual_base_cost_ex_gst", "actual_excess_weight_surcharge_ex_gst", "actual_delivery_surcharge_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_other_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "match_method", "site", "invoice_number", "stage3_tracking_number", "stage3_despatch_date", "kmart_accounting_year", "kmart_accounting_week_number_in_year", "modelled_destination_postcode", "modelled_allied_zone", "modelled_allied_cost_weight_basis_kg", "modelled_allied_base_cost_ex_gst", "modelled_allied_component_pattern", "estimator_parcel_cube_cm3", "shippit_chargeable_cube_cm3", "shippit_chargeable_weight_kg", "fuel_norm_sample_count", "fuel_norm_p50_pct", "fuel_norm_source", "predicted_fuel_surcharge_ex_gst", "modelled_allied_cost_including_fuel_ex_gst", "actual_less_modelled_base_cost_ex_gst", "actual_less_modelled_cost_ex_gst", "actual_less_modelled_cost_pct", "actual_less_modelled_chargeable_weight_kg", "actual_less_shippit_chargeable_weight_kg", "actual_less_estimator_cube_cm3", "actual_less_shippit_cube_cm3", "primary_cost_driver", "reconciliation_note"]


def _read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as input_file:
        yield from csv.DictReader(input_file)


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=headers); writer.writeheader(); writer.writerows(rows)


def _normalise(value: object) -> str: return str(value or "").strip().upper()
def _digits(value: object) -> str: return "".join(character for character in str(value or "") if character.isdigit())
def _number(value: object) -> float:
    try: result = float(value or 0)
    except (TypeError, ValueError): return 0.0
    return result if math.isfinite(result) else 0.0
def _format_number(value: float) -> str: return f"{value:.6f}".rstrip("0").rstrip(".")
def _percentage(numerator: float, denominator: float) -> str: return _format_number(numerator / denominator * 100.0) if denominator else ""
def _optional_difference(left: float, right: float, available: bool) -> str: return _format_number(left - right) if available else ""


if __name__ == "__main__":
    main()
