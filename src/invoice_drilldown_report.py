from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.db import get_session
from src import stage3_moorebank_cost_estimator as stage3_estimator


SITE_CONFIGS = {
    "MOOREBANK": {
        "label": "Moorebank",
        "stage1": Path("outputs/stage1_moorebank_parcel_assignments_full.csv"),
        "stage1_assignment_lines": Path("outputs/stage1_moorebank_parcel_assignment_lines.csv"),
        "stage2": Path("outputs/stage2_moorebank_parcel_physicals_full.csv"),
        "stage2_validation": Path("outputs/stage2_moorebank_parcelizer_validation.csv"),
        "stage3": Path("outputs/stage3_moorebank_cost_estimates_full.csv"),
    },
    "TRUGANINA": {
        "label": "Truganina",
        "stage1": Path("outputs/stage1_truganina_parcel_assignments_full.csv"),
        "stage1_assignment_lines": Path("outputs/stage1_truganina_parcel_assignment_lines.csv"),
        "stage2": Path("outputs/stage2_truganina_parcel_physicals_full.csv"),
        "stage2_validation": Path("outputs/stage2_truganina_parcelizer_validation.csv"),
        "stage3": Path("outputs/stage3_truganina_cost_estimates_full.csv"),
    },
}

OUTPUT_DIR = Path("outputs/invoice_drilldowns")
SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG = 4000.0

APPAREL_TERMS = (
    "APPAREL",
    "CLOTHING",
    "FASHION",
    "FOOTWEAR",
    "LINGERIE",
    "UNDERWEAR",
    "SWIM",
    "SOCK",
    "BODYSUIT",
    "NURSING",
)


def _pack_process_group(rule_name: str) -> str:
    rule = (rule_name or "").upper()
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


@dataclass(frozen=True)
class InvoiceContext:
    invoice_number: str
    site_key: str
    site_label: str
    stage1_rows: list[dict[str, Any]]
    parcel_assignment_lines: list[dict[str, Any]]
    stage2_rows: list[dict[str, Any]]
    validation_row: dict[str, Any]
    stage3_row: dict[str, Any]
    packed_lines: list[dict[str, Any]]
    rate_tables: dict[str, Any]
    gobundle_coverage_postcodes: set[str]
    benchmark_thresholds: dict[str, float]


def main() -> None:
    args = _parse_args()
    invoice_number = _normalize_invoice(args.invoice_number)
    site_key = _normalize_site(args.site) if args.site else ""

    context = _load_context(invoice_number, site_key)
    workbook = Workbook()
    workbook.remove(workbook.active)

    _write_summary_sheet(workbook, context)
    _write_parcel_validation_sheet(workbook, context)
    _write_parcel_detail_sheet(workbook, context)
    _write_cost_detail_sheet(workbook, context)
    _write_sku_review_sheet(workbook, context)
    _write_notes_sheet(workbook, context)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{context.site_key.lower()}_{context.invoice_number}_drilldown.xlsx"
    workbook.save(output_path)
    print(f"INVOICE_DRILLDOWN_EXPORTED={output_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an invoice drilldown workbook for Ops review.")
    parser.add_argument("--invoice-number", required=True, help="Invoice number to drill into.")
    parser.add_argument(
        "--site",
        choices=["MOOREBANK", "TRUGANINA"],
        help="Optional site override. Leave blank to auto-detect from the stage outputs.",
    )
    return parser.parse_args()


def _normalize_invoice(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("Invoice number is required.")
    return value


def _normalize_site(value: str) -> str:
    site = str(value or "").strip().upper()
    if site not in SITE_CONFIGS:
        raise ValueError(f"Unsupported site '{value}'. Use MOOREBANK or TRUGANINA.")
    return site


def _load_context(invoice_number: str, site_key: str) -> InvoiceContext:
    if site_key:
        config = SITE_CONFIGS[site_key]
        stage3_row = _find_stage3_row(config["stage3"], invoice_number)
        if stage3_row is None:
            raise FileNotFoundError(f"Invoice {invoice_number} not found in {config['stage3']}.")
        return _build_context(invoice_number, site_key, config, stage3_row)

    for detected_site_key, config in SITE_CONFIGS.items():
        stage3_row = _find_stage3_row(config["stage3"], invoice_number)
        if stage3_row is not None:
            return _build_context(invoice_number, detected_site_key, config, stage3_row)

    raise FileNotFoundError(
        f"Invoice {invoice_number} was not found in the stage 3 outputs for Moorebank or Truganina."
    )


def _build_context(
    invoice_number: str,
    site_key: str,
    config: dict[str, Path | str],
    stage3_row: dict[str, Any],
) -> InvoiceContext:
    stage1_rows = _load_invoice_rows(config["stage1"], invoice_number)
    parcel_assignment_lines = _load_invoice_rows(config["stage1_assignment_lines"], invoice_number)
    stage2_rows = _load_invoice_rows(config["stage2"], invoice_number)
    shippit_meta = _load_shippit_meta(invoice_number, stage3_row)
    session = get_session()
    try:
        rate_tables = stage3_estimator._load_rate_tables(session, site_key)
    finally:
        session.close()
    gobundle_coverage_postcodes = _load_gobundle_coverage_postcodes()
    benchmark_thresholds = _load_benchmark_thresholds()
    validation_row = _load_validation_row(config["stage2_validation"], invoice_number, stage2_rows, shippit_meta)
    validation_row = _apply_benchmark_metrics(validation_row, benchmark_thresholds)
    packed_lines = _load_order_lines(invoice_number)
    return InvoiceContext(
        invoice_number=invoice_number,
        site_key=site_key,
        site_label=str(config["label"]),
        stage1_rows=stage1_rows,
        parcel_assignment_lines=parcel_assignment_lines,
        stage2_rows=stage2_rows,
        validation_row=validation_row,
        stage3_row=stage3_row,
        packed_lines=packed_lines,
        rate_tables=rate_tables,
        gobundle_coverage_postcodes=gobundle_coverage_postcodes,
        benchmark_thresholds=benchmark_thresholds,
    )


def _find_stage3_row(path: Path, invoice_number: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("invoice_number") or "").strip() == invoice_number:
                return row
    return None


def _load_single_invoice_row(path: Path, invoice_number: str) -> dict[str, Any]:
    rows = _load_invoice_rows(path, invoice_number)
    if not rows:
        raise FileNotFoundError(f"Invoice {invoice_number} not found in {path}.")
    return rows[0]


def _load_validation_row(
    path: Path,
    invoice_number: str,
    stage2_rows: list[dict[str, Any]],
    shippit_meta: dict[str, Any],
) -> dict[str, Any]:
    if path.exists():
        rows = _load_invoice_rows(path, invoice_number)
        if rows:
            return rows[0]
    return _build_validation_row_from_stage2(invoice_number, stage2_rows, shippit_meta)


def _load_shippit_meta(invoice_number: str, stage3_row: dict[str, Any]) -> dict[str, Any]:
    session = get_session()
    try:
        rows = session.sql(
            f"""
            SELECT
                TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
                STORE_ATTRIBUTE__STORE_NAME AS store_name,
                ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE AS despatch_date,
                ORDER_LOCATION__DELIVERY_SUBURB AS dest_suburb,
                ORDER_LOCATION__DELIVERY_STATE AS dest_state,
                TO_VARCHAR(ORDER_LOCATION__DELIVERY_POSTCODE) AS dest_postcode,
                ORDER_PARCEL__PARCEL_COUNT AS parcel_count,
                ORDER_WEIGHT__DEAD_WEIGHT AS dead_weight_kg,
                ORDER_WEIGHT__VOLUMETRIC_WEIGHT AS volumetric_weight_kg,
                GREATEST(ORDER_WEIGHT__DEAD_WEIGHT, ORDER_WEIGHT__VOLUMETRIC_WEIGHT) AS chargeable_weight_kg
            FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
            WHERE TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) = '{_sql_literal(invoice_number)}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY TO_VARCHAR(ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)
                ORDER BY ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP DESC
            ) = 1
            """
        ).collect()
    finally:
        session.close()

    if rows:
        row = rows[0].asDict()
        return {
            "invoice_number": str(row.get("INVOICE_NUMBER") or invoice_number),
            "despatch_date": row.get("DESPATCH_DATE"),
            "dest_suburb": row.get("DEST_SUBURB"),
            "dest_state": row.get("DEST_STATE"),
            "dest_postcode": row.get("DEST_POSTCODE"),
            "parcel_count": row.get("PARCEL_COUNT"),
            "dead_weight_kg": _to_float(row.get("DEAD_WEIGHT_KG")),
            "volumetric_weight_kg": _to_float(row.get("VOLUMETRIC_WEIGHT_KG")),
            "chargeable_weight_kg": _to_float(row.get("CHARGEABLE_WEIGHT_KG")),
        }

    # Fall back to the stage 3 row if Snowflake is not reachable or the order has aged out.
    return {
        "invoice_number": invoice_number,
        "despatch_date": stage3_row.get("despatch_date"),
        "dest_suburb": stage3_row.get("dest_suburb"),
        "dest_state": stage3_row.get("dest_state"),
        "dest_postcode": stage3_row.get("dest_postcode"),
        "parcel_count": _to_int(stage3_row.get("actual_parcel_count")),
        "dead_weight_kg": None,
        "volumetric_weight_kg": None,
        "chargeable_weight_kg": _to_float(
            stage3_row.get("shippit_chargeable_weight_kg") or stage3_row.get("actual_order_chargeable_kg")
        ),
    }


def _build_validation_row_from_stage2(
    invoice_number: str,
    stage2_rows: list[dict[str, Any]],
    shippit_meta: dict[str, Any],
) -> dict[str, Any]:
    ordered_rows = sorted(stage2_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0)
    parcel_units = [_to_int(row.get("parcel_units")) or 0 for row in ordered_rows]
    weights = [_to_float(row.get("parcel_weight_kg")) or 0.0 for row in ordered_rows]
    cubes = [_to_float(row.get("parcel_cube_cm3")) or 0.0 for row in ordered_rows]
    fit_bands = [str(row.get("fit_band") or "") for row in ordered_rows]
    total_weight = sum(weights)
    total_cube = sum(cubes)
    estimated_chargeable = max(total_weight, total_cube / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG) if ordered_rows else 0.0
    parcel_count = len(ordered_rows)
    parcel_count_match = "Y" if parcel_count == _to_int(shippit_meta.get("parcel_count")) else "N"
    largest_weight_share_pct = (max(weights) * 100 / total_weight) if total_weight else 0.0
    largest_cube_share_pct = (max(cubes) * 100 / total_cube) if total_cube else 0.0
    fill_pct = (total_cube * 100 / ((shippit_meta.get("volumetric_weight_kg") or 0.0) * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)) if shippit_meta.get("volumetric_weight_kg") else None
    air_pct = (100.0 - fill_pct) if fill_pct is not None else None
    max_weight = max(weights) if weights else 0.0
    min_weight = min(weights) if weights else 0.0
    max_cube = max(cubes) if cubes else 0.0
    min_cube = min(cubes) if cubes else 0.0
    avg_units = sum(parcel_units) / len(parcel_units) if parcel_units else 0.0
    odd_allocation = (
        "Y"
        if len(ordered_rows) > 1
        and (
            max(parcel_units) - min(parcel_units) >= max(3, round(avg_units * 1.5))
            or largest_weight_share_pct >= 60.0
            or largest_cube_share_pct >= 60.0
        )
        else ""
    )

    return {
        "site": shippit_meta.get("store_name") or "",
        "shippit_invoice_number": invoice_number,
        "shippit_carrier": ordered_rows[0].get("actual_carrier") if ordered_rows else "",
        "shippit_parcel_count": _to_int(shippit_meta.get("parcel_count")) or 0,
        "est_parcelization_scope": "SINGLE_PARCEL" if (ordered_rows and _to_int(ordered_rows[0].get("estimated_parcel_count")) <= 1) else "MULTI_PARCEL",
        "est_pack_process_group": _pack_process_group(str(ordered_rows[0].get("rule_name") or "")) if ordered_rows else "UNKNOWN",
        "shippit_dead_weight_kg": shippit_meta.get("dead_weight_kg"),
        "shippit_volumetric_weight_kg": shippit_meta.get("volumetric_weight_kg"),
        "shippit_chargeable_weight_kg": shippit_meta.get("chargeable_weight_kg"),
        "shippit_volumetric_cube_cm3": (shippit_meta.get("volumetric_weight_kg") or 0.0) * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG if shippit_meta.get("volumetric_weight_kg") is not None else None,
        "est_parcel_count": _to_int(ordered_rows[0].get("estimated_parcel_count")) if ordered_rows else 0,
        "est_parcel_count_match": parcel_count_match,
        "est_total_parcel_weight_kg": total_weight,
        "est_total_parcel_cube_cm3": total_cube,
        "est_chargeable_weight_kg": estimated_chargeable,
        "est_parcel_units_total": sum(parcel_units),
        "est_parcel_units_pattern": "|".join(str(value) for value in parcel_units),
        "est_parcel_weight_pattern_kg": "|".join(f"{value:.2f}" for value in weights),
        "est_parcel_cube_pattern_cm3": "|".join(f"{value:.2f}" for value in cubes),
        "est_fit_band_pattern": "|".join(fit_bands),
        "est_min_parcel_weight_kg": min_weight,
        "est_max_parcel_weight_kg": max_weight,
        "est_weight_spread_kg": max_weight - min_weight,
        "est_min_parcel_cube_cm3": min_cube,
        "est_max_parcel_cube_cm3": max_cube,
        "est_cube_spread_cm3": max_cube - min_cube,
        "est_largest_parcel_weight_share_pct": largest_weight_share_pct,
        "est_largest_parcel_cube_share_pct": largest_cube_share_pct,
        "est_tote_like_count": sum(1 for row in ordered_rows if str(row.get("fit_band") or "") == "TOTE_LIKE"),
        "est_manual_like_count": sum(1 for row in ordered_rows if str(row.get("fit_band") or "") == "MANUAL_LIKE"),
        "est_bulky_like_count": sum(1 for row in ordered_rows if str(row.get("fit_band") or "") == "BULKY_LIKE"),
        "est_flag_odd_allocation": odd_allocation,
        "est_weight_gap_vs_shippit_dead_kg": total_weight - (shippit_meta.get("dead_weight_kg") or 0.0),
        "est_weight_gap_vs_shippit_chargeable_kg": estimated_chargeable - (shippit_meta.get("chargeable_weight_kg") or 0.0),
        "est_chargeable_gap_abs_kg": abs(estimated_chargeable - (shippit_meta.get("chargeable_weight_kg") or 0.0)),
        "est_cube_gap_vs_shippit_cm3": total_cube - (shippit_meta.get("volumetric_weight_kg") or 0.0) * SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG if shippit_meta.get("volumetric_weight_kg") is not None else total_cube,
        "est_fill_pct_vs_shippit_cube": fill_pct,
        "est_air_ratio_pct_vs_shippit_cube": air_pct,
        "est_pack_process": _pack_process_group(str(ordered_rows[0].get("rule_name") or "")) if ordered_rows else "",
    }


def _load_invoice_rows(path: Path, invoice_number: str) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing source file: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("invoice_number") or "").strip() == invoice_number:
                rows.append(row)
    return rows


