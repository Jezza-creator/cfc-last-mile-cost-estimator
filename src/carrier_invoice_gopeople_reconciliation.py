"""Reconcile GoPeople billed consignments against Stage 3 GoEXPRESS estimates."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


INPUT_PATH = Path("outputs/carrier_invoices/gopeople_consignments.csv")
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
STAGE3_PATHS = {"MOOREBANK": Path("outputs/stage3_moorebank_cost_estimates_full.csv"), "TRUGANINA": Path("outputs/stage3_truganina_cost_estimates_full.csv")}
DIMENSION_ALIGNMENT_THRESHOLD_PCT = 15.0
DIMENSION_MATERIAL_THRESHOLD_PCT = 25.0
WEIGHT_MATERIAL_THRESHOLD_KG = 0.5


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consignments", type=Path, default=INPUT_PATH)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument("--moorebank-stage3", type=Path, default=STAGE3_PATHS["MOOREBANK"])
    parser.add_argument("--truganina-stage3", type=Path, default=STAGE3_PATHS["TRUGANINA"])
    args = parser.parse_args()
    stage3 = {"MOOREBANK": args.moorebank_stage3, "TRUGANINA": args.truganina_stage3}
    missing = [path for path in [args.consignments, *stage3.values()] if not path.exists()]
    if missing: raise FileNotFoundError("Missing input files: " + ", ".join(str(path) for path in missing))
    args.output_directory.mkdir(parents=True, exist_ok=True)
    by_tracking = _load_stage3(stage3)
    detail = [_result(row, *_match(row, by_tracking)) for row in _read_csv(args.consignments)]
    _write_csv(args.output_directory / "gopeople_reconciliation.csv", _detail_headers(), detail)
    _write_summary(args.output_directory / "gopeople_reconciliation_summary.csv", detail)
    print(f"Wrote {args.output_directory / 'gopeople_reconciliation.csv'}")
    print(f"Wrote {args.output_directory / 'gopeople_reconciliation_summary.csv'}")


def _load_stage3(paths: dict[str, Path]) -> dict[str, list[dict[str, str]]]:
    tracking: dict[str, list[dict[str, str]]] = defaultdict(list)
    for site, path in paths.items():
        for row in _read_csv(path):
            if _normalise(row.get("actual_carrier")) != "GOEXPRESS": continue
            row["site"] = site
            if key := _normalise(row.get("carrier_tracking_number")): tracking[key].append(row)
    return tracking


def _match(actual: dict[str, str], by_tracking: dict[str, list[dict[str, str]]]) -> tuple[dict[str, str] | None, str]:
    candidates = by_tracking.get(_normalise(actual.get("carrier_tracking_number")), [])
    if len(candidates) == 1: return candidates[0], "TRACKING_NUMBER"
    dispatch_date = actual.get("consignment_date", "")
    postcode = _digits(actual.get("destination_postcode"))
    candidates = [row for row in candidates if (not dispatch_date or row.get("despatch_date") == dispatch_date) and (not postcode or _digits(row.get("dest_postcode")) == postcode)]
    if len(candidates) == 1: return candidates[0], "TRACKING_NUMBER_DATE_DESTINATION"
    return (None, "AMBIGUOUS_TRACKING_NUMBER") if candidates else (None, "UNMATCHED")


def _result(actual: dict[str, str], model: dict[str, str] | None, match_method: str) -> dict[str, str]:
    model = model or {}
    actual_cost, model_cost = _number(actual.get("actual_invoice_cost_ex_gst")), _number(model.get("actual_est_cost_ex_gst"))
    actual_fuel = _number(actual.get("actual_fuel_surcharge_ex_gst"))
    actual_base = _number(actual.get("actual_base_cost_ex_gst"))
    if actual_base <= 0 and actual_cost > 0:
        actual_base = max(actual_cost - actual_fuel, 0.0)
    model_fuel = _number(model.get("actual_est_fuel_surcharge_ex_gst"))
    model_base = _number(model.get("actual_est_base_cost_ex_gst"))
    if model_base <= 0 and model_cost > 0:
        model_base = max(model_cost - model_fuel, 0.0)
    actual_weight, model_weight = _number(actual.get("charged_weight_kg")), _number(model.get("cost_weight_basis_kg"))
    actual_cube_cm3 = _number(actual.get("customer_cubic_volume_m3")) * 1_000_000.0
    estimator_cube_cm3 = _number(model.get("est_parcel_cube_total_cm3"))
    shippit_cube_cm3 = _number(model.get("shippit_chargeable_cube_cm3"))
    shippit_weight = _number(model.get("shippit_chargeable_weight_kg")) or model_weight
    driver, estimator_status, shippit_status, root_cause, note = _classify_root_cause(
        actual,
        model,
        match_method,
        actual_cube_cm3,
        estimator_cube_cm3,
        shippit_cube_cm3,
        actual_weight,
        shippit_weight,
        actual_cost,
        model_cost,
        actual_base,
        model_base,
        actual_fuel,
        model_fuel,
    )
    return {
        "source_file": actual.get("source_file", ""),
        "source_sheet": actual.get("source_sheet", ""),
        "carrier_invoice_number": actual.get("carrier_invoice_number", ""),
        "carrier_tracking_number": actual.get("carrier_tracking_number", ""),
        "consignment_date": actual.get("consignment_date", ""),
        "account_source": actual.get("account_source", ""),
        "origin_postcode": actual.get("origin_postcode", ""),
        "destination_postcode": actual.get("destination_postcode", ""),
        "carton_count": actual.get("carton_count", ""),
        "actual_chargeable_weight_kg": actual.get("charged_weight_kg", ""),
        "actual_cubic_volume_m3": actual.get("customer_cubic_volume_m3", ""),
        "actual_cubic_volume_cm3": _fmt(actual_cube_cm3),
        "actual_base_cost_ex_gst": _fmt(actual_base),
        "actual_fuel_surcharge_ex_gst": _fmt(actual_fuel),
        "actual_fuel_surcharge_pct": actual.get("actual_fuel_surcharge_pct", ""),
        "actual_invoice_cost_ex_gst": actual.get("actual_invoice_cost_ex_gst", ""),
        "actual_gst": actual.get("actual_gst", ""),
        "invoice_notes_or_fuel": actual.get("invoice_notes_or_fuel", ""),
        "match_method": match_method,
        "site": model.get("site", ""),
        "invoice_number": model.get("invoice_number", ""),
        "stage3_tracking_number": model.get("carrier_tracking_number", ""),
        "stage3_despatch_date": model.get("despatch_date", ""),
        "kmart_accounting_year": model.get("kmart_accounting_year", ""),
        "kmart_accounting_week_number_in_year": model.get("kmart_accounting_week_number_in_year", ""),
        "modelled_destination_postcode": model.get("dest_postcode", ""),
        "modelled_goexpress_zone": model.get("goexpress_zone", ""),
        "modelled_cost_weight_basis_kg": model.get("cost_weight_basis_kg", ""),
        "modelled_parcel_count": model.get("actual_parcel_count", ""),
        "modelled_goexpress_base_cost_ex_gst": _fmt(model_base) if model else "",
        "modelled_goexpress_fuel_surcharge_ex_gst": _fmt(model_fuel) if model else "",
        "modelled_goexpress_fuel_surcharge_pct": model.get("actual_est_fuel_surcharge_pct", "") if model else "",
        "modelled_goexpress_fuel_surcharge_source": model.get("actual_est_fuel_surcharge_source", "") if model else "",
        "predicted_fuel_surcharge_ex_gst": _fmt(model_fuel) if model else "",
        "modelled_goexpress_cost_ex_gst": model.get("actual_est_cost_ex_gst", ""),
        "estimator_parcel_cube_cm3": _fmt(estimator_cube_cm3),
        "estimator_parcel_cube_m3": _fmt_m3(estimator_cube_cm3),
        "shippit_chargeable_weight_kg": model.get("shippit_chargeable_weight_kg", ""),
        "shippit_chargeable_cube_cm3": model.get("shippit_chargeable_cube_cm3", ""),
        "shippit_chargeable_cube_m3": _fmt_m3(shippit_cube_cm3),
        "actual_less_modelled_base_cost_ex_gst": _fmt(actual_base - model_base) if model else "",
        "actual_less_modelled_fuel_surcharge_ex_gst": _fmt(actual_fuel - model_fuel) if model else "",
        "actual_less_modelled_cost_ex_gst": _fmt(actual_cost - model_cost) if model else "",
        "actual_less_modelled_cost_pct": _percentage(actual_cost - model_cost, model_cost) if model else "",
        "actual_less_modelled_chargeable_weight_kg": _fmt(actual_weight - model_weight) if model else "",
        "actual_less_shippit_chargeable_weight_kg": _fmt(actual_weight - shippit_weight) if model else "",
        "actual_less_estimator_cube_cm3": _fmt(actual_cube_cm3 - estimator_cube_cm3) if model else "",
        "actual_less_shippit_cube_cm3": _fmt(actual_cube_cm3 - shippit_cube_cm3) if model else "",
        "estimator_dimension_diagnostic": estimator_status,
        "shippit_dimension_diagnostic": shippit_status,
        "primary_cost_driver": driver,
        "root_cause": root_cause,
        "reconciliation_note": note,
    }


def _classify_root_cause(
    actual: dict[str, str],
    model: dict[str, str],
    match_method: str,
    actual_cube_cm3: float,
    estimator_cube_cm3: float,
    shippit_cube_cm3: float,
    actual_weight_kg: float,
    shippit_weight_kg: float,
    actual_cost: float,
    model_cost: float,
    actual_base_cost: float | None = None,
    model_base_cost: float | None = None,
    actual_fuel_cost: float | None = None,
    model_fuel_cost: float | None = None,
) -> tuple[str, str, str, str, str]:
    if not model or match_method != "TRACKING_NUMBER":
        return "MATCHING_GAP", "CARRIER_CUBE_UNAVAILABLE", "CARRIER_CUBE_UNAVAILABLE", "MATCHING_GAP", "No unique tracking-number match is available, so physical and cost comparisons cannot yet be assessed."

    causes: list[str] = []
    physical_causes: list[str] = []
    notes: list[str] = []
    estimator_status = _dimension_status(estimator_cube_cm3, actual_cube_cm3, "ESTIMATOR")
    shippit_status = _dimension_status(shippit_cube_cm3, actual_cube_cm3, "SHIPPIT")
    if actual_cube_cm3 > 0:
        estimator_gap_pct = (estimator_cube_cm3 - actual_cube_cm3) / actual_cube_cm3 * 100.0 if estimator_cube_cm3 else 0.0
        shippit_gap_pct = (shippit_cube_cm3 - actual_cube_cm3) / actual_cube_cm3 * 100.0 if shippit_cube_cm3 else 0.0
        notes.append(
            f"GoPeople billed cube {_fmt_m3(actual_cube_cm3)} m3; estimator {_fmt_m3(estimator_cube_cm3)} m3 ({estimator_gap_pct:+.1f}%); "
            f"Shippit {_fmt_m3(shippit_cube_cm3)} m3 ({shippit_gap_pct:+.1f}%)."
        )
        if shippit_status in {"SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER", "SHIPPIT_CUBE_OVERSTATED_VS_CARRIER"}:
            physical_causes.append(shippit_status.replace("_VS_CARRIER", ""))
    else:
        notes.append("GoPeople invoice did not provide a usable cubic volume for carrier-cube comparison.")

    if actual_weight_kg > 0 and shippit_weight_kg > 0 and abs(actual_weight_kg - shippit_weight_kg) > WEIGHT_MATERIAL_THRESHOLD_KG:
        physical_causes.append("SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH")
        notes.append(
            f"GoPeople chargeable weight {actual_weight_kg:.2f} kg versus Shippit chargeable weight {shippit_weight_kg:.2f} kg "
            f"(GoPeople less Shippit {actual_weight_kg - shippit_weight_kg:+.2f} kg)."
        )
    causes.extend(physical_causes)

    actual_base = actual_cost if actual_base_cost is None else actual_base_cost
    model_base = model_cost if model_base_cost is None else model_base_cost
    actual_fuel = 0.0 if actual_fuel_cost is None else actual_fuel_cost
    model_fuel = 0.0 if model_fuel_cost is None else model_fuel_cost

    if _digits(actual.get("destination_postcode")) != _digits(model.get("dest_postcode")):
        causes.append("GOPEOPLE_DESTINATION_MISMATCH")
        notes.append(
            f"GoPeople destination postcode {actual.get('destination_postcode') or 'blank'} differs from model postcode {model.get('dest_postcode') or 'blank'}."
        )
    if abs(actual_fuel - model_fuel) > 0.05:
        causes.append("GOPEOPLE_FUEL_SURCHARGE_VARIANCE")
        notes.append(
            f"GoPeople invoice fuel ${actual_fuel:.2f} versus modelled fuel ${model_fuel:.2f}; "
            "modelled FSC uses the effective-dated GoPeople schedule."
        )
    if abs(actual_base - model_base) > 0.01:
        causes.append("GOPEOPLE_RATE_OR_CHARGE_MISMATCH")
        notes.append(f"GoPeople invoice base ${actual_base:.2f} versus modelled base ${model_base:.2f}.")
    if not causes:
        causes.append("NO_MATERIAL_VARIANCE_DRIVER")

    primary = "SHIPPIT_PHYSICAL_BASIS_MISMATCH" if physical_causes else next(
        (cause for cause in ("GOPEOPLE_DESTINATION_MISMATCH", "GOPEOPLE_FUEL_SURCHARGE_VARIANCE", "GOPEOPLE_RATE_OR_CHARGE_MISMATCH") if cause in causes),
        "NO_MATERIAL_VARIANCE_DRIVER",
    )
    return primary, estimator_status, shippit_status, " | ".join(causes), " ".join(notes) or "No material variance driver was identified."


def _dimension_status(estimate_cm3: float, actual_cm3: float, label: str) -> str:
    if actual_cm3 <= 0:
        return "CARRIER_CUBE_UNAVAILABLE"
    if estimate_cm3 <= 0:
        return f"{label}_CUBE_UNAVAILABLE"
    gap_pct = (estimate_cm3 - actual_cm3) / actual_cm3 * 100.0
    if abs(gap_pct) <= DIMENSION_ALIGNMENT_THRESHOLD_PCT:
        return f"{label}_ALIGNS_WITH_CARRIER"
    if gap_pct <= -DIMENSION_MATERIAL_THRESHOLD_PCT:
        return f"{label}_CUBE_UNDERSTATED_VS_CARRIER"
    if gap_pct >= DIMENSION_MATERIAL_THRESHOLD_PCT:
        return f"{label}_CUBE_OVERSTATED_VS_CARRIER"
    return f"{label}_MODERATE_CARRIER_GAP"


def _write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str, str, str], dict[str, float | int]] = defaultdict(
        lambda: {"count": 0, "actual_base": 0.0, "actual_fuel": 0.0, "actual": 0.0, "model_base": 0.0, "model_fuel": 0.0, "model": 0.0}
    )
    for row in rows:
        group = groups[(row["source_file"], row["match_method"], row["site"] or "Unmatched", row["primary_cost_driver"])]
        group["count"] += 1
        group["actual_base"] += _number(row["actual_base_cost_ex_gst"])
        group["actual_fuel"] += _number(row["actual_fuel_surcharge_ex_gst"])
        group["actual"] += _number(row["actual_invoice_cost_ex_gst"])
        group["model_base"] += _number(row["modelled_goexpress_base_cost_ex_gst"])
        group["model_fuel"] += _number(row["modelled_goexpress_fuel_surcharge_ex_gst"])
        group["model"] += _number(row["modelled_goexpress_cost_ex_gst"])
    output = [
        {
            "source_file": key[0], "match_method": key[1], "site": key[2], "primary_cost_driver": key[3], "billed_consignments": value["count"],
            "actual_base_cost_ex_gst": _fmt(value["actual_base"]), "actual_fuel_surcharge_ex_gst": _fmt(value["actual_fuel"]), "actual_invoice_cost_ex_gst": _fmt(value["actual"]),
            "modelled_goexpress_base_cost_ex_gst": _fmt(value["model_base"]), "modelled_goexpress_fuel_surcharge_ex_gst": _fmt(value["model_fuel"]), "modelled_goexpress_cost_ex_gst": _fmt(value["model"]),
            "actual_less_modelled_base_cost_ex_gst": _fmt(value["actual_base"] - value["model_base"]), "actual_less_modelled_fuel_surcharge_ex_gst": _fmt(value["actual_fuel"] - value["model_fuel"]),
            "actual_less_modelled_cost_ex_gst": _fmt(value["actual"] - value["model"]), "actual_less_modelled_cost_pct": _percentage(value["actual"] - value["model"], value["model"]),
        }
        for key, value in sorted(groups.items())
    ]
    _write_csv(path, ["source_file", "match_method", "site", "primary_cost_driver", "billed_consignments", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "modelled_goexpress_base_cost_ex_gst", "modelled_goexpress_fuel_surcharge_ex_gst", "modelled_goexpress_cost_ex_gst", "actual_less_modelled_base_cost_ex_gst", "actual_less_modelled_fuel_surcharge_ex_gst", "actual_less_modelled_cost_ex_gst", "actual_less_modelled_cost_pct"], output)


def _detail_headers() -> list[str]: return ["source_file", "source_sheet", "carrier_invoice_number", "carrier_tracking_number", "consignment_date", "account_source", "origin_postcode", "destination_postcode", "carton_count", "actual_chargeable_weight_kg", "actual_cubic_volume_m3", "actual_cubic_volume_cm3", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_fuel_surcharge_pct", "actual_invoice_cost_ex_gst", "actual_gst", "invoice_notes_or_fuel", "match_method", "site", "invoice_number", "stage3_tracking_number", "stage3_despatch_date", "kmart_accounting_year", "kmart_accounting_week_number_in_year", "modelled_destination_postcode", "modelled_goexpress_zone", "modelled_cost_weight_basis_kg", "modelled_parcel_count", "modelled_goexpress_base_cost_ex_gst", "modelled_goexpress_fuel_surcharge_ex_gst", "modelled_goexpress_fuel_surcharge_pct", "modelled_goexpress_fuel_surcharge_source", "predicted_fuel_surcharge_ex_gst", "modelled_goexpress_cost_ex_gst", "estimator_parcel_cube_cm3", "estimator_parcel_cube_m3", "shippit_chargeable_weight_kg", "shippit_chargeable_cube_cm3", "shippit_chargeable_cube_m3", "actual_less_modelled_base_cost_ex_gst", "actual_less_modelled_fuel_surcharge_ex_gst", "actual_less_modelled_cost_ex_gst", "actual_less_modelled_cost_pct", "actual_less_modelled_chargeable_weight_kg", "actual_less_shippit_chargeable_weight_kg", "actual_less_estimator_cube_cm3", "actual_less_shippit_cube_cm3", "estimator_dimension_diagnostic", "shippit_dimension_diagnostic", "primary_cost_driver", "root_cause", "reconciliation_note"]
def _read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle: yield from csv.DictReader(handle)
def _write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle: writer = csv.DictWriter(handle, fieldnames=headers); writer.writeheader(); writer.writerows(rows)
def _normalise(value: object) -> str: return str(value or "").strip().upper()
def _digits(value: object) -> str: return "".join(char for char in str(value or "") if char.isdigit())
def _number(value: object) -> float:
    try: result = float(value or 0)
    except (TypeError, ValueError): return 0.0
    return result if math.isfinite(result) else 0.0
def _fmt(value: float) -> str: return f"{value:.6f}".rstrip("0").rstrip(".")
def _fmt_m3(value: float | None) -> str: return _fmt((value or 0.0) / 1_000_000.0) if value is not None else ""
def _percentage(n: float, d: float) -> str: return _fmt(n / d * 100) if d else ""


if __name__ == "__main__": main()
