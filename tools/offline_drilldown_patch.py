from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


TITLE_FILL = PatternFill("solid", fgColor="1F4E78")
SUBTITLE_FILL = PatternFill("solid", fgColor="D9EAF7")
HEADER_FILL = PatternFill("solid", fgColor="D9E2F3")


SITE_CONFIGS = {
    "MOOREBANK": {
        "label": "Moorebank",
        "stage1": Path("outputs/stage1_moorebank_parcel_assignments_full.csv"),
        "stage2": Path("outputs/stage2_moorebank_parcel_physicals_full.csv"),
        "stage2_validation": Path("outputs/stage2_moorebank_parcelizer_validation.csv"),
        "stage3": Path("outputs/stage3_moorebank_cost_estimates_full.csv"),
    },
    "TRUGANINA": {
        "label": "Truganina",
        "stage1": Path("outputs/stage1_truganina_parcel_assignments_full.csv"),
        "stage2": Path("outputs/stage2_truganina_parcel_physicals_full.csv"),
        "stage2_validation": Path("outputs/stage2_truganina_parcelizer_validation.csv"),
        "stage3": Path("outputs/stage3_truganina_cost_estimates_full.csv"),
    },
}


def main() -> None:
    args = _parse_args()
    site_key = args.site.upper()
    invoice_number = str(args.invoice_number).strip()
    config = SITE_CONFIGS[site_key]

    stage1_rows = _load_rows(config["stage1"], invoice_number)
    stage2_rows = _load_rows(config["stage2"], invoice_number)
    validation_row = _load_first_row(config["stage2_validation"], invoice_number)
    stage3_row = _load_first_row(config["stage3"], invoice_number)

    if not stage3_row:
        raise FileNotFoundError(f"Invoice {invoice_number} not found in {config['stage3']}")

    workbook = Workbook()
    workbook.remove(workbook.active)
    _write_summary_sheet(workbook, config["label"], invoice_number, stage1_rows, stage2_rows, validation_row, stage3_row)
    _write_parcel_validation_sheet(workbook, config["label"], invoice_number, stage2_rows, validation_row, stage3_row)
    _write_parcel_detail_sheet(workbook, config["label"], invoice_number, stage2_rows, validation_row)
    _write_cost_detail_sheet(workbook, config["label"], invoice_number, stage2_rows, stage3_row)
    _write_sku_review_sheet(workbook, config["label"], invoice_number, stage1_rows)
    _write_notes_sheet(workbook, config["label"], invoice_number)

    out_dir = Path("outputs/invoice_drilldowns")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{site_key.lower()}_{invoice_number}_drilldown.xlsx"
    workbook.save(out_path)
    print(f"INVOICE_DRILLDOWN_EXPORTED={out_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local invoice drilldown workbook from refreshed CSV outputs.")
    parser.add_argument("--invoice-number", required=True)
    parser.add_argument("--site", required=True, choices=["MOOREBANK", "TRUGANINA"])
    return parser.parse_args()


def _load_rows(path: Path, invoice_number: str) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("invoice_number") or row.get("shippit_invoice_number") or "").strip() == invoice_number:
                rows.append(row)
    return rows


def _load_first_row(path: Path, invoice_number: str) -> dict[str, str]:
    rows = _load_rows(path, invoice_number)
    return rows[0] if rows else {}


