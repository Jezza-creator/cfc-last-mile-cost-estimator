from src.carrier_invoice_allied_ingest import _aggregate_jobs


def _line(sheet: str, component: str, amount: str) -> dict[str, str]:
    return {
        "carrier": "Allied Express",
        "source_file": "FY2027_Allied_JOB_Postcode_Summary.xlsx",
        "source_sheet": sheet,
        "source_row_number": "6",
        "allied_week_number": "2026/27",
        "docket": "CFCN0001",
        "job_number": "32700001",
        "reference": "Order 654000001",
        "external_order_id": "654000001",
        "origin_suburb": "MOOREBANK",
        "origin_postcode": "2170",
        "destination_suburb": "ZETLAND",
        "destination_postcode": "2017",
        "charged_weight_kg": "22",
        "declared_weight_kg": "20",
        "declared_volume_m3": "0.08",
        "items": "3",
        "service_code": "R",
        "charge_code": component,
        "charge_type": "I",
        "charge_component": component,
        "charge_ex_gst": amount,
        "gst": "0",
        "charge_date": "2026-08-21",
        "transaction_date": "2026-08-21",
    }


def test_allied_job_aggregates_charge_lines_across_weekly_sheets():
    jobs = _aggregate_jobs(
        [
            _line("P1W1", "BASE", "11.94"),
            _line("P2W3", "EXCESS_WEIGHT", "0.40"),
            _line("P2W3", "FUEL", "5.07"),
            _line("P2W3", "OTHER", "5.09"),
        ]
    )

    assert len(jobs) == 1
    assert jobs[0]["source_sheet"] == "P1W1|P2W3"
    assert jobs[0]["charge_line_count"] == "4"
    assert jobs[0]["actual_base_cost_ex_gst"] == "11.94"
    assert jobs[0]["actual_excess_weight_surcharge_ex_gst"] == "0.4"
    assert jobs[0]["actual_fuel_surcharge_ex_gst"] == "5.07"
    assert jobs[0]["actual_other_surcharge_ex_gst"] == "5.09"
    assert jobs[0]["actual_invoice_cost_ex_gst"] == "22.5"