def _load_order_lines(invoice_number: str) -> list[dict[str, Any]]:
    session = get_session()
    try:
        rows = session.sql(
            f"""
            WITH target_invoice AS (
                SELECT '{_sql_literal(invoice_number)}' AS invoice_number
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN target_invoice t
                  ON t.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            cfc_latest AS (
                SELECT *
                FROM (
                    SELECT
                        sa.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY sa.SKU_ID
                            ORDER BY sa.EXTRACT_TIMESTAMP DESC
                        ) AS rn
                    FROM KSFDA.CATCH_CFC.SKU_ATTRIBUTE sa
                )
                WHERE rn = 1
            ),
            plm_latest AS (
                SELECT *
                FROM (
                    SELECT
                        dss.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY dss.PRODUCT_SOURCE_IDENTIFIER
                            ORDER BY dss.EFFECTIVE_FROM DESC
                        ) AS rn
                    FROM KSFPA.MERCHANDISE_PVT.KEYCODE_CARTON_DSS_DIMENSIONS_VW dss
                )
                WHERE rn = 1
            ),
            hierarchy_latest AS (
                SELECT *
                FROM (
                    SELECT
                        kc.PRODUCT_SOURCE_IDENTIFIER,
                        atm.MAJOR_GROUP,
                        kc.RBU_SOURCE_IDENTIFIER,
                        kc.RBU_DESCRIPTION,
                        kc.DEPARTMENT_SOURCE_IDENTIFIER,
                        kc.DEPARTMENT_DESCRIPTION,
                        kc.DIVISION_SOURCE_IDENTIFIER,
                        kc.DIVISION_DESCRIPTION,
                        kc.CLASS_SOURCE_IDENTIFIER,
                        kc.CLASS_DESCRIPTION,
                        kc.SUB_CLASS_SOURCE_IDENTIFIER,
                        kc.SUB_CLASS_DESCRIPTION,
                        ROW_NUMBER() OVER (
                            PARTITION BY kc.PRODUCT_SOURCE_IDENTIFIER
                            ORDER BY kc.PRODUCT_SOURCE_IDENTIFIER
                        ) AS rn
                    FROM KSFPA.MR2C.KEYCODE kc
                    LEFT JOIN KSFPA.ITRADE.ATM_DEPARTMENT atm
                      ON atm.DEPARTMENT_ID = kc.DEPARTMENT_SOURCE_IDENTIFIER
                )
                WHERE rn = 1
            )
            SELECT
                p.invoice_number,
                p.SKU_ID,
                SUM(p.QUANTITY_PACKED) AS qty_packed,
                COALESCE(h.MAJOR_GROUP, '') AS major_group,
                COALESCE(h.RBU_DESCRIPTION, '') AS rbu_description,
                COALESCE(h.DEPARTMENT_DESCRIPTION, '') AS department_description,
                COALESCE(h.DIVISION_DESCRIPTION, '') AS division_description,
                COALESCE(h.CLASS_DESCRIPTION, '') AS class_description,
                COALESCE(h.SUB_CLASS_DESCRIPTION, '') AS sub_class_description,
                pml.ITEM_NAME AS plm_item_name,
                pml.ITEM_DESCRIPTION AS plm_item_description,
                pml.INSTORE_HEIGHT AS plm_height_cm,
                pml.INSTORE_WIDTH AS plm_width_cm,
                pml.INSTORE_LENGTH AS plm_length_cm,
                pml.INSTORE_GROSS_WEIGHT AS plm_weight_kg,
                c.HEIGHT AS cfc_height_cm,
                c.WIDTH AS cfc_width_cm,
                c.LENGTH AS cfc_length_cm,
                c.WEIGHT AS cfc_weight_kg,
                CASE
                    WHEN c.HEIGHT IS NULL OR c.WIDTH IS NULL OR c.LENGTH IS NULL THEN 'No dims'
                    WHEN LEAST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 50
                     AND (c.HEIGHT + c.WIDTH + c.LENGTH
                          - LEAST(c.HEIGHT, c.WIDTH, c.LENGTH)
                          - GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH)) <= 55
                     AND GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 55
                     AND COALESCE(c.WEIGHT, 0) <= 15 THEN 'Toteable'
                    WHEN LEAST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 105
                     AND (c.HEIGHT + c.WIDTH + c.LENGTH
                          - LEAST(c.HEIGHT, c.WIDTH, c.LENGTH)
                          - GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH)) <= 105
                     AND GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 105
                     AND COALESCE(c.WEIGHT, 0) <= 45 THEN 'Non-Toteable'
                    WHEN LEAST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 200
                     AND (c.HEIGHT + c.WIDTH + c.LENGTH
                          - LEAST(c.HEIGHT, c.WIDTH, c.LENGTH)
                          - GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH)) <= 200
                     AND GREATEST(c.HEIGHT, c.WIDTH, c.LENGTH) <= 200
                     AND COALESCE(c.WEIGHT, 0) <= 80 THEN 'Bulky'
                    ELSE 'Unknown'
                END AS cfc_size_category,
                CASE
                    WHEN pml.INSTORE_HEIGHT IS NULL OR pml.INSTORE_WIDTH IS NULL OR pml.INSTORE_LENGTH IS NULL THEN 'No dims'
                    WHEN LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 50
                     AND (pml.INSTORE_HEIGHT + pml.INSTORE_WIDTH + pml.INSTORE_LENGTH
                          - LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)
                          - GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)) <= 55
                     AND GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 55
                     AND COALESCE(pml.INSTORE_GROSS_WEIGHT, 0) <= 15 THEN 'Toteable'
                    WHEN LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 105
                     AND (pml.INSTORE_HEIGHT + pml.INSTORE_WIDTH + pml.INSTORE_LENGTH
                          - LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)
                          - GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)) <= 105
                     AND GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 105
                     AND COALESCE(pml.INSTORE_GROSS_WEIGHT, 0) <= 45 THEN 'Non-Toteable'
                    WHEN LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 200
                     AND (pml.INSTORE_HEIGHT + pml.INSTORE_WIDTH + pml.INSTORE_LENGTH
                          - LEAST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)
                          - GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH)) <= 200
                     AND GREATEST(pml.INSTORE_HEIGHT, pml.INSTORE_WIDTH, pml.INSTORE_LENGTH) <= 200
                     AND COALESCE(pml.INSTORE_GROSS_WEIGHT, 0) <= 80 THEN 'Bulky'
                    ELSE 'Unknown'
                END AS plm_size_category
            FROM packed p
            LEFT JOIN cfc_latest c
              ON c.SKU_ID = p.SKU_ID
            LEFT JOIN plm_latest pml
              ON pml.PRODUCT_SOURCE_IDENTIFIER = p.SKU_ID
            LEFT JOIN hierarchy_latest h
              ON h.PRODUCT_SOURCE_IDENTIFIER = p.SKU_ID
            GROUP BY
                p.invoice_number,
                p.SKU_ID,
                h.MAJOR_GROUP,
                h.RBU_DESCRIPTION,
                h.DEPARTMENT_DESCRIPTION,
                h.DIVISION_DESCRIPTION,
                h.CLASS_DESCRIPTION,
                h.SUB_CLASS_DESCRIPTION,
                pml.ITEM_NAME,
                pml.ITEM_DESCRIPTION,
                pml.INSTORE_HEIGHT,
                pml.INSTORE_WIDTH,
                pml.INSTORE_LENGTH,
                pml.INSTORE_GROSS_WEIGHT,
                c.HEIGHT,
                c.WIDTH,
                c.LENGTH,
                c.WEIGHT
            ORDER BY qty_packed DESC, p.SKU_ID
            """
        ).collect()
    finally:
        session.close()

    return [{str(key).lower(): value for key, value in row.asDict().items()} for row in rows]


