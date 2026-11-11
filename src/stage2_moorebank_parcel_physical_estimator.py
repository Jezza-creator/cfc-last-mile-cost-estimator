from __future__ import annotations

import csv
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

from src.db import get_session
from src.extraction_window import EXTRACTION_END_DATE, EXTRACTION_START_DATE
from src.parcel_shape import estimate_stacked_rectangular_envelope, normalize_dimension_triplet
from src.site_config import SITE_CONFIGS


OUTPUT_DIR = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
INPUT_PATH = Path(
    os.environ.get(
        "LM_STAGE1_INPUT_PATH",
        OUTPUT_DIR / "stage1_moorebank_parcel_assignments_full.csv",
    )
)
PARCEL_CONTENTS_PATH = Path(
    os.environ.get(
        "LM_STAGE1_PARCEL_CONTENTS_PATH",
        OUTPUT_DIR / "stage1_moorebank_parcel_assignment_lines.csv",
    )
)
OUTPUT_PATH = Path(
    os.environ.get("LM_STAGE2_OUTPUT_PATH", OUTPUT_DIR / "stage2_moorebank_parcel_physicals_full.csv")
)
STORE_NAME = SITE_CONFIGS["MOOREBANK"].store_name

# Simple physical bands for a first-pass costing model.
TOTE_WEIGHT_LIMIT_KG = 15.0
TOTE_CUBE_LIMIT_CM3 = 55.0 * 55.0 * 55.0
MANUAL_WEIGHT_LIMIT_KG = 45.0
MANUAL_CUBE_LIMIT_CM3 = 105.0 * 105.0 * 105.0
SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG = 4000.0


@dataclass(frozen=True)
class ParcelInput:
    invoice_number: str
    actual_carrier: str
    actual_parcel_count: int
    estimated_parcel_count: int
    rule_name: str
    process_path: str
    estimated_pack_origin_path: str
    parcel_contents_size_mix: str
    parcel_id: int
    placeholder_parcel: str
    parcel_units: int
    parcel_weight_kg: float
    parcel_cube_cm3: float
    packed_sku_cube_cm3: float
    cube_uplift_cm3: float
    estimated_length_cm: float | None
    estimated_width_cm: float | None
    estimated_height_cm: float | None
    estimated_shape_source: str
    cfc_cube_imputed: str
    parcel_sku_qty_pattern: str
    parcel_packed_sku_cube_pattern_cm3: str
    sku_ids: str
    dimension_triplets: tuple[tuple[float, float, float], ...] = ()


@dataclass(frozen=True)
class AirRatioNorm:
    sample_count: int
    p10_pct: float | None
    p50_pct: float | None
    p90_pct: float | None
    source: str


@dataclass(frozen=True)
class ParcelPhysical:
    invoice_number: str
    actual_carrier: str
    actual_parcel_count: int
    estimated_parcel_count: int
    rule_name: str
    process_path: str
    estimated_pack_origin_path: str
    parcel_contents_size_mix: str
    parcel_id: int
    placeholder_parcel: str
    parcel_units: int
    parcel_weight_kg: float
    parcel_cube_cm3: float
    packed_sku_cube_cm3: float
    cube_uplift_cm3: float
    estimated_length_cm: float | None
    estimated_width_cm: float | None
    estimated_height_cm: float | None
    estimated_shape_source: str
    cfc_cube_imputed: str
    parcel_sku_qty_pattern: str
    parcel_packed_sku_cube_pattern_cm3: str
    fit_band: str
    sku_ids: str
    shippit_chargeable_cube_cm3: float | None = None
    pricing_scale_factor: float | None = None
    normalised_parcel_cube_cm3: float | None = None
    normalised_parcel_cube_m3: float | None = None
    normalised_parcel_volumetric_weight_kg: float | None = None
    rectangularized_parcel_cube_cm3: float | None = None
    expected_air_ratio_norm_pct: float | None = None
    expected_packaging_uplift_cm3: float | None = None
    residual_vs_shippit_cube_cm3: float | None = None


@dataclass(frozen=True)
class ShippitOrderMeta:
    invoice_number: str
    dead_weight_kg: float | None
    volumetric_weight_kg: float | None
    chargeable_weight_kg: float | None


def main() -> None:
    parcel_contents = _load_parcel_contents(PARCEL_CONTENTS_PATH)
    parcel_compositions = _load_parcel_compositions(PARCEL_CONTENTS_PATH)
    parcels = _load_input(INPUT_PATH, parcel_contents)
    shippit_meta = _load_order_meta(sorted({parcel.invoice_number for parcel in parcels}))
    air_ratio_norms = _build_air_ratio_norms(parcels, shippit_meta)
    _write_air_ratio_norm_output(_air_ratio_norm_output_path(OUTPUT_PATH), air_ratio_norms)
    enriched = [
        _estimate_parcel_physical(parcel, _air_ratio_norm_for(parcel, air_ratio_norms))
        for parcel in parcels
    ]
    _write_output(OUTPUT_PATH, enriched, shippit_meta, parcel_compositions)
    validation_rows = _write_validation_output(_validation_output_path(OUTPUT_PATH), enriched, shippit_meta)
    _write_air_ratio_summary_output(_air_ratio_summary_output_path(OUTPUT_PATH), validation_rows)
    _write_keycode_review_output(_keycode_review_output_path(OUTPUT_PATH), enriched, shippit_meta)
    _print_summary(enriched)


def _load_input(
    path: Path,
    parcel_contents: dict[tuple[str, int], tuple[tuple[float, float, float], ...]],
) -> list[ParcelInput]:
    if not path.exists():
        raise FileNotFoundError(f"Missing stage 1 output: {path}")

    parcels: list[ParcelInput] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_packed_cube_cm3 = _to_float(
                row.get("raw_packed_sku_cube_cm3")
                or row.get("packed_sku_cube_cm3")
                or row.get("packed_sku_cube_total_cm3")
                or row.get("parcel_cube")
            ) or 0.0
            estimated_parcel_cube_cm3 = _to_float(
                row.get("estimated_parcel_cube_cm3")
                or row.get("parcel_cube")
                or row.get("parcel_cube_cm3")
            ) or raw_packed_cube_cm3
            parcels.append(
                ParcelInput(
                    invoice_number=str(row["invoice_number"]),
                    actual_carrier=str(row["actual_carrier"]),
                    actual_parcel_count=int(row["actual_parcel_count"] or 0),
                    estimated_parcel_count=int(row["estimated_parcel_count"] or 0),
                    rule_name=str(row["rule_name"]),
                    process_path=str(row.get("process_path") or _process_path_from_rule_name(str(row["rule_name"]))),
                    estimated_pack_origin_path=str(row.get("estimated_pack_origin_path") or row.get("process_path") or _process_path_from_rule_name(str(row["rule_name"]))),
                    parcel_contents_size_mix=str(row.get("parcel_contents_size_mix") or ""),
                    parcel_id=int(row["parcel_id"] or 0),
                    placeholder_parcel=str(row.get("placeholder_parcel") or ""),
                    parcel_units=int(row["parcel_units"] or 0),
                    parcel_weight_kg=float(row["parcel_weight_kg"] or 0.0),
                    parcel_cube_cm3=estimated_parcel_cube_cm3,
                    packed_sku_cube_cm3=raw_packed_cube_cm3,
                    cube_uplift_cm3=estimated_parcel_cube_cm3 - raw_packed_cube_cm3,
                    estimated_length_cm=_to_float(row.get("estimated_length_cm")),
                    estimated_width_cm=_to_float(row.get("estimated_width_cm")),
                    estimated_height_cm=_to_float(row.get("estimated_height_cm")),
                    estimated_shape_source=str(row.get("estimated_shape_source") or ""),
                    cfc_cube_imputed=str(row.get("cfc_cube_imputed") or ""),
                    parcel_sku_qty_pattern=str(row.get("parcel_sku_qty_pattern") or ""),
                    parcel_packed_sku_cube_pattern_cm3=str(row.get("parcel_packed_sku_cube_pattern_cm3") or ""),
                    sku_ids=str(row["sku_ids"] or ""),
                    dimension_triplets=parcel_contents.get(
                        (str(row["invoice_number"]), int(row["parcel_id"] or 0)),
                        (),
                    ),
                )
            )
    return parcels


