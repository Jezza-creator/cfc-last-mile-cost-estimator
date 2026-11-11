from __future__ import annotations

import csv
from pathlib import Path

import openpyxl


SOURCE_PATH = Path(
    r"C:\Users\jbrocher1\OneDrive - Kmart Australia Limited\T - Advanced Analytics - E2E Network Analytics\Last Mile\LM rate cards\CP ratecard - Moorebank CFC 2026.xlsx"
)
OUTPUT_PATH = Path("data/reference/cp_ratecard_moorebank_lane_rates.csv")
RATECARD_PREFIX = "CP_"
RATECARD_SUFFIX = "20260504"
WORKBOOK_REFERENCE = "CP ratecard - Moorebank CFC 2026.xlsx"


def _clean_number(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value).strip()


def main() -> None:
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(f"Source workbook not found: {SOURCE_PATH}")

    wb = openpyxl.load_workbook(SOURCE_PATH, data_only=True, read_only=True)
    if "Sheet1" not in wb.sheetnames:
        raise KeyError(f"Sheet1 not found in {SOURCE_PATH.name}; found {wb.sheetnames}")

    ws = wb["Sheet1"]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "RATECARD_ID",
                "RATE_LINE_ID",
                "SERVICE",
                "PRODUCT_CODE",
                "ORIGIN_ZONE",
                "DESTINATION_ZONE",
                "DESTINATION_DESCRIPTION",
                "FORMULA_TYPE",
                "MINIMUM_CHARGE_EX_GST",
                "BASIC_CHARGE_EX_GST",
                "PER_KG_RATE_EX_GST",
                "PER_ITEM_RATE_EX_GST",
                "FIRST_PARCEL_RATE_EX_GST",
                "SUBSEQUENT_PARCEL_RATE_EX_GST",
                "INCLUDED_WEIGHT_KG",
                "MAX_WEIGHT_KG",
                "WEIGHT_ROUNDING_METHOD",
                "RATE_BASIS",
                "SOURCE_REFERENCE",
                "NOTES",
            ]
        )

        for row in ws.iter_rows(min_row=2, values_only=True):
            ratecard_key = str(row[4] or "").strip()
            ratecard_id = f"{RATECARD_PREFIX}{ratecard_key}_{RATECARD_SUFFIX}" if ratecard_key else ""
            description = str(row[5] or "").strip()
            origin_zone = str(row[6] or "").strip().upper()
            destination_zone = str(row[7] or "").strip().upper()
            if not ratecard_id or not origin_zone or not destination_zone:
                continue
            rate_line_id = f"{ratecard_id}_{origin_zone}_{destination_zone}"
            product_code = ratecard_key
            service = "Parcel" if ratecard_key.startswith("PA") else "Road Express"
            formula_type = "PER_ITEM" if ratecard_key.startswith("PA") else "BASIC_PLUS_PER_KG"
            rate_basis = "PARCEL" if ratecard_key.startswith("PA") else "INVOICE"
            included_weight_kg = _included_weight_kg(ratecard_key)

            writer.writerow(
                [
                    ratecard_id,
                    rate_line_id,
                    service,
                    product_code,
                    origin_zone,
                    destination_zone,
                    description,
                    formula_type,
                    _clean_number(row[8]),
                    _clean_number(row[9]),
                    _clean_number(row[10]),
                    _clean_number(row[12]),
                    "",
                    "",
                    included_weight_kg,
                    included_weight_kg,
                    "CEIL",
                    rate_basis,
                    WORKBOOK_REFERENCE,
                    "Exported from CP workbook sheet 1.",
                ]
            )
            rows_written += 1

    print(f"Wrote {rows_written} rows to {OUTPUT_PATH}")


def _included_weight_kg(ratecard_key: str) -> str:
    return {
        "PA0": "0.5",
        "PA1": "1",
        "PA3": "3",
        "PA5": "5",
    }.get(ratecard_key, "")


if __name__ == "__main__":
    main()