def _write_summary_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    sheet = workbook.create_sheet("Summary")
    _style_title(sheet, f"Invoice Drilldown - {context.invoice_number}")
    sheet.freeze_panes = "A4"

    stage3 = context.stage3_row
    validation = context.validation_row
    parcel_rows = context.stage2_rows
    sku_rows = context.packed_lines

    total_units = sum(int(row.get("parcel_units") or 0) for row in parcel_rows)
    total_parcel_weight = sum(_to_float(row.get("parcel_weight_kg")) or 0.0 for row in parcel_rows)
    total_parcel_cube = sum(
        _to_float(row.get("estimated_parcel_cube_cm3")) or _to_float(row.get("parcel_cube_cm3")) or 0.0
        for row in parcel_rows
    )
    total_parcel_cube_m3 = total_parcel_cube / 1_000_000.0
    packed_sku_cube_total = sum(
        _to_float(row.get("raw_packed_sku_cube_cm3")) or _to_float(row.get("packed_sku_cube_cm3")) or 0.0
        for row in parcel_rows
    )
    rectangularization_cube_uplift_cm3 = max(total_parcel_cube - packed_sku_cube_total, 0.0)
    rectangularization_cube_uplift_m3 = rectangularization_cube_uplift_cm3 / 1_000_000.0
    estimated_chargeable_weight = max(total_parcel_weight, total_parcel_cube / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG)
    shippit_dead_weight_kg = _to_float(validation.get("shippit_dead_weight_kg"))
    shippit_chargeable_cube_cm3 = _to_float(stage3.get("shippit_chargeable_cube_cm3"))
    shippit_volumetric_cube_cm3 = _to_float(validation.get("shippit_volumetric_cube_cm3"))
    pricing_scale_factor = _to_float(stage3.get("pricing_scale_factor"))
    pricing_scaled_parcel_cube_total_cm3 = _to_float(stage3.get("pricing_scaled_parcel_cube_total_cm3"))
    pricing_scaled_parcel_cube_total_m3 = _to_float(stage3.get("pricing_scaled_parcel_cube_total_m3"))
    pricing_scaled_parcel_volumetric_weight_kg = _to_float(stage3.get("pricing_scaled_parcel_volumetric_weight_kg"))
    packaging_weight_uplift_kg = (
        max((shippit_dead_weight_kg or 0.0) - total_parcel_weight, 0.0) if shippit_dead_weight_kg is not None else None
    )
    packaging_cube_uplift_cm3 = (
        max((shippit_volumetric_cube_cm3 or 0.0) - total_parcel_cube, 0.0) if shippit_volumetric_cube_cm3 is not None else None
    )
    packaging_cube_uplift_m3 = packaging_cube_uplift_cm3 / 1_000_000.0 if packaging_cube_uplift_cm3 is not None else None
    packed_stats = _packed_sku_quality_stats(sku_rows)
    imputed_parcel_rows = sum(1 for row in parcel_rows if str(row.get("cfc_cube_imputed") or "").upper() == "Y")
    shippit_cube_cm3 = _to_float(validation.get("shippit_volumetric_cube_cm3"))
    shippit_cube_m3 = shippit_cube_cm3 / 1_000_000.0 if shippit_cube_cm3 is not None else None
    parcel_count = len(parcel_rows)
    odd_allocation = str(validation.get("est_flag_odd_allocation") or "")
    fill_pct = _to_float(validation.get("est_fill_pct_vs_shippit_cube"))
    air_pct = _to_float(validation.get("est_air_ratio_pct_vs_shippit_cube"))
    benchmark_chargeable_gap_kg = _to_float(validation.get("benchmark_chargeable_gap_kg"))
    benchmark_chargeable_gap_threshold_kg = _to_float(validation.get("benchmark_chargeable_gap_threshold_kg"))
    benchmark_air_ratio_pct = _to_float(validation.get("benchmark_air_ratio_pct"))
    benchmark_air_ratio_threshold_pct = _to_float(validation.get("benchmark_air_ratio_threshold_pct"))
    benchmark_review_flag = validation.get("benchmark_review_flag")
    p75_commentary = _summary_commentary(context, sku_rows, validation)
    actual_cost_basis_note = _cost_basis_commentary(str(stage3.get("actual_service") or ""), stage3, role="actual")
    alternative_cost_basis_note = _cost_basis_commentary(str(stage3.get("cheapest_alt_service") or ""), stage3, role="alternative")
    size_category_totals = _size_category_totals(sku_rows)

    summary_rows = [
        ("Site", context.site_label, None),
        ("Invoice number", context.invoice_number, None),
        ("Carrier", stage3.get("actual_carrier"), None),
        ("Service", stage3.get("actual_service"), None),
        ("Actual cost note", actual_cost_basis_note, None),
        ("Next-cheapest note", alternative_cost_basis_note, None),
        ("Destination", f"{stage3.get('dest_suburb')}, {stage3.get('dest_state')} {stage3.get('dest_postcode')}", None),
        ("Allied zone", stage3.get("allied_zone"), None),
        ("Allied zone source", stage3.get("allied_zone_source"), None),
        ("CP zone", stage3.get("cp_zone"), None),
        ("AP zone", stage3.get("ap_zone"), None),
        ("AP pricing zone", stage3.get("ap_pricing_zone"), None),
        ("GoEXPRESS zone", stage3.get("goexpress_zone"), None),
        ("GoEXPRESS zone source", stage3.get("goexpress_zone_source"), None),
        ("Actual parcel count", _to_int(stage3.get("actual_parcel_count")), "#,##0"),
        ("Estimated parcel count", _to_int(stage3.get("estimated_parcel_count")), "#,##0"),
        ("Parcel validation match", validation.get("est_parcel_count_match"), None),
        ("Parcelizer scope", validation.get("est_parcelization_scope"), None),
        ("Pack process group", validation.get("est_pack_process_group"), None),
        ("Estimated pack origin path", validation.get("est_estimated_pack_origin_path"), None),
        ("Parcel contents size mix", validation.get("est_parcel_contents_size_mix"), None),
        ("Shape method", validation.get("est_shape_source_pattern"), None),
        ("Parcel detail rows", parcel_count, "#,##0"),
        ("Packed SKU rows", packed_stats["row_count"], "#,##0"),
        ("Packed SKU units", packed_stats["unit_count"], "#,##0"),
        ("Packed SKU rows missing CFC weight", packed_stats["missing_cfc_weight_row_pct"], "0.0%"),
        ("Packed SKU units missing CFC weight", packed_stats["missing_cfc_weight_unit_pct"], "0.0%"),
        ("Packed SKU rows missing CFC dims", packed_stats["missing_cfc_dims_row_pct"], "0.0%"),
        ("Packed SKU units missing CFC dims", packed_stats["missing_cfc_dims_unit_pct"], "0.0%"),
        ("Packed SKU rows missing both CFC and PLM dims", packed_stats["missing_both_dims_row_pct"], "0.0%"),
        ("Packed SKU units missing both CFC and PLM dims", packed_stats["missing_both_dims_unit_pct"], "0.0%"),
        ("Imputed parcel rows", imputed_parcel_rows, "#,##0"),
        ("Imputed parcel row pct", (imputed_parcel_rows * 100.0 / parcel_count) if parcel_count else 0.0, "0.0%"),
        ("Packed SKU weight kg", total_parcel_weight, "#,##0.00"),
        ("Raw packed SKU cube", packed_sku_cube_total, "#,##0.00"),
        ("Raw packed SKU cube (m3)", packed_sku_cube_total / 1_000_000.0 if packed_sku_cube_total is not None else None, "#,##0.000000"),
        ("Estimated parcel cube", total_parcel_cube, "#,##0.00"),
        ("Estimated parcel cube (m3)", total_parcel_cube_m3, "#,##0.000000"),
        ("Rectangularization cube uplift cm3", rectangularization_cube_uplift_cm3, "#,##0.00"),
        ("Rectangularization cube uplift m3", rectangularization_cube_uplift_m3, "#,##0.000000"),
        ("Expected packaging uplift", packaging_weight_uplift_kg, "#,##0.00"),
        ("Expected packaging uplift cube cm3", packaging_cube_uplift_cm3, "#,##0.00"),
        ("Expected packaging uplift cube m3", packaging_cube_uplift_m3, "#,##0.000000"),
        ("Estimated chargeable kg", estimated_chargeable_weight, "#,##0.00"),
        ("Shippit chargeable cube cm3", shippit_chargeable_cube_cm3, "#,##0.00"),
        ("Residual vs Shippit cube gap cm3", _to_float(stage3.get("shippit_vs_estimated_cube_gap_cm3")), "#,##0.00"),
        ("Shippit chargeable kg", _to_float(validation.get("shippit_chargeable_weight_kg")), "#,##0.00"),
        ("Shippit dead weight kg", _to_float(validation.get("shippit_dead_weight_kg")), "#,##0.00"),
        ("Shippit volumetric kg", _to_float(validation.get("shippit_volumetric_weight_kg")), "#,##0.00"),
        ("Shippit volumetric cube m3", shippit_cube_m3, "#,##0.000000"),
        ("Normalised parcel cube scale factor", pricing_scale_factor, "0.0000"),
        ("Normalised parcel cube", pricing_scaled_parcel_cube_total_cm3, "#,##0.00"),
        ("Normalised parcel cube (m3)", pricing_scaled_parcel_cube_total_m3, "#,##0.000000"),
        ("Normalised parcel volumetric kg", pricing_scaled_parcel_volumetric_weight_kg, "#,##0.00"),
        ("CP invoice chargeable kg (rounded)", _to_float(stage3.get("cp_pricing_chargeable_weight_kg")), "#,##0.00"),
        ("CP 10-parcel cap applied", stage3.get("cp_pricing_cap_applied"), None),
        ("Fill vs Shippit cube", (fill_pct / 100.0) if fill_pct is not None else None, "0.0%"),
        ("Air vs Shippit cube", (air_pct / 100.0) if air_pct is not None else None, "0.0%"),
        ("Odd allocation flag", odd_allocation, None),
        ("Absolute estimated vs Shippit chargeable gap kg", benchmark_chargeable_gap_kg, "#,##0.00"),
        ("Estate p95 chargeable gap threshold kg", benchmark_chargeable_gap_threshold_kg, "#,##0.00"),
        ("Rectangularized residual vs Shippit cube", (benchmark_air_ratio_pct / 100.0) if benchmark_air_ratio_pct is not None else None, "0.0%"),
        (
            "Estate p95 absolute cube-residual threshold",
            (benchmark_air_ratio_threshold_pct / 100.0) if benchmark_air_ratio_threshold_pct is not None else None,
            "0.0%",
        ),
        ("Benchmark review flag", benchmark_review_flag, None),
        ("Actual est cost ex GST", _to_float(stage3.get("actual_est_cost_ex_gst")), "$#,##0.00"),
        ("Cheapest alt cost ex GST", _to_float(stage3.get("cheapest_alt_cost_ex_gst")), "$#,##0.00"),
        ("Estimated saving ex GST", _to_float(stage3.get("est_saving_ex_gst")), "$#,##0.00"),
        ("Cheaper option exists", stage3.get("cheaper_option_exists"), None),
        ("Review commentary", p75_commentary, None),
    ]

    sheet.append(["Metric", "Value"])
    _style_header(sheet)
    for label, value, number_format in summary_rows:
        sheet.append([label, value])
        if number_format:
            sheet.cell(sheet.max_row, 2).number_format = number_format

    sheet.append([])
    sheet.append(["Size category", "Total units"])
    _style_header(sheet)
    size_section_start = sheet.max_row
    for category in ["Toteable", "Non-Toteable", "Bulky", "No Dims", "Other / Unknown"]:
        sheet.append([category, size_category_totals.get(category, 0)])
    for row_idx in range(size_section_start + 1, sheet.max_row + 1):
        sheet.cell(row_idx, 2).number_format = "#,##0"

    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A1"].fill = _TITLE_FILL
    sheet["A1"].alignment = Alignment(horizontal="left")
    sheet.merge_cells("A1:B1")
    sheet["A2"] = (
        "This drilldown is a starting point. Use the parcel validation first, then check the parcel detail and SKU review "
        "to decide whether the issue sits in the split logic, source dimensions, packaging uplift, or a carrier assumption."
    )
    sheet.merge_cells("A2:B2")
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet["A2"].fill = _SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    sheet.row_dimensions[2].height = 42
    _autosize(sheet)
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 95