def _write_summary_sheet(
    workbook: Workbook,
    site_label: str,
    invoice_number: str,
    stage1_rows: list[dict[str, str]],
    stage2_rows: list[dict[str, str]],
    validation_row: dict[str, str],
    stage3_row: dict[str, str],
) -> None:
    sheet = workbook.create_sheet("Summary")
    _style_title(sheet, f"Invoice Drilldown - {invoice_number}", site_label)

    parcel_units_total = _sum_int(stage2_rows, "parcel_units")
    packed_sku_rows = len({str(value) for row in stage1_rows for value in _split_ids(row.get("sku_ids") or row.get("skus") or "")})
    packed_sku_weight = _to_float(stage3_row.get("est_packed_sku_weight_kg"))
    packed_sku_cube_cm3 = _to_float(stage3_row.get("est_parcel_cube_total_cm3"))
    packed_sku_cube_m3 = _to_float(stage3_row.get("est_parcel_cube_total_m3"))
    est_chargeable = _to_float(stage3_row.get("est_chargeable_weight_kg"))
    shippit_chargeable = _to_float(stage3_row.get("shippit_chargeable_weight_kg"))
    shippit_dead = _to_float(stage3_row.get("shippit_dead_weight_kg"))
    shippit_vol_weight = _to_float(stage3_row.get("shippit_volumetric_weight_kg"))
    shippit_vol_cube = _to_float(stage3_row.get("shippit_volumetric_cube_cm3"))
    fill_pct = _to_float(validation_row.get("est_fill_pct_vs_shippit_cube"))
    air_pct = _to_float(validation_row.get("est_air_ratio_pct_vs_shippit_cube"))

    size_breakdown = _size_breakdown(stage2_rows)
    rows = [
        ("Site", site_label),
        ("Invoice number", invoice_number),
        ("Carrier", stage3_row.get("actual_carrier") or ""),
        ("Service", stage3_row.get("actual_service") or ""),
        ("Actual cost basis note", "Couriers Please consignment-level charging applied and the invoice chargeable weight was rounded up to the next whole kg. Per-parcel figures are shown as an allocated review share rather than a carrier tariff." if (stage3_row.get("actual_service") or "") == "Couriers Please" else ""),
        ("Alternative cost basis note", "Carrier costing is shown using the best available rule for the selected service."),
        ("Destination", f"{stage3_row.get('dest_suburb')}, {stage3_row.get('dest_state')} {stage3_row.get('dest_postcode')}"),
        ("Allied zone", stage3_row.get("allied_zone") or ""),
        ("Allied zone source", stage3_row.get("allied_zone_source") or ""),
        ("CP zone", stage3_row.get("cp_zone") or ""),
        ("AP zone", stage3_row.get("ap_zone") or ""),
        ("AP pricing zone", stage3_row.get("ap_pricing_zone") or ""),
        ("GoEXPRESS zone", stage3_row.get("goexpress_zone") or ""),
        ("GoEXPRESS zone source", stage3_row.get("goexpress_zone_source") or ""),
        ("Actual parcel count", _to_int(stage3_row.get("actual_parcel_count")) or 0),
        ("Estimated parcel count", _to_int(stage3_row.get("estimated_parcel_count")) or 0),
        ("Parcel validation match", "Y" if (str(validation_row.get("est_parcel_count_match") or "").upper() == "Y") else ""),
        ("Parcelizer scope", validation_row.get("est_parcelization_scope") or ""),
        ("Pack process group", validation_row.get("est_pack_process_group") or ""),
        ("Parcel detail rows", len(stage2_rows)),
        ("Packed SKU rows", packed_sku_rows),
        ("Packed SKU units", parcel_units_total),
        ("Packed SKU rows missing CFC weight", _sum_int(stage2_rows, "cfc_weight_missing_units")),
        ("Packed SKU units missing CFC weight", _sum_int(stage2_rows, "cfc_weight_missing_units")),
        ("Packed SKU rows missing CFC dims", _sum_int(stage2_rows, "cfc_dims_missing_units")),
        ("Packed SKU units missing CFC dims", _sum_int(stage2_rows, "cfc_dims_missing_units")),
        ("Packed SKU rows missing both CFC and PLM dims", _sum_int(stage2_rows, "both_dims_missing_units")),
        ("Packed SKU units missing both CFC and PLM dims", _sum_int(stage2_rows, "both_dims_missing_units")),
        ("Imputed parcel rows", _sum_int(stage2_rows, "cfc_cube_imputed")),
        ("Imputed parcel row pct", _pct(_sum_int(stage2_rows, "cfc_cube_imputed"), len(stage2_rows))),
        ("Packed SKU weight kg", packed_sku_weight),
        ("Packed SKU cube cm3", packed_sku_cube_cm3),
        ("Packed SKU cube m3", packed_sku_cube_m3),
        ("Estimated box + dunnage weight kg", _to_float(stage3_row.get("est_box_dunnage_weight_kg"))),
        ("Estimated box + dunnage cube cm3", _to_float(stage3_row.get("shippit_vs_estimated_cube_gap_cm3"))),
        ("Estimated box + dunnage cube m3", _to_float(stage3_row.get("shippit_vs_estimated_cube_gap_m3"))),
        ("Shippit chargeable kg", shippit_chargeable),
        ("Shippit dead weight kg", shippit_dead),
        ("Shippit volumetric kg", shippit_vol_weight),
        ("Shippit volumetric cube m3", shippit_vol_cube / 1_000_000.0 if shippit_vol_cube is not None else None),
        ("Fill vs Shippit cube", fill_pct / 100.0 if fill_pct is not None and fill_pct > 1 else fill_pct),
        ("Air vs Shippit cube", air_pct / 100.0 if air_pct is not None and air_pct > 1 else air_pct),
        ("Odd allocation flag", "Y" if _odd_allocation_flag(stage2_rows) else ""),
        ("Actual est cost ex GST", _to_float(stage3_row.get("actual_est_cost_ex_gst"))),
        ("Cheapest alt cost ex GST", _to_float(stage3_row.get("cheapest_alt_cost_ex_gst"))),
        ("Estimated saving ex GST", _to_float(stage3_row.get("est_saving_ex_gst"))),
        ("Cheaper option exists", stage3_row.get("cheaper_option_exists") or ""),
        ("Review commentary", _summary_commentary(stage3_row, stage2_rows)),
    ]
    rows.extend(size_breakdown)

    sheet.append(["Metric", "Value"])
    _style_header(sheet)
    for row in rows:
        sheet.append(list(row))
    _autosize(sheet)
    _format_currency(sheet, ["Actual est cost ex GST", "Cheapest alt cost ex GST", "Estimated saving ex GST"])