def _load_parcel_contents(path: Path) -> dict[tuple[str, int], tuple[tuple[float, float, float], ...]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing Stage 1 parcel-content output: {path}. Run Stage 1 before Stage 2."
        )

    contents: dict[tuple[str, int], list[tuple[float, float, float]]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (str(row["invoice_number"]), int(row["parcel_id"] or 0))
            dims = normalize_dimension_triplet(
                _to_float(row.get("cfc_length_cm")),
                _to_float(row.get("cfc_width_cm")),
                _to_float(row.get("cfc_height_cm")),
            )
            if dims is None:
                continue
            qty = max(int(row.get("quantity_packed") or 0), 0)
            contents.setdefault(key, []).extend([dims] * qty)
    return {key: tuple(value) for key, value in contents.items()}


def _load_parcel_compositions(path: Path) -> dict[tuple[str, int], dict[str, int]]:
    """Return parcel-level unit and distinct-keycode counts by review category."""
    composition: dict[tuple[str, int], dict[str, object]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (str(row["invoice_number"]), int(row["parcel_id"] or 0))
            category = str(row.get("size_category") or "").strip()
            category = category if category in {"Toteable", "Non-Toteable", "Bulky"} else "No dims"
            quantity = max(int(row.get("quantity_packed") or 0), 0)
            current = composition.setdefault(
                key,
                {
                    "toteable_unit_count": 0,
                    "non_toteable_unit_count": 0,
                    "bulky_unit_count": 0,
                    "no_dims_unit_count": 0,
                    "toteable_keycodes": set(),
                    "non_toteable_keycodes": set(),
                    "bulky_keycodes": set(),
                    "no_dims_keycodes": set(),
                },
            )
            prefix = category.lower().replace("-", "_").replace(" ", "_")
            current[f"{prefix}_unit_count"] = int(current[f"{prefix}_unit_count"]) + quantity
            current[f"{prefix}_keycodes"].add(str(row.get("sku_id") or ""))

    result: dict[tuple[str, int], dict[str, int]] = {}
    for key, values in composition.items():
        result[key] = {
            "toteable_unit_count": int(values["toteable_unit_count"]),
            "non_toteable_unit_count": int(values["non_toteable_unit_count"]),
            "bulky_unit_count": int(values["bulky_unit_count"]),
            "no_dims_unit_count": int(values["no_dims_unit_count"]),
            "toteable_keycode_count": len(values["toteable_keycodes"]),
            "non_toteable_keycode_count": len(values["non_toteable_keycodes"]),
            "bulky_keycode_count": len(values["bulky_keycodes"]),
            "no_dims_keycode_count": len(values["no_dims_keycodes"]),
        }
    return result


def _estimate_parcel_physical(parcel: ParcelInput, air_norm: AirRatioNorm) -> ParcelPhysical:
    rectangle = estimate_stacked_rectangular_envelope(
        list(parcel.dimension_triplets),
        parcel.packed_sku_cube_cm3,
    )
    if rectangle is None:
        length_cm, width_cm, height_cm = _estimate_dimensions(parcel.packed_sku_cube_cm3, parcel.parcel_units)
        rectangularized_cube_cm3 = parcel.packed_sku_cube_cm3
        shape_source = "CUBE_FALLBACK"
    else:
        length_cm = rectangle.estimated_length_cm
        width_cm = rectangle.estimated_width_cm
        height_cm = rectangle.estimated_height_cm
        rectangularized_cube_cm3 = rectangle.estimated_cube_cm3
        shape_source = rectangle.shape_source

    air_ratio = max(float(air_norm.p50_pct or 0.0), 0.0) / 100.0
    norm_cube_cm3 = parcel.packed_sku_cube_cm3 / (1.0 - air_ratio) if air_ratio < 1.0 else parcel.packed_sku_cube_cm3

    # Rectangularization already models product-combination void. Applying a
    # broad observed p50 air ratio again at every parcel can double count air,
    # especially for large single items. Keep it as a benchmark diagnostic;
    # use the physical envelope as the unscaled parcel estimate.
    estimated_cube_cm3 = rectangularized_cube_cm3
    fit_band = _fit_band(parcel.parcel_weight_kg, estimated_cube_cm3)

    return ParcelPhysical(
        invoice_number=parcel.invoice_number,
        actual_carrier=parcel.actual_carrier,
        actual_parcel_count=parcel.actual_parcel_count,
        estimated_parcel_count=parcel.estimated_parcel_count,
        rule_name=parcel.rule_name,
        process_path=parcel.process_path,
        estimated_pack_origin_path=parcel.estimated_pack_origin_path,
        parcel_contents_size_mix=parcel.parcel_contents_size_mix,
        parcel_id=parcel.parcel_id,
        placeholder_parcel=parcel.placeholder_parcel,
        parcel_units=parcel.parcel_units,
        parcel_weight_kg=parcel.parcel_weight_kg,
        parcel_cube_cm3=estimated_cube_cm3,
        packed_sku_cube_cm3=parcel.packed_sku_cube_cm3,
        cube_uplift_cm3=estimated_cube_cm3 - parcel.packed_sku_cube_cm3,
        estimated_length_cm=length_cm,
        estimated_width_cm=width_cm,
        estimated_height_cm=height_cm,
        estimated_shape_source=shape_source,
        cfc_cube_imputed=parcel.cfc_cube_imputed,
        parcel_sku_qty_pattern=parcel.parcel_sku_qty_pattern,
        parcel_packed_sku_cube_pattern_cm3=parcel.parcel_packed_sku_cube_pattern_cm3,
        fit_band=fit_band,
        sku_ids=parcel.sku_ids,
        rectangularized_parcel_cube_cm3=rectangularized_cube_cm3,
        expected_air_ratio_norm_pct=air_norm.p50_pct,
        expected_packaging_uplift_cm3=max(norm_cube_cm3 - parcel.packed_sku_cube_cm3, 0.0),
    )


def _air_ratio_norm_key(parcel: ParcelInput) -> tuple[str, str, str]:
    return (
        parcel.estimated_pack_origin_path or "UNKNOWN",
        parcel.parcel_contents_size_mix or "Unknown",
        "SINGLE_UNIT" if parcel.parcel_units <= 1 else "MULTI_UNIT",
    )


def _build_air_ratio_norms(
    parcels: list[ParcelInput],
    shippit_meta: dict[str, ShippitOrderMeta],
) -> dict[tuple[str, str, str], AirRatioNorm]:
    grouped: dict[tuple[str, str, str], list[float]] = {}
    invoice_counts: dict[str, int] = {}
    for parcel in parcels:
        invoice_counts[parcel.invoice_number] = invoice_counts.get(parcel.invoice_number, 0) + 1

    for parcel in parcels:
        meta = shippit_meta.get(parcel.invoice_number)
        if invoice_counts.get(parcel.invoice_number) != 1 or parcel.actual_parcel_count != 1:
            continue
        if meta is None or meta.volumetric_weight_kg is None or parcel.packed_sku_cube_cm3 <= 0:
            continue
        shippit_cube_cm3 = meta.volumetric_weight_kg * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG
        if shippit_cube_cm3 <= 0:
            continue
        air_ratio_pct = (shippit_cube_cm3 - parcel.packed_sku_cube_cm3) * 100.0 / shippit_cube_cm3
        if 0.0 <= air_ratio_pct < 100.0:
            grouped.setdefault(_air_ratio_norm_key(parcel), []).append(air_ratio_pct)

    norms: dict[tuple[str, str, str], AirRatioNorm] = {}
    for key, values in grouped.items():
        values.sort()
        norms[key] = AirRatioNorm(
            sample_count=len(values),
            p10_pct=_percentile(values, 10),
            p50_pct=_percentile(values, 50),
            p90_pct=_percentile(values, 90),
            source="SINGLE_PARCEL_SHIPPIT_VOLUMETRIC_CUBE",
        )
    return norms


def _air_ratio_norm_for(
    parcel: ParcelInput,
    norms: dict[tuple[str, str, str], AirRatioNorm],
) -> AirRatioNorm:
    exact_key = _air_ratio_norm_key(parcel)
    exact = norms.get(exact_key)
    if exact is not None:
        return exact

    origin, size_mix, unit_band = exact_key
    candidates = [
        (origin, size_mix, "MULTI_UNIT"),
        (origin, size_mix, "SINGLE_UNIT"),
        (origin, "Toteable only", unit_band),
    ]
    for key in candidates:
        if key in norms:
            fallback = norms[key]
            return AirRatioNorm(
                fallback.sample_count,
                fallback.p10_pct,
                fallback.p50_pct,
                fallback.p90_pct,
                f"FALLBACK:{fallback.source}",
            )
    return AirRatioNorm(0, 0.0, 0.0, 0.0, "NO_SINGLE_PARCEL_NORM")


def _parcelization_scope(estimated_parcel_count: int) -> str:
    return "SINGLE_PARCEL" if estimated_parcel_count <= 1 else "MULTI_PARCEL"


def _pack_process_group(rule_name: str) -> str:
    rule = (rule_name or "").upper()
    if rule.startswith("FULFILLMENT_DETAIL"):
        return "FULFILLMENT_DETAIL"
    if rule.startswith("ALL_AUTOMATION"):
        return "ALL_TOTEABLE"
    if rule.startswith("ALL_MANUAL"):
        return "ALL_MANUAL"
    if "MIXED_AUTOMATION_MANUAL" in rule:
        return "MIXED"
    if "MIXED_MANUAL_TOTEABLE" in rule:
        return "MIXED"
    if "SINGLE_SKU_EQUAL_SPLIT" in rule:
        return "SINGLE_SKU"
    if "CONTAINS_BULKY" in rule:
        return "BULKY"
    if "FALLBACK" in rule:
        return "FALLBACK"
    if "UNKNOWN" in rule:
        return "UNKNOWN"
    return rule or "UNKNOWN"


def _process_path_from_rule_name(rule_name: str) -> str:
    rule = (rule_name or "").upper()
    if rule.startswith("FULFILLMENT_DETAIL"):
        return "FULFILLMENT_DETAIL"
    if rule.startswith("ALL_TOTEABLE"):
        return "TOTEABLE"
    if rule.startswith("ALL_MANUAL"):
        return "MANUAL"
    if "CONTAINS_BULKY" in rule:
        return "BULKY"
    if "SINGLE_SKU_EQUAL_SPLIT" in rule:
        return "SINGLE_SKU"
    if "MIXED_MANUAL_TOTEABLE" in rule or "MIXED_AUTOMATION_MANUAL" in rule:
        return "MIXED"
    if "FALLBACK" in rule:
        return "FALLBACK"
    return "UNKNOWN"


def _load_order_meta(invoices: list[str]) -> dict[str, ShippitOrderMeta]:
    if not invoices:
        return {}

    target_invoices = set(invoices)
    session = get_session()
    try:
        rows = []
        for start in range(0, len(invoices), 5_000):
            invoice_batch = invoices[start : start + 5_000]
            values_sql = ", ".join(
                "('" + invoice.replace("'", "''") + "')" for invoice in invoice_batch
            )
            rows.extend(
                session.sql(
                    f"""
                    WITH target_invoices AS (
                        SELECT column1 AS invoice_number
                        FROM VALUES {values_sql}
                    )
                    SELECT
                        TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
                        s.ORDER_WEIGHT__DEAD_WEIGHT AS dead_weight_kg,
                        s.ORDER_WEIGHT__VOLUMETRIC_WEIGHT AS volumetric_weight_kg,
                        GREATEST(s.ORDER_WEIGHT__DEAD_WEIGHT, s.ORDER_WEIGHT__VOLUMETRIC_WEIGHT) AS chargeable_weight_kg
                    FROM KSFPA.FREIGHT.SHIPPIT_ORDERS s
                    JOIN target_invoices t
                      ON t.invoice_number = TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)
                    WHERE s.STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
                      AND s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                            BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
                    """
                ).collect()
            )
    finally:
        session.close()

    meta: dict[str, ShippitOrderMeta] = {}
    for row in rows:
        invoice_number = str(row["INVOICE_NUMBER"])
        if invoice_number not in target_invoices:
            continue
        meta[invoice_number] = ShippitOrderMeta(
            invoice_number=invoice_number,
            dead_weight_kg=_to_float(row["DEAD_WEIGHT_KG"]),
            volumetric_weight_kg=_to_float(row["VOLUMETRIC_WEIGHT_KG"]),
            chargeable_weight_kg=_to_float(row["CHARGEABLE_WEIGHT_KG"]),
        )
    return meta


def _estimate_dimensions(parcel_cube_cm3: float, parcel_units: int) -> tuple[float | None, float | None, float | None]:
    if parcel_cube_cm3 <= 0:
        return None, None, None

    # Keep the dimensions cube-consistent but slightly elongated so the parcel
    # does not collapse into a perfect cube.
    side = parcel_cube_cm3 ** (1 / 3)
    elongation = 1.15 if parcel_units <= 2 else 1.25 if parcel_units <= 5 else 1.4
    length_cm = round(side * elongation, 1)
    width_cm = round(side, 1)
    height_cm = round(parcel_cube_cm3 / (length_cm * width_cm), 1) if length_cm and width_cm else None

    return length_cm, width_cm, height_cm


def _fit_band(weight_kg: float, cube_cm3: float) -> str:
    if weight_kg <= TOTE_WEIGHT_LIMIT_KG and cube_cm3 <= TOTE_CUBE_LIMIT_CM3:
        return "TOTE_LIKE"
    if weight_kg <= MANUAL_WEIGHT_LIMIT_KG and cube_cm3 <= MANUAL_CUBE_LIMIT_CM3:
        return "MANUAL_LIKE"
    return "BULKY_LIKE"


def _write_output(
    path: Path,
    rows: list[ParcelPhysical],
    shippit_meta: dict[str, ShippitOrderMeta],
    parcel_compositions: dict[tuple[str, int], dict[str, int]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[ParcelPhysical]] = {}
    for row in rows:
        grouped.setdefault(row.invoice_number, []).append(row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "invoice_number",
                "actual_carrier",
                "actual_parcel_count",
                "estimated_parcel_count",
                "rule_name",
                "process_path",
                "estimated_pack_origin_path",
                "parcel_contents_size_mix",
                "parcel_id",
                "placeholder_parcel",
                "parcel_units",
                "parcel_weight_kg",
                "raw_packed_sku_cube_cm3",
                "rectangularized_parcel_cube_cm3",
                "rectangularization_uplift_cm3",
                "p50_air_ratio_norm_pct",
                "p50_air_norm_benchmark_cube_cm3",
                "p50_air_norm_uplift_cm3",
                "estimated_parcel_cube_cm3",
                "parcel_cube_cm3",
                "cube_uplift_cm3",
                "rectangularization_fill_pct_vs_raw_product_cube",
                "rectangularization_air_ratio_pct_vs_raw_product_cube",
                "shippit_chargeable_cube_cm3",
                "pricing_scale_factor",
                "pricing_scaled_parcel_cube_cm3",
                "pricing_scaled_parcel_cube_m3",
                "pricing_scaled_parcel_volumetric_weight_kg",
                "allocated_shippit_fill_pct_vs_raw_product_cube",
                "allocated_shippit_air_ratio_pct_vs_raw_product_cube",
                "cfc_cube_imputed",
                "estimated_length_cm",
                "estimated_width_cm",
                "estimated_height_cm",
                "estimated_shape_source",
                "fit_band",
                "toteable_unit_count",
                "toteable_keycode_count",
                "non_toteable_unit_count",
                "non_toteable_keycode_count",
                "bulky_unit_count",
                "bulky_keycode_count",
                "no_dims_unit_count",
                "no_dims_keycode_count",
                "parcel_sku_qty_pattern",
                "parcel_packed_sku_cube_pattern_cm3",
                "sku_ids",
            ]
        )
        for invoice_number, invoice_rows in sorted(grouped.items()):
            invoice_rows = sorted(invoice_rows, key=lambda item: item.parcel_id)
            meta = shippit_meta.get(invoice_number)
            estimated_cube_total = sum(max(row.parcel_cube_cm3 or 0.0, 0.0) for row in invoice_rows)
            shippit_chargeable_cube_cm3 = (
                (meta.chargeable_weight_kg * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)
                if meta and meta.chargeable_weight_kg is not None
                else None
            )
            pricing_scale_factor = (
                (shippit_chargeable_cube_cm3 / estimated_cube_total)
                if shippit_chargeable_cube_cm3 is not None and estimated_cube_total > 0
                else None
            )
            for row in invoice_rows:
                composition = parcel_compositions.get((row.invoice_number, row.parcel_id), {})
                pricing_scaled_parcel_cube_cm3 = (
                    row.parcel_cube_cm3 * pricing_scale_factor if pricing_scale_factor is not None else None
                )
                rectangularization_fill_pct = (
                    row.packed_sku_cube_cm3 * 100.0 / row.parcel_cube_cm3
                    if row.parcel_cube_cm3 > 0
                    else None
                )
                allocated_shippit_fill_pct = (
                    row.packed_sku_cube_cm3 * 100.0 / pricing_scaled_parcel_cube_cm3
                    if pricing_scaled_parcel_cube_cm3 and pricing_scaled_parcel_cube_cm3 > 0
                    else None
                )
                writer.writerow(
                    [
                        row.invoice_number,
                        row.actual_carrier,
                        row.actual_parcel_count,
                        row.estimated_parcel_count,
                        row.rule_name,
                        row.process_path,
                        row.estimated_pack_origin_path,
                        row.parcel_contents_size_mix,
                        row.parcel_id,
                        row.placeholder_parcel,
                        row.parcel_units,
                        round(row.parcel_weight_kg, 3),
                        round(row.packed_sku_cube_cm3, 3),
                        _fmt(row.rectangularized_parcel_cube_cm3),
                        _fmt(
                            (row.rectangularized_parcel_cube_cm3 or 0.0) - row.packed_sku_cube_cm3
                        ),
                        _fmt(row.expected_air_ratio_norm_pct),
                        _fmt(
                            row.packed_sku_cube_cm3 + (row.expected_packaging_uplift_cm3 or 0.0)
                        ),
                        _fmt(row.expected_packaging_uplift_cm3),
                        round(row.parcel_cube_cm3, 3),
                        round(row.parcel_cube_cm3, 3),
                        round(row.cube_uplift_cm3, 3),
                        _fmt(rectangularization_fill_pct),
                        _fmt(100.0 - rectangularization_fill_pct if rectangularization_fill_pct is not None else None),
                        _fmt(shippit_chargeable_cube_cm3),
                        _fmt_ratio(pricing_scale_factor),
                        _fmt(pricing_scaled_parcel_cube_cm3),
                        _fmt(pricing_scaled_parcel_cube_cm3 / 1_000_000.0 if pricing_scaled_parcel_cube_cm3 is not None else None),
                        _fmt(pricing_scaled_parcel_cube_cm3 / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG if pricing_scaled_parcel_cube_cm3 is not None else None),
                        _fmt(allocated_shippit_fill_pct),
                        _fmt(100.0 - allocated_shippit_fill_pct if allocated_shippit_fill_pct is not None else None),
                        row.cfc_cube_imputed,
                        _fmt(row.estimated_length_cm),
                        _fmt(row.estimated_width_cm),
                        _fmt(row.estimated_height_cm),
                        row.estimated_shape_source,
                        row.fit_band,
                        composition.get("toteable_unit_count", 0),
                        composition.get("toteable_keycode_count", 0),
                        composition.get("non_toteable_unit_count", 0),
                        composition.get("non_toteable_keycode_count", 0),
                        composition.get("bulky_unit_count", 0),
                        composition.get("bulky_keycode_count", 0),
                        composition.get("no_dims_unit_count", 0),
                        composition.get("no_dims_keycode_count", 0),
                        row.parcel_sku_qty_pattern,
                        row.parcel_packed_sku_cube_pattern_cm3,
                        row.sku_ids,
                    ]
                )


def _validation_output_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_parcel_physicals_full.csv"):
        name = name.replace("_parcel_physicals_full.csv", "_parcelizer_validation.csv")
    else:
        name = f"{output_path.stem}_parcelizer_validation.csv"
    return output_path.with_name(name)


def _air_ratio_norm_output_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_parcel_physicals_full.csv"):
        name = name.replace("_parcel_physicals_full.csv", "_air_ratio_norms.csv")
    else:
        name = f"{output_path.stem}_air_ratio_norms.csv"
    return output_path.with_name(name)


def _write_air_ratio_norm_output(
    path: Path,
    norms: dict[tuple[str, str, str], AirRatioNorm],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "estimated_pack_origin_path",
                "parcel_contents_size_mix",
                "parcel_unit_band",
                "sample_count",
                "p10_air_ratio_pct",
                "p50_air_ratio_pct",
                "p90_air_ratio_pct",
                "norm_source",
            ]
        )
        for (origin, size_mix, unit_band), norm in sorted(norms.items()):
            writer.writerow(
                [
                    origin,
                    size_mix,
                    unit_band,
                    norm.sample_count,
                    _fmt(norm.p10_pct),
                    _fmt(norm.p50_pct),
                    _fmt(norm.p90_pct),
                    norm.source,
                ]
            )


def _site_name_from_output_path(output_path: Path) -> str:
    stem = output_path.stem.lower()
    if "truganina" in stem:
        return "TRUGANINA"
    if "moorebank" in stem:
        return "MOOREBANK"
    return "UNKNOWN"


def _write_validation_output(
    path: Path,
    rows: list[ParcelPhysical],
    shippit_meta: dict[str, ShippitOrderMeta],
) -> list[dict[str, object]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[ParcelPhysical]] = {}
    for row in rows:
        grouped.setdefault(row.invoice_number, []).append(row)

    site = _site_name_from_output_path(path)
    validation_rows: list[dict[str, object]] = []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "site",
                "shippit_invoice_number",
                "shippit_carrier",
                "shippit_parcel_count",
                "est_parcelization_scope",
                "est_pack_process_group",
                "est_process_path",
                "est_estimated_pack_origin_path",
                "est_parcel_contents_size_mix",
                "shippit_dead_weight_kg",
                "shippit_volumetric_weight_kg",
                "shippit_chargeable_weight_kg",
                "shippit_volumetric_cube_cm3",
                "est_shippit_chargeable_cube_cm3",
                "est_parcel_count",
                "est_parcel_count_match",
                "est_total_parcel_weight_kg",
                "est_raw_packed_sku_cube_cm3",
                "est_packed_sku_cube_cm3",
                "est_estimated_parcel_cube_cm3",
                "est_total_parcel_cube_cm3",
                "est_total_parcel_cube_m3",
                "est_cube_uplift_cm3",
                "est_raw_product_fill_pct_vs_shippit_cube",
                "est_raw_product_air_ratio_pct_vs_shippit_cube",
                "est_rectangularized_fill_pct_vs_shippit_cube",
                "est_rectangularized_air_ratio_pct_vs_shippit_cube",
                "est_rectangularization_cube_uplift_cm3",
                "est_rectangularized_volumetric_weight_kg",
                "est_chargeable_weight_kg",
                "est_pricing_scale_factor",
                "est_pricing_scaled_parcel_cube_cm3",
                "est_pricing_scaled_parcel_cube_m3",
                "est_pricing_scaled_parcel_volumetric_weight_kg",
                "shippit_observed_packaging_weight_uplift_kg",
                "shippit_cube_residual_vs_rectangularized_cm3",
                "shippit_cube_residual_vs_rectangularized_m3",
                "est_parcel_units_total",
                "est_parcel_units_pattern",
                "est_parcel_weight_pattern_kg",
                "est_parcel_cube_pattern_cm3",
                "est_parcel_cube_pattern_m3",
                "est_fit_band_pattern",
                "est_min_parcel_weight_kg",
                "est_max_parcel_weight_kg",
                "est_weight_spread_kg",
                "est_min_parcel_cube_cm3",
                "est_max_parcel_cube_cm3",
                "est_cube_spread_cm3",
                "est_largest_parcel_weight_share_pct",
                "est_largest_parcel_cube_share_pct",
                "est_tote_like_count",
                "est_manual_like_count",
                "est_bulky_like_count",
                "est_flag_odd_allocation",
                "est_shape_source_pattern",
                "est_weight_gap_vs_shippit_dead_kg",
                "est_weight_gap_vs_shippit_chargeable_kg",
                "est_chargeable_gap_abs_kg",
                "est_cube_gap_vs_shippit_cm3",
                "est_fill_pct_vs_shippit_cube",
                "est_air_ratio_pct_vs_shippit_cube",
                "est_rectangularized_air_ratio_abs_pct_vs_shippit_cube",
                "cfc_cube_imputed_row_count",
                "cfc_cube_imputed_unit_count",
                "cfc_cube_imputed_row_pct",
                "cfc_cube_imputed_unit_pct",
            ]
        )
        for invoice_number, invoice_rows in sorted(grouped.items()):
            invoice_rows = sorted(invoice_rows, key=lambda item: item.parcel_id)
            meta = shippit_meta.get(invoice_number)
            parcel_units = [row.parcel_units for row in invoice_rows]
            weights = [row.parcel_weight_kg for row in invoice_rows]
            cubes = [row.parcel_cube_cm3 for row in invoice_rows]
            packed_cubes = [row.packed_sku_cube_cm3 for row in invoice_rows]
            fit_bands = [row.fit_band for row in invoice_rows]
            total_weight = sum(weights)
            total_cube = sum(cubes)
            packed_cube_total = sum(packed_cubes)
            total_cube_m3 = total_cube / 1_000_000.0
            estimated_chargeable = max(total_weight, total_cube / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG) if invoice_rows else 0.0
            rectangularized_volumetric_weight_kg = total_cube / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG
            parcelization_scope = _parcelization_scope(invoice_rows[0].estimated_parcel_count if invoice_rows else 0)
            pack_process_group = _pack_process_group(invoice_rows[0].rule_name if invoice_rows else "")
            process_path = _process_path_from_rule_name(invoice_rows[0].rule_name if invoice_rows else "")
            estimated_pack_origin_path = invoice_rows[0].estimated_pack_origin_path if invoice_rows else ""
            parcel_contents_size_mix = invoice_rows[0].parcel_contents_size_mix if invoice_rows else ""
            shippit_dead_weight = meta.dead_weight_kg if meta else None
            shippit_volumetric_weight = meta.volumetric_weight_kg if meta else None
            shippit_chargeable_weight = meta.chargeable_weight_kg if meta else None
            shippit_volumetric_cube = (
                (shippit_volumetric_weight * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)
                if shippit_volumetric_weight is not None
                else None
            )
            shippit_chargeable_cube_cm3 = (
                (shippit_chargeable_weight * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)
                if shippit_chargeable_weight is not None
                else None
            )
            pricing_scale_factor = (
                (shippit_chargeable_cube_cm3 / total_cube)
                if shippit_chargeable_cube_cm3 is not None and total_cube > 0
                else None
            )
            pricing_scaled_parcel_cube_cm3 = (
                total_cube * pricing_scale_factor if pricing_scale_factor is not None else None
            )
            pricing_scaled_parcel_cube_m3 = pricing_scaled_parcel_cube_cm3 / 1_000_000.0 if pricing_scaled_parcel_cube_cm3 is not None else None
            pricing_scaled_parcel_volumetric_weight_kg = (
                pricing_scaled_parcel_cube_cm3 / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG
                if pricing_scaled_parcel_cube_cm3 is not None
                else None
            )
            packaging_weight_uplift_kg = (
                max(shippit_dead_weight - total_weight, 0.0)
                if shippit_dead_weight is not None
                else None
            )
            shippit_cube_residual_vs_rectangularized_cm3 = (
                shippit_volumetric_cube - total_cube
                if shippit_volumetric_cube is not None
                else None
            )
            shippit_cube_residual_vs_rectangularized_m3 = (
                shippit_cube_residual_vs_rectangularized_cm3 / 1_000_000.0
                if shippit_cube_residual_vs_rectangularized_cm3 is not None
                else None
            )
            rectangularization_cube_uplift_cm3 = total_cube - packed_cube_total
            max_weight = max(weights) if weights else 0.0
            min_weight = min(weights) if weights else 0.0
            max_cube = max(cubes) if cubes else 0.0
            min_cube = min(cubes) if cubes else 0.0
            parcel_count_match = "Y" if len(invoice_rows) == (invoice_rows[0].actual_parcel_count if invoice_rows else 0) else "N"
            largest_weight_share_pct = (max_weight * 100 / total_weight) if total_weight else 0.0
            largest_cube_share_pct = (max_cube * 100 / total_cube) if total_cube else 0.0
            fill_pct = (total_cube * 100 / shippit_volumetric_cube) if shippit_volumetric_cube else None
            air_ratio_pct = 100.0 - fill_pct if fill_pct is not None else None
            raw_product_fill_pct = (packed_cube_total * 100 / shippit_volumetric_cube) if shippit_volumetric_cube else None
            raw_product_air_ratio_pct = 100.0 - raw_product_fill_pct if raw_product_fill_pct is not None else None
            weight_gap_vs_shippit_dead = (
                total_weight - shippit_dead_weight if shippit_dead_weight is not None else None
            )
            weight_gap_vs_shippit_chargeable = (
                estimated_chargeable - shippit_chargeable_weight
                if shippit_chargeable_weight is not None
                else None
            )
            chargeable_gap_abs = (
                abs(weight_gap_vs_shippit_chargeable)
                if weight_gap_vs_shippit_chargeable is not None
                else None
            )
            cube_gap_vs_shippit = (
                total_cube - shippit_volumetric_cube
                if shippit_volumetric_cube is not None
                else None
            )
            pack_process = _pack_process_bucket(invoice_rows[0].rule_name if invoice_rows else "")
            shape_sources = sorted({row.estimated_shape_source for row in invoice_rows if row.estimated_shape_source})
            shape_source_pattern = "|".join(shape_sources)
            tote_like_count = sum(1 for band in fit_bands if band == "TOTE_LIKE")
            manual_like_count = sum(1 for band in fit_bands if band == "MANUAL_LIKE")
            bulky_like_count = sum(1 for band in fit_bands if band == "BULKY_LIKE")
            imputed_rows = [row for row in invoice_rows if str(row.cfc_cube_imputed or "").upper() == "Y"]
            imputed_row_count = len(imputed_rows)
            imputed_unit_count = sum(row.parcel_units for row in imputed_rows)
            avg_units = sum(parcel_units) / len(parcel_units) if parcel_units else 0.0
            odd_allocation = (
                "Y"
                if len(invoice_rows) > 1
                and (
                    max(parcel_units) - min(parcel_units) >= max(3, round(avg_units * 1.5))
                    or largest_weight_share_pct >= 60.0
                    or largest_cube_share_pct >= 60.0
                )
                else ""
            )

            writer.writerow(
                [
                    site,
                    invoice_number,
                    invoice_rows[0].actual_carrier if invoice_rows else "",
                    invoice_rows[0].actual_parcel_count if invoice_rows else 0,
                    parcelization_scope,
                    pack_process_group,
                    process_path,
                    estimated_pack_origin_path,
                    parcel_contents_size_mix,
                    _fmt(meta.dead_weight_kg if meta else None),
                    _fmt(meta.volumetric_weight_kg if meta else None),
                    _fmt(meta.chargeable_weight_kg if meta else None),
                    _fmt(shippit_volumetric_cube),
                    _fmt(shippit_chargeable_cube_cm3),
                    invoice_rows[0].estimated_parcel_count if invoice_rows else 0,
                    parcel_count_match,
                    f"{total_weight:.2f}",
                    f"{packed_cube_total:.2f}",
                    f"{packed_cube_total:.2f}",
                    f"{total_cube:.2f}",
                    f"{total_cube:.2f}",
                    f"{total_cube_m3:.6f}",
                    f"{(total_cube - packed_cube_total):.2f}",
                    _fmt(raw_product_fill_pct),
                    _fmt(raw_product_air_ratio_pct),
                    _fmt(fill_pct),
                    _fmt(air_ratio_pct),
                    f"{rectangularization_cube_uplift_cm3:.2f}",
                    f"{rectangularized_volumetric_weight_kg:.2f}",
                    f"{estimated_chargeable:.2f}",
                    _fmt_ratio(pricing_scale_factor),
                    _fmt(pricing_scaled_parcel_cube_cm3),
                    _fmt(pricing_scaled_parcel_cube_m3),
                    _fmt(pricing_scaled_parcel_volumetric_weight_kg),
                    _fmt(packaging_weight_uplift_kg),
                    _fmt(shippit_cube_residual_vs_rectangularized_cm3),
                    _fmt(shippit_cube_residual_vs_rectangularized_m3),
                    sum(parcel_units),
                    "|".join(str(value) for value in parcel_units),
                    "|".join(f"{value:.2f}" for value in weights),
                    "|".join(f"{value:.2f}" for value in cubes),
                    "|".join(f"{value / 1_000_000.0:.6f}" for value in cubes),
                    "|".join(fit_bands),
                    f"{min_weight:.2f}",
                    f"{max_weight:.2f}",
                    f"{(max_weight - min_weight):.2f}",
                    f"{min_cube:.2f}",
                    f"{max_cube:.2f}",
                    f"{(max_cube - min_cube):.2f}",
                    f"{largest_weight_share_pct:.2f}",
                    f"{largest_cube_share_pct:.2f}",
                    tote_like_count,
                    manual_like_count,
                    bulky_like_count,
                    odd_allocation,
                    shape_source_pattern,
                    _fmt(weight_gap_vs_shippit_dead),
                    _fmt(weight_gap_vs_shippit_chargeable),
                    _fmt(chargeable_gap_abs),
                    _fmt(cube_gap_vs_shippit),
                    _fmt(fill_pct),
                    _fmt(air_ratio_pct),
                    _fmt(abs(air_ratio_pct) if air_ratio_pct is not None else None),
                    imputed_row_count,
                    imputed_unit_count,
                    f"{(imputed_row_count * 100 / len(invoice_rows)) if invoice_rows else 0.0:.2f}",
                    f"{(imputed_unit_count * 100 / sum(parcel_units)) if sum(parcel_units) else 0.0:.2f}",
                ]
            )
            validation_rows.append(
                {
                    "site": site,
                    "shippit_invoice_number": invoice_number,
                    "shippit_carrier": invoice_rows[0].actual_carrier if invoice_rows else "",
                    "shippit_parcel_count": invoice_rows[0].actual_parcel_count if invoice_rows else 0,
                    "est_parcelization_scope": parcelization_scope,
                    "est_pack_process_group": pack_process_group,
                    "est_process_path": process_path,
                    "est_estimated_pack_origin_path": estimated_pack_origin_path,
                    "est_parcel_contents_size_mix": parcel_contents_size_mix,
                    "shippit_dead_weight_kg": meta.dead_weight_kg if meta else None,
                    "shippit_volumetric_weight_kg": meta.volumetric_weight_kg if meta else None,
                    "shippit_chargeable_weight_kg": meta.chargeable_weight_kg if meta else None,
                    "shippit_volumetric_cube_cm3": shippit_volumetric_cube,
                    "est_shippit_chargeable_cube_cm3": shippit_chargeable_cube_cm3,
                    "est_parcel_count": invoice_rows[0].estimated_parcel_count if invoice_rows else 0,
                    "est_parcel_count_match": parcel_count_match,
                    "est_total_parcel_weight_kg": total_weight,
                    "est_raw_packed_sku_cube_cm3": packed_cube_total,
                    "est_packed_sku_cube_cm3": packed_cube_total,
                    "est_estimated_parcel_cube_cm3": total_cube,
                    "est_total_parcel_cube_cm3": total_cube,
                    "est_total_parcel_cube_m3": total_cube_m3,
                    "est_cube_uplift_cm3": total_cube - packed_cube_total,
                    "est_raw_product_fill_pct_vs_shippit_cube": raw_product_fill_pct,
                    "est_raw_product_air_ratio_pct_vs_shippit_cube": raw_product_air_ratio_pct,
                    "est_rectangularized_fill_pct_vs_shippit_cube": fill_pct,
                    "est_rectangularized_air_ratio_pct_vs_shippit_cube": air_ratio_pct,
                    "est_rectangularization_cube_uplift_cm3": rectangularization_cube_uplift_cm3,
                    "est_rectangularized_volumetric_weight_kg": rectangularized_volumetric_weight_kg,
                    "est_chargeable_weight_kg": estimated_chargeable,
                    "est_pricing_scale_factor": pricing_scale_factor,
                    "est_pricing_scaled_parcel_cube_cm3": pricing_scaled_parcel_cube_cm3,
                    "est_pricing_scaled_parcel_cube_m3": pricing_scaled_parcel_cube_m3,
                    "est_pricing_scaled_parcel_volumetric_weight_kg": pricing_scaled_parcel_volumetric_weight_kg,
                    "shippit_observed_packaging_weight_uplift_kg": packaging_weight_uplift_kg,
                    "shippit_cube_residual_vs_rectangularized_cm3": shippit_cube_residual_vs_rectangularized_cm3,
                    "shippit_cube_residual_vs_rectangularized_m3": shippit_cube_residual_vs_rectangularized_m3,
                    "est_parcel_units_total": sum(parcel_units),
                    "est_parcel_units_pattern": "|".join(str(value) for value in parcel_units),
                    "est_parcel_weight_pattern_kg": "|".join(f"{value:.2f}" for value in weights),
                    "est_parcel_cube_pattern_cm3": "|".join(f"{value:.2f}" for value in cubes),
                    "est_parcel_cube_pattern_m3": "|".join(f"{value / 1_000_000.0:.6f}" for value in cubes),
                    "est_fit_band_pattern": "|".join(fit_bands),
                    "est_min_parcel_weight_kg": min_weight,
                    "est_max_parcel_weight_kg": max_weight,
                    "est_weight_spread_kg": max_weight - min_weight,
                    "est_min_parcel_cube_cm3": min_cube,
                    "est_max_parcel_cube_cm3": max_cube,
                    "est_cube_spread_cm3": max_cube - min_cube,
                    "est_largest_parcel_weight_share_pct": largest_weight_share_pct,
                    "est_largest_parcel_cube_share_pct": largest_cube_share_pct,
                    "est_tote_like_count": tote_like_count,
                    "est_manual_like_count": manual_like_count,
                    "est_bulky_like_count": bulky_like_count,
                    "est_flag_odd_allocation": odd_allocation,
                    "est_shape_source_pattern": shape_source_pattern,
                    "est_weight_gap_vs_shippit_dead_kg": weight_gap_vs_shippit_dead,
                    "est_weight_gap_vs_shippit_chargeable_kg": weight_gap_vs_shippit_chargeable,
                    "est_chargeable_gap_abs_kg": chargeable_gap_abs,
                    "est_cube_gap_vs_shippit_cm3": cube_gap_vs_shippit,
                    "est_fill_pct_vs_shippit_cube": fill_pct,
                    "est_air_ratio_pct_vs_shippit_cube": air_ratio_pct,
                    "est_rectangularized_air_ratio_abs_pct_vs_shippit_cube": (
                        abs(air_ratio_pct) if air_ratio_pct is not None else None
                    ),
                    "cfc_cube_imputed_row_count": imputed_row_count,
                    "cfc_cube_imputed_unit_count": imputed_unit_count,
                    "cfc_cube_imputed_row_pct": (imputed_row_count * 100 / len(invoice_rows)) if invoice_rows else 0.0,
                    "cfc_cube_imputed_unit_pct": (imputed_unit_count * 100 / sum(parcel_units)) if sum(parcel_units) else 0.0,
                    "est_pack_process": pack_process,
                }
            )

    return validation_rows


