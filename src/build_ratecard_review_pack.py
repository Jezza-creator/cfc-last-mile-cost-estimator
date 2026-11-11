from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter


OUTPUT_PATH = Path("outputs/ratecard_review_pack.xlsx")
SOURCE_FILES = [
    ("Moorebank", Path("outputs/stage3_moorebank_cost_estimates_full.csv")),
    ("Truganina", Path("outputs/stage3_truganina_cost_estimates_full.csv")),
]


@dataclass(frozen=True)
class Stage3Row:
    store_name: str
    invoice_number: str
    actual_carrier: str
    actual_service: str
    actual_parcel_count: int
    estimated_parcel_count: int
    actual_est_cost_ex_gst: float | None
    cheapest_alt_cost_ex_gst: float | None
    est_saving_ex_gst: float | None
    actual_est_cost_missing_reason: str
    actual_order_chargeable_kg: float | None
    estimated_order_chargeable_kg: float | None
    parcel_cube_total_cm3: float | None
    parcel_volumetric_equivalent_kg: float | None
    pack_flow_summary: str
    assignment_scope: str
    costing_grain: str
    reporting_weight_band: str
    box_suite_source: str
    box_suite_family: str
    box_suite_band: str


def main() -> None:
    rows = _load_rows()
    workbook = Workbook()
    workbook.remove(workbook.active)

    _write_executive_summary(workbook, rows)
    _write_carrier_review(workbook, rows)
    _write_missing_reasons(workbook, rows)
    _write_allied_pricing_hierarchy(workbook)
    _write_assumptions(workbook)
    _write_data_required(workbook)
    _write_source_register(workbook)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(OUTPUT_PATH)
    print(f"RATECARD_REVIEW_PACK_EXPORTED={OUTPUT_PATH}")


def _load_rows() -> list[Stage3Row]:
    rows: list[Stage3Row] = []
    for store_name, path in SOURCE_FILES:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows.append(
                    Stage3Row(
                        store_name=store_name,
                        invoice_number=str(row.get("invoice_number") or ""),
                        actual_carrier=str(row.get("actual_carrier") or ""),
                        actual_service=str(row.get("actual_service") or ""),
                        actual_parcel_count=int(float(row.get("actual_parcel_count") or 0)),
                        estimated_parcel_count=int(float(row.get("estimated_parcel_count") or 0)),
                        actual_est_cost_ex_gst=_to_float(row.get("actual_est_cost_ex_gst")),
                        cheapest_alt_cost_ex_gst=_to_float(row.get("cheapest_alt_cost_ex_gst")),
                        est_saving_ex_gst=_to_float(row.get("est_saving_ex_gst")),
                        actual_est_cost_missing_reason=str(row.get("actual_est_cost_missing_reason") or ""),
                        actual_order_chargeable_kg=_to_float(row.get("actual_order_chargeable_kg")),
                        estimated_order_chargeable_kg=_to_float(row.get("estimated_order_chargeable_kg")),
                        parcel_cube_total_cm3=_to_float(row.get("parcel_cube_total_cm3")),
                        parcel_volumetric_equivalent_kg=_to_float(row.get("parcel_volumetric_equivalent_kg")),
                        pack_flow_summary=str(row.get("pack_flow_summary") or ""),
                        assignment_scope=str(row.get("assignment_scope") or ""),
                        costing_grain=str(row.get("costing_grain") or ""),
                        reporting_weight_band=str(row.get("reporting_weight_band") or ""),
                        box_suite_source=str(row.get("box_suite_source") or ""),
                        box_suite_family=str(row.get("box_suite_family") or ""),
                        box_suite_band=str(row.get("box_suite_band") or ""),
                    )
                )
    return rows


