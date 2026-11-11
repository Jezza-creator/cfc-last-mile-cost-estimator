"""Normalise Couriers Please invoice exports into reconciliation-ready charge lines."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook


INPUT_DIRECTORY = Path("data/inputs/carrier_invoices/couriers_please")
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
CHARGE_LINES_PATH = OUTPUT_DIRECTORY / "couriers_please_charge_lines.csv"
CONTROL_SUMMARY_PATH = OUTPUT_DIRECTORY / "couriers_please_invoice_control_summary.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-directory", type=Path, default=INPUT_DIRECTORY)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()

    workbooks = sorted(args.input_directory.glob("*.xlsx"))
    if not workbooks:
        raise FileNotFoundError(f"No .xlsx files found in {args.input_directory}")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    charge_lines_path = args.output_directory / CHARGE_LINES_PATH.name
    control_summary_path = args.output_directory / CONTROL_SUMMARY_PATH.name
    _write_outputs(workbooks, charge_lines_path, control_summary_path)
    print(f"Wrote {charge_lines_path}")
    print(f"Wrote {control_summary_path}")


def _write_outputs(workbooks: list[Path], charge_lines_path: Path, control_summary_path: Path) -> None:
    raw_rows = [row for workbook_path in workbooks for row in _read_charge_lines(workbook_path)]
    charge_lines = _deduplicate_charge_lines(raw_rows)
    summaries: dict[tuple[str, str], dict[str, float | int | set[str]]] = defaultdict(
        lambda: {"charge_line_count": 0, "consignment_references": set(), "total_ex_gst": 0.0, "total_gst": 0.0}
    )
    with charge_lines_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=_charge_line_headers())
        writer.writeheader()
        for row in charge_lines:
            writer.writerow({key: value for key, value in row.items() if not key.startswith("_")})
            summary = summaries[(row["source_file"], row["source_sheet"])]
            summary["charge_line_count"] = int(summary["charge_line_count"]) + 1
            summary["consignment_references"].add(row["carrier_consignment_reference"])
            summary["total_ex_gst"] = float(summary["total_ex_gst"]) + _number(row["actual_invoice_cost_ex_gst"])
            summary["total_gst"] = float(summary["total_gst"]) + _number(row["gst"])

    with control_summary_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=["source_file", "source_sheet", "charge_line_count", "distinct_consignment_count", "actual_invoice_cost_ex_gst", "gst", "actual_invoice_cost_inc_gst"])
        writer.writeheader()
        for (source_file, source_sheet), summary in summaries.items():
            total_ex_gst = float(summary["total_ex_gst"])
            gst = float(summary["total_gst"])
            writer.writerow(
                {
                    "source_file": source_file,
                    "source_sheet": source_sheet,
                    "charge_line_count": summary["charge_line_count"],
                    "distinct_consignment_count": len(summary["consignment_references"]),
                    "actual_invoice_cost_ex_gst": _format_number(total_ex_gst),
                    "gst": _format_number(gst),
                    "actual_invoice_cost_inc_gst": _format_number(total_ex_gst + gst),
                }
            )


def _deduplicate_charge_lines(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep one billed row per CP consignment, preferring consolidated workbooks."""
    retained: dict[str, dict[str, str]] = {}
    for row in rows:
        key = _text(row.get("carrier_consignment_reference"))
        if not key:
            continue
        current = retained.get(key)
        if current is None or _charge_line_priority(row) >= _charge_line_priority(current):
            retained[key] = row
    return list(retained.values())


def _charge_line_priority(row: dict[str, str]) -> tuple[int, str, str, int]:
    source_file = _text(row.get("source_file"))
    consolidated = int("fy2027 courier please kmart" in source_file.lower())
    try:
        source_row = int(row.get("source_row_number", 0))
    except (TypeError, ValueError):
        source_row = 0
    return (consolidated, source_file, _text(row.get("source_sheet")), source_row)