def _air_ratio_summary_output_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_parcel_physicals_full.csv"):
        name = name.replace("_parcel_physicals_full.csv", "_parcelizer_air_ratio_summary.csv")
    else:
        name = f"{output_path.stem}_parcelizer_air_ratio_summary.csv"
    return output_path.with_name(name)


def _keycode_review_output_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_parcel_physicals_full.csv"):
        name = name.replace("_parcel_physicals_full.csv", "_parcelizer_keycode_review.csv")
    else:
        name = f"{output_path.stem}_parcelizer_keycode_review.csv"
    return output_path.with_name(name)


def _write_air_ratio_summary_output(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (
            str(row.get("site") or ""),
            str(row.get("est_parcelization_scope") or ""),
            str(row.get("est_pack_process_group") or ""),
            str(row.get("shippit_carrier") or ""),
        )
        grouped.setdefault(key, []).append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "site",
                "est_parcelization_scope",
                "est_pack_process_group",
                "shippit_carrier",
                "invoice_count",
                "parcel_count_total",
                "avg_shippit_chargeable_weight_kg",
                "avg_est_total_parcel_weight_kg",
                "avg_shippit_volumetric_cube_cm3",
                "avg_est_total_parcel_cube_cm3",
                "avg_fill_pct_vs_shippit_cube",
                "median_fill_pct_vs_shippit_cube",
                "p25_fill_pct_vs_shippit_cube",
                "p75_fill_pct_vs_shippit_cube",
                "avg_air_ratio_pct_vs_shippit_cube",
                "median_air_ratio_pct_vs_shippit_cube",
                "p90_air_ratio_pct_vs_shippit_cube",
                "outlier_invoice_count",
                "odd_allocation_invoice_count",
                "est_air_ratio_review_flag",
                "est_outlier_review_flag",
                "review_commentary",
            ]
        )

        for (site, parcelization_scope, pack_process_group, carrier), group_rows in sorted(grouped.items()):
            fill_pcts = [float(row["est_fill_pct_vs_shippit_cube"]) for row in group_rows if row.get("est_fill_pct_vs_shippit_cube") is not None]
            air_pcts = [float(row["est_air_ratio_pct_vs_shippit_cube"]) for row in group_rows if row.get("est_air_ratio_pct_vs_shippit_cube") is not None]
            shippit_chargeables = [float(row["shippit_chargeable_weight_kg"]) for row in group_rows if row.get("shippit_chargeable_weight_kg") is not None]
            est_weights = [float(row["est_total_parcel_weight_kg"]) for row in group_rows if row.get("est_total_parcel_weight_kg") is not None]
            shippit_cubes = [float(row["shippit_volumetric_cube_cm3"]) for row in group_rows if row.get("shippit_volumetric_cube_cm3") is not None]
            est_cubes = [float(row["est_total_parcel_cube_cm3"]) for row in group_rows if row.get("est_total_parcel_cube_cm3") is not None]
            parcel_count_total = sum(int(row.get("shippit_parcel_count") or 0) for row in group_rows)

            fill_outlier_count = _outlier_count(fill_pcts)
            odd_allocation_count = sum(1 for row in group_rows if row.get("est_flag_odd_allocation") == "Y")
            air_ratio_review_flag = _air_ratio_review_flag(_avg(air_pcts), _median(air_pcts))
            outlier_review_flag = _outlier_review_flag(fill_outlier_count, len(group_rows), odd_allocation_count)
            review_commentary = _air_ratio_summary_commentary(
                parcelization_scope,
                pack_process_group,
                _avg(fill_pcts),
                _avg(air_pcts),
                fill_outlier_count,
                odd_allocation_count,
                air_ratio_review_flag,
                outlier_review_flag,
            )

            writer.writerow(
                [
                    site,
                    parcelization_scope,
                    pack_process_group,
                    carrier,
                    len(group_rows),
                    parcel_count_total,
                    _fmt(_avg(shippit_chargeables)),
                    _fmt(_avg(est_weights)),
                    _fmt(_avg(shippit_cubes)),
                    _fmt(_avg(est_cubes)),
                    _fmt(_avg(fill_pcts)),
                    _fmt(_median(fill_pcts)),
                    _fmt(_percentile(fill_pcts, 25)),
                    _fmt(_percentile(fill_pcts, 75)),
                    _fmt(_avg(air_pcts)),
                    _fmt(_median(air_pcts)),
                    _fmt(_percentile(air_pcts, 90)),
                    fill_outlier_count,
                    odd_allocation_count,
                    air_ratio_review_flag,
                    outlier_review_flag,
                    review_commentary,
                ]
            )


