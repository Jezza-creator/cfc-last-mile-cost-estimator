from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook

from src.db import get_session


STORE_NAME = "1194 Moorebank CFC NSW AU"
DAYS_BACK = 6
OUTPUT_PATH = Path("outputs/cfc_plm_variance_report.csv")
KEYCODE_REVIEW_OUTPUT_PATH = Path("outputs/cfc_keycode_review.csv")
ROLLUP_OUTPUT_PATH = Path("outputs/cfc_plm_variance_rollup.csv")
VARIANCE_BUCKET_OUTPUT_PATH = Path("outputs/cfc_plm_variance_buckets.csv")
MAJOR_GROUP_TOP_SKUS_OUTPUT_PATH = Path("outputs/cfc_plm_variance_major_group_top_skus.csv")
CARRIER_FLOW_OUTPUT_PATH = Path("outputs/cfc_plm_variance_carrier_flow_rollup.csv")
WORKBOOK_OUTPUT_PATH = Path("outputs/PLM_CFC_Dimension_Variance_Report.xlsx")
STAGE3_OUTPUT_PATH = Path("outputs/stage3_moorebank_cost_estimates_full.csv")
TOP_LIMIT = 100
TOP_SKUS_PER_GROUP = 5


@dataclass(frozen=True)
class SkuVarianceRow:
    sku_id: str
    plm_item_name: str
    plm_item_description: str
    major_group: str
    rbu_code: str
    rbu_description: str
    department_code: str
    department_description: str
    division_code: str
    division_description: str
    class_code: str
    class_description: str
    sub_class_code: str
    sub_class_description: str
    packed_rows: int
    order_count: int
    total_qty_packed: int
    cfc_height_cm: float | None
    cfc_width_cm: float | None
    cfc_length_cm: float | None
    cfc_weight_kg: float | None
    plm_height_cm: float | None
    plm_width_cm: float | None
    plm_length_cm: float | None
    plm_instore_gross_weight_kg: float | None
    cfc_size_category: str
    plm_size_category: str


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(_build_query()).collect()
        variances = [_row_from_snowflake(row) for row in rows]
        ranked = sorted(variances, key=_ops_impact_sort_key, reverse=True)
        _write_output(OUTPUT_PATH, ranked)
        _write_keycode_review_output(KEYCODE_REVIEW_OUTPUT_PATH, ranked)
        _write_rollup_output(ROLLUP_OUTPUT_PATH, ranked)
        _write_variance_bucket_output(VARIANCE_BUCKET_OUTPUT_PATH, ranked)
        _write_major_group_top_skus_output(MAJOR_GROUP_TOP_SKUS_OUTPUT_PATH, ranked)
        _write_carrier_flow_output(CARRIER_FLOW_OUTPUT_PATH, STAGE3_OUTPUT_PATH)
        _write_workbook(WORKBOOK_OUTPUT_PATH, ranked, STAGE3_OUTPUT_PATH)
        _print_summary(ranked)
        _print_rollup_summary(ranked)
        _print_variance_bucket_summary(ranked)
        _print_group_top_skus_summary(ranked)
        _print_carrier_flow_summary(STAGE3_OUTPUT_PATH)
        print(f"WORKBOOK_EXPORTED={WORKBOOK_OUTPUT_PATH}")
        _print_top_rows(ranked, TOP_LIMIT)
    finally:
        session.close()


def _build_query() -> str:
    return f"""
    WITH shippit AS (
        SELECT DISTINCT
            ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number
        FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
        WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
          AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                BETWEEN DATEADD(day, -{DAYS_BACK}, CURRENT_DATE()) AND CURRENT_DATE()
          AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
    ),
    packed AS (
        SELECT
            REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
            p.SKU_ID,
            p.QUANTITY_PACKED
        FROM KSFDA.CATCH_CFC.PACKED p
        JOIN shippit s
          ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
    ),
    plm_weight_latest AS (
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
    plm_latest AS (
        SELECT DISTINCT
            w.PRODUCT_SOURCE_IDENTIFIER AS KEYCODE,
            w.ITEM_NAME,
            w.ITEM_DESCRIPTION,
            w.INSTORE_HEIGHT,
            w.INSTORE_WIDTH,
            w.INSTORE_LENGTH,
            w.INSTORE_GROSS_WEIGHT
        FROM plm_weight_latest w
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
    ),
    sku_rollup AS (
        SELECT
            p.SKU_ID,
            COUNT(*) AS packed_rows,
            COUNT(DISTINCT p.invoice_number) AS order_count,
            SUM(p.QUANTITY_PACKED) AS total_qty_packed
        FROM packed p
        GROUP BY 1
    )
    SELECT
        r.SKU_ID,
        pml.ITEM_NAME AS plm_item_name,
        pml.ITEM_DESCRIPTION AS plm_item_description,
        COALESCE(h.MAJOR_GROUP, '') AS major_group,
        COALESCE(h.RBU_SOURCE_IDENTIFIER, '') AS rbu_code,
        COALESCE(h.RBU_DESCRIPTION, '') AS rbu_description,
        COALESCE(h.DEPARTMENT_SOURCE_IDENTIFIER, '') AS department_code,
        COALESCE(h.DEPARTMENT_DESCRIPTION, '') AS department_description,
        COALESCE(h.DIVISION_SOURCE_IDENTIFIER, '') AS division_code,
        COALESCE(h.DIVISION_DESCRIPTION, '') AS division_description,
        COALESCE(h.CLASS_SOURCE_IDENTIFIER, '') AS class_code,
        COALESCE(h.CLASS_DESCRIPTION, '') AS class_description,
        COALESCE(h.SUB_CLASS_SOURCE_IDENTIFIER, '') AS sub_class_code,
        COALESCE(h.SUB_CLASS_DESCRIPTION, '') AS sub_class_description,
        r.packed_rows,
        r.order_count,
        r.total_qty_packed,
        c.HEIGHT AS cfc_height_cm,
        c.WIDTH AS cfc_width_cm,
        c.LENGTH AS cfc_length_cm,
        c.WEIGHT AS cfc_weight_kg,
        pml.INSTORE_HEIGHT AS plm_height_cm,
        pml.INSTORE_WIDTH AS plm_width_cm,
        pml.INSTORE_LENGTH AS plm_length_cm,
        pml.INSTORE_GROSS_WEIGHT AS plm_instore_gross_weight_kg,
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
    FROM sku_rollup r
    LEFT JOIN cfc_latest c
      ON c.SKU_ID = r.SKU_ID
    LEFT JOIN plm_latest pml
      ON pml.KEYCODE = r.SKU_ID
    LEFT JOIN hierarchy_latest h
      ON h.PRODUCT_SOURCE_IDENTIFIER = r.SKU_ID
    ORDER BY r.packed_rows DESC, r.total_qty_packed DESC, r.SKU_ID
    """