def _write_executive_summary(workbook: Workbook, rows: list[Stage3Row]) -> None:
    sheet = workbook.create_sheet("Executive Summary")
    sheet.freeze_panes = "A2"
    sheet.append(
        [
            "Store",
            "Carrier",
            "Orders",
            "Single Pack",
            "Multi Pack",
            "Avg Actual Cost",
            "Avg Alt Cost",
            "Avg Saving",
            "Cheaper Option %",
            "Missing Cost %",
            "Top Missing Reason",
            "Risk Note",
        ]
    )
    _style_header(sheet)

    grouped: dict[tuple[str, str], list[Stage3Row]] = defaultdict(list)
    for row in rows:
        grouped[(row.store_name, row.actual_carrier)].append(row)

    for (store_name, carrier), group_rows in sorted(grouped.items()):
        orders = len(group_rows)
        single_pack = sum(1 for row in group_rows if row.assignment_scope == "Single package")
        multi_pack = orders - single_pack
        actual_costs = [row.actual_est_cost_ex_gst for row in group_rows if row.actual_est_cost_ex_gst is not None]
        alt_costs = [row.cheapest_alt_cost_ex_gst for row in group_rows if row.cheapest_alt_cost_ex_gst is not None]
        savings = [row.est_saving_ex_gst for row in group_rows if row.est_saving_ex_gst is not None]
        cheaper = sum(1 for row in group_rows if row.est_saving_ex_gst is not None and row.est_saving_ex_gst > 0)
        missing = [row for row in group_rows if row.actual_est_cost_missing_reason]
        missing_pct = (len(missing) * 100.0 / orders) if orders else 0.0
        top_missing_reason = Counter(row.actual_est_cost_missing_reason for row in missing).most_common(1)
        top_reason = top_missing_reason[0][0] if top_missing_reason else ""

        risk_note = ""
        if carrier == "GoEXPRESS":
            risk_note = "Under-costed trend to review"
        elif carrier == "Allied Express":
            risk_note = "Check zone and charge basis"
        elif top_reason:
            risk_note = f"Review {top_reason}"

        sheet.append(
            [
                store_name,
                carrier,
                orders,
                single_pack,
                multi_pack,
                _avg(actual_costs),
                _avg(alt_costs),
                _avg(savings),
                _pct(cheaper, orders),
                round(missing_pct, 2),
                top_reason,
                risk_note,
            ]
        )

    _autosize(sheet)


def _write_carrier_review(workbook: Workbook, rows: list[Stage3Row]) -> None:
    sheet = workbook.create_sheet("Carrier Review")
    sheet.freeze_panes = "A2"
    sheet.append(
        [
            "Store",
            "Carrier",
            "Service",
            "Orders",
            "Actual Parcels",
            "Est Parcels",
            "Avg Actual Kg",
            "Avg Est Kg",
            "Avg Cube cm3",
            "Avg Volumetric Kg",
            "Avg Actual Cost",
            "Avg Alt Cost",
            "Avg Saving",
            "Cheaper Option %",
            "Missing Cost Reasons",
            "Box Suite Source",
            "Box Suite Family",
            "Box Suite Band",
        ]
    )
    _style_header(sheet)

    grouped: dict[tuple[str, str, str], list[Stage3Row]] = defaultdict(list)
    for row in rows:
        grouped[(row.store_name, row.actual_carrier, row.actual_service)].append(row)

    for (store_name, carrier, service), group_rows in sorted(grouped.items()):
        orders = len(group_rows)
        actual_parcels = sum(row.actual_parcel_count for row in group_rows)
        est_parcels = sum(row.estimated_parcel_count for row in group_rows)
        missing_reasons = Counter(row.actual_est_cost_missing_reason for row in group_rows if row.actual_est_cost_missing_reason)
        top_reasons = "; ".join(f"{reason}:{count}" for reason, count in missing_reasons.most_common(3))
        sheet.append(
            [
                store_name,
                carrier,
                service,
                orders,
                actual_parcels,
                est_parcels,
                _avg([row.actual_order_chargeable_kg for row in group_rows]),
                _avg([row.estimated_order_chargeable_kg for row in group_rows]),
                _avg([row.parcel_cube_total_cm3 for row in group_rows]),
                _avg([row.parcel_volumetric_equivalent_kg for row in group_rows]),
                _avg([row.actual_est_cost_ex_gst for row in group_rows]),
                _avg([row.cheapest_alt_cost_ex_gst for row in group_rows]),
                _avg([row.est_saving_ex_gst for row in group_rows]),
                _pct(sum(1 for row in group_rows if row.est_saving_ex_gst is not None and row.est_saving_ex_gst > 0), orders),
                top_reasons,
                _mode([row.box_suite_source for row in group_rows]),
                _mode([row.box_suite_family for row in group_rows]),
                _mode([row.box_suite_band for row in group_rows]),
            ]
        )

    _autosize(sheet)