def _write_keycode_review_output(
    path: Path,
    rows: list[ParcelPhysical],
    shippit_meta: dict[str, ShippitOrderMeta],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    invoice_rows: dict[str, list[ParcelPhysical]] = {}
    for row in rows:
        invoice_rows.setdefault(row.invoice_number, []).append(row)

    review_invoices: dict[str, dict[str, object]] = {}
    for invoice_number, invoice_rows_list in sorted(invoice_rows.items()):
        invoice_rows_list = sorted(invoice_rows_list, key=lambda item: item.parcel_id)
        meta = shippit_meta.get(invoice_number)
        parcel_count_match = len(invoice_rows_list) == (invoice_rows_list[0].actual_parcel_count if invoice_rows_list else 0)
        parcelization_scope = _parcelization_scope(invoice_rows_list[0].estimated_parcel_count if invoice_rows_list else 0)
        pack_process_group = _pack_process_group(invoice_rows_list[0].rule_name if invoice_rows_list else "")
        parcel_units = [row.parcel_units for row in invoice_rows_list]
        weights = [row.parcel_weight_kg for row in invoice_rows_list]
        cubes = [row.parcel_cube_cm3 for row in invoice_rows_list]
        total_weight = sum(weights)
        total_cube = sum(cubes)
        shippit_chargeable = meta.chargeable_weight_kg if meta else None
        shippit_volumetric_cube = (
            (meta.volumetric_weight_kg * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)
            if meta and meta.volumetric_weight_kg is not None
            else None
        )
        fill_pct = (total_cube * 100 / shippit_volumetric_cube) if shippit_volumetric_cube else None
        air_ratio_pct = (100.0 - fill_pct) if fill_pct is not None else None
        weight_gap_chargeable = total_weight - (shippit_chargeable or 0.0)
        weight_gap_dead = total_weight - ((meta.dead_weight_kg) if meta and meta.dead_weight_kg is not None else 0.0)
        largest_weight_share_pct = (max(weights) * 100 / total_weight) if total_weight else 0.0
        largest_cube_share_pct = (max(cubes) * 100 / total_cube) if total_cube else 0.0
        avg_units = sum(parcel_units) / len(parcel_units) if parcel_units else 0.0
        review_reasons: list[str] = []
        if (
            len(invoice_rows_list) > 1
            and (
                max(parcel_units) - min(parcel_units) >= max(3, round(avg_units * 1.5))
                or largest_weight_share_pct >= 60.0
                or largest_cube_share_pct >= 60.0
            )
        ):
            review_reasons.append("ODD_ALLOCATION")
        if not parcel_count_match:
            review_reasons.append("PARCEL_COUNT_MISMATCH")
        if air_ratio_pct is not None and air_ratio_pct >= 70.0:
            review_reasons.append("HIGH_AIR_RATIO")
        if air_ratio_pct is not None and air_ratio_pct <= 20.0:
            review_reasons.append("LOW_AIR_RATIO")
        if shippit_chargeable is not None and shippit_chargeable > 0:
            if abs(weight_gap_chargeable) / shippit_chargeable >= 0.2:
                review_reasons.append("CHARGEABLE_WEIGHT_GAP")
        if meta and meta.dead_weight_kg is not None and meta.dead_weight_kg > 0:
            if abs(weight_gap_dead) / meta.dead_weight_kg >= 0.2:
                review_reasons.append("DEAD_WEIGHT_GAP")

        keycodes = sorted(
            {
                sku.strip()
                for parcel in invoice_rows_list
                for sku in str(parcel.sku_ids or "").replace("...", "").split(",")
                if sku.strip()
            }
        )

        review_invoices[invoice_number] = {
            "invoice_number": invoice_number,
            "actual_carrier": invoice_rows_list[0].actual_carrier if invoice_rows_list else "",
            "actual_parcel_count": invoice_rows_list[0].actual_parcel_count if invoice_rows_list else 0,
            "estimated_parcel_count": invoice_rows_list[0].estimated_parcel_count if invoice_rows_list else 0,
            "est_parcelization_scope": parcelization_scope,
            "est_pack_process_group": pack_process_group,
            "shippit_dead_weight_kg": meta.dead_weight_kg if meta else None,
            "shippit_volumetric_weight_kg": meta.volumetric_weight_kg if meta else None,
            "shippit_chargeable_weight_kg": shippit_chargeable,
            "est_total_parcel_weight_kg": total_weight,
            "est_total_parcel_cube_cm3": total_cube,
            "est_fill_pct_vs_shippit_cube": fill_pct,
            "est_air_ratio_pct_vs_shippit_cube": air_ratio_pct,
            "est_weight_gap_vs_shippit_chargeable_kg": weight_gap_chargeable,
            "est_weight_gap_vs_shippit_dead_kg": weight_gap_dead,
            "review_reasons": review_reasons,
            "keycodes": keycodes,
        }

    flagged_invoices = [row for row in review_invoices.values() if row["review_reasons"]]
    packed_keycodes = _load_packed_keycodes([str(row["invoice_number"]) for row in flagged_invoices])

    aggregated: dict[str, dict[str, object]] = {}
    for invoice_row in flagged_invoices:
        invoice_number = str(invoice_row["invoice_number"])
        for sku_id, quantity_packed in packed_keycodes.get(invoice_number, {}).items():
            current = aggregated.setdefault(
                sku_id,
                {
                    "site": _site_name_from_output_path(path),
                    "sku_id": sku_id,
                    "parcelization_scopes": set(),
                    "pack_process_groups": set(),
                    "flagged_invoice_count": 0,
                    "total_quantity_packed": 0,
                    "flagged_invoice_occurrences": 0,
                    "sum_shippit_chargeable_weight_kg": 0.0,
                    "sum_est_total_parcel_weight_kg": 0.0,
                    "sum_est_air_ratio_pct_vs_shippit_cube": 0.0,
                    "sum_est_fill_pct_vs_shippit_cube": 0.0,
                    "sum_weight_gap_vs_shippit_chargeable_kg": 0.0,
                    "sum_weight_gap_vs_shippit_dead_kg": 0.0,
                    "sum_est_total_parcel_cube_cm3": 0.0,
                    "review_reason_set": set(),
                    "example_invoice_numbers": [],
                    "example_carrier": "",
                    "example_parcel_count": 0,
                },
            )
            current["parcelization_scopes"].add(str(invoice_row["est_parcelization_scope"] or ""))
            current["pack_process_groups"].add(str(invoice_row["est_pack_process_group"] or ""))
            current["flagged_invoice_count"] = int(current["flagged_invoice_count"]) + 1
            current["total_quantity_packed"] = int(current["total_quantity_packed"]) + int(quantity_packed)
            current["flagged_invoice_occurrences"] = int(current["flagged_invoice_occurrences"]) + 1
            current["sum_shippit_chargeable_weight_kg"] = float(current["sum_shippit_chargeable_weight_kg"]) + float(invoice_row["shippit_chargeable_weight_kg"] or 0.0)
            current["sum_est_total_parcel_weight_kg"] = float(current["sum_est_total_parcel_weight_kg"]) + float(invoice_row["est_total_parcel_weight_kg"] or 0.0)
            current["sum_est_air_ratio_pct_vs_shippit_cube"] = float(current["sum_est_air_ratio_pct_vs_shippit_cube"]) + float(invoice_row["est_air_ratio_pct_vs_shippit_cube"] or 0.0)
            current["sum_est_fill_pct_vs_shippit_cube"] = float(current["sum_est_fill_pct_vs_shippit_cube"]) + float(invoice_row["est_fill_pct_vs_shippit_cube"] or 0.0)
            current["sum_weight_gap_vs_shippit_chargeable_kg"] = float(current["sum_weight_gap_vs_shippit_chargeable_kg"]) + float(invoice_row["est_weight_gap_vs_shippit_chargeable_kg"] or 0.0)
            current["sum_weight_gap_vs_shippit_dead_kg"] = float(current["sum_weight_gap_vs_shippit_dead_kg"]) + float(invoice_row["est_weight_gap_vs_shippit_dead_kg"] or 0.0)
            current["sum_est_total_parcel_cube_cm3"] = float(current["sum_est_total_parcel_cube_cm3"]) + float(invoice_row["est_total_parcel_cube_cm3"] or 0.0)
            current["review_reason_set"].update(str(reason) for reason in invoice_row["review_reasons"])
            if len(current["example_invoice_numbers"]) < 3:
                current["example_invoice_numbers"].append(invoice_number)
                current["example_carrier"] = str(invoice_row["actual_carrier"] or "")
                current["example_parcel_count"] = int(invoice_row["actual_parcel_count"] or 0)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "site",
                "sku_id",
                "est_parcelization_scope",
                "est_pack_process_group",
                "flagged_invoice_count",
                "total_quantity_packed",
                "flagged_invoice_occurrences",
                "avg_shippit_chargeable_weight_kg",
                "avg_est_total_parcel_weight_kg",
                "avg_est_fill_pct_vs_shippit_cube",
                "avg_est_air_ratio_pct_vs_shippit_cube",
                "avg_weight_gap_vs_shippit_chargeable_kg",
                "avg_weight_gap_vs_shippit_dead_kg",
                "avg_est_total_parcel_cube_cm3",
                "review_reasons",
                "example_invoice_numbers",
                "example_carrier",
                "example_parcel_count",
                "review_commentary",
            ]
        )
        for sku_id, row in sorted(
            aggregated.items(),
            key=lambda item: (
                int(item[1]["flagged_invoice_count"]),
                int(item[1]["total_quantity_packed"]),
                item[0],
            ),
            reverse=True,
        ):
            flagged_invoice_count = int(row["flagged_invoice_count"])
            writer.writerow(
                [
                    row["site"],
                    sku_id,
                    "|".join(sorted(value for value in row["parcelization_scopes"] if value)),
                    "|".join(sorted(value for value in row["pack_process_groups"] if value)),
                    flagged_invoice_count,
                    row["total_quantity_packed"],
                    row["flagged_invoice_occurrences"],
                    _fmt(float(row["sum_shippit_chargeable_weight_kg"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_est_total_parcel_weight_kg"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_est_fill_pct_vs_shippit_cube"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_est_air_ratio_pct_vs_shippit_cube"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_weight_gap_vs_shippit_chargeable_kg"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_weight_gap_vs_shippit_dead_kg"]) / max(flagged_invoice_count, 1)),
                    _fmt(float(row["sum_est_total_parcel_cube_cm3"]) / max(flagged_invoice_count, 1)),
                    "|".join(sorted(str(reason) for reason in row["review_reason_set"])),
                    ",".join(row["example_invoice_numbers"]),
                    row["example_carrier"],
                    row["example_parcel_count"],
                    _keycode_review_commentary(
                        "|".join(sorted(value for value in row["parcelization_scopes"] if value)),
                        "|".join(sorted(value for value in row["pack_process_groups"] if value)),
                        "|".join(sorted(str(reason) for reason in row["review_reason_set"])),
                        int(row["flagged_invoice_count"]),
                        int(row["total_quantity_packed"]),
                    ),
                ]
            )


def _load_packed_keycodes(invoice_numbers: list[str]) -> dict[str, dict[str, int]]:
    if not invoice_numbers:
        return {}

    session = get_session()
    try:
        rows = []
        for start in range(0, len(invoice_numbers), 5_000):
            values_sql = ", ".join(f"('{invoice}')" for invoice in invoice_numbers[start : start + 5_000])
            rows.extend(
                session.sql(
                    f"""
                    WITH target_invoices AS (
                        SELECT column1 AS invoice_number
                        FROM VALUES {values_sql}
                    )
                    SELECT
                        REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                        TO_VARCHAR(p.SKU_ID) AS sku_id,
                        SUM(COALESCE(p.QUANTITY_PACKED, 0)) AS quantity_packed
                    FROM KSFDA.CATCH_CFC.PACKED p
                    JOIN target_invoices t
                      ON t.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
                    GROUP BY 1, 2
                    """
                ).collect()
            )
    finally:
        session.close()

    packed: dict[str, dict[str, int]] = {}
    for row in rows:
        invoice_number = str(row["INVOICE_NUMBER"])
        sku_id = str(row["SKU_ID"])
        packed.setdefault(invoice_number, {})[sku_id] = int(float(row["QUANTITY_PACKED"] or 0))
    return packed


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}"


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pack_process_bucket(rule_name: str) -> str:
    rule = (rule_name or "").upper()
    if rule.startswith("FULFILLMENT_DETAIL"):
        return "FULFILLMENT_DETAIL"
    if rule.startswith("ALL_AUTOMATION"):
        return "ALL_AUTOMATION"
    if rule.startswith("ALL_MANUAL"):
        return "ALL_MANUAL"
    if "MIXED_AUTOMATION_MANUAL" in rule:
        return "MIXED_AUTOMATION_MANUAL"
    if "MIXED_MANUAL_TOTEABLE" in rule:
        return "MIXED_MANUAL_TOTEABLE"
    if "SINGLE_SKU_EQUAL_SPLIT" in rule:
        return "SINGLE_SKU_EQUAL_SPLIT"
    if "CONTAINS_BULKY" in rule:
        return "CONTAINS_BULKY"
    if "FALLBACK" in rule:
        return "FALLBACK"
    if "UNKNOWN" in rule:
        return "UNKNOWN"
    return rule or "UNKNOWN"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])

    if pct <= 0:
        return float(ordered[0])
    if pct >= 100:
        return float(ordered[-1])

    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[int(rank)])
    lower_weight = upper - rank
    upper_weight = rank - lower
    return float(ordered[lower] * lower_weight + ordered[upper] * upper_weight)


