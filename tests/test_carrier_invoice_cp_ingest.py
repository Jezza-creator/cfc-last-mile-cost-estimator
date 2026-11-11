from openpyxl import Workbook
from pathlib import Path

from src.carrier_invoice_cp_ingest import _deduplicate_charge_lines, _read_charge_lines


def _write_cp_workbook(path, sheets):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets:
        worksheet = workbook.create_sheet(title)
        for row in rows:
            worksheet.append(row)
    workbook.save(path)


def test_cp_ingest_reads_invoice_sheets_and_skips_summary() -> None:
    path = Path(__file__).with_name("_cp_ingest_test.xlsx")
    header = ["Inv. No.", "Consignment Ref.", "Ex GST", "Fuel Surcharge"]
    try:
        _write_cp_workbook(
            path,
            [
                ("K-Summ", [[None, None, None, None]]),
                ("P2W1", [header, ["100", "TRK-1", 10, 1]]),
                ("P2W2", [header, ["100", "TRK-2", 20, 2]]),
            ],
        )

        rows = list(_read_charge_lines(path))

        assert [row["carrier_consignment_reference"] for row in rows] == ["TRK-1", "TRK-2"]
        assert [row["source_sheet"] for row in rows] == ["P2W1", "P2W2"]
    finally:
        path.unlink(missing_ok=True)


def test_cp_ingest_prefers_consolidated_workbook_for_duplicate_consignments() -> None:
    old_row = {
        "source_file": "KMART - InvoicesWK19JUL26.xlsx",
        "source_sheet": "Sheet1",
        "source_row_number": 2,
        "carrier_consignment_reference": "TRK-1",
        "actual_invoice_cost_ex_gst": "10",
    }
    consolidated_row = {
        "source_file": "FY2027 Courier Please Kmart P2.xlsx",
        "source_sheet": "P2W1",
        "source_row_number": 2,
        "carrier_consignment_reference": "TRK-1",
        "actual_invoice_cost_ex_gst": "11",
    }

    rows = _deduplicate_charge_lines([old_row, consolidated_row])

    assert len(rows) == 1
    assert rows[0]["source_file"] == "FY2027 Courier Please Kmart P2.xlsx"
    assert rows[0]["actual_invoice_cost_ex_gst"] == "11"