def _write_missing_reasons(workbook: Workbook, rows: list[Stage3Row]) -> None:
    sheet = workbook.create_sheet("Missing Reasons")
    sheet.freeze_panes = "A2"
    sheet.append(["Store", "Reason", "Orders", "Pct", "Carrier Focus", "What to check"])
    _style_header(sheet)

    grouped: dict[tuple[str, str], list[Stage3Row]] = defaultdict(list)
    for row in rows:
        if row.actual_est_cost_missing_reason:
            grouped[(row.store_name, row.actual_est_cost_missing_reason)].append(row)

    for (store_name, reason), group_rows in sorted(grouped.items(), key=lambda item: (item[0][0], -len(item[1]), item[0][1])):
        orders = len(group_rows)
        carriers = Counter(row.actual_carrier for row in group_rows).most_common(3)
        carrier_focus = ", ".join(f"{carrier}:{count}" for carrier, count in carriers)
        sheet.append(
            [
                store_name,
                reason,
                orders,
                round((orders * 100.0 / max(len([r for r in rows if r.store_name == store_name]), 1)), 2),
                carrier_focus,
                _reason_action(reason),
            ]
        )

    _autosize(sheet)


def _write_allied_pricing_hierarchy(workbook: Workbook) -> None:
    sheet = workbook.create_sheet("Allied Pricing")
    sheet.freeze_panes = "A2"
    sheet.append(["Layer", "Rule", "What it means", "Source / note"])
    _style_header(sheet)

    rows = [
        (
            "1. Base lane rate",
            "Use the Allied road lane table for the from-zone to to-zone pair.",
            "Allied pricing starts with a route-based lane price, not a generic national rate.",
            "Proposed B2C Road - [26B2CRX05VF-KMA]",
        ),
        (
            "2. Chargeable weight",
            "Charge on the greater of dead weight or cubic weight.",
            "If the parcel cubes out heavier than it weighs, the cubic basis wins.",
            "ECOM-ASVF-V4-KMART",
        ),
        (
            "3. Cubic divisor",
            "Use 250 kg per cubic metre for cubic weight.",
            "Parcel cube must be converted using the Allied divisor before lane pricing.",
            "ECOM-ASVF-V4-KMART",
        ),
        (
            "4. Charge breakout",
            "Break the Allied estimate into lane, remote, overweight, oversized and other surcharge buckets.",
            "This makes the commercial drivers visible for ops and finance review.",
            "ECOM-ASVF-V4-KMART",
        ),
        (
            "5. Minimum charge",
            "Apply the lane minimum charge after the basic + per-kg calculation.",
            "No parcel should fall below the lane minimum.",
            "Proposed B2C Road - [26B2CRX05VF-KMA]",
        ),
        (
            "6. Remote area overlay",
            "If the postcode/suburb matches the remote area schedule, add the surcharge.",
            "Remote or island destinations can carry a large fixed or per-kg uplift.",
            "ECOM-RAS-KMA-0726",
        ),
        (
            "7. Additional services",
            "Apply overweight, oversized, tail lift, hand unload, waiting time, and other surcharges where applicable.",
            "These charges sit on top of the base freight cost and can materially change the order total.",
            "ECOM-ASVF-V4-KMART",
        ),
        (
            "8. Reverse logistics",
            "Apply the 30% reverse logistics surcharge to the relevant rate card components.",
            "Reverse moves are priced above the standard rate card to reflect added handling.",
            "ECOM-ASVF-V4-KMART",
        ),
        (
            "9. Fuel and GST",
            "Keep fuel surcharge and GST separate unless a source sheet explicitly says otherwise.",
            "The Allied sheets are exclusive of GST and fuel unless otherwise noted.",
            "Commercial note",
        ),
    ]

    for row in rows:
        sheet.append(list(row))

    sheet.append([])
    sheet.append(["Practical order of application", "Base lane", "Chargeable weight", "Remote area", "Additional services"])
    sheet.append(
        [
            "",
            "Start with the lane rate for the destination zone.",
            "Use max(dead weight, cubic weight).",
            "Add remote surcharge if postcode/suburb matches.",
            "Apply any relevant special handling surcharge.",
        ]
    )

    _autosize(sheet)