def _write_parcel_validation_sheet(
    workbook: Workbook,
    site_label: str,
    invoice_number: str,
    stage2_rows: list[dict[str, str]],
    validation_row: dict[str, str],
    stage3_row: dict[str, str],
) -> None:
    sheet = workbook.create_sheet("Parcel Validation")
    _style_title(sheet, "Parcel validation and air ratio review", site_label)
    rows = [
        ("Site", site_label),
        ("Invoice number", invoice_number),
        ("Parcelization scope", validation_row.get("est_parcelization_scope") or ""),
        ("Pack process group", validation_row.get("est_pack_process_group") or ""),
        ("Parcel count match", validation_row.get("est_parcel_count_match") or ""),
        ("Parcel detail rows", len(stage2_rows)),
        ("Total parcel units", _sum_int(stage2_rows, "parcel_units")),
        ("Estimated parcel count", _to_int(validation_row.get("est_parcel_count")) or 0),
        ("Shippit parcel count", _to_int(validation_row.get("shippit_parcel_count")) or 0),
        ("Shippit dead weight kg", _to_float(validation_row.get("shippit_dead_weight_kg"))),
        ("Shippit volumetric kg", _to_float(validation_row.get("shippit_volumetric_weight_kg"))),
        ("Shippit chargeable kg", _to_float(validation_row.get("shippit_chargeable_weight_kg"))),
        ("Estimated parcel weight kg", _to_float(validation_row.get("est_total_parcel_weight_kg"))),
        ("Estimated parcel cube cm3", _to_float(validation_row.get("est_total_parcel_cube_cm3"))),
        ("Estimated chargeable weight kg", _to_float(validation_row.get("est_chargeable_weight_kg"))),
        ("Fill vs Shippit cube", _to_float(validation_row.get("est_fill_pct_vs_shippit_cube"))),
        ("Air ratio vs Shippit cube", _to_float(validation_row.get("est_air_ratio_pct_vs_shippit_cube"))),
        ("Odd allocation flag", validation_row.get("est_flag_odd_allocation") or ""),
        ("Review commentary", _validation_commentary(validation_row, stage2_rows, stage3_row)),
    ]
    sheet.append(["Metric", "Value"])
    _style_header(sheet)
    for row in rows:
        sheet.append(list(row))
    _autosize(sheet)


