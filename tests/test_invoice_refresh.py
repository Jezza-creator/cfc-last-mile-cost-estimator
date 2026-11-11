from pathlib import Path
import os

import pandas as pd

from src.invoice_refresh import _check_excel_availability, invoice_refresh_plan
from src.reconciliation_store import _incremental_stage3_slice


def test_invoice_refresh_plan_runs_only_the_selected_excel_carrier() -> None:
    plan = invoice_refresh_plan("allied")

    assert [module for module, _ in plan] == [
        "src.carrier_invoice_allied_ingest",
        "src.reconciliation_store",
        "src.carrier_invoice_allied_reconciliation",
        "src.reconciliation_store",
    ]
    assert plan[1][1] == (
        "--mode",
        "export-stage3-history",
        "--dataset-name",
        "live",
    )
    assert plan[-1][1] == (
        "--mode",
        "refresh-invoices",
        "--dataset-name",
        "live",
        "--incremental-carrier",
        "allied",
    )


def test_invoice_refresh_plan_passes_the_ap_date_window() -> None:
    plan = invoice_refresh_plan(
        "australia_post",
        start_date="2026-08-20",
        end_date="2026-09-02",
    )

    assert plan[0] == (
        "src.carrier_invoice_australia_post_ingest",
        ("--start-date", "2026-08-20", "--end-date", "2026-09-02"),
    )
    assert plan[1] == (
        "src.reconciliation_store",
        ("--mode", "export-stage3-history", "--dataset-name", "live"),
    )
    assert plan[2][0] == "src.carrier_invoice_australia_post_reconciliation"
    assert plan[3][1] == (
        "--mode",
        "refresh-invoices",
        "--dataset-name",
        "live",
        "--incremental-carrier",
        "australia_post",
        "--invoice-start-date",
        "2026-08-20",
        "--invoice-end-date",
        "2026-09-02",
    )


def test_invoice_refresh_plan_can_pass_changed_workbook_names() -> None:
    plan = invoice_refresh_plan(
        "allied",
        source_files=("FY2027_Allied_JOB_Postcode_Summary.xlsx",),
    )

    assert plan[-1][1][-2:] == (
        "--invoice-source-file",
        "FY2027_Allied_JOB_Postcode_Summary.xlsx",
    )


def test_invoice_refresh_plan_supports_explicit_full_rematch() -> None:
    plan = invoice_refresh_plan("australia_post", incremental=False)

    assert plan[-1][1] == (
        "--mode",
        "refresh-invoices",
        "--dataset-name",
        "live",
    )


def test_incremental_stage3_slice_restricts_to_carrier_and_invoice_window() -> None:
    stage3 = pd.DataFrame(
        {
            "actual_carrier": ["AUSPOST", "AUSPOST", "COURIERSP"],
            "invoice_number": ["100", "101", "102"],
            "despatch_date": ["2026-08-20", "2026-09-03", "2026-08-20"],
        }
    )

    result = _incremental_stage3_slice(
        stage3,
        "australia_post",
        "2026-08-18",
        "2026-08-31",
        set(),
    )

    assert result["invoice_number"].tolist() == ["100"]


def test_excel_availability_flags_workbook_newer_than_control_summary(tmp_path: Path) -> None:
    input_directory = tmp_path / "carrier_invoices"
    input_directory.mkdir()
    workbook = input_directory / "invoice.xlsx"
    workbook.write_bytes(b"placeholder")
    control_path = tmp_path / "control.csv"
    control_path.write_text("source_file,source_sheet\ninvoice.xlsx,P1W1\n", encoding="utf-8")
    os.utime(control_path, (100, 100))

    status = _check_excel_availability(
        "allied",
        {"input_directory": input_directory, "control_path": control_path},
        tmp_path,
    )

    assert status["can_refresh"] is True
    assert status["new_available"] is True
    assert status["status"] == "New or changed invoice workbook available"