def _write_parcel_validation_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    sheet = workbook.create_sheet("Parcel Validation")
    _style_title(sheet, "Parcelizer validation")
    sheet.freeze_panes = "A4"

    stage3 = context.stage3_row
    validation_row = dict(context.validation_row)
    validation_row["est_total_parcel_cube_m3"] = _to_float(validation_row.get("est_total_parcel_cube_cm3")) / 1_000_000.0 if validation_row.get("est_total_parcel_cube_cm3") is not None else None
    validation_row["shippit_volumetric_cube_m3"] = _to_float(validation_row.get("shippit_volumetric_cube_cm3")) / 1_000_000.0 if validation_row.get("shippit_volumetric_cube_cm3") is not None else None
    validation_row["est_parcel_cube_pattern_m3"] = _pattern_m3(validation_row.get("est_parcel_cube_pattern_cm3"))
    validation_row["shippit_chargeable_cube_cm3"] = stage3.get("shippit_chargeable_cube_cm3")
    validation_row["pricing_scale_factor"] = stage3.get("pricing_scale_factor")
    validation_row["pricing_scaled_parcel_cube_total_cm3"] = stage3.get("pricing_scaled_parcel_cube_total_cm3")
    validation_row["pricing_scaled_parcel_cube_total_m3"] = stage3.get("pricing_scaled_parcel_cube_total_m3")
    validation_row["pricing_scaled_parcel_volumetric_weight_kg"] = stage3.get("pricing_scaled_parcel_volumetric_weight_kg")
    validation_row["est_packaging_weight_uplift_kg"] = max(
        (_to_float(validation_row.get("shippit_dead_weight_kg")) or 0.0) - (_to_float(validation_row.get("est_total_parcel_weight_kg")) or 0.0),
        0.0,
    )
    validation_row["est_packaging_cube_uplift_cm3"] = max(
        (_to_float(validation_row.get("shippit_volumetric_cube_cm3")) or 0.0) - (_to_float(validation_row.get("est_total_parcel_cube_cm3")) or 0.0),
        0.0,
    )
    validation_row["est_packaging_cube_uplift_m3"] = (
        _to_float(validation_row.get("est_packaging_cube_uplift_cm3")) / 1_000_000.0 if validation_row.get("est_packaging_cube_uplift_cm3") is not None else None
    )
    validation_row["est_chargeable_weight_kg"] = context.validation_row.get("est_chargeable_weight_kg")
    validation_row["benchmark_chargeable_gap_kg"] = context.validation_row.get("benchmark_chargeable_gap_kg")
    validation_row["benchmark_chargeable_gap_threshold_kg"] = context.validation_row.get("benchmark_chargeable_gap_threshold_kg")
    validation_row["benchmark_chargeable_gap_flag"] = context.validation_row.get("benchmark_chargeable_gap_flag")
    validation_row["benchmark_air_ratio_pct"] = context.validation_row.get("benchmark_air_ratio_pct")
    validation_row["benchmark_air_ratio_threshold_pct"] = context.validation_row.get("benchmark_air_ratio_threshold_pct")
    validation_row["benchmark_air_ratio_flag"] = context.validation_row.get("benchmark_air_ratio_flag")
    validation_row["benchmark_review_flag"] = context.validation_row.get("benchmark_review_flag")

    summary_fields = [
        "site",
        "shippit_invoice_number",
        "shippit_carrier",
        "shippit_parcel_count",
        "est_parcelization_scope",
        "est_pack_process_group",
        "est_shape_source_pattern",
        "shippit_dead_weight_kg",
        "shippit_volumetric_weight_kg",
        "shippit_chargeable_cube_cm3",
        "shippit_chargeable_weight_kg",
        "shippit_volumetric_cube_cm3",
        "shippit_volumetric_cube_m3",
        "est_parcel_count",
        "est_parcel_count_match",
        "est_total_parcel_weight_kg",
        "est_total_parcel_cube_cm3",
        "est_total_parcel_cube_m3",
        "est_packaging_weight_uplift_kg",
        "est_packaging_cube_uplift_cm3",
        "est_packaging_cube_uplift_m3",
        "est_chargeable_weight_kg",
        "pricing_scale_factor",
        "pricing_scaled_parcel_cube_total_cm3",
        "pricing_scaled_parcel_cube_total_m3",
        "pricing_scaled_parcel_volumetric_weight_kg",
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
        "est_weight_gap_vs_shippit_dead_kg",
        "est_weight_gap_vs_shippit_chargeable_kg",
        "est_cube_gap_vs_shippit_cm3",
        "est_fill_pct_vs_shippit_cube",
        "est_air_ratio_pct_vs_shippit_cube",
        "benchmark_chargeable_gap_kg",
        "benchmark_chargeable_gap_threshold_kg",
        "benchmark_chargeable_gap_flag",
        "benchmark_air_ratio_pct",
        "benchmark_air_ratio_threshold_pct",
        "benchmark_air_ratio_flag",
        "benchmark_review_flag",
    ]

    sheet.append(["Metric", "Value"])
    _style_header(sheet)
    for field in summary_fields:
        value = validation_row.get(field)
        sheet.append([field, value])
        if field in {
            "shippit_dead_weight_kg",
            "shippit_volumetric_weight_kg",
            "shippit_chargeable_cube_cm3",
            "shippit_chargeable_weight_kg",
            "est_total_parcel_weight_kg",
            "est_total_parcel_cube_cm3",
            "shippit_volumetric_cube_cm3",
            "est_packaging_weight_uplift_kg",
            "est_packaging_cube_uplift_cm3",
            "est_weight_spread_kg",
            "est_min_parcel_weight_kg",
            "est_max_parcel_weight_kg",
            "est_min_parcel_cube_cm3",
            "est_max_parcel_cube_cm3",
            "est_cube_spread_cm3",
            "est_weight_gap_vs_shippit_dead_kg",
            "est_weight_gap_vs_shippit_chargeable_kg",
            "est_cube_gap_vs_shippit_cm3",
            "pricing_scale_factor",
            "pricing_scaled_parcel_cube_total_cm3",
        }:
            sheet.cell(sheet.max_row, 2).number_format = "#,##0.00"
        elif field in {
            "shippit_volumetric_cube_m3",
            "est_total_parcel_cube_m3",
            "est_packaging_cube_uplift_m3",
            "pricing_scaled_parcel_cube_total_m3",
        }:
            sheet.cell(sheet.max_row, 2).number_format = "#,##0.000000"
        elif field in {
            "est_largest_parcel_weight_share_pct",
            "est_largest_parcel_cube_share_pct",
            "est_fill_pct_vs_shippit_cube",
            "est_air_ratio_pct_vs_shippit_cube",
        }:
            sheet.cell(sheet.max_row, 2).number_format = "0.0%"
        elif field in {"pricing_scaled_parcel_volumetric_weight_kg"}:
            sheet.cell(sheet.max_row, 2).number_format = "#,##0.00"
        elif field in {"shippit_parcel_count", "est_parcel_count", "est_parcel_units_total", "est_tote_like_count", "est_manual_like_count", "est_bulky_like_count"}:
            sheet.cell(sheet.max_row, 2).number_format = "#,##0"

    sheet.append([])
    sheet.append(["Parcel id", "Parcel allocation rule", "Parcel units", "Parcel weight kg", "Parcel cube cm3", "Parcel cube m3", "Fit band", "SKU ids"])
    _style_header(sheet)
    for row in sorted(context.stage1_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0):
        cube_cm3 = _to_float(row.get("parcel_cube"))
        sheet.append(
            [
                _to_int(row.get("parcel_id")),
                row.get("rule_name"),
                _to_int(row.get("parcel_units")),
                _to_float(row.get("parcel_weight_kg")),
                cube_cm3,
                cube_cm3 / 1_000_000.0 if cube_cm3 is not None else None,
                row.get("fit_band") or row.get("parcel_fit_band") or "",
                row.get("sku_ids"),
            ]
        )
    for row_idx in range(sheet.max_row - len(context.stage1_rows) + 1, sheet.max_row + 1):
        for col_idx in [1, 3]:
            sheet.cell(row_idx, col_idx).number_format = "#,##0"
        for col_idx in [4, 5]:
            sheet.cell(row_idx, col_idx).number_format = "#,##0.00"
        sheet.cell(row_idx, 6).number_format = "#,##0.000000"
    _autosize(sheet)


