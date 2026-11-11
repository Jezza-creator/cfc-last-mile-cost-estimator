"""Normalise GoPeople weekly invoice workbooks into carton and consignment inputs."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook


INPUT_DIRECTORY = Path("data/inputs/carrier_invoices/GoPeople")
OUTPUT_DIRECTORY = Path("outputs/carrier_invoices")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-directory", type=Path, default=INPUT_DIRECTORY)
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_DIRECTORY)
    args = parser.parse_args()
    files = sorted(args.input_directory.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No .xlsx files found in {args.input_directory}")
    args.output_directory.mkdir(parents=True, exist_ok=True)
    lines = [line for path in files for line in _read_lines(path)]
    _write_csv(args.output_directory / "gopeople_charge_lines.csv", _line_headers(), lines)
    consignments = _aggregate_consignments(lines)
    _write_csv(args.output_directory / "gopeople_consignments.csv", _consignment_headers(), consignments)
    _write_control(args.output_directory / "gopeople_invoice_control_summary.csv", lines, consignments)
    print(f"Wrote {args.output_directory / 'gopeople_charge_lines.csv'}")
    print(f"Wrote {args.output_directory / 'gopeople_consignments.csv'}")
    print(f"Wrote {args.output_directory / 'gopeople_invoice_control_summary.csv'}")


def _read_lines(path: Path) -> list[dict[str, str]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    output: list[dict[str, str]] = []
    for worksheet in workbook.worksheets:
        header_row = _header_row(worksheet)
        if header_row is None:
            continue
        values = next(worksheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
        headers = {_normalise(value): index for index, value in enumerate(values) if _normalise(value)}
        for row_number, row in enumerate(worksheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            consignment = _cell(row, headers, "consignment_number")
            if not consignment:
                continue
            charge = _number(_cell(row, headers, "price_gst_exc"))
            raw_fuel = _fuel_amount(_cell(row, headers, "invoice_notes_comments_fuel"))
            fuel_charge = min(max(raw_fuel or 0.0, 0.0), max(charge, 0.0))
            base_charge = max(charge - fuel_charge, 0.0)
            fuel_pct = fuel_charge / base_charge * 100.0 if raw_fuel is not None and base_charge > 0 else None
            output.append({
                "carrier": "GoPeople", "source_file": path.name, "source_sheet": worksheet.title, "source_row_number": str(row_number),
                "account_source": _text(row[0]) if row else "", "account": _cell(row, headers, "store_id_ref"), "state": _cell(row, headers, "state"),
                "carrier_tracking_number": consignment, "consignment_date": _date(_cell_value(row, headers, "date")), "carrier_invoice_number": _cell(row, headers, "invoice_number"),
                "origin_suburb": _cell(row, headers, "from_suburb"), "origin_postcode": _cell(row, headers, "from_postcode"),
                "destination_suburb": _cell(row, headers, "to_suburb"), "destination_postcode": _cell(row, headers, "to_postcode"),
                "item_carton_number": _cell(row, headers, "item_carton_number"), "charged_weight_kg": _fmt(_number(_cell(row, headers, "charged_weight"))),
                "customer_cubic_weight_kg": _fmt(_number(_cell(row, headers, "customer_cubic_weight"))), "customer_declared_weight_kg": _fmt(_number(_cell(row, headers, "customer_declared_weight"))),
                "customer_cubic_volume_m3": _fmt(_number(_cell(row, headers, "customer_cubic_volume"))), "distance_km": _fmt(_number(_cell(row, headers, "distance"))),
                "charge_ex_gst": _fmt(charge), "actual_base_cost_ex_gst": _fmt(base_charge), "actual_fuel_surcharge_ex_gst": _fmt(fuel_charge), "actual_fuel_surcharge_pct": _fmt(fuel_pct) if fuel_pct is not None else "", "gst": _fmt(_number(_cell(row, headers, "gst"))),
                "invoice_notes_or_fuel": _cell(row, headers, "invoice_notes_comments_fuel"), "year": _cell(row, headers, "year"), "week_number": _cell(row, headers, "week_no"),
            })
    return output


def _header_row(worksheet) -> int | None:
    for row_number, values in enumerate(worksheet.iter_rows(max_row=12, values_only=True), start=1):
        headers = {_normalise(value) for value in values}
        if "consignment_number" in headers and "price_gst_exc" in headers and "item_carton_number" in headers:
            return row_number
    return None


def _aggregate_consignments(lines: list[dict[str, str]]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for line in lines:
        groups[(line["source_file"], line["source_sheet"], line["carrier_invoice_number"], line["carrier_tracking_number"])].append(line)
    output = []
    for _, rows in sorted(groups.items()):
        first = rows[0]
        total_base = sum(_number(row["actual_base_cost_ex_gst"]) for row in rows)
        total_fuel = sum(_number(row["actual_fuel_surcharge_ex_gst"]) for row in rows)
        output.append({
            "carrier": "GoPeople", "source_file": first["source_file"], "source_sheet": first["source_sheet"], "carrier_invoice_number": first["carrier_invoice_number"],
            "carrier_tracking_number": first["carrier_tracking_number"], "consignment_date": first["consignment_date"], "account_source": first["account_source"], "account": first["account"],
            "origin_suburb": first["origin_suburb"], "origin_postcode": first["origin_postcode"], "destination_suburb": first["destination_suburb"], "destination_postcode": first["destination_postcode"],
            "carton_count": str(len(rows)), "charged_weight_kg": _fmt(sum(_number(row["charged_weight_kg"]) for row in rows)), "customer_cubic_weight_kg": _fmt(sum(_number(row["customer_cubic_weight_kg"]) for row in rows)),
            "customer_declared_weight_kg": _fmt(sum(_number(row["customer_declared_weight_kg"]) for row in rows)), "customer_cubic_volume_m3": _fmt(sum(_number(row["customer_cubic_volume_m3"]) for row in rows)),
            "distance_km": first["distance_km"], "actual_base_cost_ex_gst": _fmt(total_base), "actual_fuel_surcharge_ex_gst": _fmt(total_fuel), "actual_fuel_surcharge_pct": _fmt(total_fuel / total_base * 100.0) if total_base > 0 else "", "actual_invoice_cost_ex_gst": _fmt(sum(_number(row["charge_ex_gst"]) for row in rows)), "actual_gst": _fmt(sum(_number(row["gst"]) for row in rows)),
            "invoice_notes_or_fuel": "|".join(sorted({row["invoice_notes_or_fuel"] for row in rows if row["invoice_notes_or_fuel"]})), "year": first["year"], "week_number": first["week_number"],
        })
    return output


def _write_control(path: Path, lines: list[dict[str, str]], consignments: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str], dict[str, float | int]] = defaultdict(lambda: {"lines": 0, "consignments": 0, "cost": 0.0})
    for line in lines: groups[(line["source_file"], line["source_sheet"])]["lines"] += 1
    for row in consignments:
        group = groups[(row["source_file"], row["source_sheet"])]; group["consignments"] += 1; group["cost"] += _number(row["actual_invoice_cost_ex_gst"])
    _write_csv(path, ["source_file", "source_sheet", "charge_line_count", "billed_consignment_count", "actual_invoice_cost_ex_gst"], [{"source_file": key[0], "source_sheet": key[1], "charge_line_count": value["lines"], "billed_consignment_count": value["consignments"], "actual_invoice_cost_ex_gst": _fmt(value["cost"])} for key, value in sorted(groups.items())])


def _line_headers() -> list[str]:
    return ["carrier", "source_file", "source_sheet", "source_row_number", "account_source", "account", "state", "carrier_tracking_number", "consignment_date", "carrier_invoice_number", "origin_suburb", "origin_postcode", "destination_suburb", "destination_postcode", "item_carton_number", "charged_weight_kg", "customer_cubic_weight_kg", "customer_declared_weight_kg", "customer_cubic_volume_m3", "distance_km", "charge_ex_gst", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_fuel_surcharge_pct", "gst", "invoice_notes_or_fuel", "year", "week_number"]


def _consignment_headers() -> list[str]:
    return ["carrier", "source_file", "source_sheet", "carrier_invoice_number", "carrier_tracking_number", "consignment_date", "account_source", "account", "origin_suburb", "origin_postcode", "destination_suburb", "destination_postcode", "carton_count", "charged_weight_kg", "customer_cubic_weight_kg", "customer_declared_weight_kg", "customer_cubic_volume_m3", "distance_km", "actual_base_cost_ex_gst", "actual_fuel_surcharge_ex_gst", "actual_fuel_surcharge_pct", "actual_invoice_cost_ex_gst", "actual_gst", "invoice_notes_or_fuel", "year", "week_number"]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=headers); writer.writeheader(); writer.writerows(rows)


def _cell(row: tuple[object, ...], headers: dict[str, int], name: str) -> str: return _text(_cell_value(row, headers, name))
def _cell_value(row: tuple[object, ...], headers: dict[str, int], name: str) -> object:
    index = headers.get(name); return row[index] if index is not None and index < len(row) else None
def _normalise(value: object) -> str: return "".join(char if char.isalnum() else "_" for char in _text(value).lower()).strip("_").replace("__", "_")
def _text(value: object) -> str: return "" if value is None else str(value).strip()
def _date(value: object) -> str: return value.date().isoformat() if isinstance(value, datetime) else value.isoformat() if isinstance(value, date) else _text(value)
def _number(value: object) -> float:
    try: result = float(value or 0)
    except (TypeError, ValueError): return 0.0
    return result if math.isfinite(result) else 0.0
def _fmt(value: float) -> str: return f"{value:.6f}".rstrip("0").rstrip(".")


def _fuel_amount(value: object) -> float | None:
    text = _text(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


if __name__ == "__main__":
    main()