def _write_parcel_detail_sheet(
    workbook: Workbook,
    site_label: str,
    invoice_number: str,
    stage2_rows: list[dict[str, str]],
    validation_row: dict[str, str],
) -> None:
    sheet = workbook.create_sheet("Parcel Detail")
    _style_title(sheet, "Parcel assignment and physical detail", site_label)
    headers = [
        "parcel_id",
        "parcel_units",
        "parcel_weight_kg",
        "parcel_cube_cm3",
        "parcel_cube_m3",
        "parcel_fill_pct_vs_shippit_cube",
        "parcel_air_ratio_pct_vs_shippit_cube",
        "fit_band",
        "sku_ids",
        "parcel_review_commentary",
    ]
    sheet.append(headers)
    _style_header(sheet)
    shippit_cube_cm3 = _to_float(validation_row.get("shippit_volumetric_cube_cm3")) or 0.0
    for row in sorted(stage2_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0):
        cube_cm3 = _to_float(row.get("parcel_cube_cm3")) or 0.0
        fill = (cube_cm3 * 100.0 / shippit_cube_cm3) if shippit_cube_cm3 else None
        air = (100.0 - fill) if fill is not None else None
        commentary = []
        if fill is not None and fill <= 20:
            commentary.append("Very airy versus Shippit cube; check carton choice and dunnage.")
        elif fill is not None and fill >= 80:
            commentary.append("Tight fill versus Shippit cube.")
        if str(row.get("cfc_cube_imputed") or "").upper() == "Y":
            commentary.append("Cube was imputed from invoice-average CFC data.")
        sheet.append(
            [
                _to_int(row.get("parcel_id")) or 0,
                _to_int(row.get("parcel_units")) or 0,
                _to_float(row.get("parcel_weight_kg")),
                cube_cm3,
                cube_cm3 / 1_000_000.0,
                fill / 100.0 if fill is not None and fill > 1 else fill,
                air / 100.0 if air is not None and air > 1 else air,
                row.get("fit_band") or "",
                row.get("sku_ids") or "",
                " ".join(commentary),
            ]
        )
    _autosize(sheet)


def _write_cost_detail_sheet(
    workbook: Workbook,
    site_label: str,
    invoice_number: str,
    stage2_rows: list[dict[str, str]],
    stage3_row: dict[str, str],
) -> None:
    sheet = workbook.create_sheet("Cost Detail")
    _style_title(sheet, "Carrier costing detail", site_label)
    headers = [
        "parcel_id",
        "parcel_units",
        "parcel_weight_kg",
        "parcel_cube_cm3",
        "parcel_fill_pct_vs_shippit_cube",
        "parcel_air_ratio_pct_vs_shippit_cube",
        "cost_share_basis",
        "cost_share_pct",
        "actual_est_cost_allocated_ex_gst",
        "cheapest_alt_cost_allocated_ex_gst",
        "sku_ids",
    ]
    sheet.append(headers)
    _style_header(sheet)

    total_weight = sum(_to_float(row.get("parcel_weight_kg")) or 0.0 for row in stage2_rows)
    actual_cost = _to_float(stage3_row.get("actual_est_cost_ex_gst"))
    alt_cost = _to_float(stage3_row.get("cheapest_alt_cost_ex_gst"))
    shippit_cube_cm3 = _to_float(stage3_row.get("shippit_volumetric_cube_cm3")) or 0.0

    for row in sorted(stage2_rows, key=lambda item: _to_int(item.get("parcel_id")) or 0):
        weight = _to_float(row.get("parcel_weight_kg")) or 0.0
        cube_cm3 = _to_float(row.get("parcel_cube_cm3")) or 0.0
        share = (weight / total_weight) if total_weight else 0.0
        fill = (cube_cm3 * 100.0 / shippit_cube_cm3) if shippit_cube_cm3 else None
        air = (100.0 - fill) if fill is not None else None
        sheet.append(
            [
                _to_int(row.get("parcel_id")) or 0,
                _to_int(row.get("parcel_units")) or 0,
                weight,
                cube_cm3,
                fill / 100.0 if fill is not None and fill > 1 else fill,
                air / 100.0 if air is not None and air > 1 else air,
                "WEIGHT",
                share,
                actual_cost * share if actual_cost is not None else None,
                alt_cost * share if alt_cost is not None else None,
                row.get("sku_ids") or "",
            ]
        )
    _autosize(sheet)
    _format_currency(sheet, ["actual_est_cost_allocated_ex_gst", "cheapest_alt_cost_allocated_ex_gst"])