def _write_assumptions(workbook: Workbook) -> None:
    sheet = workbook.create_sheet("Assumptions")
    sheet.freeze_panes = "A2"
    sheet.append(["Area", "Current overlay/assumption", "Why it is used", "Data required to replace it"])
    _style_header(sheet)

    assumptions = [
        (
            "Parcelization",
            "Pack timestamps plus flow heuristics are used to infer parcels.",
            "The raw pack feed does not explicitly provide a parcel container id for every order.",
            "A reliable parcel/session identifier from WMS or packing system.",
        ),
        (
            "Box suite",
            "Proxy non-CFC box suite is used until a CFC suite is supplied.",
            "We need carton air/overboxing to estimate volumetric cost before CFC packaging data exists.",
            "The approved CFC box suite export with dimensions and service usage.",
        ),
        (
            "GoEXPRESS bands",
            "Current band matches use the loaded ratecard mapping; unmatched bands remain flagged.",
            "Some parcels do not fit the current GoEXPRESS weight-band structure.",
            "Confirmed GoExpress zone and band definitions, including edge cases and effective dates.",
        ),
        (
            "Allied zones",
            "Carrier zone lookups rely on the current Allied zone guide mapping and proxy fallback.",
            "Some orders still miss a matching zone at the order-to-zone stage and need a best guess to keep pricing moving.",
            "Authoritative Allied origin/destination zone guide, remote area matrix, and exception handling notes.",
        ),
        (
            "Allied pricing",
            "Base lane pricing, remote surcharge, and additional services are layered separately.",
            "The Allied commercial structure spans lane rates plus remote and special-handling overlays.",
            "A single consolidated Allied pricing master with effective dates and applicability rules.",
        ),
        (
            "AP over 22kg",
            "Parcel weights are capped at 21.99kg for AP costing when overflow would exceed support.",
            "The current AP pricing logic does not support the higher parcel outcome in the model.",
            "The correct AP over-22kg service rule or a split pricing schedule.",
        ),
        (
            "In Process locations",
            "In Process is ignored as a packing-process indicator.",
            "Those bins often represent waiting putaway or staging rather than parcel composition.",
            "A trusted operational event or status field that separates pack-ready from non-pack-ready work.",
        ),
        (
            "Carrier aliases",
            "Carrier/service aliases are mapped to the model's supported service names.",
            "Shippit, ratecard, and internal naming are not always aligned.",
            "An approved carrier/service master with canonical names and aliases.",
        ),
    ]

    for row in assumptions:
        sheet.append(list(row))

    _autosize(sheet)


def _write_data_required(workbook: Workbook) -> None:
    sheet = workbook.create_sheet("Data Required")
    sheet.freeze_panes = "A2"
    sheet.append(["Priority", "Data required", "Why it matters", "Current impact"])
    _style_header(sheet)

    rows = [
        ("High", "CFC box suite export", "Needed to replace proxy carton assumptions", "Volumetric cost accuracy"),
        ("High", "GoExpress zone/band definitions", "Needed to resolve no_goexpress_band_match", "Under-costed GoExpress orders"),
        ("High", "Allied zone guide and lane mapping", "Needed to resolve Allied missing-zone cases", "Allied cost coverage"),
        ("High", "Allied remote area and additional services sheets", "Needed to model remote and surcharge overlays", "Allied price completeness"),
        ("High", "Carrier/service master", "Needed to standardise source names and aliases", "Safer ratecard ingestion"),
        ("Medium", "Packing event/session identifier", "Would reduce dependence on timestamp heuristics", "Better parcelisation"),
        ("Medium", "AP over-22kg supported pricing rule", "Needed for the overflow path", "AP large parcel coverage"),
        ("Medium", "Manual vs automation operational flag", "Would sharpen flow classification", "Better ops review"),
        ("Low", "Pack/putaway status history", "Would allow more precise exclusion of in-process locations", "Cleaner exception handling"),
    ]
    for row in rows:
        sheet.append(list(row))

    _autosize(sheet)