def _write_parcel_detail_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    records = []
    validation = context.validation_row
    stage3 = context.stage3_row
    shippit_cube_cm3 = _to_float(validation.get("shippit_volumetric_cube_cm3"))
    pricing_scale_factor = _to_float(stage3.get("pricing_scale_factor"))
    stage1_by_parcel = {str(row.get("parcel_id") or ""): row for row in context.stage1_rows}
    for row in sorted(context.stage2_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0):
        parcel_id = str(row.get("parcel_id") or "")
        merged = dict(stage1_by_parcel.get(parcel_id, {}))
        merged.update(row)
        parcel_cube_cm3 = _to_float(merged.get("estimated_parcel_cube_cm3")) or _to_float(merged.get("parcel_cube_cm3"))
        packed_sku_cube_cm3 = _to_float(merged.get("raw_packed_sku_cube_cm3")) or _to_float(merged.get("packed_sku_cube_cm3"))
        fill_pct = (parcel_cube_cm3 * 100 / shippit_cube_cm3) if parcel_cube_cm3 is not None and shippit_cube_cm3 else None
        rectangularization_uplift_cm3 = max((parcel_cube_cm3 or 0.0) - (packed_sku_cube_cm3 or 0.0), 0.0)
        merged["parcel_product_cube_cm3"] = parcel_cube_cm3
        merged["parcel_raw_packed_sku_cube_cm3"] = packed_sku_cube_cm3
        merged["parcel_packed_sku_cube_cm3"] = packed_sku_cube_cm3
        merged["parcel_estimated_cube_cm3"] = parcel_cube_cm3
        merged["rectangularization_cube_uplift_cm3"] = rectangularization_uplift_cm3
        merged["rectangularization_cube_uplift_m3"] = rectangularization_uplift_cm3 / 1_000_000.0
        merged["parcel_fill_pct_vs_shippit_cube"] = fill_pct
        merged["parcel_air_ratio_pct_vs_shippit_cube"] = (100.0 - fill_pct) if fill_pct is not None else None
        merged["pricing_scale_factor"] = pricing_scale_factor
        merged["parcel_pricing_cube_cm3"] = (parcel_cube_cm3 * pricing_scale_factor) if parcel_cube_cm3 is not None and pricing_scale_factor is not None else None
        merged["parcel_pricing_cube_m3"] = (
            (merged["parcel_pricing_cube_cm3"] or 0.0) / 1_000_000.0 if merged.get("parcel_pricing_cube_cm3") is not None else None
        )
        merged["parcel_pricing_volumetric_weight_kg"] = (
            (merged["parcel_pricing_cube_cm3"] or 0.0) / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG
            if merged.get("parcel_pricing_cube_cm3") is not None
            else None
        )
        merged["cfc_cube_imputed"] = merged.get("cfc_cube_imputed") or ""
        commentary_bits = []
        if merged["cfc_cube_imputed"] == "Y":
            commentary_bits.append("Cube was imputed from invoice-average CFC data.")
        if merged.get("estimated_shape_source"):
            commentary_bits.append(
                f"Rectangular envelope method {merged.get('estimated_shape_source')} used to derive the parcel shape."
            )
        if pricing_scale_factor is not None:
            commentary_bits.append("Normalised parcel cube is scaled to the Shippit chargeable cube so carrier comparisons stay aligned.")
        if fill_pct is not None:
            if fill_pct <= 20:
                commentary_bits.append("Very airy versus Shippit cube; check carton choice and dunnage.")
            elif fill_pct >= 80:
                commentary_bits.append("Tight fill versus Shippit cube.")
        merged["parcel_review_commentary"] = " ".join(commentary_bits)
        records.append(merged)

    sheet = workbook.create_sheet("Parcel Detail")
    _write_records_sheet(sheet, records, title="Parcel assignment and physical detail")


def _write_cost_detail_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    sheet = workbook.create_sheet("Cost Detail")
    _style_title(sheet, "Carrier costing detail")
    sheet.freeze_panes = "A4"

    stage3 = context.stage3_row
    parcel_rows = sorted(context.stage2_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0)
    rated_parcel_costs = _rated_parcel_costs(context, parcel_rows)
    alternative_parcel_costs = _alternative_parcel_costs(context, parcel_rows)
    total_weight = sum(_to_float(row.get("parcel_weight_kg")) or 0.0 for row in parcel_rows)
    total_cost = _to_float(stage3.get("actual_est_cost_ex_gst"))
    row_count = len(parcel_rows)
    if row_count == 0:
        sheet.append(["No parcel rows found"])
        return

    records = []
    for index, row in enumerate(parcel_rows):
        weight = _to_float(row.get("parcel_weight_kg")) or 0.0
        share = (weight / total_weight) if total_weight > 0 else (1.0 / row_count)
        rated_cost = rated_parcel_costs[index] if index < len(rated_parcel_costs) else None
        alt_choice = alternative_parcel_costs[index] if index < len(alternative_parcel_costs) else None
        allocated_cost = (total_cost * share) if total_cost is not None else None
        rating_basis = "INVOICE_LEVEL_ALLOCATED" if str(stage3.get("actual_service") or "") == "Couriers Please" else "PARCEL_LEVEL_TARIFF"
        parcel_cube_cm3 = _to_float(row.get("parcel_cube_cm3"))
        pricing_scale_factor = _to_float(stage3.get("pricing_scale_factor"))
        parcel_pricing_cube_cm3 = (parcel_cube_cm3 * pricing_scale_factor) if parcel_cube_cm3 is not None and pricing_scale_factor is not None else None
        records.append(
            {
                "invoice_number": stage3.get("invoice_number"),
                "actual_carrier": stage3.get("actual_carrier"),
                "actual_service": stage3.get("actual_service"),
                "dest_suburb": stage3.get("dest_suburb"),
                "dest_state": stage3.get("dest_state"),
                "dest_postcode": stage3.get("dest_postcode"),
                "parcel_id": _to_int(row.get("parcel_id")),
                "parcel_units": _to_int(row.get("parcel_units")),
                "parcel_weight_kg": weight,
                "parcel_cube_cm3": parcel_cube_cm3,
                "pricing_scale_factor": pricing_scale_factor,
                "parcel_pricing_cube_cm3": parcel_pricing_cube_cm3,
                "parcel_pricing_cube_m3": (parcel_pricing_cube_cm3 / 1_000_000.0) if parcel_pricing_cube_cm3 is not None else None,
                "parcel_pricing_volumetric_weight_kg": (
                    parcel_pricing_cube_cm3 / SHIPPIT_VOL_WEIGHT_DIVISOR_CM3_PER_KG if parcel_pricing_cube_cm3 is not None else None
                ),
                "parcel_fill_pct_vs_shippit_cube": _parcel_fill_pct(row, context.validation_row),
                "parcel_air_ratio_pct_vs_shippit_cube": _parcel_air_pct(row, context.validation_row),
                "parcel_review_commentary": (
                    "Cube was imputed from invoice-average CFC data. "
                    if str(row.get("cfc_cube_imputed") or "").upper() == "Y"
                    else ""
                )
                + (
                    "Couriers Please is billed at invoice level on rounded chargeable weight; parcel costs are an allocated review share. "
                    if str(stage3.get("actual_service") or "") == "Couriers Please"
                    else ""
                )
                + (
                    "Normalised parcel cube is scaled to the Shippit chargeable cube so carrier comparisons stay aligned. "
                    if pricing_scale_factor is not None
                    else ""
                )
                + (
                    "Very airy versus Shippit cube; check carton choice and dunnage."
                    if (_parcel_fill_pct(row, context.validation_row) is not None and _parcel_fill_pct(row, context.validation_row) <= 20)
                    else ""
                ),
                "allied_zone": stage3.get("allied_zone"),
                "cp_zone": stage3.get("cp_zone"),
                "ap_zone": stage3.get("ap_zone"),
                "ap_pricing_zone": stage3.get("ap_pricing_zone"),
                "goexpress_zone": stage3.get("goexpress_zone"),
                "goexpress_zone_source": stage3.get("goexpress_zone_source"),
                "parcel_rating_basis": rating_basis,
                "couriers_please_invoice_chargeable_kg": _to_float(stage3.get("cp_pricing_chargeable_weight_kg")),
                "couriers_please_cap_applied": stage3.get("cp_pricing_cap_applied"),
                "actual_cost_basis_note": _cost_basis_commentary(str(stage3.get("actual_service") or ""), stage3, role="actual"),
                "next_cheapest_cost_basis_note": _cost_basis_commentary(
                    str(alt_choice["service"]) if alt_choice else "",
                    stage3,
                    role="alternative",
                ),
                "parcel_rated_cost_ex_gst": rated_cost,
                "cost_share_basis": "WEIGHT" if total_weight > 0 else "EQUAL_SPLIT",
                "cost_share_pct": share,
                "actual_est_cost_allocated_ex_gst": allocated_cost,
                "parcel_next_cheapest_carrier": alt_choice["carrier"] if alt_choice else "",
                "parcel_next_cheapest_service": alt_choice["service"] if alt_choice else "",
                "parcel_next_cheapest_cost_ex_gst": alt_choice["cost"] if alt_choice else None,
                "parcel_gap_vs_next_cheapest_ex_gst": (
                    (rated_cost - alt_choice["cost"])
                    if rated_cost is not None and alt_choice is not None and alt_choice.get("cost") is not None
                    else None
                ),
                "sku_ids": row.get("sku_ids") or "",
            }
        )

    sheet["A2"] = (
        "Costs are allocated across parcels in the same order as parcel_units_per_package so Ops can compare "
        "parcel shape and cost side by side. Normalised parcel cube comparisons are scaled back to the Shippit chargeable cube. "
        + _cost_basis_commentary(str(stage3.get("actual_service") or ""), stage3, role="actual")
    )
    sheet["A2"].fill = _SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    sheet.append(list(records[0].keys()))
    _style_header(sheet)
    for record in records:
        sheet.append([_normalize_cell_value(record.get(header)) for header in records[0].keys()])
    _format_table(sheet, list(records[0].keys()), len(records))
    _autosize(sheet)