def _outlier_count(values: list[float]) -> int:
    if len(values) < 4:
        return 0
    q1 = _percentile(values, 25)
    q3 = _percentile(values, 75)
    if q1 is None or q3 is None:
        return 0
    iqr = q3 - q1
    lower = q1 - (1.5 * iqr)
    upper = q3 + (1.5 * iqr)
    return sum(1 for value in values if value < lower or value > upper)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _air_ratio_summary_commentary(
    parcelization_scope: str,
    pack_process_group: str,
    avg_fill_pct: float | None,
    avg_air_ratio_pct: float | None,
    outlier_invoice_count: int,
    odd_allocation_count: int,
    air_ratio_review_flag: str,
    outlier_review_flag: str,
) -> str:
    parts: list[str] = []
    scope = parcelization_scope.lower().replace("_", " ")
    process = pack_process_group.lower().replace("_", " ")
    parts.append(f"{scope} invoices using {process} logic")
    if avg_fill_pct is not None and avg_air_ratio_pct is not None:
        if avg_air_ratio_pct < 0:
            parts.append("estimated cube is larger than the Shippit volumetric basis")
        elif avg_air_ratio_pct >= 70:
            parts.append("very airy cartons compared with Shippit")
        elif avg_air_ratio_pct <= 20:
            parts.append("tight cartons with little apparent air")
        else:
            parts.append("fill is broadly in a normal range")
    if outlier_invoice_count > 0 or odd_allocation_count > 0:
        parts.append(
            f"{outlier_invoice_count} fill outliers and {odd_allocation_count} odd allocations in the group"
        )
    if air_ratio_review_flag == "HIGH_AIR_REVIEW":
        parts.append("review for overboxing or inflated dimensions")
    elif air_ratio_review_flag == "LOW_AIR_REVIEW":
        parts.append("review for dimension under-reporting or a bulky pack process")
    if outlier_review_flag == "REVIEW":
        parts.append("group is noisy enough to warrant a closer look")
    return "; ".join(parts)