def _row_from_snowflake(row: object) -> SkuVarianceRow:
    record = row.asDict()
    return SkuVarianceRow(
        sku_id=str(record["SKU_ID"] or ""),
        plm_item_name=str(record["PLM_ITEM_NAME"] or ""),
        plm_item_description=str(record["PLM_ITEM_DESCRIPTION"] or ""),
        major_group=str(record["MAJOR_GROUP"] or ""),
        rbu_code=str(record["RBU_CODE"] or ""),
        rbu_description=str(record["RBU_DESCRIPTION"] or ""),
        department_code=str(record["DEPARTMENT_CODE"] or ""),
        department_description=str(record["DEPARTMENT_DESCRIPTION"] or ""),
        division_code=str(record["DIVISION_CODE"] or ""),
        division_description=str(record["DIVISION_DESCRIPTION"] or ""),
        class_code=str(record["CLASS_CODE"] or ""),
        class_description=str(record["CLASS_DESCRIPTION"] or ""),
        sub_class_code=str(record["SUB_CLASS_CODE"] or ""),
        sub_class_description=str(record["SUB_CLASS_DESCRIPTION"] or ""),
        packed_rows=int(record["PACKED_ROWS"] or 0),
        order_count=int(record["ORDER_COUNT"] or 0),
        total_qty_packed=int(record["TOTAL_QTY_PACKED"] or 0),
        cfc_height_cm=_to_float(record["CFC_HEIGHT_CM"]),
        cfc_width_cm=_to_float(record["CFC_WIDTH_CM"]),
        cfc_length_cm=_to_float(record["CFC_LENGTH_CM"]),
        cfc_weight_kg=_to_float(record["CFC_WEIGHT_KG"]),
        plm_height_cm=_to_float(record["PLM_HEIGHT_CM"]),
        plm_width_cm=_to_float(record["PLM_WIDTH_CM"]),
        plm_length_cm=_to_float(record["PLM_LENGTH_CM"]),
        plm_instore_gross_weight_kg=_to_float(record["PLM_INSTORE_GROSS_WEIGHT_KG"]),
        cfc_size_category=str(record["CFC_SIZE_CATEGORY"] or "Unknown"),
        plm_size_category=str(record["PLM_SIZE_CATEGORY"] or "Unknown"),
    )


def _ops_impact_sort_key(row: SkuVarianceRow) -> tuple[float, int, int, str]:
    return (
        _ops_impact_score(row),
        row.total_qty_packed,
        row.packed_rows,
        row.sku_id,
    )


def _ops_impact_score(row: SkuVarianceRow) -> float:
    cfc_chargeable = _chargeable_kg(
        row.cfc_height_cm,
        row.cfc_width_cm,
        row.cfc_length_cm,
        row.cfc_weight_kg,
        None,
        cubage_is_cm3=True,
    )
    plm_chargeable = _chargeable_kg(
        row.plm_height_cm,
        row.plm_width_cm,
        row.plm_length_cm,
        row.plm_instore_gross_weight_kg,
        None,
        cubage_is_cm3=True,
    )

    delta = abs((cfc_chargeable or 0.0) - (plm_chargeable or 0.0))
    exposure = max(row.total_qty_packed, 1) * (
        1.0
        + min(row.order_count, 25) / 10.0
        + min(row.packed_rows, 250) / 100.0
    )
    score = delta * exposure
    if row.plm_size_category == "No dims" and row.cfc_size_category != "No dims":
        score += 75.0 * exposure
    if row.plm_instore_gross_weight_kg is None and row.cfc_weight_kg is not None:
        score += 15.0 * exposure
    if row.cfc_size_category != row.plm_size_category:
        score += 20.0 * exposure
    return score