def _write_sku_review_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    packed_lines_by_sku = {
        str(row.get("SKU_ID") or row.get("sku_id") or ""): row for row in context.packed_lines
    }
    by_parcel_sku: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for line in context.parcel_assignment_lines:
        parcel_id = _to_int(line.get("parcel_id"))
        sku_id = str(line.get("sku_id") or "").strip()
        if parcel_id is not None and sku_id:
            by_parcel_sku[(parcel_id, sku_id)].append(line)

    records = []
    for (parcel_id, sku_id), assigned_lines in sorted(by_parcel_sku.items()):
        source_row = packed_lines_by_sku.get(sku_id, {})
        qty_packed = sum(_to_int(line.get("quantity_packed")) or 0 for line in assigned_lines)
        packed_cube_cm3 = sum(_to_float(line.get("raw_packed_sku_cube_cm3_per_line")) or 0.0 for line in assigned_lines)
        qty_pattern = "|".join(str(_to_int(line.get("quantity_packed")) or 0) for line in assigned_lines)
        records.append(
            {
                "parcel_id": parcel_id,
                "sku_id": sku_id,
                "qty_packed": qty_packed,
                "parcel_qty_pattern": qty_pattern,
                "packed_sku_cube_cm3_per_line": packed_cube_cm3,
                "product_description": source_row.get("plm_item_description") or source_row.get("plm_item_name") or "",
                "size_category": source_row.get("cfc_size_category") or assigned_lines[0].get("size_category") or "",
                "department": source_row.get("department_description") or "",
                "rbu": source_row.get("rbu_description") or "",
                "major_group": source_row.get("major_group") or "",
                "department_description": source_row.get("department_description") or "",
                "class_description": source_row.get("class_description") or "",
                "cfc_size_category": source_row.get("cfc_size_category") or assigned_lines[0].get("size_category") or "",
                "plm_size_category": source_row.get("plm_size_category") or "",
                "plm_expected": "N" if _plm_not_expected(source_row) else "Y",
                "cfc_height_cm": source_row.get("cfc_height_cm") or assigned_lines[0].get("cfc_height_cm"),
                "cfc_width_cm": source_row.get("cfc_width_cm") or assigned_lines[0].get("cfc_width_cm"),
                "cfc_length_cm": source_row.get("cfc_length_cm") or assigned_lines[0].get("cfc_length_cm"),
                "cfc_weight_kg": source_row.get("cfc_weight_kg") or assigned_lines[0].get("cfc_weight_kg"),
                "plm_height_cm": source_row.get("plm_height_cm"),
                "plm_width_cm": source_row.get("plm_width_cm"),
                "plm_length_cm": source_row.get("plm_length_cm"),
                "plm_weight_kg": source_row.get("plm_weight_kg"),
                "review_commentary": _sku_review_commentary(source_row),
            }
        )

    sheet = workbook.create_sheet("SKU Review")
    _write_records_sheet(sheet, records, title="Parcel-SKU dimension checks")


def _write_notes_sheet(workbook: Workbook, context: InvoiceContext) -> None:
    sheet = workbook.create_sheet("Notes")
    _style_title(sheet, "How to read this workbook")
    sheet.freeze_panes = "A4"
    notes = [
        (
            "1. Start with Parcel Validation",
            "Check whether the estimated parcel count matches Shippit, whether the shape method looks sensible, whether fill and air ratios look sensible, whether packaging uplift is material, and whether the split looks odd.",
        ),
        (
            "2. Then open Parcel Detail",
            "Look at the parcel weights, cubes, commentary, and parcel sizes to see whether the pack looks balanced or concentrated into one parcel.",
        ),
        (
            "3. Then review SKU Review",
            "Compare CFC and PLM dims, but do not treat apparel-like keycodes as a PLM problem if PLM dims are naturally absent.",
        ),
        (
            "4. Finish with Cost Detail",
            "Use the carrier breakdown to see what drove the estimated cost and whether the cheapest alternative makes commercial sense.",
        ),
        (
            "4b. Read the cost basis note",
            "The workbook now spells out whether a carrier is billed per parcel, per lane, or at consignment level so the numbers are easier to interpret.",
        ),
        (
            "5. Treat commentary as a starting point",
            "The comments are designed to help Ops challenge the logic, not to claim the first-pass answer is final.",
        ),
        (
            "6. Benchmark thresholds are estate-based",
            "The benchmark flags use thresholds derived from the full stage 2 validation estate, not a fixed 5% assumption.",
        ),
        (
            "7. Benchmark method",
            "Chargeable-weight review uses the parcelizer chargeable weight, defined as max(parcel weight, parcel cube / 4000), then compares the absolute kg gap versus Shippit chargeable weight. Cube review uses the p95 of the absolute rectangularized-cube residual versus Shippit, while retaining the signed residual for diagnosis.",
        ),
        (
            "8. Rectangular envelope",
            "The parcelizer starts from the packed SKU dimensions, builds a rectangular envelope around them, and then uses that estimated parcel cube as the costing starting point.",
        ),
        (
            "9. Packaging uplift",
            "Packed SKU weight and cube are shown separately from the estimated box + dunnage uplift. That uplift is a review signal only and does not change the current cost basis unless the logic is explicitly approved.",
        ),
        (
            "10. Pricing cube",
            "The pricing cube is a Shippit-normalized version of the parcel cube. It keeps the carrier comparison aligned to the weight or volumetric basis that Shippit actually holds.",
        ),
        (
            "11. Residual vs Shippit",
            "The residual versus Shippit shows the gap between the parcelizer cube and the Shippit chargeable cube. Use it to see whether the parcel logic is systematically high or low.",
        ),
        (
            "12. How to use the benchmark",
            "If the benchmark flag is Y, the parcelizer result is outside the normal estate range and should be reviewed as a candidate for rule, dimension, or packaging changes.",
        ),
        (
            "13. CFC cube imputation",
            "If cfc_cube_imputed = Y, the parcel used an invoice-average cube because the original CFC cube was missing. Treat that row as a proxy and review the underlying SKU master if the missing data rate is material.",
        ),
    ]
    sheet.append(["Step", "Guidance"])
    _style_header(sheet)
    for row in notes:
        sheet.append(list(row))
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A1"].fill = _TITLE_FILL
    sheet.merge_cells("A1:B1")
    sheet["A2"] = f"Invoice {context.invoice_number} for {context.site_label}"
    sheet.merge_cells("A2:B2")
    sheet["A2"].fill = _SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    _autosize(sheet)
    sheet.column_dimensions["A"].width = 28
    sheet.column_dimensions["B"].width = 110


def _write_records_sheet(sheet, records: list[dict[str, Any]], title: str) -> None:
    _style_title(sheet, title)
    sheet.freeze_panes = "A4"
    if not records:
        sheet.append(["No rows found"])
        return

    headers = list(records[0].keys())
    sheet.append(headers)
    _style_header(sheet)

    for record in records:
        sheet.append([_normalize_cell_value(record.get(header)) for header in headers])

    _format_table(sheet, headers, len(records))
    _autosize(sheet)


def _style_title(sheet, title: str) -> None:
    sheet["A1"] = title
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A1"].fill = _TITLE_FILL
    sheet["A1"].alignment = Alignment(horizontal="left")
    sheet.merge_cells("A1:B1")
    sheet["A2"] = (
        "This report is intentionally a review starting point. Use the commentary to challenge the logic and "
        "bring Ops or Transport feedback back into the rules."
    )
    sheet["A2"].fill = _SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells("A2:B2")
    sheet.row_dimensions[2].height = 38


def _style_header(sheet) -> None:
    header_row = 3
    for cell in sheet[header_row]:
        if cell.value is None:
            continue
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _format_table(sheet, headers: list[str], record_count: int) -> None:
    percent_headers = {
        "est_fill_pct_vs_shippit_cube",
        "est_air_ratio_pct_vs_shippit_cube",
        "avg_fill_pct_vs_shippit_cube",
        "median_fill_pct_vs_shippit_cube",
        "p25_fill_pct_vs_shippit_cube",
        "p75_fill_pct_vs_shippit_cube",
        "avg_air_ratio_pct_vs_shippit_cube",
        "median_air_ratio_pct_vs_shippit_cube",
        "p90_air_ratio_pct_vs_shippit_cube",
        "largest_parcel_weight_share_pct",
        "largest_parcel_cube_share_pct",
        "est_largest_parcel_weight_share_pct",
        "est_largest_parcel_cube_share_pct",
        "cheaper_option_exists",
        "parcel_fill_pct_vs_shippit_cube",
        "parcel_air_ratio_pct_vs_shippit_cube",
        "cost_share_pct",
    }
    ratio_headers = {
        "pricing_scale_factor",
    }
    currency_headers = {
        "quoted_cost_inc_gst",
        "actual_est_cost_ex_gst",
        "cheapest_alt_cost_ex_gst",
        "est_saving_ex_gst",
        "actual_est_cost_allocated_ex_gst",
        "parcel_rated_cost_ex_gst",
        "parcel_next_cheapest_cost_ex_gst",
        "parcel_gap_vs_next_cheapest_ex_gst",
        "allied_base_cost_ex_gst",
        "allied_remote_area_surcharge_ex_gst",
        "allied_overweight_surcharge_ex_gst",
        "allied_oversized_item_surcharge_ex_gst",
        "allied_manual_measurement_fee_ex_gst",
        "allied_reverse_logistics_surcharge_ex_gst",
        "allied_additional_services_ex_gst",
        "allied_total_cost_ex_gst",
        "gobundle_plug_cost_per_parcel_ex_gst",
    }
    count_headers = {
        "actual_parcel_count",
        "estimated_parcel_count",
        "parcel_id",
        "parcel_units",
        "parcel_units_total",
        "box_suite_match_count",
        "shippit_parcel_count",
        "est_parcel_count",
        "invoice_count",
        "parcel_count_total",
        "outlier_invoice_count",
        "odd_allocation_invoice_count",
        "flagged_invoice_count",
        "flagged_invoice_occurrences",
        "total_quantity_packed",
        "qty_packed",
        "parcel_id",
    }
    cubic_meter_headers = {
        "total_parcel_cube_m3",
        "shippit_volumetric_cube_m3",
        "est_total_parcel_cube_m3",
        "parcel_cube_m3",
        "parcel_product_cube_cm3",
        "est_parcel_cube_pattern_m3",
        "packed_sku_cube_cm3_per_line",
    }

    for col_idx, header in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        for row_idx in range(4, 4 + record_count):
            cell = sheet[f"{col_letter}{row_idx}"]
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if header in percent_headers and isinstance(cell.value, (int, float)):
                value = float(cell.value)
                if value > 1.0:
                    cell.value = value / 100.0
                cell.number_format = "0.0%"
            elif header in ratio_headers and isinstance(cell.value, (int, float)):
                cell.number_format = "0.0000"
            elif header in currency_headers and isinstance(cell.value, (int, float)):
                cell.number_format = "$#,##0.00"
            elif header in cubic_meter_headers and isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0.000000"
            elif header in count_headers and isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0"
            elif isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0.00"


def _autosize(sheet) -> None:
    widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            width = len(str(cell.value))
            widths[cell.column] = max(widths.get(cell.column, 0), min(width, 80))
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = min(width + 2, 80)


def _plm_not_expected(row: dict[str, Any]) -> bool:
    text_bits = [
        str(row.get("major_group") or ""),
        str(row.get("department_description") or ""),
        str(row.get("class_description") or ""),
        str(row.get("sub_class_description") or ""),
        str(row.get("plm_item_name") or ""),
        str(row.get("plm_item_description") or ""),
    ]
    combined = " ".join(text_bits).upper()
    return any(term in combined for term in APPAREL_TERMS)


