"""Reconcile Couriers Please billed consignments against Stage 3 model outputs."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.site_config import SITE_CONFIGS

CP_CHARGE_LINES_PATH = Path("outputs/carrier_invoices/couriers_please_charge_lines.csv")
STAGE3_PATHS = {
    "MOOREBANK": Path("outputs/stage3_moorebank_cost_estimates_full.csv"),
    "TRUGANINA": Path("outputs/stage3_truganina_cost_estimates_full.csv"),
}
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
RECONCILIATION_PATH = OUTPUT_DIRECTORY / "couriers_please_reconciliation.csv"
CONTROL_SUMMARY_PATH = OUTPUT_DIRECTORY / "couriers_please_reconciliation_summary.csv"
FUEL_NORMS_PATH = OUTPUT_DIRECTORY / "couriers_please_fuel_norms.csv"
DIMENSION_DIAGNOSTICS_PATH = OUTPUT_DIRECTORY / "couriers_please_dimension_diagnostics_summary.csv"
COST_BRIDGE_PATH = OUTPUT_DIRECTORY / "couriers_please_reconciliation_cost_bridge.csv"
MIN_LANE_FUEL_NORM_SAMPLE_COUNT = 30

# CP uses PWF service codes on invoice files for the corresponding PA pricing
# families used by the estimator's ratecards.
CP_SERVICE_CODE_MAP = {
    "PWF0": "PA0",
    "PWF1": "PA1",
    "PWF3": "PA3",
    "PWF5": "PA5",
}


@dataclass(frozen=True)
class FuelNorm:
    sample_count: int
    p10_pct: float
    p50_pct: float
    p90_pct: float
    source: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charge-lines", type=Path, default=CP_CHARGE_LINES_PATH)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument("--moorebank-stage3", type=Path, default=STAGE3_PATHS["MOOREBANK"])
    parser.add_argument("--truganina-stage3", type=Path, default=STAGE3_PATHS["TRUGANINA"])
    args = parser.parse_args()

    stage3_paths = {"MOOREBANK": args.moorebank_stage3, "TRUGANINA": args.truganina_stage3}
    missing_paths = [path for path in [args.charge_lines, *stage3_paths.values()] if not path.exists()]
    if missing_paths:
        raise FileNotFoundError("Missing input files: " + ", ".join(str(path) for path in missing_paths))

    args.output_directory.mkdir(parents=True, exist_ok=True)
    reconciliation_path = args.output_directory / RECONCILIATION_PATH.name
    summary_path = args.output_directory / CONTROL_SUMMARY_PATH.name
    fuel_norms_path = args.output_directory / FUEL_NORMS_PATH.name
    dimension_diagnostics_path = args.output_directory / DIMENSION_DIAGNOSTICS_PATH.name
    cost_bridge_path = args.output_directory / COST_BRIDGE_PATH.name
    fuel_norms, fallback_fuel_norm = _write_fuel_norms(args.charge_lines, fuel_norms_path)
    _write_reconciliation(args.charge_lines, stage3_paths, reconciliation_path, summary_path, fuel_norms, fallback_fuel_norm)
    _write_dimension_diagnostics(reconciliation_path, dimension_diagnostics_path)
    _write_cost_bridge(reconciliation_path, cost_bridge_path)
    print(f"Wrote {fuel_norms_path}")
    print(f"Wrote {dimension_diagnostics_path}")
    print(f"Wrote {cost_bridge_path}")
    print(f"Wrote {reconciliation_path}")
    print(f"Wrote {summary_path}")


def _write_reconciliation(
    charge_lines_path: Path,
    stage3_paths: dict[str, Path],
    reconciliation_path: Path,
    summary_path: Path,
    fuel_norms: dict[tuple[str, str], FuelNorm],
    fallback_fuel_norm: FuelNorm,
) -> None:
    by_tracking, by_invoice = _load_cp_stage3_rows(stage3_paths)
    summaries: dict[tuple[str, str, str, str], dict[str, float | int]] = defaultdict(
        lambda: {
            "billed_consignments": 0,
            "actual_base_cost_ex_gst": 0.0,
            "actual_fuel_surcharge_ex_gst": 0.0,
            "actual_other_surcharges_ex_gst": 0.0,
            "actual_cost_ex_gst": 0.0,
            "modelled_base_cost_ex_gst": 0.0,
            "predicted_fuel_surcharge_ex_gst": 0.0,
            "modelled_cost_including_fuel_ex_gst": 0.0,
            "cost_variance_ex_gst": 0.0,
        }
    )
    with charge_lines_path.open(encoding="utf-8", newline="") as input_file, reconciliation_path.open(
        "w", encoding="utf-8", newline=""
    ) as output_file:
        reader = csv.DictReader(input_file)
        writer = csv.DictWriter(output_file, fieldnames=_reconciliation_headers())
        writer.writeheader()
        for actual in reader:
            stage3_row, match_method = _find_stage3_match(actual, by_tracking, by_invoice)
            result = _build_result(actual, stage3_row, match_method, fuel_norms, fallback_fuel_norm)
            writer.writerow(result)
            summary_key = (result["source_file"], result["match_method"], result["site"] or "Unmatched", result["primary_cost_driver"])
            summary = summaries[summary_key]
            summary["billed_consignments"] = int(summary["billed_consignments"]) + 1
            summary["actual_base_cost_ex_gst"] = float(summary["actual_base_cost_ex_gst"]) + _number(result["actual_base_cost_ex_gst"])
            summary["actual_fuel_surcharge_ex_gst"] = float(summary["actual_fuel_surcharge_ex_gst"]) + _number(result["actual_fuel_surcharge_ex_gst"])
            summary["actual_other_surcharges_ex_gst"] = float(summary["actual_other_surcharges_ex_gst"]) + _number(result["actual_other_surcharges_ex_gst"])
            summary["actual_cost_ex_gst"] = float(summary["actual_cost_ex_gst"]) + _number(result["actual_invoice_cost_ex_gst"])
            summary["modelled_base_cost_ex_gst"] = float(summary["modelled_base_cost_ex_gst"]) + _number(result["modelled_cp_base_cost_ex_gst"])
            summary["predicted_fuel_surcharge_ex_gst"] = float(summary["predicted_fuel_surcharge_ex_gst"]) + _number(result["predicted_fuel_surcharge_ex_gst"])
            summary["modelled_cost_including_fuel_ex_gst"] = float(summary["modelled_cost_including_fuel_ex_gst"]) + _number(result["modelled_cp_cost_including_fuel_ex_gst"])
            summary["cost_variance_ex_gst"] = float(summary["cost_variance_ex_gst"]) + _number(result["actual_less_modelled_cost_including_fuel_ex_gst"])

    with summary_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=_summary_headers())
        writer.writeheader()
        for (source_file, match_method, site, primary_cost_driver), summary in sorted(summaries.items()):
            actual_cost = float(summary["actual_cost_ex_gst"])
            modelled_cost = float(summary["modelled_cost_including_fuel_ex_gst"])
            writer.writerow(
                {
                    "source_file": source_file,
                    "match_method": match_method,
                    "site": site,
                    "primary_cost_driver": primary_cost_driver,
                    "billed_consignments": summary["billed_consignments"],
                    "actual_base_cost_ex_gst": _format_number(float(summary["actual_base_cost_ex_gst"])),
                    "actual_fuel_surcharge_ex_gst": _format_number(float(summary["actual_fuel_surcharge_ex_gst"])),
                    "actual_other_surcharges_ex_gst": _format_number(float(summary["actual_other_surcharges_ex_gst"])),
                    "actual_invoice_cost_ex_gst": _format_number(actual_cost),
                    "modelled_cp_base_cost_ex_gst": _format_number(float(summary["modelled_base_cost_ex_gst"])),
                    "predicted_fuel_surcharge_ex_gst": _format_number(float(summary["predicted_fuel_surcharge_ex_gst"])),
                    "modelled_cp_cost_including_fuel_ex_gst": _format_number(modelled_cost),
                    "actual_less_modelled_cost_including_fuel_ex_gst": _format_number(float(summary["cost_variance_ex_gst"])),
                    "actual_less_modelled_cost_pct": _percentage(actual_cost - modelled_cost, modelled_cost),
                }
            )


def _write_fuel_norms(charge_lines_path: Path, fuel_norms_path: Path) -> tuple[dict[tuple[str, str], FuelNorm], FuelNorm]:
    lane_samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    all_samples: list[float] = []
    with charge_lines_path.open(encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            base_cost = _number(row.get("base_cost_ex_gst"))
            fuel_cost = _number(row.get("fuel_surcharge_ex_gst"))
            if base_cost <= 0 or fuel_cost < 0:
                continue
            fuel_pct = fuel_cost / base_cost * 100.0
            lane_key = (_normalise(row.get("origin_zone")), _normalise(row.get("destination_zone")))
            lane_samples[lane_key].append(fuel_pct)
            all_samples.append(fuel_pct)

    fallback = _fuel_norm(all_samples, "ALL_LANES_P50")
    norms = {lane: _fuel_norm(samples, "LANE_P50") for lane, samples in lane_samples.items()}
    with fuel_norms_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=["origin_zone", "destination_zone", "sample_count", "p10_fuel_surcharge_pct", "p50_fuel_surcharge_pct", "p90_fuel_surcharge_pct", "norm_source"],
        )
        writer.writeheader()
        for (origin_zone, destination_zone), norm in sorted(norms.items()):
            writer.writerow(
                {
                    "origin_zone": origin_zone,
                    "destination_zone": destination_zone,
                    "sample_count": norm.sample_count,
                    "p10_fuel_surcharge_pct": _format_number(norm.p10_pct),
                    "p50_fuel_surcharge_pct": _format_number(norm.p50_pct),
                    "p90_fuel_surcharge_pct": _format_number(norm.p90_pct),
                    "norm_source": norm.source,
                }
            )
    return norms, fallback


def _write_dimension_diagnostics(reconciliation_path: Path, diagnostics_path: Path) -> None:
    groups: dict[tuple[str, str, str, str], dict[str, float | int]] = defaultdict(
        lambda: {"consignments": 0, "actual_cube_cm3": 0.0, "estimator_cube_cm3": 0.0, "shippit_cube_cm3": 0.0}
    )
    with reconciliation_path.open(encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            if row.get("match_method") != "TRACKING_NUMBER" or _number(row.get("actual_chargeable_cube_cm3")) <= 0:
                continue
            key = (
                row.get("source_file", ""),
                row.get("site", ""),
                row.get("estimator_dimension_diagnostic", ""),
                row.get("shippit_dimension_diagnostic", ""),
            )
            group = groups[key]
            group["consignments"] = int(group["consignments"]) + 1
            group["actual_cube_cm3"] = float(group["actual_cube_cm3"]) + _number(row.get("actual_chargeable_cube_cm3"))
            group["estimator_cube_cm3"] = float(group["estimator_cube_cm3"]) + _number(row.get("estimator_parcel_cube_cm3"))
            group["shippit_cube_cm3"] = float(group["shippit_cube_cm3"]) + _number(row.get("shippit_chargeable_cube_cm3"))

    with diagnostics_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=_dimension_diagnostic_headers())
        writer.writeheader()
        for (source_file, site, estimator_status, shippit_status), group in sorted(groups.items()):
            actual_cube = float(group["actual_cube_cm3"])
            estimator_cube = float(group["estimator_cube_cm3"])
            shippit_cube = float(group["shippit_cube_cm3"])
            writer.writerow(
                {
                    "source_file": source_file,
                    "site": site,
                    "estimator_dimension_diagnostic": estimator_status,
                    "shippit_dimension_diagnostic": shippit_status,
                    "recommended_action": _dimension_recommended_action(estimator_status, shippit_status),
                    "matched_consignments": group["consignments"],
                    "carrier_cube_cm3": _format_number(actual_cube),
                    "estimator_cube_cm3": _format_number(estimator_cube),
                    "shippit_cube_cm3": _format_number(shippit_cube),
                    "estimator_vs_carrier_cube_pct": _percentage(estimator_cube - actual_cube, actual_cube),
                    "shippit_vs_carrier_cube_pct": _percentage(shippit_cube - actual_cube, actual_cube),
                }
            )


def _dimension_recommended_action(estimator_status: str, shippit_status: str) -> str:
    if shippit_status == "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER" and estimator_status == "ESTIMATOR_ALIGNS_WITH_CARRIER":
        return "Prioritise Shippit/CFC dimensional hand-off review; the parcel estimator is supported by carrier measurement."
    if estimator_status == "ESTIMATOR_CUBE_UNDERSTATED_VS_CARRIER":
        return "Review CFC SKU dimensions, air norms, and carton selection; carrier cube exceeds the parcel estimate."
    if estimator_status == "ESTIMATOR_CUBE_OVERSTATED_VS_CARRIER":
        return "Review rectangularisation and air assumptions; parcel estimate exceeds carrier cube."
    if shippit_status == "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER":
        return "Investigate Shippit/CFC dimensional hand-off; Shippit is below carrier measurement."
    if shippit_status == "SHIPPIT_CUBE_OVERSTATED_VS_CARRIER":
        return "Investigate Shippit dimensional data; Shippit is above carrier measurement."
    return "Monitor; Shippit and carrier dimensions are broadly aligned for this group."


def _write_cost_bridge(reconciliation_path: Path, bridge_path: Path) -> None:
    groups: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {
            "matched_consignments": 0,
            "model_base": 0.0,
            "model_fuel": 0.0,
            "actual_base": 0.0,
            "actual_fuel": 0.0,
            "actual_other": 0.0,
            "carrier_cube_cm3": 0.0,
            "estimator_cube_cm3": 0.0,
            "shippit_cube_cm3": 0.0,
            "buckets": defaultdict(
                lambda: {"count": 0, "base": 0.0, "fuel": 0.0, "other": 0.0, "carrier_cube_cm3": 0.0, "estimator_cube_cm3": 0.0, "shippit_cube_cm3": 0.0, "estimator_aligned_count": 0}
            ),
        }
    )
    with reconciliation_path.open(encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            if row.get("match_method") != "TRACKING_NUMBER":
                continue
            group = groups[(row.get("source_file", ""), row.get("site", ""))]
            group["matched_consignments"] = int(group["matched_consignments"]) + 1
            group["model_base"] = float(group["model_base"]) + _number(row.get("modelled_cp_base_cost_ex_gst"))
            group["model_fuel"] = float(group["model_fuel"]) + _number(row.get("predicted_fuel_surcharge_ex_gst"))
            group["actual_base"] = float(group["actual_base"]) + _number(row.get("actual_base_cost_ex_gst"))
            group["actual_fuel"] = float(group["actual_fuel"]) + _number(row.get("actual_fuel_surcharge_ex_gst"))
            group["actual_other"] = float(group["actual_other"]) + _number(row.get("actual_other_surcharges_ex_gst"))
            group["carrier_cube_cm3"] = float(group["carrier_cube_cm3"]) + _number(row.get("actual_chargeable_cube_cm3"))
            group["estimator_cube_cm3"] = float(group["estimator_cube_cm3"]) + _number(row.get("estimator_parcel_cube_cm3"))
            group["shippit_cube_cm3"] = float(group["shippit_cube_cm3"]) + _number(row.get("shippit_chargeable_cube_cm3"))
            bucket = group["buckets"][row.get("primary_cost_driver", "NO_MATERIAL_VARIANCE_DRIVER")]
            bucket["count"] += 1
            bucket["base"] += _number(row.get("actual_less_modelled_base_cost_ex_gst"))
            bucket["fuel"] += _number(row.get("actual_less_predicted_fuel_ex_gst"))
            bucket["other"] += _number(row.get("actual_other_surcharges_ex_gst"))
            bucket["carrier_cube_cm3"] += _number(row.get("actual_chargeable_cube_cm3"))
            bucket["estimator_cube_cm3"] += _number(row.get("estimator_parcel_cube_cm3"))
            bucket["shippit_cube_cm3"] += _number(row.get("shippit_chargeable_cube_cm3"))
            if row.get("estimator_dimension_diagnostic") == "ESTIMATOR_ALIGNS_WITH_CARRIER":
                bucket["estimator_aligned_count"] += 1

    with bridge_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=_cost_bridge_headers())
        writer.writeheader()
        for (source_file, site), group in sorted(groups.items()):
            model_base = float(group["model_base"])
            model_fuel = float(group["model_fuel"])
            model_total = model_base + model_fuel
            actual_base = float(group["actual_base"])
            actual_fuel = float(group["actual_fuel"])
            actual_other = float(group["actual_other"])
            actual_total = actual_base + actual_fuel + actual_other
            writer.writerow(_bridge_row(source_file, site, "MODEL_ESTIMATED_COST", "", int(group["matched_consignments"]), model_base, model_fuel, 0.0, model_total, "", None, None, None, 0))
            for primary_cost_driver, bucket in sorted(group["buckets"].items()):
                base = float(bucket["base"])
                fuel = float(bucket["fuel"])
                other = float(bucket["other"])
                total = base + fuel + other
                writer.writerow(_bridge_row(source_file, site, "VARIANCE_BUCKET", primary_cost_driver, int(bucket["count"]), base, fuel, other, total, _percentage(total, model_total), float(bucket["carrier_cube_cm3"]), float(bucket["estimator_cube_cm3"]), float(bucket["shippit_cube_cm3"]), int(bucket["estimator_aligned_count"])))
            writer.writerow(_bridge_row(source_file, site, "ACTUAL_INVOICED_COST", "", int(group["matched_consignments"]), actual_base, actual_fuel, actual_other, actual_total, _percentage(actual_total - model_total, model_total), float(group["carrier_cube_cm3"]), float(group["estimator_cube_cm3"]), float(group["shippit_cube_cm3"]), 0))


def _bridge_row(
    source_file: str,
    site: str,
    bridge_row_type: str,
    primary_cost_driver: str,
    matched_consignments: int,
    base: float,
    fuel: float,
    other: float,
    total: float,
    variance_pct: str,
    carrier_cube_cm3: float | None,
    estimator_cube_cm3: float | None,
    shippit_cube_cm3: float | None,
    estimator_aligned_count: int,
) -> dict[str, str | int]:
    return {
        "source_file": source_file,
        "site": site,
        "carrier": "Couriers Please",
        "bridge_row_type": bridge_row_type,
        "primary_cost_driver": primary_cost_driver,
        "matched_consignments": matched_consignments,
        "base_freight_ex_gst": _format_number(base),
        "fuel_surcharge_ex_gst": _format_number(fuel),
        "other_surcharges_ex_gst": _format_number(other),
        "total_ex_gst": _format_number(total),
        "variance_from_model_pct": variance_pct,
        "carrier_cube_m3": _format_m3(carrier_cube_cm3),
        "estimated_parcel_cube_m3": _format_m3(estimator_cube_cm3),
        "shippit_cube_m3": _format_m3(shippit_cube_cm3),
        "estimator_vs_carrier_cube_pct": _percentage((estimator_cube_cm3 or 0.0) - (carrier_cube_cm3 or 0.0), carrier_cube_cm3 or 0.0) if carrier_cube_cm3 else "",
        "shippit_vs_carrier_cube_pct": _percentage((shippit_cube_cm3 or 0.0) - (carrier_cube_cm3 or 0.0), carrier_cube_cm3 or 0.0) if carrier_cube_cm3 else "",
        "estimator_aligned_with_carrier_count": estimator_aligned_count or "",
    }


def _fuel_norm(samples: list[float], source: str) -> FuelNorm:
    if not samples:
        return FuelNorm(0, 0.0, 0.0, 0.0, "NO_FUEL_NORM")
    return FuelNorm(
        len(samples),
        _percentile(samples, 0.10),
        _percentile(samples, 0.50),
        _percentile(samples, 0.90),
        source,
    )


def _select_fuel_norm(actual: dict[str, str], norms: dict[tuple[str, str], FuelNorm], fallback: FuelNorm) -> FuelNorm:
    lane_key = (_normalise(actual.get("origin_zone")), _normalise(actual.get("destination_zone")))
    lane_norm = norms.get(lane_key)
    return lane_norm if lane_norm and lane_norm.sample_count >= MIN_LANE_FUEL_NORM_SAMPLE_COUNT else fallback


def _classify_root_cause(
    actual: dict[str, str],
    model: dict[str, str],
    match_method: str,
    actual_cube_cm3: float,
    estimated_cube_cm3: float,
    shippit_cube_cm3: float,
    actual_chargeable_weight_kg: float,
    shippit_chargeable_weight_kg: float,
    actual_base_cost: float,
    modelled_base_cost: float,
    actual_fuel_cost: float,
    predicted_fuel_cost: float,
) -> tuple[str, str, str, str, str]:
    if match_method != "TRACKING_NUMBER":
        return "MATCHING_GAP", "CARRIER_CUBE_UNAVAILABLE", "CARRIER_CUBE_UNAVAILABLE", "MATCHING_GAP", "No unique tracking-number match is available, so physical and cost comparisons cannot yet be assessed."

    causes: list[str] = []
    notes: list[str] = []
    physical_causes: list[str] = []
    dimension_diagnostic = "CARRIER_CUBE_UNAVAILABLE"
    shippit_dimension_diagnostic = "CARRIER_CUBE_UNAVAILABLE"
    if actual_cube_cm3 > 0:
        estimator_gap_pct = (estimated_cube_cm3 - actual_cube_cm3) / actual_cube_cm3 * 100.0 if estimated_cube_cm3 else 0.0
        shippit_gap_pct = (shippit_cube_cm3 - actual_cube_cm3) / actual_cube_cm3 * 100.0 if shippit_cube_cm3 else 0.0
        if abs(estimator_gap_pct) <= 15:
            dimension_diagnostic = "ESTIMATOR_ALIGNS_WITH_CARRIER"
        elif estimator_gap_pct <= -25:
            dimension_diagnostic = "ESTIMATOR_CUBE_UNDERSTATED_VS_CARRIER"
        elif estimator_gap_pct >= 25:
            dimension_diagnostic = "ESTIMATOR_CUBE_OVERSTATED_VS_CARRIER"
        else:
            dimension_diagnostic = "ESTIMATOR_MODERATE_CARRIER_GAP"
        if abs(shippit_gap_pct) <= 15:
            shippit_dimension_diagnostic = "SHIPPIT_ALIGNS_WITH_CARRIER"
        elif shippit_gap_pct <= -25:
            shippit_dimension_diagnostic = "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER"
            physical_causes.append("SHIPPIT_CUBE_UNDERSTATED")
            notes.append("Carrier cube is materially above Shippit, which directly understates the current Shippit-scaled CP pricing basis.")
        elif shippit_gap_pct >= 25:
            shippit_dimension_diagnostic = "SHIPPIT_CUBE_OVERSTATED_VS_CARRIER"
            physical_causes.append("SHIPPIT_CUBE_OVERSTATED")
            notes.append("Carrier cube is materially below Shippit, which directly overstates the current Shippit-scaled CP pricing basis.")
        else:
            shippit_dimension_diagnostic = "SHIPPIT_MODERATE_CARRIER_GAP"
        notes.append(
            f"Carrier cube {actual_cube_cm3:,.0f} cm3; estimator {estimated_cube_cm3:,.0f} cm3 ({estimator_gap_pct:+.1f}%); Shippit {shippit_cube_cm3:,.0f} cm3 ({shippit_gap_pct:+.1f}%)."
        )
    else:
        notes.append("CP invoice did not provide a usable total volume for carrier-cube comparison.")

    if actual_chargeable_weight_kg > 0 and shippit_chargeable_weight_kg > 0 and abs(actual_chargeable_weight_kg - shippit_chargeable_weight_kg) > 0.5:
        physical_causes.append("SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH")
        notes.append(
            f"CP chargeable weight {actual_chargeable_weight_kg:.2f} kg versus Shippit chargeable weight {shippit_chargeable_weight_kg:.2f} kg "
            f"(CP less Shippit {actual_chargeable_weight_kg - shippit_chargeable_weight_kg:+.2f} kg)."
        )

    causes.extend(physical_causes)

    manifest = str(actual.get("manifest_reference") or "")
    if "CHANGING" in manifest.upper():
        causes.append("CARRIER_SERVICE_REGRADE")
        notes.append(f"CP manifest indicates a service change: {manifest}.")
    base_cost_variance_reason = _base_cost_variance_reason(actual, model, actual_base_cost, modelled_base_cost)
    if base_cost_variance_reason:
        causes.append(base_cost_variance_reason)
        notes.append(f"Carrier base ${actual_base_cost:.2f} versus modelled base ${modelled_base_cost:.2f}: {base_cost_variance_reason}.")
    if abs(actual_fuel_cost - predicted_fuel_cost) > 0.05:
        causes.append("FUEL_NORM_VARIANCE")
        notes.append(f"Carrier fuel ${actual_fuel_cost:.2f} versus p50 fuel prediction ${predicted_fuel_cost:.2f}.")
    if not causes:
        causes.append("NO_MATERIAL_VARIANCE_DRIVER")
    primary_root_cause = "SHIPPIT_PHYSICAL_BASIS_MISMATCH" if physical_causes else next(
        (
            cause
            for cause in (
                "CARRIER_SERVICE_REGRADE",
                "CP_ORIGIN_ZONE_MISMATCH",
                "CP_DESTINATION_ZONE_MISMATCH",
                "CP_SERVICE_MISMATCH",
                "CP_CHARGEABLE_WEIGHT_MISMATCH",
                "CP_RATE_OR_CHARGE_MISMATCH",
                "FUEL_NORM_VARIANCE",
            )
            if cause in causes
        ),
        "NO_MATERIAL_VARIANCE_DRIVER",
    )
    return primary_root_cause, dimension_diagnostic, shippit_dimension_diagnostic, " | ".join(causes), " ".join(notes)


def _base_cost_variance_reason(actual: dict[str, str], model: dict[str, str], actual_base_cost: float, modelled_base_cost: float) -> str:
    if abs(actual_base_cost - modelled_base_cost) <= 0.05:
        return ""
    site_config = SITE_CONFIGS.get(str(model.get("site") or "").upper())
    expected_origin_zone = site_config.cp_origin_zone if site_config else ""
    if expected_origin_zone and _normalise(actual.get("origin_zone")) != _normalise(expected_origin_zone):
        return "CP_ORIGIN_ZONE_MISMATCH"
    if model.get("cp_zone") and _normalise(actual.get("destination_zone")) != _normalise(model.get("cp_zone")):
        return "CP_DESTINATION_ZONE_MISMATCH"
    actual_service = _normalise_cp_service(actual.get("service_code"))
    modelled_service = _modelled_cp_service(model.get("cp_pricing_ratecard_id"))
    if actual_service and modelled_service and actual_service != modelled_service:
        return "CP_SERVICE_MISMATCH"
    actual_pricing_weight = math.ceil(max(_number(actual.get("chargeable_weight_kg")), 0.0))
    modelled_pricing_weight = _number(model.get("cp_pricing_chargeable_weight_kg"))
    if modelled_pricing_weight and actual_pricing_weight != modelled_pricing_weight:
        return "CP_CHARGEABLE_WEIGHT_MISMATCH"
    return "CP_RATE_OR_CHARGE_MISMATCH"


def _normalise_cp_service(value: object) -> str:
    service = _normalise(value)
    return CP_SERVICE_CODE_MAP.get(service, service)


def _cp_service_mapping_status(value: object) -> str:
    service = _normalise(value)
    if service in CP_SERVICE_CODE_MAP:
        return "PWF_TO_PA"
    if service in set(CP_SERVICE_CODE_MAP.values()):
        return "DIRECT_PA"
    return "NOT_MAPPED"


def _modelled_cp_service(ratecard_id: object) -> str:
    parts = _normalise(ratecard_id).split("_")
    return parts[1] if len(parts) >= 2 else ""


def _load_cp_stage3_rows(stage3_paths: dict[str, Path]) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    by_tracking: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_invoice: dict[str, list[dict[str, str]]] = defaultdict(list)
    for site, path in stage3_paths.items():
        with path.open(encoding="utf-8-sig", newline="") as input_file:
            for row in csv.DictReader(input_file):
                if _normalise(row.get("actual_carrier")) != "COURIERS PLEASE":
                    continue
                row["site"] = site
                tracking_number = _normalise(row.get("carrier_tracking_number"))
                invoice_number = _normalise(row.get("invoice_number"))
                if tracking_number:
                    by_tracking[tracking_number].append(row)
                if invoice_number:
                    by_invoice[invoice_number].append(row)
    return by_tracking, by_invoice


def _find_stage3_match(
    actual: dict[str, str],
    by_tracking: dict[str, list[dict[str, str]]],
    by_invoice: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, str] | None, str]:
    tracking_matches = by_tracking.get(_normalise(actual.get("carrier_tracking_number")), [])
    if len(tracking_matches) == 1:
        return tracking_matches[0], "TRACKING_NUMBER"
    if len(tracking_matches) > 1:
        return None, "AMBIGUOUS_TRACKING_NUMBER"
    invoice_matches = by_invoice.get(_normalise(actual.get("kmart_invoice_number")), [])
    if len(invoice_matches) == 1:
        return invoice_matches[0], "KMART_INVOICE_NUMBER"
    if len(invoice_matches) > 1:
        return None, "AMBIGUOUS_KMART_INVOICE_NUMBER"
    return None, "UNMATCHED"


def _build_result(
    actual: dict[str, str],
    model: dict[str, str] | None,
    match_method: str,
    fuel_norms: dict[tuple[str, str], FuelNorm],
    fallback_fuel_norm: FuelNorm,
) -> dict[str, str]:
    model = model or {}
    actual_cost = _number(actual.get("actual_invoice_cost_ex_gst"))
    actual_base_cost = _number(actual.get("base_cost_ex_gst"))
    actual_fuel_cost = _number(actual.get("fuel_surcharge_ex_gst"))
    actual_other_cost = actual_cost - actual_base_cost - actual_fuel_cost
    modelled_base_cost = _number(model.get("actual_est_cost_ex_gst"))
    fuel_norm = _select_fuel_norm(actual, fuel_norms, fallback_fuel_norm)
    predicted_fuel_cost = modelled_base_cost * fuel_norm.p50_pct / 100.0 if model else 0.0
    modelled_cost_including_fuel = modelled_base_cost + predicted_fuel_cost if model else 0.0
    variance = actual_cost - modelled_cost_including_fuel if model else 0.0
    actual_chargeable_weight = _number(actual.get("chargeable_weight_kg"))
    actual_cube_cm3 = _number(actual.get("total_volume_m3")) * 1_000_000.0
    estimated_cube_cm3 = _number(model.get("est_parcel_cube_total_cm3"))
    shippit_cube_cm3 = _number(model.get("shippit_chargeable_cube_cm3"))
    shippit_weight_kg = _number(model.get("shippit_chargeable_weight_kg"))
    primary_cost_driver, dimension_diagnostic, shippit_dimension_diagnostic, root_cause, commentary = _classify_root_cause(
        actual,
        model,
        match_method,
        actual_cube_cm3,
        estimated_cube_cm3,
        shippit_cube_cm3,
        actual_chargeable_weight,
        shippit_weight_kg,
        actual_base_cost,
        modelled_base_cost,
        actual_fuel_cost,
        predicted_fuel_cost,
    )
    return {
        "source_file": actual.get("source_file", ""),
        "source_row_number": actual.get("source_row_number", ""),
        "carrier_invoice_number": actual.get("carrier_invoice_number", ""),
        "carrier_invoice_date": actual.get("carrier_invoice_date", ""),
        "carrier_consignment_reference": actual.get("carrier_consignment_reference", ""),
        "carrier_tracking_number": actual.get("carrier_tracking_number", ""),
        "kmart_invoice_number": actual.get("kmart_invoice_number", ""),
        "consignment_date": actual.get("consignment_date", ""),
        "actual_service_code": actual.get("service_code", ""),
        "actual_cp_service_family": _normalise_cp_service(actual.get("service_code")),
        "cp_service_mapping_status": _cp_service_mapping_status(actual.get("service_code")),
        "actual_service_name": actual.get("service_name", ""),
        "carrier_manifest_reference": actual.get("manifest_reference", ""),
        "actual_cp_origin_zone": actual.get("origin_zone", ""),
        "actual_cp_destination_zone": actual.get("destination_zone", ""),
        "actual_chargeable_weight_kg": actual.get("chargeable_weight_kg", ""),
        "actual_rounded_chargeable_weight_kg": _format_number(math.ceil(max(actual_chargeable_weight, 0.0))),
        "actual_total_volume_m3": actual.get("total_volume_m3", ""),
        "actual_chargeable_cube_cm3": _format_number(actual_cube_cm3),
        "actual_base_cost_ex_gst": actual.get("base_cost_ex_gst", ""),
        "actual_invoice_cost_ex_gst": actual.get("actual_invoice_cost_ex_gst", ""),
        "actual_fuel_surcharge_ex_gst": actual.get("fuel_surcharge_ex_gst", ""),
        "actual_insurance_ex_gst": actual.get("insurance_ex_gst", ""),
        "actual_other_fee_ex_gst": actual.get("other_fee_ex_gst", ""),
        "actual_other_surcharges_ex_gst": _format_number(actual_other_cost),
        "actual_remote_area_surcharge_ex_gst": actual.get("remote_area_surcharge_ex_gst", ""),
        "match_method": match_method,
        "site": model.get("site", ""),
        "invoice_number": model.get("invoice_number", ""),
        "stage3_invoice_number": model.get("invoice_number", ""),
        "stage3_tracking_number": model.get("carrier_tracking_number", ""),
        "stage3_despatch_date": model.get("despatch_date", ""),
        "kmart_accounting_year": model.get("kmart_accounting_year", ""),
        "kmart_accounting_week_number_in_year": model.get("kmart_accounting_week_number_in_year", ""),
        "stage3_service": model.get("actual_service", ""),
        "modelled_cp_origin_zone": SITE_CONFIGS.get(str(model.get("site") or "").upper()).cp_origin_zone if model.get("site") in SITE_CONFIGS else "",
        "modelled_cp_destination_zone": model.get("cp_zone", ""),
        "modelled_cp_ratecard_id": model.get("cp_pricing_ratecard_id", ""),
        "modelled_cp_service_code": _modelled_cp_service(model.get("cp_pricing_ratecard_id")),
        "modelled_cp_rate_basis_note": model.get("cp_pricing_basis_note", ""),
        "estimator_parcel_cube_cm3": model.get("est_parcel_cube_total_cm3", ""),
        "shippit_chargeable_cube_cm3": model.get("shippit_chargeable_cube_cm3", ""),
        "modelled_cp_chargeable_weight_kg": model.get("cp_pricing_chargeable_weight_kg", ""),
        "shippit_chargeable_weight_kg": model.get("shippit_chargeable_weight_kg", ""),
        "modelled_cp_base_cost_ex_gst": model.get("actual_est_cost_ex_gst", ""),
        "fuel_norm_sample_count": str(fuel_norm.sample_count) if model else "",
        "fuel_norm_p50_pct": _format_number(fuel_norm.p50_pct) if model else "",
        "fuel_norm_source": fuel_norm.source if model else "",
        "predicted_fuel_surcharge_ex_gst": _format_number(predicted_fuel_cost) if model else "",
        "modelled_cp_cost_including_fuel_ex_gst": _format_number(modelled_cost_including_fuel) if model else "",
        "actual_less_modelled_base_cost_ex_gst": _format_number(actual_base_cost - modelled_base_cost) if model else "",
        "actual_less_predicted_fuel_ex_gst": _format_number(actual_fuel_cost - predicted_fuel_cost) if model else "",
        "actual_less_modelled_insurance_ex_gst": actual.get("insurance_ex_gst", "") if model else "",
        "actual_less_modelled_other_fee_ex_gst": actual.get("other_fee_ex_gst", "") if model else "",
        "actual_less_modelled_remote_area_surcharge_ex_gst": actual.get("remote_area_surcharge_ex_gst", "") if model else "",
        "actual_less_modelled_cost_including_fuel_ex_gst": _format_number(variance) if model else "",
        "actual_less_modelled_cost_including_fuel_pct": _percentage(variance, modelled_cost_including_fuel) if model else "",
        "actual_less_modelled_chargeable_weight_kg": _format_number(actual_chargeable_weight - _number(model.get("cp_pricing_chargeable_weight_kg"))) if model else "",
        "actual_less_shippit_chargeable_weight_kg": _format_number(actual_chargeable_weight - _number(model.get("shippit_chargeable_weight_kg"))) if model else "",
        "actual_less_estimator_cube_cm3": _format_number(actual_cube_cm3 - estimated_cube_cm3) if model else "",
        "actual_less_shippit_cube_cm3": _format_number(actual_cube_cm3 - shippit_cube_cm3) if model else "",
        "primary_cost_driver": primary_cost_driver,
        "estimator_dimension_diagnostic": dimension_diagnostic,
        "shippit_dimension_diagnostic": shippit_dimension_diagnostic,
        "root_cause": root_cause,
        "reconciliation_note": commentary,
    }


def _reconciliation_headers() -> list[str]:
    return [
        "source_file", "source_row_number", "carrier_invoice_number", "carrier_invoice_date", "carrier_consignment_reference",
        "carrier_tracking_number", "kmart_invoice_number", "consignment_date", "actual_service_code", "actual_cp_service_family",
        "cp_service_mapping_status", "actual_service_name",
        "carrier_manifest_reference", "actual_cp_origin_zone", "actual_cp_destination_zone", "actual_chargeable_weight_kg",
        "actual_rounded_chargeable_weight_kg", "actual_total_volume_m3", "actual_chargeable_cube_cm3", "actual_base_cost_ex_gst",
        "actual_fuel_surcharge_ex_gst", "actual_insurance_ex_gst", "actual_other_fee_ex_gst", "actual_other_surcharges_ex_gst", "actual_invoice_cost_ex_gst",
        "actual_remote_area_surcharge_ex_gst", "match_method", "site", "invoice_number", "stage3_invoice_number",
        "stage3_tracking_number", "stage3_despatch_date", "kmart_accounting_year", "kmart_accounting_week_number_in_year", "stage3_service", "estimator_parcel_cube_cm3",
        "modelled_cp_origin_zone", "modelled_cp_destination_zone", "modelled_cp_ratecard_id", "modelled_cp_service_code",
        "modelled_cp_rate_basis_note", "shippit_chargeable_cube_cm3", "modelled_cp_chargeable_weight_kg", "shippit_chargeable_weight_kg",
        "modelled_cp_base_cost_ex_gst", "fuel_norm_sample_count", "fuel_norm_p50_pct", "fuel_norm_source",
        "predicted_fuel_surcharge_ex_gst", "modelled_cp_cost_including_fuel_ex_gst", "actual_less_modelled_base_cost_ex_gst",
        "actual_less_predicted_fuel_ex_gst", "actual_less_modelled_insurance_ex_gst", "actual_less_modelled_other_fee_ex_gst",
        "actual_less_modelled_remote_area_surcharge_ex_gst", "actual_less_modelled_cost_including_fuel_ex_gst",
        "actual_less_modelled_cost_including_fuel_pct", "actual_less_modelled_chargeable_weight_kg",
        "actual_less_shippit_chargeable_weight_kg", "actual_less_estimator_cube_cm3", "actual_less_shippit_cube_cm3",
        "primary_cost_driver", "estimator_dimension_diagnostic", "shippit_dimension_diagnostic", "root_cause", "reconciliation_note",
    ]


def _summary_headers() -> list[str]:
    return [
        "source_file", "match_method", "site", "primary_cost_driver", "billed_consignments", "actual_base_cost_ex_gst",
        "actual_fuel_surcharge_ex_gst", "actual_other_surcharges_ex_gst", "actual_invoice_cost_ex_gst",
        "modelled_cp_base_cost_ex_gst", "predicted_fuel_surcharge_ex_gst", "modelled_cp_cost_including_fuel_ex_gst",
        "actual_less_modelled_cost_including_fuel_ex_gst", "actual_less_modelled_cost_pct",
    ]


def _dimension_diagnostic_headers() -> list[str]:
    return [
        "source_file", "site", "estimator_dimension_diagnostic", "shippit_dimension_diagnostic", "recommended_action",
        "matched_consignments", "carrier_cube_cm3", "estimator_cube_cm3", "shippit_cube_cm3",
        "estimator_vs_carrier_cube_pct", "shippit_vs_carrier_cube_pct",
    ]


def _cost_bridge_headers() -> list[str]:
    return [
        "source_file", "site", "carrier", "bridge_row_type", "primary_cost_driver", "matched_consignments",
        "base_freight_ex_gst", "fuel_surcharge_ex_gst", "other_surcharges_ex_gst", "total_ex_gst", "variance_from_model_pct",
        "carrier_cube_m3", "estimated_parcel_cube_m3", "shippit_cube_m3", "estimator_vs_carrier_cube_pct", "shippit_vs_carrier_cube_pct", "estimator_aligned_with_carrier_count",
    ]


def _normalise(value: object) -> str:
    return str(value or "").strip().upper()


def _number(value: object) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _format_m3(cube_cm3: float | None) -> str:
    return _format_number(cube_cm3 / 1_000_000.0) if cube_cm3 is not None else ""


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _percentage(numerator: float, denominator: float) -> str:
    return _format_number((numerator / denominator) * 100.0) if denominator else ""


if __name__ == "__main__":
    main()