def _sorted_dimensions(height_cm: float | None, width_cm: float | None, length_cm: float | None) -> tuple[float | None, float | None, float | None]:
    dims = [value for value in (height_cm, width_cm, length_cm) if value is not None]
    if len(dims) != 3:
        return None, None, None
    shortest, median, longest = sorted(dims)
    return shortest, median, longest


def _gross_cube_cm3(height_cm: float | None, width_cm: float | None, length_cm: float | None) -> float | None:
    if height_cm is None or width_cm is None or length_cm is None:
        return None
    return height_cm * width_cm * length_cm


def _volumetric_weight_kg(height_cm: float | None, width_cm: float | None, length_cm: float | None) -> float | None:
    cube_cm3 = _gross_cube_cm3(height_cm, width_cm, length_cm)
    if cube_cm3 is None:
        return None
    return cube_cm3 / 6000.0


def _variance_pct(base: float | None, compare: float | None) -> float | None:
    if base is None or compare is None or base == 0:
        return None
    return ((compare - base) / base) * 100.0


def _variance_bucket(gross_cube_pct: float | None, volumetric_pct: float | None) -> str:
    candidate = max(
        abs(gross_cube_pct) if gross_cube_pct is not None else 0.0,
        abs(volumetric_pct) if volumetric_pct is not None else 0.0,
    )
    if candidate < 10:
        return "0-9%"
    if candidate < 25:
        return "10-24%"
    if candidate < 50:
        return "25-49%"
    return "50%+"


def _pct0(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(value))


def _dimension_metrics(height_cm: float | None, width_cm: float | None, length_cm: float | None, weight_kg: float | None) -> dict[str, float | None]:
    shortest_cm, median_cm, longest_cm = _sorted_dimensions(height_cm, width_cm, length_cm)
    gross_cube_cm3 = _gross_cube_cm3(height_cm, width_cm, length_cm)
    volumetric_weight_kg = _volumetric_weight_kg(height_cm, width_cm, length_cm)
    chargeable_weight_kg = _chargeable_kg(height_cm, width_cm, length_cm, weight_kg, None, cubage_is_cm3=True)
    return {
        "shortest_cm": shortest_cm,
        "median_cm": median_cm,
        "longest_cm": longest_cm,
        "gross_cube_cm3": gross_cube_cm3,
        "volumetric_weight_kg": volumetric_weight_kg,
        "chargeable_weight_kg": chargeable_weight_kg,
    }


def _chargeable_kg(
    height_cm: float | None,
    width_cm: float | None,
    length_cm: float | None,
    weight_kg: float | None,
    cubage: float | None,
    *,
    cubage_is_cm3: bool,
) -> float | None:
    volumetric_kg: float | None = None
    if height_cm is not None and width_cm is not None and length_cm is not None:
        volumetric_kg = (height_cm * width_cm * length_cm) / 6000.0
    elif cubage is not None:
        volumetric_kg = cubage / 6000.0 if cubage_is_cm3 else (cubage * 1_000_000.0) / 6000.0

    if weight_kg is None and volumetric_kg is None:
        return None
    if weight_kg is None:
        return volumetric_kg
    if volumetric_kg is None:
        return weight_kg
    return max(weight_kg, volumetric_kg)


def _write_output(path: Path, rows: list[SkuVarianceRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "sku_id",
                "major_group",
                "rbu_code",
                "rbu_description",
                "department_code",
                "department_description",
                "division_code",
                "division_description",
                "class_code",
                "class_description",
                "sub_class_code",
                "sub_class_description",
                "packed_rows",
                "order_count",
                "total_qty_packed",
                "cfc_shortest_cm",
                "cfc_median_cm",
                "cfc_longest_cm",
                "cfc_weight_kg",
                "cfc_gross_cube_cm3",
                "cfc_volumetric_weight_kg",
                "plm_item_name",
                "plm_item_description",
                "plm_shortest_cm",
                "plm_median_cm",
                "plm_longest_cm",
                "plm_instore_gross_weight_kg",
                "plm_gross_cube_cm3",
                "plm_volumetric_weight_kg",
                "cfc_size_category",
                "plm_size_category",
                "cfc_chargeable_kg",
                "plm_chargeable_kg",
                "gross_cube_variance_pct",
                "volumetric_weight_variance_pct",
                "weight_variance_pct",
                "variance_bucket",
                "review_reason",
                "ops_impact_score",
            ]
        )
        for row in rows:
            cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
            plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
            cfc_chargeable = cfc_metrics["chargeable_weight_kg"]
            plm_chargeable = plm_metrics["chargeable_weight_kg"]
            gross_cube_variance_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
            volumetric_weight_variance_pct = _variance_pct(
                cfc_metrics["volumetric_weight_kg"],
                plm_metrics["volumetric_weight_kg"],
            )
            weight_variance_pct = _variance_pct(row.cfc_weight_kg, row.plm_instore_gross_weight_kg)
            writer.writerow(
                [
                    row.sku_id,
                    row.major_group,
                    row.rbu_code,
                    row.rbu_description,
                    row.department_code,
                    row.department_description,
                    row.division_code,
                    row.division_description,
                    row.class_code,
                    row.class_description,
                    row.sub_class_code,
                    row.sub_class_description,
                    row.packed_rows,
                    row.order_count,
                    row.total_qty_packed,
                    _fmt(cfc_metrics["shortest_cm"]),
                    _fmt(cfc_metrics["median_cm"]),
                    _fmt(cfc_metrics["longest_cm"]),
                    _fmt(row.cfc_weight_kg),
                    _fmt(cfc_metrics["gross_cube_cm3"]),
                    _fmt(cfc_metrics["volumetric_weight_kg"]),
                    row.plm_item_name,
                    row.plm_item_description,
                    _fmt(plm_metrics["shortest_cm"]),
                    _fmt(plm_metrics["median_cm"]),
                    _fmt(plm_metrics["longest_cm"]),
                    _fmt(row.plm_instore_gross_weight_kg),
                    _fmt(plm_metrics["gross_cube_cm3"]),
                    _fmt(plm_metrics["volumetric_weight_kg"]),
                    row.cfc_size_category,
                    row.plm_size_category,
                    _fmt(cfc_chargeable),
                    _fmt(plm_chargeable),
                    _pct0(gross_cube_variance_pct),
                    _pct0(volumetric_weight_variance_pct),
                    _pct0(weight_variance_pct),
                    _variance_bucket(gross_cube_variance_pct, volumetric_weight_variance_pct),
                    _review_reason(row),
                    _fmt(_ops_impact_score(row)),
                ]
            )