def _sku_review_commentary(row: dict[str, Any]) -> str:
    plm_missing = _missing_dimension_triplet(
        row.get("plm_height_cm"),
        row.get("plm_width_cm"),
        row.get("plm_length_cm"),
    )
    cfc_missing = _missing_dimension_triplet(
        row.get("cfc_height_cm"),
        row.get("cfc_width_cm"),
        row.get("cfc_length_cm"),
    )
    if _plm_not_expected(row):
        return "PLM dims are not expected for apparel-like keycodes, so missing PLM data should not be treated as a gap."
    if plm_missing and not cfc_missing:
        return "PLM dims are missing; check the source master if this keycode should carry PLM dimensional data."
    if cfc_missing and not plm_missing:
        return "CFC dims are missing; compare against the source master and confirm the SKU attribute feed."
    if cfc_missing and plm_missing:
        return "Both CFC and PLM dims are missing; the source master needs review."

    cfc_dims = sorted(
        [float(row.get("cfc_height_cm") or 0), float(row.get("cfc_width_cm") or 0), float(row.get("cfc_length_cm") or 0)]
    )
    plm_dims = sorted(
        [float(row.get("plm_height_cm") or 0), float(row.get("plm_width_cm") or 0), float(row.get("plm_length_cm") or 0)]
    )
    cfc_weight = _to_float(row.get("cfc_weight_kg"))
    plm_weight = _to_float(row.get("plm_weight_kg"))
    size_gap = str(row.get("cfc_size_category") or "") != str(row.get("plm_size_category") or "")
    dim_gap = _relative_gap(max(cfc_dims), max(plm_dims)) >= 0.2 or any(
        _relative_gap(cfc, plm) >= 0.2 for cfc, plm in zip(cfc_dims, plm_dims)
    )
    volume_gap = False
    if all(value > 0 for value in cfc_dims) and all(value > 0 for value in plm_dims):
        cfc_volume = cfc_dims[0] * cfc_dims[1] * cfc_dims[2]
        plm_volume = plm_dims[0] * plm_dims[1] * plm_dims[2]
        volume_gap = _relative_gap(cfc_volume, plm_volume) >= 0.2
    weight_gap = False
    if cfc_weight is not None and plm_weight is not None:
        weight_gap = _relative_gap(cfc_weight, plm_weight) >= 0.2
    if size_gap or dim_gap or volume_gap or weight_gap:
        return "CFC and PLM disagree enough to review the source dimensions and sizing logic."
    return "CFC and PLM are broadly aligned."


def _summary_commentary(context: InvoiceContext, sku_rows: list[dict[str, Any]], validation_row: dict[str, Any]) -> str:
    pack_scope = str(validation_row.get("est_parcelization_scope") or "")
    pack_group = str(validation_row.get("est_pack_process_group") or "")
    parcel_match = str(validation_row.get("est_parcel_count_match") or "")
    odd_allocation = str(validation_row.get("est_flag_odd_allocation") or "")
    air_ratio = _to_float(validation_row.get("est_rectangularized_air_ratio_pct_vs_shippit_cube"))
    if air_ratio is None:
        air_ratio = _to_float(validation_row.get("est_air_ratio_pct_vs_shippit_cube"))
    chargeable_gap = _to_float(validation_row.get("benchmark_chargeable_gap_kg"))
    air_threshold = context.benchmark_thresholds.get("air_ratio_threshold_pct") or 70.0
    chargeable_gap_threshold = context.benchmark_thresholds.get("chargeable_gap_threshold_kg") or 18.0
    if pack_scope == "SINGLE_PARCEL" and parcel_match == "Y":
        if air_ratio is not None and abs(air_ratio) >= air_threshold:
            return (
                f"Single-parcel order in {pack_group.lower()} flow. The parcel matches the Shippit count, but its cube residual is unusually large, "
                "so the carton fill and source dimensions should be checked."
            )
        if air_ratio is not None and air_ratio <= 20:
            return (
                f"Single-parcel order in {pack_group.lower()} flow. The parcel count matches Shippit and the fill is tight, so the split "
                "looks operationally sensible."
            )
        return f"Single-parcel order in {pack_group.lower()} flow. Use the parcel validation and SKU review to confirm dimensions."
    if odd_allocation == "Y":
        return (
            "Multi-parcel order with an odd allocation pattern. Check whether one parcel is carrying too much weight or cube "
            "before relying on the cost comparison."
        )
    if chargeable_gap is not None and chargeable_gap >= chargeable_gap_threshold:
            return "Multi-parcel order with a meaningful Shippit chargeable-weight gap. Review the parcelizer split and the source dimensions first."
    return "Multi-parcel order with no obvious allocation issue. Use the parcel detail and SKU review to look for dimension outliers."


def _cost_basis_commentary(
    service: str,
    stage3_row: dict[str, Any],
    *,
    role: str,
) -> str:
    service = str(service or "")
    role = "actual" if role == "actual" else "alternative"
    if service == "Couriers Please":
        cap_applied = str(stage3_row.get("cp_pricing_cap_applied") or "")
        cap_text = " The 10-parcel review cap was applied." if cap_applied == "Y" else ""
        if role == "actual":
            return (
                "Couriers Please bills at consignment level on rounded chargeable weight. "
                "Per-parcel figures are review shares only." + cap_text
            )
        return (
            "Couriers Please as the next-cheapest alternative is also billed at consignment level on rounded chargeable weight. "
            "Per-parcel figures are review shares only." + cap_text
        )
    if service == "Australia Post":
        return (
            "Australia Post is priced at parcel level using first/subsequent parcel logic where relevant."
            if role == "actual"
            else "Australia Post alternative pricing uses the same parcel-level first/subsequent logic."
        )
    if service == "Allied Express":
        return (
            "Allied cost includes the base lane price plus any remote area and additional service surcharges."
            if role == "actual"
            else "Allied alternative pricing includes the same lane and surcharge overlays."
        )
    if service == "GoEXPRESS":
        return (
            "GoEXPRESS cost is banded by zone and chargeable weight."
            if role == "actual"
            else "GoEXPRESS alternative pricing is banded by zone and chargeable weight."
        )
    if service == "GoBUNDLE":
        return (
            "GoBUNDLE cost uses the configured per-parcel plug until a live ratecard is available."
            if role == "actual"
            else "GoBUNDLE alternative pricing uses the configured per-parcel plug until a live ratecard is available."
        )
    return "Carrier costing is shown using the best available rule for the selected service."


def _size_category_totals(sku_rows: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"Toteable": 0, "Non-Toteable": 0, "Bulky": 0, "No Dims": 0, "Other / Unknown": 0}
    for row in sku_rows:
        qty = _to_int(row.get("qty_packed")) or 0
        category = str(row.get("cfc_size_category") or "").strip()
        if category in totals:
            totals[category] += qty
        elif category == "No dims":
            totals["No Dims"] += qty
        else:
            totals["Other / Unknown"] += qty
    return totals


def _packed_sku_quality_stats(sku_rows: list[dict[str, Any]]) -> dict[str, float]:
    row_count = len(sku_rows)
    unit_count = sum((_to_int(row.get("qty_packed")) or 0) for row in sku_rows)
    missing_cfc_weight_rows = 0
    missing_cfc_weight_units = 0
    missing_cfc_dims_rows = 0
    missing_cfc_dims_units = 0
    missing_both_dims_rows = 0
    missing_both_dims_units = 0

    for row in sku_rows:
        qty = _to_int(row.get("qty_packed")) or 0
        cfc_weight_missing = _to_float(row.get("cfc_weight_kg")) is None
        cfc_dims_missing = _missing_dimension_triplet(row.get("cfc_height_cm"), row.get("cfc_width_cm"), row.get("cfc_length_cm"))
        plm_dims_missing = _missing_dimension_triplet(row.get("plm_height_cm"), row.get("plm_width_cm"), row.get("plm_length_cm"))
        both_dims_missing = cfc_dims_missing and plm_dims_missing

        if cfc_weight_missing:
            missing_cfc_weight_rows += 1
            missing_cfc_weight_units += qty
        if cfc_dims_missing:
            missing_cfc_dims_rows += 1
            missing_cfc_dims_units += qty
        if both_dims_missing:
            missing_both_dims_rows += 1
            missing_both_dims_units += qty

    return {
        "row_count": row_count,
        "unit_count": unit_count,
        "missing_cfc_weight_row_pct": (missing_cfc_weight_rows * 100.0 / row_count) if row_count else 0.0,
        "missing_cfc_weight_unit_pct": (missing_cfc_weight_units * 100.0 / unit_count) if unit_count else 0.0,
        "missing_cfc_dims_row_pct": (missing_cfc_dims_rows * 100.0 / row_count) if row_count else 0.0,
        "missing_cfc_dims_unit_pct": (missing_cfc_dims_units * 100.0 / unit_count) if unit_count else 0.0,
        "missing_both_dims_row_pct": (missing_both_dims_rows * 100.0 / row_count) if row_count else 0.0,
        "missing_both_dims_unit_pct": (missing_both_dims_units * 100.0 / unit_count) if unit_count else 0.0,
    }


def _sum_pattern_cm3(pattern: Any) -> float:
    total = 0.0
    for part in str(pattern or "").split("|"):
        value = _to_float(part.strip())
        if value is not None:
            total += value
    return total


def _pattern_m3(pattern_cm3: Any) -> str:
    parts = [part for part in str(pattern_cm3 or "").split("|") if part]
    if not parts:
        return ""
    converted = []
    for part in parts:
        value = _to_float(part)
        converted.append(f"{(value / 1_000_000.0):.6f}" if value is not None else "")
    return "|".join(converted)


def _parcel_fill_pct(parcel_row: dict[str, Any], validation_row: dict[str, Any]) -> float | None:
    shippit_cube = _to_float(validation_row.get("shippit_volumetric_cube_cm3"))
    parcel_cube = _to_float(parcel_row.get("parcel_cube_cm3"))
    if shippit_cube is None or shippit_cube == 0 or parcel_cube is None:
        return None
    return (parcel_cube * 100.0) / shippit_cube


def _parcel_air_pct(parcel_row: dict[str, Any], validation_row: dict[str, Any]) -> float | None:
    fill_pct = _parcel_fill_pct(parcel_row, validation_row)
    return None if fill_pct is None else 100.0 - fill_pct


