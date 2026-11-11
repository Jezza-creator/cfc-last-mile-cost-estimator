"""Normalise Allied weekly invoice workbooks into job-level reconciliation inputs."""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook


INPUT_DIRECTORY = Path("data/inputs/carrier_invoices/Allied")
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")
CHARGE_LINES_PATH = OUTPUT_DIRECTORY / "allied_charge_lines.csv"
JOBS_PATH = OUTPUT_DIRECTORY / "allied_jobs.csv"
CONTROL_SUMMARY_PATH = OUTPUT_DIRECTORY / "allied_invoice_control_summary.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-directory", type=Path, default=INPUT_DIRECTORY)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    parser.add_argument(
        "--sheet",
        action="append",
        dest="sheets",
        help="Worksheet to include; repeat for multiple sheets. By default, all data worksheets are read.",
    )
    args = parser.parse_args()

    workbooks = sorted(args.input_directory.glob("*.xlsx"))
    if not workbooks:
        raise FileNotFoundError(f"No .xlsx files found in {args.input_directory}")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    charge_lines_path = args.output_directory / CHARGE_LINES_PATH.name
    jobs_path = args.output_directory / JOBS_PATH.name
    control_summary_path = args.output_directory / CONTROL_SUMMARY_PATH.name
    selected_sheets = {sheet.strip() for sheet in (args.sheets or []) if sheet.strip()}
    charge_lines = [
        line
        for workbook in workbooks
        for line in _read_charge_lines(workbook, selected_sheets or None)
    ]
    _write_csv(charge_lines_path, _charge_line_headers(), charge_lines)
    jobs = _aggregate_jobs(charge_lines)
    _write_csv(jobs_path, _job_headers(), jobs)
    _write_control_summary(control_summary_path, charge_lines, jobs)
    print(f"Wrote {charge_lines_path}")
    print(f"Wrote {jobs_path}")
    print(f"Wrote {control_summary_path}")


def _read_charge_lines(workbook_path: Path, selected_sheets: set[str] | None = None) -> list[dict[str, str]]:
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    rows: list[dict[str, str]] = []
    for worksheet in workbook.worksheets:
        if selected_sheets is not None and worksheet.title not in selected_sheets:
            continue
        header_row = _find_header_row(worksheet)
        if header_row is None:
            continue
        headers = {
            _normalise_header(value): index
            for index, value in enumerate(next(worksheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True)))
            if _normalise_header(value)
        }
        for source_row_number, values in enumerate(worksheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            docket = _cell(values, headers, "docket")
            job_number = _cell(values, headers, "job_no")
            if not docket and not job_number:
                continue
            charge_code = _cell(values, headers, "charge_code")
            charge = _number(_cell(values, headers, "charge"))
            if not charge_code and charge == 0:
                continue
            reference = _cell(values, headers, "reference")
            rows.append(
                {
                    "carrier": "Allied Express",
                    "source_file": workbook_path.name,
                    "source_sheet": worksheet.title,
                    "source_row_number": str(source_row_number),
                    "allied_week_number": _cell(values, headers, "week_no"),
                    "docket": docket,
                    "job_number": job_number,
                    "reference": reference,
                    "external_order_id": _external_order_id(reference),
                    "origin_suburb": _cell(values, headers, "from_suburb"),
                    "origin_postcode": _cell(values, headers, "from_postcode"),
                    "destination_suburb": _cell(values, headers, "to_suburb"),
                    "destination_postcode": _cell(values, headers, "to_postcode"),
                    "charged_weight_kg": _format_number(_number(_cell(values, headers, "charged_weight"))),
                    "declared_weight_kg": _format_number(_number(_cell(values, headers, "declared_weight"))),
                    "declared_volume_m3": _format_number(_number(_cell(values, headers, "declared_volume"))),
                    "items": _format_number(_number(_cell(values, headers, "items"))),
                    "service_code": _cell(values, headers, "service"),
                    "charge_code": charge_code,
                    "charge_type": _cell(values, headers, "charge_type"),
                    "charge_component": _charge_component(charge_code),
                    "charge_ex_gst": _format_number(charge),
                    "gst": _format_number(_number(_cell(values, headers, "gst"))),
                    "charge_date": _date(_cell_value(values, headers, "charge_date")),
                    "transaction_date": _date(_cell_value(values, headers, "trans_date")),
                }
            )
    return rows


def _find_header_row(worksheet) -> int | None:
    for row_number, values in enumerate(worksheet.iter_rows(max_row=12, values_only=True), start=1):
        headers = {_normalise_header(value) for value in values}
        if "docket" in headers and "job_no" in headers and "charge" in headers:
            return row_number
    return None


def _aggregate_jobs(charge_lines: list[dict[str, str]]) -> list[dict[str, str]]:
    """Aggregate all charge lines for an Allied job across weekly worksheets."""
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for line in charge_lines:
        groups[(line["job_number"], line["docket"])].append(line)

    jobs: list[dict[str, str]] = []
    for (job_number, docket), lines in sorted(groups.items()):
        first = lines[0]
        components = defaultdict(float)
        for line in lines:
            components[line["charge_component"]] += _number(line["charge_ex_gst"])
        total = sum(components.values())
        jobs.append(
            {
                "carrier": "Allied Express",
                "source_file": _join_unique(lines, "source_file"),
                "source_sheet": _join_unique(lines, "source_sheet"),
                "allied_week_number": _join_unique(lines, "allied_week_number"),
                "docket": docket,
                "job_number": job_number,
                "reference": first["reference"],
                "external_order_id": first["external_order_id"],
                "origin_suburb": first["origin_suburb"],
                "origin_postcode": first["origin_postcode"],
                "destination_suburb": first["destination_suburb"],
                "destination_postcode": first["destination_postcode"],
                "charged_weight_kg": _format_number(max(_number(line["charged_weight_kg"]) for line in lines)),
                "declared_weight_kg": _format_number(max(_number(line["declared_weight_kg"]) for line in lines)),
                "declared_volume_m3": _format_number(max(_number(line["declared_volume_m3"]) for line in lines)),
                "items": _format_number(max(_number(line["items"]) for line in lines)),
                "service_code": first["service_code"],
                "charge_line_count": str(len(lines)),
                "charge_code_pattern": "|".join(sorted({line["charge_code"] for line in lines if line["charge_code"]})),
                "actual_base_cost_ex_gst": _format_number(components["BASE"]),
                "actual_excess_weight_surcharge_ex_gst": _format_number(components["EXCESS_WEIGHT"]),
                "actual_delivery_surcharge_ex_gst": _format_number(components["DELIVERY_SURCHARGE"]),
                "actual_fuel_surcharge_ex_gst": _format_number(components["FUEL"]),
                "actual_other_surcharge_ex_gst": _format_number(components["OTHER"]),
                "actual_invoice_cost_ex_gst": _format_number(total),
                "actual_gst": _format_number(sum(_number(line["gst"]) for line in lines)),
            }
        )
    return jobs


def _write_control_summary(path: Path, lines: list[dict[str, str]], jobs: list[dict[str, str]] | None = None) -> None:
    summaries: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"line_count": 0, "job_keys": set(), "cost": 0.0}
    )
    for line in lines:
        summary = summaries[(line["source_file"], line["source_sheet"])]
        summary["line_count"] = int(summary["line_count"]) + 1
        summary["job_keys"].add((line["job_number"], line["docket"]))
        summary["cost"] = float(summary["cost"]) + _number(line["charge_ex_gst"])
    rows = [
        {
            "source_file": source_file,
            "source_sheet": source_sheet,
            "charge_line_count": summary["line_count"],
            "billed_job_count": len(summary["job_keys"]),
            "actual_invoice_cost_ex_gst": _format_number(summary["cost"]),
        }
        for (source_file, source_sheet), summary in sorted(summaries.items())
    ]
    _write_csv(path, ["source_file", "source_sheet", "charge_line_count", "billed_job_count", "actual_invoice_cost_ex_gst"], rows)