def _read_charge_lines(workbook_path: Path):
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        for worksheet in workbook.worksheets:
            try:
                header_values = next(worksheet.iter_rows(values_only=True))
            except StopIteration:
                continue
            headers = [_normalise_header(value) for value in header_values]
            if "consignment_ref" not in headers:
                continue

            for source_row_number, values in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2):
                source = {headers[index]: values[index] for index in range(len(headers))}
                consignment_reference = _text(source.get("consignment_ref"))
                if not consignment_reference:
                    continue
                base_cost = _number(source.get("ex_gst"))
                fuel_surcharge = _number(source.get("fuel_surcharge"))
                insurance = _number(source.get("insurance"))
                over_freight = _number(source.get("o_f"))
                remote_area_surcharge = _number(source.get("ras"))
                yield {
                    "carrier": "Couriers Please",
                    "source_file": workbook_path.name,
                    "source_sheet": worksheet.title,
                    "source_row_number": source_row_number,
                    "carrier_invoice_number": _text(source.get("inv_no")),
                    "carrier_invoice_date": _date(source.get("inv_date")),
                    "carrier_consignment_reference": consignment_reference,
                    "carrier_tracking_number": consignment_reference,
                    "consignment_date": _date(source.get("consignment_date")),
                    "manifest_reference": _text(source.get("manifest_ref")),
                    "kmart_invoice_number": _text(source.get("customer_other_reference_1")),
                    "customer_reference": _text(source.get("customer_ref")),
                    "service_code": _text(source.get("service_code")),
                    "service_name": _text(source.get("service_name")),
                    "origin_postcode": _text(source.get("origin_postcode")),
                    "origin_locality": _text(source.get("origin_locality")),
                    "destination_postcode": _text(source.get("destination_postcode")),
                    "destination_locality": _text(source.get("destination_locality")),
                    "total_items": _format_number(_number(source.get("total_items"))),
                    "total_weight_kg": _format_number(_number(source.get("total_weight"))),
                    "total_volume_m3": _format_number(_number(source.get("total_volume"))),
                    "chargeable_weight_kg": _format_number(_number(source.get("chargeable_weight"))),
                    "declared_weight_kg": _format_number(_number(source.get("declared_weight"))),
                    "measured_weight_kg": _format_number(_number(source.get("measured_weight"))),
                    "declared_volume_m3": _format_number(_number(source.get("declared_volume"))),
                    "measured_volume_m3": _format_number(_number(source.get("measured_volume"))),
                    "origin_zone": _text(source.get("origin_zone")),
                    "destination_zone": _text(source.get("destination_zone")),
                    "base_cost_ex_gst": _format_number(base_cost),
                    "fuel_surcharge_ex_gst": _format_number(fuel_surcharge),
                    "insurance_ex_gst": _format_number(insurance),
                    "other_fee_ex_gst": _format_number(over_freight),
                    "remote_area_surcharge_ex_gst": _format_number(remote_area_surcharge),
                    "actual_invoice_cost_ex_gst": _format_number(base_cost + fuel_surcharge + insurance + over_freight + remote_area_surcharge),
                    "gst": _format_number(_number(source.get("gst"))),
                }
    finally:
        workbook.close()


def _charge_line_headers() -> list[str]:
    return [
        "carrier", "source_file", "source_sheet", "source_row_number", "carrier_invoice_number", "carrier_invoice_date",
        "carrier_consignment_reference", "carrier_tracking_number", "consignment_date", "manifest_reference",
        "kmart_invoice_number", "customer_reference", "service_code", "service_name", "origin_postcode",
        "origin_locality", "destination_postcode", "destination_locality", "total_items", "total_weight_kg",
        "total_volume_m3", "chargeable_weight_kg", "declared_weight_kg", "measured_weight_kg",
        "declared_volume_m3", "measured_volume_m3", "origin_zone", "destination_zone", "base_cost_ex_gst",
        "fuel_surcharge_ex_gst", "insurance_ex_gst", "other_fee_ex_gst", "remote_area_surcharge_ex_gst",
        "actual_invoice_cost_ex_gst", "gst",
    ]


def _normalise_header(value: object) -> str:
    text = _text(value).lower()
    return "".join(character if character.isalnum() else "_" for character in text).strip("_").replace("__", "_")


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


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