def _rated_parcel_costs(context: InvoiceContext, parcel_rows: list[dict[str, Any]]) -> list[float | None]:
    stage3 = context.stage3_row
    actual_service = str(stage3.get("actual_service") or "")
    rate_tables = context.rate_tables
    parcel_count = len(parcel_rows)
    total_weight = sum(_to_float(row.get("parcel_weight_kg")) or 0.0 for row in parcel_rows)
    cp_invoice_cost, _, _, _, _ = _couriers_please_invoice_cost(context, parcel_count)
    allied_invoice_cost = stage3_estimator._allied_cost(
        str(stage3.get("allied_zone") or ""), total_weight, rate_tables.get("allied_rates", {})
    )
    costs: list[float | None] = []
    for index, row in enumerate(parcel_rows):
        weight = _to_float(row.get("parcel_weight_kg")) or 0.0
        cost: float | None = None
        if actual_service == "Couriers Please":
            if cp_invoice_cost is not None and total_weight > 0:
                cost = cp_invoice_cost * (weight / total_weight)
        elif actual_service == "Allied Express":
            cost = _allocate_consignment_cost(allied_invoice_cost, weight, total_weight, parcel_count)
        elif actual_service == "GoEXPRESS":
            zone = str(stage3.get("goexpress_zone") or "")
            warehouse = str(rate_tables.get("goexpress_warehouse") or "")
            cost = stage3_estimator._goexpress_band_cost(
                warehouse,
                zone,
                weight,
                rate_tables.get("goexpress_bands", {}),
            )
        elif actual_service == "GoBUNDLE":
            plug = rate_tables.get("gobundle_plug", {}) or {}
            cost = _to_float(plug.get("cost_per_parcel_ex_gst"))
        elif actual_service == "eParcel":
            ap_zone = str(stage3.get("ap_zone") or "")
            pricing_zone = stage3_estimator._ap_pricing_zone(
                ap_zone,
                stage3.get("dest_state"),
                rate_tables.get("ap_area_to_zone", {}) or {},
                context.site_key,
            )
            if pricing_zone:
                if weight <= 5.0:
                    cost = stage3_estimator._weight_band_cost(pricing_zone, weight, rate_tables.get("ap_weight_bands", {}))
                else:
                    lane = (rate_tables.get("ap_lane_rates", {}) or {}).get(pricing_zone)
                    if lane is not None:
                        step = "first" if index == 0 else "subsequent"
                        cost = lane[step] + lane["per_kg"] * weight
                        cost = max(lane["minimum"], cost)
        costs.append(cost)
    return costs


def _allocate_consignment_cost(
    consignment_cost: float | None,
    parcel_weight_kg: float,
    total_weight_kg: float,
    parcel_count: int,
) -> float | None:
    if consignment_cost is None:
        return None
    if total_weight_kg > 0:
        return consignment_cost * parcel_weight_kg / total_weight_kg
    return consignment_cost / parcel_count if parcel_count else None


def _alternative_parcel_costs(context: InvoiceContext, parcel_rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    stage3 = context.stage3_row
    actual_service = str(stage3.get("actual_service") or "")
    rate_tables = context.rate_tables
    dest_postcode = str(stage3.get("dest_postcode") or "").strip()
    parcel_count = len(parcel_rows)
    total_weight = sum(_to_float(row.get("parcel_weight_kg")) or 0.0 for row in parcel_rows)
    cp_invoice_cost, _, _, _, _ = _couriers_please_invoice_cost(context, parcel_count)
    allied_invoice_cost = stage3_estimator._allied_cost(
        str(stage3.get("allied_zone") or ""), total_weight, rate_tables.get("allied_rates", {})
    )
    alternatives: list[dict[str, Any] | None] = []
    for index, row in enumerate(parcel_rows):
        weight = _to_float(row.get("parcel_weight_kg")) or 0.0
        candidates: list[dict[str, Any]] = []

        def add_candidate(service: str, carrier: str, cost: float | None) -> None:
            if cost is None:
                return
            if service == actual_service:
                return
            candidates.append({"service": service, "carrier": carrier, "cost": cost})

        add_candidate(
            "Allied Express",
            "Allied Express",
            _allocate_consignment_cost(allied_invoice_cost, weight, total_weight, parcel_count),
        )
        add_candidate(
            "Couriers Please",
            "Couriers Please",
            (cp_invoice_cost * (weight / total_weight)) if cp_invoice_cost is not None and total_weight > 0 else None,
        )
        add_candidate(
            "GoEXPRESS",
            "GoPeople",
            stage3_estimator._goexpress_band_cost(
                str(rate_tables.get("goexpress_warehouse") or ""),
                str(stage3.get("goexpress_zone") or ""),
                weight,
                rate_tables.get("goexpress_bands", {}),
            ),
        )
        plug = rate_tables.get("gobundle_plug", {}) or {}
        if actual_service == "GoBUNDLE" or dest_postcode in context.gobundle_coverage_postcodes:
            add_candidate("GoBUNDLE", "GoBUNDLE", _to_float(plug.get("cost_per_parcel_ex_gst")))

        ap_zone = str(stage3.get("ap_zone") or "")
        pricing_zone = stage3_estimator._ap_pricing_zone(
            ap_zone,
            stage3.get("dest_state"),
            rate_tables.get("ap_area_to_zone", {}) or {},
            context.site_key,
        )
        if pricing_zone:
            if weight <= 5.0:
                ap_cost = stage3_estimator._weight_band_cost(pricing_zone, weight, rate_tables.get("ap_weight_bands", {}))
            else:
                lane = (rate_tables.get("ap_lane_rates", {}) or {}).get(pricing_zone)
                ap_cost = None
                if lane is not None:
                    step = "first" if index == 0 else "subsequent"
                    ap_cost = lane[step] + lane["per_kg"] * weight
                    ap_cost = max(lane["minimum"], ap_cost)
            add_candidate("eParcel", "Australia Post", ap_cost)

        alternatives.append(min(candidates, key=lambda item: item["cost"]) if candidates else None)

    return alternatives


def _couriers_please_invoice_cost(
    context: InvoiceContext, parcel_count: int
) -> tuple[float | None, float | None, str, str, str]:
    stage3 = context.stage3_row
    rate_tables = context.rate_tables
    zone = str(stage3.get("cp_zone") or "")
    chargeable_kg = _to_float(stage3.get("cp_pricing_chargeable_weight_kg"))
    if chargeable_kg is None:
        chargeable_kg = _to_float(stage3.get("shippit_chargeable_weight_kg") or stage3.get("actual_order_chargeable_kg"))
    if chargeable_kg is None:
        return None, None, "", "", ""
    return stage3_estimator._couriers_please_invoice_cost(
        zone,
        chargeable_kg,
        parcel_count,
        rate_tables.get("cp_rates", {}),
    )


def _load_gobundle_coverage_postcodes() -> set[str]:
    coverage: set[str] = set()
    for config in SITE_CONFIGS.values():
        stage3_path = Path(config["stage3"])
        if not stage3_path.exists():
            continue
        with stage3_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if str(row.get("actual_carrier") or "").strip() == "GoBUNDLE":
                    postcode = str(row.get("dest_postcode") or "").strip()
                    if postcode:
                        coverage.add(postcode)
    return coverage


@lru_cache(maxsize=1)
def _load_benchmark_thresholds() -> dict[str, float]:
    rows: list[dict[str, Any]] = []
    for config in SITE_CONFIGS.values():
        validation_path = Path(config["stage2_validation"])
        if not validation_path.exists():
            continue
        with validation_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows.extend(list(reader))

    chargeable_gap_kg: list[float] = []
    air_ratio_pcts: list[float] = []
    for row in rows:
        chargeable_gap = _to_float(row.get("est_chargeable_gap_abs_kg"))
        if chargeable_gap is None:
            chargeable_gap = _to_float(row.get("est_weight_gap_vs_shippit_chargeable_kg"))
            chargeable_gap = abs(chargeable_gap) if chargeable_gap is not None else None
        if chargeable_gap is not None:
            chargeable_gap_kg.append(chargeable_gap)

        air_ratio = _to_float(row.get("est_rectangularized_air_ratio_abs_pct_vs_shippit_cube"))
        if air_ratio is None:
            raw_air_ratio = _to_float(row.get("est_rectangularized_air_ratio_pct_vs_shippit_cube"))
            if raw_air_ratio is None:
                raw_air_ratio = _to_float(row.get("est_air_ratio_pct_vs_shippit_cube"))
            air_ratio = abs(raw_air_ratio) if raw_air_ratio is not None else None
        if air_ratio is not None:
            air_ratio_pcts.append(air_ratio)

    return {
        "chargeable_gap_threshold_kg": _percentile(chargeable_gap_kg, 95.0),
        "air_ratio_threshold_pct": _percentile(air_ratio_pcts, 95.0),
    }


def _apply_benchmark_metrics(validation_row: dict[str, Any], thresholds: dict[str, float]) -> dict[str, Any]:
    row = dict(validation_row)
    chargeable_gap = _to_float(row.get("est_chargeable_gap_abs_kg"))
    if chargeable_gap is None:
        chargeable_gap = _to_float(row.get("est_weight_gap_vs_shippit_chargeable_kg"))
        chargeable_gap = abs(chargeable_gap) if chargeable_gap is not None else None
    air_ratio = _to_float(row.get("est_rectangularized_air_ratio_pct_vs_shippit_cube"))
    if air_ratio is None:
        air_ratio = _to_float(row.get("est_air_ratio_pct_vs_shippit_cube"))

    chargeable_gap_kg = chargeable_gap
    chargeable_threshold = thresholds.get("chargeable_gap_threshold_kg")
    air_threshold = thresholds.get("air_ratio_threshold_pct")
    chargeable_flag = "Y" if chargeable_gap_kg is not None and chargeable_threshold is not None and chargeable_gap_kg >= chargeable_threshold else "N"
    air_flag = "Y" if air_ratio is not None and air_threshold is not None and abs(air_ratio) >= air_threshold else "N"
    review_flag = "Y" if chargeable_flag == "Y" or air_flag == "Y" else "N"

    row.update(
        {
            "benchmark_chargeable_gap_kg": chargeable_gap_kg,
            "benchmark_chargeable_gap_threshold_kg": chargeable_threshold,
            "benchmark_chargeable_gap_flag": chargeable_flag,
            "benchmark_air_ratio_pct": air_ratio,
            "benchmark_air_ratio_threshold_pct": air_threshold,
            "benchmark_air_ratio_flag": air_flag,
            "benchmark_review_flag": review_flag,
        }
    )
    return row


def _missing_dimension_triplet(*values: Any) -> bool:
    return all(_to_float(value) is None for value in values)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    lower = ordered[low]
    upper = ordered[high]
    weight = rank - low
    return lower + (upper - lower) * weight


def _relative_gap(left: float, right: float) -> float:
    if left == 0 and right == 0:
        return 0.0
    baseline = max(abs(left), abs(right), 1e-9)
    return abs(left - right) / baseline


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_cell_value(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


_TITLE_FILL = PatternFill("solid", fgColor="1F4E78")
_SUBTITLE_FILL = PatternFill("solid", fgColor="D9E2F3")
_HEADER_FILL = PatternFill("solid", fgColor="D9E2F3")


if __name__ == "__main__":
    main()