def _charge_component(charge_code: str) -> str:
    code = charge_code.upper()
    if "FUEL" in code:
        return "FUEL"
    if code == "JOB":
        return "BASE"
    if code == "EW":
        return "EXCESS_WEIGHT"
    if code == "DHS":
        return "DELIVERY_SURCHARGE"
    return "OTHER"


def _cell(values: tuple[object, ...], headers: dict[str, int], key: str) -> str:
    return _text(_cell_value(values, headers, key))


def _cell_value(values: tuple[object, ...], headers: dict[str, int], key: str) -> object:
    index = headers.get(key)
    return values[index] if index is not None and index < len(values) else None


def _join_unique(lines: list[dict[str, str]], key: str) -> str:
    values: list[str] = []
    for line in lines:
        value = _text(line.get(key))
        if value and value not in values:
            values.append(value)
    return "|".join(values)


def _external_order_id(reference: str) -> str:
    match = re.search(r"\border\s+(\d+)\b", reference, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _charge_line_headers() -> list[str]:
    return [
        "carrier", "source_file", "source_sheet", "source_row_number", "allied_week_number", "docket", "job_number", "reference", "external_order_id",
        "origin_suburb", "origin_postcode", "destination_suburb", "destination_postcode", "charged_weight_kg", "declared_weight_kg", "declared_volume_m3", "items",
        "service_code", "charge_code", "charge_type", "charge_component", "charge_ex_gst", "gst", "charge_date", "transaction_date",
    ]


def _job_headers() -> list[str]:
    return [
        "carrier", "source_file", "source_sheet", "allied_week_number", "docket", "job_number", "reference", "external_order_id",
        "origin_suburb", "origin_postcode", "destination_suburb", "destination_postcode", "charged_weight_kg", "declared_weight_kg", "declared_volume_m3", "items", "service_code",
        "charge_line_count", "charge_code_pattern", "actual_base_cost_ex_gst", "actual_excess_weight_surcharge_ex_gst", "actual_delivery_surcharge_ex_gst",
        "actual_fuel_surcharge_ex_gst", "actual_other_surcharge_ex_gst", "actual_invoice_cost_ex_gst", "actual_gst",
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