def _keycode_review_commentary(
    parcelization_scope: str,
    pack_process_groups: str,
    review_reasons: str,
    flagged_invoice_count: int,
    total_quantity_packed: int,
) -> str:
    scope = parcelization_scope.lower().replace("_", " ")
    processes = pack_process_groups.lower().replace("_", " ")
    reasons = review_reasons.replace("|", ", ").lower()
    parts = [f"{scope} keycode seen in {flagged_invoice_count} flagged invoices across {processes}"]
    if reasons:
        parts.append(f"review reasons: {reasons}")
    if total_quantity_packed >= 100:
        parts.append("high exposure keycode with material volume")
    elif total_quantity_packed >= 20:
        parts.append("moderate volume keycode")
    else:
        parts.append("low volume but still worth spot checking")
    return "; ".join(parts)


def _air_ratio_review_flag(avg_air_ratio: float | None, median_air_ratio: float | None) -> str:
    if avg_air_ratio is None and median_air_ratio is None:
        return ""

    observed = [value for value in [avg_air_ratio, median_air_ratio] if value is not None]
    high = max(observed) if observed else 0.0
    low = min(observed) if observed else 0.0

    if high >= 70.0:
        return "HIGH_AIR_REVIEW"
    if low <= 20.0:
        return "LOW_AIR_REVIEW"
    return "OK"