def _write_source_register(workbook: Workbook) -> None:
    sheet = workbook.create_sheet("Source Register")
    sheet.freeze_panes = "A2"
    sheet.append(["File name", "Carrier", "Source type", "Effective date", "Status", "Notes"])
    _style_header(sheet)

    rows = [
        ("AP ratecard - Kmart Australia Limited 05082025.pdf", "Australia Post", "PDF", "2025-08-05", "Loaded", "Source in SharePoint rate cards folder"),
        ("AP Metro to Metro - Kmart MELB 01AUG25.pdf", "Australia Post", "PDF", "2025-08-01", "Loaded", "Source in SharePoint rate cards folder"),
        ("Allied - ZoneGuideCondensed.pdf", "Allied Express", "PDF", "2026-07", "Loaded", "Allied zone guide used for fallback lookups"),
        ("Allied ratecard - Online Rate Schedule 26ECOMRXC5VF-KMA (Proposed)26.xlsx", "Allied Express", "XLSX", "2026-07-06", "Loaded", "Primary Allied road ratecard"),
        ("Allied Additional Services VFF", "Allied Express", "Text / XLSX", "2026-07-06", "Loaded", "Allied surcharge and handling rules"),
        ("Allied Remote Area Surcharge", "Allied Express", "Text / XLSX", "2026-07-06", "Loaded", "Allied remote area surcharge matrix"),
        ("CP - Master_Zone_File_November_2023_Final_2_2a874cf3a0.xls", "Couriers Please", "XLS", "2023-11", "Loaded", "Zone guide used by current lookup"),
        ("CP ratecard - Moorebank CFC 2026.xlsx", "Couriers Please", "XLSX", "2026", "Loaded", "Current Moorebank carrier ratecard"),
        ("Go People Commercials (Updated June 2026).docx", "GoEXPRESS", "DOCX", "2026-06", "Loaded", "Commercial reference document"),
        ("Proxy_box_suite_data_Non-CFC.24082026.xlsx", "Proxy", "XLSX", "2026-08-24", "Loaded", "Proxy box suite input"),
        ("cfc_box_suite.csv", "CFC", "CSV", "", "Placeholder", "Drop the approved CFC box suite here later"),
    ]
    for row in rows:
        sheet.append(list(row))

    _autosize(sheet)


def _reason_action(reason: str) -> str:
    if reason == "unsupported_carrier":
        return "Confirm if carrier should be supported or remain intentionally excluded"
    if reason == "no_goexpress_band_match_parcel_1" or reason.startswith("no_goexpress_band_match"):
        return "Check GoExpress zone and weight-band definitions"
    if reason == "missing_allied_zone":
        return "Check Allied destination zone guide and lane mapping"
    if reason == "missing_ap_zone":
        return "Check AP postcode/area zone mapping"
    if reason == "no_ap_weight_band_match_under_5kg":
        return "Check AP weight-band table for the under-5kg range"
    if reason == "no_ap_lane_rate_match_over_5kg":
        return "Check AP lane pricing for over-5kg parcels"
    if reason == "ap_over_22kg_not_supported":
        return "Confirm the AP overflow / split-pricing rule"
    return "Review the relevant carrier ratecard or zone mapping"


def _style_header(sheet) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")


def _autosize(sheet) -> None:
    widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            width = len(str(cell.value))
            widths[cell.column] = max(widths.get(cell.column, 0), min(width, 60))
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width + 2


def _avg(values: list[float | None]) -> float:
    numeric = [value for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else 0.0


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator * 100.0) / denominator


def _mode(values: list[str]) -> str:
    cleaned = [value for value in values if value]
    if not cleaned:
        return ""
    return Counter(cleaned).most_common(1)[0][0]


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