def _write_sku_review_sheet(
    workbook: Workbook,
    site_label: str,
    invoice_number: str,
    stage1_rows: list[dict[str, str]],
) -> None:
    sheet = workbook.create_sheet("SKU Review")
    _style_title(sheet, "Ordered keycodes and dimension checks", site_label)

    counts: Counter[str] = Counter()
    for row in stage1_rows:
        qty = _to_int(row.get("parcel_units")) or 0
        skus = _split_ids(row.get("sku_ids") or row.get("skus") or "")
        if not skus:
            continue
        per_sku = max(1, round(qty / len(skus))) if qty else 1
        for sku in skus:
            counts[sku] += per_sku

    headers = [
        "sku_id",
        "qty_packed",
        "product_description",
        "size_category",
        "department",
        "rbu",
        "major_group",
        "department_description",
        "class_description",
        "cfc_size_category",
        "plm_size_category",
        "plm_expected",
        "cfc_height_cm",
        "cfc_width_cm",
        "cfc_length_cm",
        "cfc_weight_kg",
        "plm_height_cm",
        "plm_width_cm",
        "plm_length_cm",
        "plm_weight_kg",
        "review_commentary",
    ]
    sheet.append(headers)
    _style_header(sheet)

    for sku, qty in sorted(counts.items(), key=lambda item: item[0]):
        sheet.append(
            [
                sku,
                qty,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "Offline patch built from refreshed CSV outputs; full SKU master lookup was not available in this direct rebuild.",
            ]
        )
    if not counts:
        sheet.append(["No SKU rows found"])
    _autosize(sheet)


def _write_notes_sheet(workbook: Workbook, site_label: str, invoice_number: str) -> None:
    sheet = workbook.create_sheet("Notes")
    _style_title(sheet, "How to read this workbook", site_label)
    sheet.append(["Step", "Guidance"])
    _style_header(sheet)
    rows = [
        ("1. Start with Parcel Validation", "Check whether the estimated parcel count matches Shippit, whether the fill and air ratios look sensible, and whether the split looks odd."),
        ("2. Then open Parcel Detail", "Look at the parcel weights, cubes, commentary, and parcel sizes to see whether the pack looks balanced or concentrated into one parcel."),
        ("3. Then review SKU Review", "Compare CFC and PLM dims where available. Apparel-like keycodes may naturally have missing PLM dims."),
        ("4. Finish with Cost Detail", "Use the carrier breakdown to see what drove the estimated cost and whether the cheapest alternative makes commercial sense."),
        ("5. Treat commentary as a starting point", "The comments are designed to help Ops challenge the logic, not to claim the first-pass answer is final."),
        ("6. Offline patch", "This workbook was rebuilt from the refreshed stage 1, stage 2, and stage 3 CSV outputs because the interactive drilldown login flow was not available."),
    ]
    for row in rows:
        sheet.append(list(row))
    sheet["A2"] = f"Invoice {invoice_number} for {site_label}"
    sheet.merge_cells("A2:B2")
    sheet["A2"].fill = SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    _autosize(sheet)
    sheet.column_dimensions["A"].width = 36
    sheet.column_dimensions["B"].width = 110


def _style_title(sheet, title: str, site_label: str) -> None:
    sheet["A1"] = title
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A1"].fill = TITLE_FILL
    sheet.merge_cells("A1:B1")
    sheet["A2"] = (
        "This drilldown is a starting point. Use the parcel validation first, then check the parcel detail and SKU review to decide whether the issue sits in the split logic, source dimensions, packaging uplift, or a carrier assumption."
    )
    sheet["A2"].fill = SUBTITLE_FILL
    sheet["A2"].font = Font(italic=True)
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells("A2:B2")
    sheet.row_dimensions[2].height = 38