def _outlier_review_flag(fill_outlier_count: int, invoice_count: int, odd_allocation_count: int) -> str:
    if invoice_count <= 0:
        return ""
    if fill_outlier_count >= max(3, round(invoice_count * 0.2)):
        return "REVIEW"
    if odd_allocation_count >= max(3, round(invoice_count * 0.25)):
        return "REVIEW"
    return "OK"


def _print_summary(rows: list[ParcelPhysical]) -> None:
    parcel_count = len(rows)
    avg_weight = sum(row.parcel_weight_kg for row in rows) / parcel_count if parcel_count else 0.0
    avg_cube = sum(row.parcel_cube_cm3 for row in rows) / parcel_count if parcel_count else 0.0
    tote_like = sum(1 for row in rows if row.fit_band == "TOTE_LIKE")
    manual_like = sum(1 for row in rows if row.fit_band == "MANUAL_LIKE")
    bulky_like = sum(1 for row in rows if row.fit_band == "BULKY_LIKE")

    print("STAGE 2 SUMMARY")
    print(
        "Row("
        f"PARCEL_COUNT={parcel_count}, "
        f"AVG_WEIGHT_KG={avg_weight:.2f}, "
        f"AVG_CUBE_CM3={avg_cube:.2f}, "
        f"TOTE_LIKE_PCT={(tote_like * 100 / parcel_count) if parcel_count else 0.0:.2f}, "
        f"MANUAL_LIKE_PCT={(manual_like * 100 / parcel_count) if parcel_count else 0.0:.2f}, "
        f"BULKY_LIKE_PCT={(bulky_like * 100 / parcel_count) if parcel_count else 0.0:.2f}"
        ")"
    )
    print(f"CSV_EXPORTED={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