def _write_keycode_review_output(path: Path, rows: list[SkuVarianceRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    review_rows = [row for row in rows if _review_reason(row) or _ops_impact_score(row) >= 100]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "sku_id",
                "major_group",
                "rbu_description",
                "department_description",
                "packed_rows",
                "order_count",
                "total_qty_packed",
                "cfc_size_category",
                "plm_size_category",
                "gross_cube_variance_pct",
                "volumetric_weight_variance_pct",
                "weight_variance_pct",
                "variance_bucket",
                "review_reason",
                "ops_impact_score",
                "review_commentary",
            ]
        )
        for row in sorted(review_rows, key=_ops_impact_sort_key, reverse=True):
            cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
            plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
            gross_cube_variance_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
            volumetric_weight_variance_pct = _variance_pct(
                cfc_metrics["volumetric_weight_kg"],
                plm_metrics["volumetric_weight_kg"],
            )
            weight_variance_pct = _variance_pct(row.cfc_weight_kg, row.plm_instore_gross_weight_kg)
            review_commentary = _plm_review_commentary(
                row,
                gross_cube_variance_pct,
                volumetric_weight_variance_pct,
                weight_variance_pct,
            )
            writer.writerow(
                [
                    row.sku_id,
                    row.major_group,
                    row.rbu_description,
                    row.department_description,
                    row.packed_rows,
                    row.order_count,
                    row.total_qty_packed,
                    row.cfc_size_category,
                    row.plm_size_category,
                    _pct0(gross_cube_variance_pct),
                    _pct0(volumetric_weight_variance_pct),
                    _pct0(weight_variance_pct),
                    _variance_bucket(gross_cube_variance_pct, volumetric_weight_variance_pct),
                    _review_reason(row),
                    _fmt(_ops_impact_score(row)),
                    review_commentary,
                ]
            )


def _write_rollup_output(path: Path, rows: list[SkuVarianceRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, str, str], list[SkuVarianceRow]] = {}
    for row in rows:
        key = (row.major_group, row.rbu_description, row.department_description)
        grouped.setdefault(key, []).append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "major_group",
                "rbu_description",
                "department_description",
                "sku_count",
                "packed_rows",
                "total_qty_packed",
                "avg_ops_impact_score",
                "high_impact_skus",
            ]
        )
        for (major_group, rbu_description, department_description), group_rows in sorted(
            grouped.items(),
            key=lambda item: (
                sum(r.packed_rows for r in item[1]),
                sum(r.total_qty_packed for r in item[1]),
            ),
            reverse=True,
        ):
            writer.writerow(
                [
                    major_group,
                    rbu_description,
                    department_description,
                    len(group_rows),
                    sum(r.packed_rows for r in group_rows),
                    sum(r.total_qty_packed for r in group_rows),
                    _fmt(
                        sum(_ops_impact_score(r) for r in group_rows) / max(len(group_rows), 1)
                    ),
                    sum(1 for r in group_rows if _ops_impact_score(r) >= 100),
                ]
            )


