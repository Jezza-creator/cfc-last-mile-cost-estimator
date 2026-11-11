"""Ingest Australia Post eParcel invoice rows from Snowflake for reconciliation."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from src.db import get_session


SOURCE_TABLE = "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST"
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
CHARGE_LINES_PATH = OUTPUT_DIRECTORY / "australia_post_charge_lines.csv"
CONSIGNMENTS_PATH = OUTPUT_DIRECTORY / "australia_post_consignments.csv"
CONTROL_SUMMARY_PATH = OUTPUT_DIRECTORY / "australia_post_invoice_control_summary.csv"
DEFAULT_LOOKBACK_DAYS = 6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=_iso_date, default=_default_start_date(), help="Inclusive SERVICE_DATE in YYYY-MM-DD format.")
    parser.add_argument("--end-date", type=_iso_date, default=date.today().isoformat(), help="Inclusive SERVICE_DATE in YYYY-MM-DD format.")
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit for a smoke test.")
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise ValueError("--start-date must be on or before --end-date.")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be a positive integer.")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    rows = _fetch_rows(args.start_date, args.end_date, args.limit)
    charge_lines = [_normalise_row(row, index) for index, row in enumerate(rows, start=1)]
    charge_lines = [row for row in charge_lines if row["invoice_no"] or row["consignment_id"] or row["material"]]
    consignments = _aggregate_consignments(charge_lines)
    _write_csv(args.output_directory / CHARGE_LINES_PATH.name, _charge_line_headers(), charge_lines)
    _write_csv(args.output_directory / CONSIGNMENTS_PATH.name, _consignment_headers(), consignments)
    _write_control_summary(args.output_directory / CONTROL_SUMMARY_PATH.name, charge_lines, consignments, args.start_date, args.end_date)
    print(f"Read {len(charge_lines)} Australia Post source rows from {SOURCE_TABLE}.")
    print(f"Wrote {args.output_directory / CHARGE_LINES_PATH.name}")
    print(f"Wrote {args.output_directory / CONSIGNMENTS_PATH.name}")
    print(f"Wrote {args.output_directory / CONTROL_SUMMARY_PATH.name}")


def _fetch_rows(start_date: str, end_date: str, limit: int | None) -> list[dict[str, object]]:
    query = f"""
        SELECT *
        FROM {SOURCE_TABLE}
        WHERE TRY_TO_DATE(SERVICE_DATE, 'YYYYMMDD')
              BETWEEN TO_DATE('{start_date}') AND TO_DATE('{end_date}')
        ORDER BY SERVICE_DATE, INVOICE_NO, ASSIGNMENT_NO, CONSIGNMENT_ID, MATERIAL, ITEM
    """
    if limit is not None:
        query += f"\nLIMIT {limit}"
    session = get_session()
    try:
        return [dict(row.as_dict()) for row in session.sql(query).collect()]
    finally:
        session.close()


def _normalise_row(raw: dict[str, object], source_row_number: int) -> dict[str, str]:
    source = {_normalise_header(key): value for key, value in raw.items()}
    return {
        "carrier": "Australia Post",
        "source_table": SOURCE_TABLE,
        "source_row_number": str(source_row_number),
        "invoice_no": _text(source.get("invoice_no")),
        "billing_date": _date(source.get("billing_date")),
        "service_date": _date(source.get("service_date")),
        "billing_doc": _text(source.get("billing_doc")),
        "assignment_no": _text(source.get("assignment_no")),
        "customer_ref_1": _text(source.get("cust_ref_1")),
        "customer_ref_2": _text(source.get("cust_ref_2")),
        "customer_ref_3": _text(source.get("cust_ref_3")),
        "sales_order": _text(source.get("sales_order")),
        "sales_order_item": _text(source.get("sales_order_item")),
        "consignment_id": _text(source.get("consignment_id")),
        "article_id": _text(source.get("article_id")),
        "material": _text(source.get("material")),
        "description": _text(source.get("description")),
        "qty": _format_number(_number(source.get("qty"))),
        "total_qty": _format_number(_number(source.get("total_qty"))),
        "amount_excl_tax": _format_number(_number(source.get("amount_excl_tax"))),
        "tax_amount": _format_number(_number(source.get("tax_amount"))),
        "amount_incl_tax": _format_number(_number(source.get("amount_incl_tax"))),
        "invoice_total_excl_tax": _format_number(_number(source.get("tot_amount_excl_tax"))),
        "invoice_total_incl_tax": _format_number(_number(source.get("invoice_total"))),
        "tot_amount_excl_tax": _format_number(_number(source.get("tot_amount_excl_tax"))),
        "fuel_surcharge_field_value": _format_number(_number(source.get("fuel_surcharge_pct"))),
        "fuel_surcharge_discount": _format_number(_number(source.get("fuel_surcharge_disc"))),
        "fuel_gst": _format_number(_number(source.get("fuel_gst"))),
        "charge_code": _text(source.get("charge_code")),
        "charge_zone": _text(source.get("charge_zone")),
        "lodgement_zone": _text(source.get("lodgement_zone")),
        "destination_zone": _text(source.get("destination_zone")),
        "lodgement_date": _date(source.get("lodgement_date")),
        "origin_name": _text(source.get("from_name")),
        "origin_city": _text(source.get("from_city")),
        "origin_state": _text(source.get("from_state")),
        "origin_postcode": _text(source.get("from_postal_code")),
        "destination_name": _text(source.get("to_name")),
        "destination_city": _text(source.get("to_city")),
        "destination_state": _text(source.get("to_state")),
        "destination_postcode": _text(source.get("to_postal_code")),
        "actual_weight_kg": _format_number(_number(source.get("actual_weight"))),
        "actual_length_cm": _format_number(_number(source.get("actual_length"))),
        "actual_width_cm": _format_number(_number(source.get("actual_width"))),
        "actual_height_cm": _format_number(_number(source.get("actual_height"))),
        "declared_weight_kg": _format_number(_number(source.get("declared_weight"))),
        "declared_length_cm": _format_number(_number(source.get("declared_length"))),
        "declared_width_cm": _format_number(_number(source.get("declared_width"))),
        "declared_height_cm": _format_number(_number(source.get("declared_height"))),
        "billed_length_m": _format_number(_number(source.get("billed_length"))),
        "billed_width_m": _format_number(_number(source.get("billed_width"))),
        "billed_height_m": _format_number(_number(source.get("billed_height"))),
        "cubic_weight_kg": _format_number(_number(source.get("cubic_weight"))),
        "billed_weight_kg": _format_number(_number(source.get("billed_weight"))),
        "row_hash": _text(source.get("row_hash")),
        "charge_component": _charge_component(source.get("material"), source.get("description"), source.get("charge_code")),
    }


def _aggregate_consignments(charge_lines: list[dict[str, str]]) -> list[dict[str, str]]:
    assignment_tracking: dict[tuple[str, str], set[str]] = defaultdict(set)
    for line in charge_lines:
        if line["assignment_no"] and line["consignment_id"]:
            assignment_tracking[(line["invoice_no"], line["assignment_no"])].add(line["consignment_id"])

    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    line_link_methods: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for line in charge_lines:
        assignment_key = (line["invoice_no"], line["assignment_no"])
        tracking_ids = assignment_tracking.get(assignment_key, set())
        if line["consignment_id"]:
            key = ("TRACKING_NUMBER", line["invoice_no"], line["consignment_id"])
            line_link_methods[key].add("TRACKING_NUMBER")
        elif line["assignment_no"]:
            key = ("ASSIGNMENT", line["invoice_no"], line["assignment_no"])
            line_link_methods[key].add(
                "ASSIGNMENT_LEVEL_UNALLOCATED"
                if tracking_ids
                else "ASSIGNMENT_NO_NO_CONSIGNMENT"
            )
        elif line["billing_doc"]:
            key = ("BILLING_DOCUMENT", line["invoice_no"], line["billing_doc"])
            line_link_methods[key].add("UNALLOCATED_BILLING_DOCUMENT")
        else:
            key = ("INVOICE", line["invoice_no"], "")
            line_link_methods[key].add("UNALLOCATED_INVOICE")
        groups[key].append(line)

    output: list[dict[str, str]] = []
    for (owner_type, invoice_no, owner_id), lines in sorted(groups.items()):
        first = lines[0]
        amounts = defaultdict(float)
        for line in lines:
            amounts[line["charge_component"]] += _number(line["amount_excl_tax"])
        base = amounts["BASE"]
        fuel_line = amounts["FUEL"]
        fuel_field = sum(_number(line["fuel_surcharge_field_value"]) for line in lines)
        fuel = fuel_line + fuel_field
        other = amounts["OTHER"]
        physical_rows, physical_audit = _physical_measurement_rows(lines)
        billed_weight = sum(_number(line["billed_weight_kg"]) for line in physical_rows)
        cubic_weight = sum(_number(line["cubic_weight_kg"]) for line in physical_rows)
        actual_weight = max(billed_weight, cubic_weight)
        if actual_weight <= 0:
            actual_weight = max(
                sum(_number(line["actual_weight_kg"]) for line in physical_rows),
                sum(_number(line["declared_weight_kg"]) for line in physical_rows),
            )
        billed_cube_values = [_number(line["billed_cube_cm3"]) for line in physical_rows]
        declared_cube_values = [_number(line["declared_cube_cm3"]) for line in physical_rows]
        billed_cube_cm3 = sum(billed_cube_values)
        declared_cube_cm3 = sum(declared_cube_values)
        total = base + fuel + other
        assignment_key = (invoice_no, first["assignment_no"])
        tracking_count = len(assignment_tracking.get(assignment_key, set())) if first["assignment_no"] else 0
        output.append(
            {
                "carrier": "Australia Post",
                "source_table": SOURCE_TABLE,
                "invoice_no": invoice_no,
                "billing_date": _first_value(lines, "billing_date"),
                "service_date": _first_value(lines, "service_date"),
                "owner_type": owner_type,
                "owner_id": owner_id,
                "charge_link_method": "|".join(sorted(line_link_methods[(owner_type, invoice_no, owner_id)])),
                "assignment_no": _first_value(lines, "assignment_no"),
                "carrier_tracking_number": owner_id if owner_type == "TRACKING_NUMBER" else "",
                "assignment_consignment_count": str(tracking_count),
                "customer_ref_1": _first_value(lines, "customer_ref_1"),
                "customer_ref_2": _first_value(lines, "customer_ref_2"),
                "customer_ref_3": _first_value(lines, "customer_ref_3"),
                "sales_order": _first_value(lines, "sales_order"),
                "source_line_count": str(len(lines)),
                "material_pattern": "|".join(sorted({line["material"] for line in lines if line["material"]})),
                "description_pattern": "|".join(sorted({line["description"] for line in lines if line["description"]})),
                "charge_code_pattern": "|".join(sorted({line["charge_code"] for line in lines if line["charge_code"]})),
                "lodgement_zone_pattern": "|".join(sorted({line["lodgement_zone"] for line in lines if line["lodgement_zone"]})),
                "destination_zone_pattern": "|".join(sorted({line["destination_zone"] for line in lines if line["destination_zone"]})),
                "origin_city": _first_value(lines, "origin_city"),
                "origin_state": _first_value(lines, "origin_state"),
                "origin_postcode": _first_value(lines, "origin_postcode"),
                "destination_city": _first_value(lines, "destination_city"),
                "destination_state": _first_value(lines, "destination_state"),
                "destination_postcode": _first_value(lines, "destination_postcode"),
                "actual_weight_kg": _format_number(sum(_number(line["actual_weight_kg"]) for line in physical_rows)),
                "declared_weight_kg": _format_number(sum(_number(line["declared_weight_kg"]) for line in physical_rows)),
                "billed_weight_kg": _format_number(billed_weight),
                "cubic_weight_kg": _format_number(cubic_weight),
                "actual_chargeable_weight_kg": _format_number(actual_weight),
                "billed_cube_cm3": _format_number(billed_cube_cm3),
                "declared_cube_cm3": _format_number(declared_cube_cm3),
                "physical_article_row_count": str(physical_audit["article_row_count"]),
                "physical_distinct_article_count": str(physical_audit["distinct_article_count"]),
                "physical_article_duplicate_row_count": str(physical_audit["duplicate_row_count"]),
                "physical_anonymous_row_count": str(physical_audit["anonymous_row_count"]),
                "physical_aggregation_method": physical_audit["aggregation_method"],
                "physical_article_cube_sum_cm3": _format_number(sum(billed_cube_values)),
                "physical_article_cube_max_cm3": _format_number(max(billed_cube_values, default=0.0)),
                "physical_article_billed_weight_sum_kg": _format_number(billed_weight),
                "physical_article_billed_weight_max_kg": _format_number(max((_number(line["billed_weight_kg"]) for line in physical_rows), default=0.0)),
                "physical_article_cubic_weight_sum_kg": _format_number(cubic_weight),
                "physical_article_cubic_weight_max_kg": _format_number(max((_number(line["cubic_weight_kg"]) for line in physical_rows), default=0.0)),
                "actual_base_cost_ex_gst": _format_number(base),
                "actual_fuel_surcharge_ex_gst": _format_number(fuel),
                "fuel_surcharge_line_ex_gst": _format_number(fuel_line),
                "fuel_surcharge_field_ex_gst": _format_number(fuel_field),
                "fuel_allocation_status": (
                    "TRACKING_LINE_FUEL_FIELD"
                    if owner_type == "TRACKING_NUMBER" and fuel_field > 0
                    else "ASSIGNMENT_LEVEL_UNALLOCATED"
                    if owner_type == "ASSIGNMENT" and fuel_line > 0
                    else "DIRECT_FUEL_LINE"
                    if fuel_line > 0
                    else "NO_FUEL"
                ),
                "actual_other_surcharge_ex_gst": _format_number(other),
                "actual_invoice_cost_ex_gst": _format_number(total),
                "actual_tax_amount": _format_number(sum(_number(line["tax_amount"]) for line in lines)),
                "actual_fuel_rate_pct": _format_number(fuel / base * 100.0) if base > 0 else "",
                "source_invoice_total_excl_tax": _format_number(max((_number(line["invoice_total_excl_tax"]) for line in lines), default=0.0)),
                "source_invoice_total_incl_tax": _format_number(max((_number(line["invoice_total_incl_tax"]) for line in lines), default=0.0)),
            }
        )
    return output


def _physical_measurement_rows(lines: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int | str]]:
    """Return one physical measurement row per article, with an explicit fallback.

    AP charge lines can contain multiple financial lines for the same article. The
    physical fields must therefore be deduplicated before being rolled up to the
    consignment. Groups without article IDs cannot be safely expanded, so they use
    the legacy maximum and remain visible through the audit fields.
    """
    article_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    anonymous_rows = 0
    for line in lines:
        article_id = _text(line.get("article_id"))
        if article_id:
            article_groups[article_id].append(line)
        elif _has_physical_measurement(line):
            anonymous_rows += 1

    if article_groups:
        measurements = [_physical_measurement(article_lines) for article_lines in article_groups.values()]
        return measurements, {
            "article_row_count": sum(len(article_lines) for article_lines in article_groups.values()),
            "distinct_article_count": len(article_groups),
            "duplicate_row_count": sum(len(article_lines) - 1 for article_lines in article_groups.values()),
            "anonymous_row_count": anonymous_rows,
            "aggregation_method": "SUM_UNIQUE_ARTICLE_MEASUREMENTS",
        }

    measurements = [_physical_measurement(lines)] if any(_has_physical_measurement(line) for line in lines) else [_physical_measurement([])]
    return measurements, {
        "article_row_count": 0,
        "distinct_article_count": 0,
        "duplicate_row_count": 0,
        "anonymous_row_count": anonymous_rows,
        "aggregation_method": "MAX_WITHOUT_ARTICLE_ID",
    }


def _physical_measurement(lines: list[dict[str, str]]) -> dict[str, str]:
    """Collapse duplicate rows for one article using the largest physical value."""
    if not lines:
        return {
            "actual_weight_kg": "0",
            "declared_weight_kg": "0",
            "billed_weight_kg": "0",
            "cubic_weight_kg": "0",
            "billed_cube_cm3": "0",
            "declared_cube_cm3": "0",
        }
    return {
        "actual_weight_kg": _format_number(max(_number(line.get("actual_weight_kg")) for line in lines)),
        "declared_weight_kg": _format_number(max(_number(line.get("declared_weight_kg")) for line in lines)),
        "billed_weight_kg": _format_number(max(_number(line.get("billed_weight_kg")) for line in lines)),
        "cubic_weight_kg": _format_number(max(_number(line.get("cubic_weight_kg")) for line in lines)),
        "billed_cube_cm3": _format_number(max(_billed_cube_cm3(line) for line in lines)),
        "declared_cube_cm3": _format_number(max(_declared_cube_cm3(line) for line in lines)),
    }


def _has_physical_measurement(line: dict[str, str]) -> bool:
    return any(
        _number(line.get(field)) > 0
        for field in (
            "actual_weight_kg",
            "declared_weight_kg",
            "billed_weight_kg",
            "cubic_weight_kg",
            "billed_length_m",
            "billed_width_m",
            "billed_height_m",
            "declared_length_cm",
            "declared_width_cm",
            "declared_height_cm",
        )
    )


def _write_control_summary(path: Path, lines: list[dict[str, str]], consignments: list[dict[str, str]], start_date: str, end_date: str) -> None:
    line_counts: dict[str, int] = defaultdict(int)
    for line in lines:
        line_counts[line["invoice_no"]] += 1
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in consignments:
        groups[row["invoice_no"]].append(row)
    rows: list[dict[str, str]] = []
    for invoice_no, invoice_groups in sorted(groups.items()):
        allocated = [row for row in invoice_groups if row["owner_type"] == "TRACKING_NUMBER"]
        ambiguous = [row for row in invoice_groups if row["owner_type"] == "ASSIGNMENT"]
        unallocated = [row for row in invoice_groups if row["owner_type"] in {"BILLING_DOCUMENT", "INVOICE"}]
        rows.append(
            {
                "source_table": SOURCE_TABLE,
                "invoice_no": invoice_no,
                "requested_start_date": start_date,
                "requested_end_date": end_date,
                "source_charge_line_count": str(line_counts[invoice_no]),
                "logical_group_count": str(len(invoice_groups)),
                "allocated_tracking_group_count": str(len(allocated)),
                "ambiguous_assignment_group_count": str(len(ambiguous)),
                "unallocated_group_count": str(len(unallocated)),
                "allocated_base_cost_ex_gst": _format_number(sum(_number(row["actual_base_cost_ex_gst"]) for row in allocated)),
                "allocated_fuel_surcharge_ex_gst": _format_number(sum(_number(row["actual_fuel_surcharge_ex_gst"]) for row in allocated)),
                "allocated_other_surcharge_ex_gst": _format_number(sum(_number(row["actual_other_surcharge_ex_gst"]) for row in allocated)),
                "allocated_invoice_cost_ex_gst": _format_number(sum(_number(row["actual_invoice_cost_ex_gst"]) for row in allocated)),
                "all_source_cost_ex_gst": _format_number(sum(_number(row["actual_invoice_cost_ex_gst"]) for row in invoice_groups)),
                "unallocated_cost_ex_gst": _format_number(sum(_number(row["actual_invoice_cost_ex_gst"]) for row in ambiguous + unallocated)),
            }
        )
    _write_csv(path, _control_headers(), rows)


def _charge_component(material: object, description: object, charge_code: object) -> str:
    material_text = _normalise(material)
    description_text = _normalise(description)
    charge_code_text = _normalise(charge_code)
    if material_text in {"D05", "DS1"} or "FUEL SURCHARGE" in description_text:
        return "FUEL"
    if material_text in {"B48", "B5R", "B5S"}:
        return "BASE"
    if "FUEL" in charge_code_text:
        return "FUEL"
    return "OTHER"


def _billed_cube_cm3(row: dict[str, str]) -> float:
    dimensions = [_number(row.get(key)) for key in ("billed_length_m", "billed_width_m", "billed_height_m")]
    cube = math.prod(dimensions) * 1_000_000.0 if all(value > 0 for value in dimensions) else 0.0
    return max(cube, _number(row.get("cubic_weight_kg")) * 4000.0)


def _declared_cube_cm3(row: dict[str, str]) -> float:
    dimensions = [_number(row.get(key)) for key in ("declared_length_cm", "declared_width_cm", "declared_height_cm")]
    return math.prod(dimensions) if all(value > 0 for value in dimensions) else 0.0


def _first_value(lines: list[dict[str, str]], key: str) -> str:
    return next((line[key] for line in lines if line.get(key)), "")


def _charge_line_headers() -> list[str]:
    return [
        "carrier", "source_table", "source_row_number", "invoice_no", "billing_date", "service_date", "billing_doc", "assignment_no",
        "customer_ref_1", "customer_ref_2", "customer_ref_3", "sales_order", "sales_order_item", "consignment_id", "article_id",
        "material", "description", "qty", "total_qty", "amount_excl_tax", "tax_amount", "amount_incl_tax", "invoice_total_excl_tax",
        "invoice_total_incl_tax", "tot_amount_excl_tax", "fuel_surcharge_field_value", "fuel_surcharge_discount", "fuel_gst", "charge_code",
        "charge_zone", "lodgement_zone", "destination_zone", "lodgement_date", "origin_name", "origin_city", "origin_state", "origin_postcode",
        "destination_name", "destination_city", "destination_state", "destination_postcode", "actual_weight_kg", "actual_length_cm", "actual_width_cm",
        "actual_height_cm", "declared_weight_kg", "declared_length_cm", "declared_width_cm", "declared_height_cm", "billed_length_m", "billed_width_m",
        "billed_height_m", "cubic_weight_kg", "billed_weight_kg", "row_hash", "charge_component",
    ]


def _consignment_headers() -> list[str]:
    return [
        "carrier", "source_table", "invoice_no", "billing_date", "service_date", "owner_type", "owner_id", "charge_link_method", "assignment_no",
        "carrier_tracking_number", "assignment_consignment_count", "customer_ref_1", "customer_ref_2", "customer_ref_3", "sales_order", "source_line_count",
        "material_pattern", "description_pattern", "charge_code_pattern", "lodgement_zone_pattern", "destination_zone_pattern", "origin_city", "origin_state", "origin_postcode", "destination_city",
        "destination_state", "destination_postcode", "actual_weight_kg", "declared_weight_kg", "billed_weight_kg", "cubic_weight_kg",
        "actual_chargeable_weight_kg", "billed_cube_cm3", "declared_cube_cm3", "physical_article_row_count", "physical_distinct_article_count",
        "physical_article_duplicate_row_count", "physical_anonymous_row_count", "physical_aggregation_method", "physical_article_cube_sum_cm3",
        "physical_article_cube_max_cm3", "physical_article_billed_weight_sum_kg", "physical_article_billed_weight_max_kg",
        "physical_article_cubic_weight_sum_kg", "physical_article_cubic_weight_max_kg", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst",
        "fuel_surcharge_line_ex_gst", "fuel_surcharge_field_ex_gst", "fuel_allocation_status", "actual_other_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "actual_tax_amount", "actual_fuel_rate_pct", "source_invoice_total_excl_tax",
        "source_invoice_total_incl_tax",
    ]


def _control_headers() -> list[str]:
    return [
        "source_table", "invoice_no", "requested_start_date", "requested_end_date", "source_charge_line_count", "logical_group_count",
        "allocated_tracking_group_count", "ambiguous_assignment_group_count", "unallocated_group_count", "allocated_base_cost_ex_gst",
        "allocated_fuel_surcharge_ex_gst", "allocated_other_surcharge_ex_gst", "allocated_invoice_cost_ex_gst", "all_source_cost_ex_gst",
        "unallocated_cost_ex_gst",
    ]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _default_start_date() -> str:
    value = os.environ.get("LM_EXTRACTION_START_DATE", "").strip()
    if value:
        return _iso_date(value)
    return (date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()


def _iso_date(value: str) -> str:
    return date.fromisoformat(str(value).strip()).isoformat()


def _normalise_header(value: object) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())).strip("_")


def _normalise(value: object) -> str:
    return str(value or "").strip().upper()


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    for format_string in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, format_string).date().isoformat()
        except ValueError:
            continue
    return text


def _number(value: object) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    main()