def _style_header(sheet) -> None:
    for cell in sheet[3]:
        if cell.value is None:
            continue
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _autosize(sheet) -> None:
    widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            widths[cell.column] = max(widths.get(cell.column, 0), len(str(cell.value)))
    for col_idx, width in widths.items():
        sheet.column_dimensions[get_column_letter(col_idx)].width = min(max(width + 2, 12), 60)


def _format_currency(sheet, headers: list[str]) -> None:
    header_row = 4
    for row in sheet.iter_rows(min_row=header_row):
        for cell in row:
            if cell.column <= len(row) and sheet.cell(header_row - 1, cell.column).value in headers and isinstance(cell.value, (int, float)):
                cell.number_format = "$#,##0.00"


def _load_rows_generic(path: Path, invoice_number: str) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("invoice_number") or row.get("shippit_invoice_number") or "").strip() == invoice_number:
                rows.append(row)
    return rows


def _sum_int(rows: list[dict[str, str]], key: str) -> int:
    total = 0
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        try:
            total += int(float(value))
        except ValueError:
            continue
    return total


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _split_ids(value: str) -> list[str]:
    value = str(value or "").strip()
    if not value:
        return []
    parts = [part.strip() for part in value.replace("|", ",").split(",")]
    return [part for part in parts if part]


def _pct(value: int, denom: int) -> float:
    return (value * 100.0 / denom) if denom else 0.0


def _size_breakdown(stage2_rows: list[dict[str, str]]) -> list[tuple[str, object]]:
    buckets = defaultdict(int)
    for row in stage2_rows:
        fit = str(row.get("fit_band") or "").upper()
        units = _to_int(row.get("parcel_units")) or 0
        if "TOTE" in fit:
            buckets["Toteable"] += units
        elif "BULKY" in fit:
            buckets["Bulky"] += units
        elif not fit:
            buckets["No Dims"] += units
        else:
            buckets["Non-Toteable"] += units
    return [
        ("Size category", "Total units"),
        ("Toteable", buckets.get("Toteable", 0)),
        ("Non-Toteable", buckets.get("Non-Toteable", 0)),
        ("Bulky", buckets.get("Bulky", 0)),
        ("No Dims", buckets.get("No Dims", 0)),
        ("Other / Unknown", 0),
    ]


def _odd_allocation_flag(stage2_rows: list[dict[str, str]]) -> bool:
    units = [_to_int(row.get("parcel_units")) or 0 for row in stage2_rows]
    if len(units) <= 1:
        return False
    if max(units) - min(units) >= max(3, round((sum(units) / len(units)) * 1.5)):
        return True
    return False


def _summary_commentary(stage3_row: dict[str, str], stage2_rows: list[dict[str, str]]) -> str:
    commentary = []
    if _odd_allocation_flag(stage2_rows):
        commentary.append("Multi-parcel order with an odd allocation pattern.")
    if (stage3_row.get("actual_service") or "") == "Couriers Please":
        commentary.append("Couriers Please is billed at consignment level on rounded chargeable weight; parcel cost shares are for review only.")
    if _to_float(stage3_row.get("est_box_dunnage_weight_kg")) and _to_float(stage3_row.get("est_box_dunnage_weight_kg")) > 0.5:
        commentary.append("Estimated box and dunnage uplift is material.")
    return " ".join(commentary)


def _validation_commentary(validation_row: dict[str, str], stage2_rows: list[dict[str, str]], stage3_row: dict[str, str]) -> str:
    pieces = []
    if validation_row.get("est_flag_odd_allocation") == "Y":
        pieces.append("Odd allocation flag is raised.")
    if _to_float(validation_row.get("est_air_ratio_pct_vs_shippit_cube")) is not None:
        air = _to_float(validation_row.get("est_air_ratio_pct_vs_shippit_cube")) or 0.0
        if air >= 80:
            pieces.append("Very airy versus Shippit cube.")
        elif air <= 20:
            pieces.append("Tight fill versus Shippit cube.")
    if (stage3_row.get("actual_service") or "") == "Couriers Please":
        pieces.append("CP actual cost is invoice-level on rounded chargeable weight.")
    return " ".join(pieces)


if __name__ == "__main__":
    main()