def _write_variance_bucket_output(path: Path, rows: list[SkuVarianceRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buckets: dict[str, list[SkuVarianceRow]] = {}
    for row in rows:
        cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
        plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
        gross_cube_variance_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
        volumetric_weight_variance_pct = _variance_pct(cfc_metrics["volumetric_weight_kg"], plm_metrics["volumetric_weight_kg"])
        bucket = _variance_bucket(gross_cube_variance_pct, volumetric_weight_variance_pct)
        buckets.setdefault(bucket, []).append(row)

    bucket_order = ["0-9%", "10-24%", "25-49%", "50%+"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "variance_bucket",
                "sku_count",
                "packed_rows",
                "order_count",
                "avg_gross_cube_variance_pct",
                "avg_volumetric_weight_variance_pct",
                "avg_ops_impact_score",
                "high_volumetric_skus",
            ]
        )
        for bucket in bucket_order:
            group_rows = buckets.get(bucket, [])
            if not group_rows:
                continue
            gross_pcts: list[float] = []
            volumetric_pcts: list[float] = []
            for row in group_rows:
                cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
                plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
                gross_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
                vol_pct = _variance_pct(cfc_metrics["volumetric_weight_kg"], plm_metrics["volumetric_weight_kg"])
                if gross_pct is not None:
                    gross_pcts.append(gross_pct)
                if vol_pct is not None:
                    volumetric_pcts.append(vol_pct)
            writer.writerow(
                [
                    bucket,
                    len(group_rows),
                    sum(r.packed_rows for r in group_rows),
                    sum(r.order_count for r in group_rows),
                    _pct0(sum(gross_pcts) / max(len(gross_pcts), 1)) if gross_pcts else None,
                    _pct0(sum(volumetric_pcts) / max(len(volumetric_pcts), 1)) if volumetric_pcts else None,
                    _fmt(sum(_ops_impact_score(r) for r in group_rows) / max(len(group_rows), 1)),
                    sum(
                        1
                        for r in group_rows
                        if _dimension_metrics(r.plm_height_cm, r.plm_width_cm, r.plm_length_cm, r.plm_instore_gross_weight_kg)["volumetric_weight_kg"]
                        is not None
                        and _dimension_metrics(r.plm_height_cm, r.plm_width_cm, r.plm_length_cm, r.plm_instore_gross_weight_kg)["volumetric_weight_kg"]
                        > (r.plm_instore_gross_weight_kg or 0.0)
                    ),
                ]
            )


def _write_major_group_top_skus_output(path: Path, rows: list[SkuVarianceRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[SkuVarianceRow]] = {}
    for row in rows:
        grouped.setdefault(row.major_group, []).append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "major_group",
                "sku_id",
                "plm_item_name",
                "rbu_description",
                "department_description",
                "packed_rows",
                "order_count",
                "total_qty_packed",
                "ops_impact_score",
                "cfc_size_category",
                "plm_size_category",
                "review_reason",
            ]
        )
        for major_group, group_rows in sorted(
            grouped.items(),
            key=lambda item: sum(_ops_impact_score(r) for r in item[1]),
            reverse=True,
        ):
            for row in sorted(group_rows, key=_ops_impact_sort_key, reverse=True)[:TOP_SKUS_PER_GROUP]:
                writer.writerow(
                    [
                        major_group,
                        row.sku_id,
                        row.plm_item_name,
                        row.rbu_description,
                        row.department_description,
                        row.packed_rows,
                        row.order_count,
                        row.total_qty_packed,
                        _fmt(_ops_impact_score(row)),
                        row.cfc_size_category,
                        row.plm_size_category,
                        _review_reason(row),
                    ]
                )


def _write_carrier_flow_output(path: Path, stage3_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not stage3_path.exists():
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "actual_carrier",
                    "pack_flow_summary",
                    "order_count",
                    "actual_parcel_count_total",
                    "estimated_parcel_count_total",
                    "avg_actual_parcel_count",
                    "avg_estimated_parcel_count",
                    "avg_actual_est_cost_ex_gst",
                    "avg_cheapest_alt_cost_ex_gst",
                    "avg_est_saving_ex_gst",
                    "cheaper_option_count",
                    "missing_cost_count",
                ]
            )
        return

    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    with stage3_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            key = (str(row.get("actual_carrier") or ""), str(row.get("pack_flow_summary") or ""))
            grouped.setdefault(key, []).append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "actual_carrier",
                "pack_flow_summary",
                "order_count",
                "actual_parcel_count_total",
                "estimated_parcel_count_total",
                "avg_actual_parcel_count",
                "avg_estimated_parcel_count",
                "avg_actual_est_cost_ex_gst",
                "avg_cheapest_alt_cost_ex_gst",
                "avg_est_saving_ex_gst",
                "volumetric_important_order_count",
                "volumetric_important_order_pct",
                "cheaper_option_count",
                "missing_cost_count",
            ]
        )
        for (actual_carrier, pack_flow_summary), group_rows in sorted(
            grouped.items(),
            key=lambda item: (
                len(item[1]),
                sum(_to_float(row.get("est_saving_ex_gst")) or 0.0 for row in item[1]),
            ),
            reverse=True,
            ):
            order_count = len(group_rows)
            actual_parcel_count_total = sum(int(float(row.get("actual_parcel_count") or 0)) for row in group_rows)
            estimated_parcel_count_total = sum(int(float(row.get("estimated_parcel_count") or 0)) for row in group_rows)
            avg_actual_parcel_count = actual_parcel_count_total / max(order_count, 1)
            avg_estimated_parcel_count = estimated_parcel_count_total / max(order_count, 1)
            actual_costs = [_to_float(row.get("actual_est_cost_ex_gst")) for row in group_rows if _to_float(row.get("actual_est_cost_ex_gst")) is not None]
            alt_costs = [_to_float(row.get("cheapest_alt_cost_ex_gst")) for row in group_rows if _to_float(row.get("cheapest_alt_cost_ex_gst")) is not None]
            savings = [_to_float(row.get("est_saving_ex_gst")) for row in group_rows if _to_float(row.get("est_saving_ex_gst")) is not None]
            volumetric_important_order_count = sum(
                1
                for row in group_rows
                if (_to_float(row.get("parcel_volumetric_equivalent_kg")) or 0.0)
                > (_to_float(row.get("raw_parcel_weight_total_kg")) or 0.0)
            )
            cheaper_option_count = sum(1 for row in group_rows if str(row.get("cheaper_option_exists") or "") == "Y")
            missing_cost_count = sum(1 for row in group_rows if str(row.get("actual_est_cost_missing_reason") or ""))
            writer.writerow(
                [
                    actual_carrier,
                    pack_flow_summary,
                    order_count,
                    actual_parcel_count_total,
                    estimated_parcel_count_total,
                    _fmt(avg_actual_parcel_count),
                    _fmt(avg_estimated_parcel_count),
                    _fmt(sum(actual_costs) / max(len(actual_costs), 1) if actual_costs else None),
                    _fmt(sum(alt_costs) / max(len(alt_costs), 1) if alt_costs else None),
                    _fmt(sum(savings) / max(len(savings), 1) if savings else None),
                    volumetric_important_order_count,
                    _pct0((volumetric_important_order_count / max(order_count, 1)) * 100.0),
                    cheaper_option_count,
                    missing_cost_count,
                ]
            )


def _write_workbook(path: Path, rows: list[SkuVarianceRow], stage3_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    _write_csv_sheet(workbook, "Variance", OUTPUT_PATH)
    _write_csv_sheet(workbook, "Keycode Review", KEYCODE_REVIEW_OUTPUT_PATH)
    _write_csv_sheet(workbook, "Variance Buckets", VARIANCE_BUCKET_OUTPUT_PATH)
    _write_csv_sheet(workbook, "Hierarchy Rollup", ROLLUP_OUTPUT_PATH)
    _write_csv_sheet(workbook, "Top Major Group SKUs", MAJOR_GROUP_TOP_SKUS_OUTPUT_PATH)
    _write_csv_sheet(workbook, "Carrier Flow", CARRIER_FLOW_OUTPUT_PATH)
    workbook.save(path)


def _write_csv_sheet(workbook: Workbook, sheet_name: str, csv_path: Path) -> None:
    sheet = workbook.create_sheet(title=sheet_name[:31])
    if not csv_path.exists():
        return
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        if headers:
            sheet.append(headers)
        for row in reader:
            sheet.append([row.get(header) for header in headers])


def _carrier_flow_groups(stage3_path: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    if not stage3_path.exists():
        return grouped
    with stage3_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            key = (str(row.get("actual_carrier") or ""), str(row.get("pack_flow_summary") or ""))
            grouped.setdefault(key, []).append(row)
    return grouped


def _review_reason(row: SkuVarianceRow) -> str:
    reasons: list[str] = []
    if row.plm_size_category == "No dims" and row.cfc_size_category != "No dims":
        reasons.append("PLM_MISSING_DIMS")
    if row.plm_instore_gross_weight_kg is None and row.cfc_weight_kg is not None:
        reasons.append("PLM_MISSING_WEIGHT")
    if row.cfc_size_category != row.plm_size_category:
        reasons.append("SIZE_CATEGORY_DIFF")
    return "|".join(reasons)


def _plm_review_commentary(
    row: SkuVarianceRow,
    gross_cube_variance_pct: float | None,
    volumetric_weight_variance_pct: float | None,
    weight_variance_pct: float | None,
) -> str:
    parts: list[str] = []
    if row.plm_size_category == "No dims" and row.cfc_size_category != "No dims":
        parts.append("PLM is missing dimensions for a SKU that has CFC dimensions")
    if row.plm_instore_gross_weight_kg is None and row.cfc_weight_kg is not None:
        parts.append("PLM is missing weight, so the comparison may understate or distort exposure")
    if row.cfc_size_category != row.plm_size_category:
        parts.append(f"CFC and PLM size categories differ ({row.cfc_size_category} vs {row.plm_size_category})")
    if gross_cube_variance_pct is not None:
        if abs(gross_cube_variance_pct) >= 50:
            parts.append("gross cube variance is very large and should be checked first")
        elif abs(gross_cube_variance_pct) >= 25:
            parts.append("gross cube variance is material")
    if volumetric_weight_variance_pct is not None:
        if abs(volumetric_weight_variance_pct) >= 50:
            parts.append("volumetric weight variance is very large")
        elif abs(volumetric_weight_variance_pct) >= 25:
            parts.append("volumetric weight variance is material")
    if weight_variance_pct is not None and abs(weight_variance_pct) >= 25:
        parts.append("gross weight differs enough to affect chargeable weight")
    if not parts:
        parts.append("review because the item contributes meaningful exposure but the comparison is not obviously clean")
    return "; ".join(parts)


def _print_summary(rows: list[SkuVarianceRow]) -> None:
    compared = len(rows)
    plm_missing_dims = sum(1 for row in rows if row.plm_size_category == "No dims")
    plm_missing_weight = sum(1 for row in rows if row.plm_instore_gross_weight_kg is None)
    category_diff = sum(1 for row in rows if row.cfc_size_category != row.plm_size_category)
    high_impact = sum(1 for row in rows if _ops_impact_score(row) >= 100)
    gross_cube_25_plus = 0
    volumetric_25_plus = 0
    volumetric_important = 0
    for row in rows:
        cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
        plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
        gross_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
        vol_pct = _variance_pct(cfc_metrics["volumetric_weight_kg"], plm_metrics["volumetric_weight_kg"])
        if gross_pct is not None and gross_pct >= 25:
            gross_cube_25_plus += 1
        if vol_pct is not None and vol_pct >= 25:
            volumetric_25_plus += 1
        if (plm_metrics["volumetric_weight_kg"] or 0.0) > (row.plm_instore_gross_weight_kg or 0.0):
            volumetric_important += 1
    print("CFC VS PLM VARIANCE SUMMARY")
    print(
        "Row("
        f"SKU_COUNT={compared}, "
        f"PLM_MISSING_DIMS={plm_missing_dims}, "
        f"PLM_MISSING_WEIGHT={plm_missing_weight}, "
        f"SIZE_CATEGORY_DIFF={category_diff}, "
        f"GROSS_CUBE_25PCT_PLUS={gross_cube_25_plus}, "
        f"VOLUMETRIC_25PCT_PLUS={volumetric_25_plus}, "
        f"VOLUMETRIC_IMPORTANT_SKUS={volumetric_important}, "
        f"HIGH_IMPACT_SKUS={high_impact}"
        ")"
    )
    print(f"CSV_EXPORTED={OUTPUT_PATH}")
    print(f"KEYCODE_REVIEW_EXPORTED={KEYCODE_REVIEW_OUTPUT_PATH}")
    print(f"ROLLUP_EXPORTED={ROLLUP_OUTPUT_PATH}")


def _print_rollup_summary(rows: list[SkuVarianceRow]) -> None:
    grouped: dict[tuple[str, str, str], list[SkuVarianceRow]] = {}
    for row in rows:
        key = (row.major_group, row.rbu_description, row.department_description)
        grouped.setdefault(key, []).append(row)
    top_groups = sorted(
        grouped.items(),
        key=lambda item: sum(_ops_impact_score(r) for r in item[1]),
        reverse=True,
    )[:10]
    print("\nTOP HIERARCHY GROUPS")
    for (major_group, rbu_description, department_description), group_rows in top_groups:
        print(
            "Row("
            f"MAJOR_GROUP={major_group}, "
            f"RBU={rbu_description}, "
            f"DEPARTMENT={department_description}, "
            f"SKU_COUNT={len(group_rows)}, "
            f"PACKED_ROWS={sum(r.packed_rows for r in group_rows)}, "
            f"TOTAL_QTY={sum(r.total_qty_packed for r in group_rows)}, "
            f"AVG_IMPACT_SCORE={_fmt(sum(_ops_impact_score(r) for r in group_rows) / max(len(group_rows), 1))}"
            ")"
            )


def _print_variance_bucket_summary(rows: list[SkuVarianceRow]) -> None:
    buckets: dict[str, list[SkuVarianceRow]] = {}
    for row in rows:
        cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
        plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
        gross_cube_variance_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
        volumetric_weight_variance_pct = _variance_pct(
            cfc_metrics["volumetric_weight_kg"],
            plm_metrics["volumetric_weight_kg"],
        )
        bucket = _variance_bucket(gross_cube_variance_pct, volumetric_weight_variance_pct)
        buckets.setdefault(bucket, []).append(row)

    print("\nVARIANCE BUCKET SUMMARY")
    for bucket in ["0-9%", "10-24%", "25-49%", "50%+"]:
        group_rows = buckets.get(bucket, [])
        if not group_rows:
            continue
        print(
            "Row("
            f"VARIANCE_BUCKET={bucket}, "
            f"SKU_COUNT={len(group_rows)}, "
            f"PACKED_ROWS={sum(r.packed_rows for r in group_rows)}, "
            f"TOTAL_QTY={sum(r.total_qty_packed for r in group_rows)}, "
            f"AVG_IMPACT_SCORE={_fmt(sum(_ops_impact_score(r) for r in group_rows) / max(len(group_rows), 1))}"
            ")"
        )


def _print_group_top_skus_summary(rows: list[SkuVarianceRow]) -> None:
    grouped: dict[str, list[SkuVarianceRow]] = {}
    for row in rows:
        grouped.setdefault(row.major_group, []).append(row)

    top_groups = sorted(
        grouped.items(),
        key=lambda item: sum(_ops_impact_score(r) for r in item[1]),
        reverse=True,
    )[:10]
    print("\nTOP SKU GROUPS BY MAJOR GROUP")
    for major_group, group_rows in top_groups:
        top_skus = sorted(group_rows, key=_ops_impact_sort_key, reverse=True)[:TOP_SKUS_PER_GROUP]
        print(
            "Row("
            f"MAJOR_GROUP={major_group}, "
            f"SKU_COUNT={len(group_rows)}, "
            f"TOP_SKUS={len(top_skus)}, "
            f"GROUP_IMPACT_SCORE={_fmt(sum(_ops_impact_score(r) for r in group_rows))}"
            ")"
        )
        for row in top_skus:
            print(
                "  Row("
                f"SKU_ID={row.sku_id}, "
                f"PLM_ITEM_NAME={row.plm_item_name}, "
                f"OPS_IMPACT_SCORE={_fmt(_ops_impact_score(row))}, "
                f"PACKED_ROWS={row.packed_rows}, "
                f"TOTAL_QTY={row.total_qty_packed}"
                ")"
            )


def _print_carrier_flow_summary(stage3_path: Path) -> None:
    if not stage3_path.exists():
        print("\nCARRIER/FLOW SUMMARY")
        print("No stage 3 file found for carrier/flow rollup.")
        return

    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    with stage3_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            key = (str(row.get("actual_carrier") or ""), str(row.get("pack_flow_summary") or ""))
            grouped.setdefault(key, []).append(row)

    top_groups = sorted(
        grouped.items(),
        key=lambda item: len(item[1]),
        reverse=True,
    )[:10]
    print("\nCARRIER/FLOW SUMMARY")
    for (actual_carrier, pack_flow_summary), group_rows in top_groups:
        order_count = len(group_rows)
        actual_parcel_count_total = sum(int(float(row.get("actual_parcel_count") or 0)) for row in group_rows)
        estimated_parcel_count_total = sum(int(float(row.get("estimated_parcel_count") or 0)) for row in group_rows)
        volumetric_important_order_count = sum(
            1
            for row in group_rows
            if (_to_float(row.get("parcel_volumetric_equivalent_kg")) or 0.0)
            > (_to_float(row.get("raw_parcel_weight_total_kg")) or 0.0)
        )
        cheaper_option_count = sum(1 for row in group_rows if str(row.get("cheaper_option_exists") or "") == "Y")
        missing_cost_count = sum(1 for row in group_rows if str(row.get("actual_est_cost_missing_reason") or ""))
        print(
            "Row("
            f"ACTUAL_CARRIER={actual_carrier}, "
            f"PACK_FLOW={pack_flow_summary}, "
            f"ORDER_COUNT={order_count}, "
            f"ACTUAL_PARCELS={actual_parcel_count_total}, "
            f"ESTIMATED_PARCELS={estimated_parcel_count_total}, "
            f"VOLUMETRIC_IMPORTANT_ORDERS={volumetric_important_order_count}, "
            f"VOLUMETRIC_IMPORTANT_PCT={_pct0((volumetric_important_order_count / max(order_count, 1)) * 100.0)}, "
            f"CHEAPER_OPTION_COUNT={cheaper_option_count}, "
            f"MISSING_COST_COUNT={missing_cost_count}"
            ")"
        )


def _print_top_rows(rows: list[SkuVarianceRow], limit: int) -> None:
    print("\nTOP SKU VARIANCES")
    for row in rows[:limit]:
        cfc_metrics = _dimension_metrics(row.cfc_height_cm, row.cfc_width_cm, row.cfc_length_cm, row.cfc_weight_kg)
        plm_metrics = _dimension_metrics(row.plm_height_cm, row.plm_width_cm, row.plm_length_cm, row.plm_instore_gross_weight_kg)
        gross_cube_variance_pct = _variance_pct(cfc_metrics["gross_cube_cm3"], plm_metrics["gross_cube_cm3"])
        volumetric_weight_variance_pct = _variance_pct(cfc_metrics["volumetric_weight_kg"], plm_metrics["volumetric_weight_kg"])
        print(
            "Row("
            f"SKU_ID={row.sku_id}, "
            f"PLM_ITEM_NAME={row.plm_item_name}, "
            f"PACKED_ROWS={row.packed_rows}, "
            f"ORDER_COUNT={row.order_count}, "
            f"TOTAL_QTY={row.total_qty_packed}, "
            f"CFC_SHORTEST={_fmt(cfc_metrics['shortest_cm'])}, "
            f"CFC_MEDIAN={_fmt(cfc_metrics['median_cm'])}, "
            f"CFC_LONGEST={_fmt(cfc_metrics['longest_cm'])}, "
            f"CFC_GROSS_CUBE={_fmt(cfc_metrics['gross_cube_cm3'])}, "
            f"CFC_VOLUMETRIC_KG={_fmt(cfc_metrics['volumetric_weight_kg'])}, "
            f"PLM_SHORTEST={_fmt(plm_metrics['shortest_cm'])}, "
            f"PLM_MEDIAN={_fmt(plm_metrics['median_cm'])}, "
            f"PLM_LONGEST={_fmt(plm_metrics['longest_cm'])}, "
            f"PLM_GROSS_CUBE={_fmt(plm_metrics['gross_cube_cm3'])}, "
            f"PLM_VOLUMETRIC_KG={_fmt(plm_metrics['volumetric_weight_kg'])}, "
            f"GROSS_CUBE_VAR_PCT={_pct0(gross_cube_variance_pct)}, "
            f"VOLUMETRIC_VAR_PCT={_pct0(volumetric_weight_variance_pct)}, "
            f"VAR_BUCKET={_variance_bucket(gross_cube_variance_pct, volumetric_weight_variance_pct)}, "
            f"OPS_IMPACT_SCORE={_fmt(_ops_impact_score(row))}, "
            f"CFC_CATEGORY={row.cfc_size_category}, "
            f"PLM_CATEGORY={row.plm_size_category}, "
            f"REVIEW={_review_reason(row)}"
            ")"
        )


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
