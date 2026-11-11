from pathlib import Path

import pytest

from src.fuel_surcharge_validation import (
    invoice_validated_rates,
    validate_model_rules,
)


def _rule(**overrides: str) -> dict[str, str]:
    row = {
        "carrier": "Australia Post",
        "service": "eParcel",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "fuel_surcharge_pct": "12.30",
        "source_note": "Published",
    }
    row.update(overrides)
    return row


def test_model_rules_reject_overlap_and_invalid_rate() -> None:
    errors = validate_model_rules(
        [
            _rule(),
            _rule(start_date="2026-07-31", fuel_surcharge_pct="not-a-rate"),
        ]
    )

    assert any("overlap" in error for error in errors)
    assert any("between 0 and 100" in error for error in errors)


def test_ap_validation_uses_base_row_fuel_field_once() -> None:
    test_directory = Path("outputs/carrier_invoices/_test_fsc_validation_input")
    test_directory.mkdir(parents=True, exist_ok=True)
    source = test_directory / "australia_post_charge_lines.csv"
    try:
        source.write_text(
            "carrier,material,service_date,amount_excl_tax,fuel_surcharge_field_value\n"
            "Australia Post,B5S,2026-07-15,100,12.30\n"
            "Australia Post,D05,2026-07-15,12.30,0\n"
            "Australia Post,B5S,2026-07-16,50,6.15\n",
            encoding="utf-8",
        )

        result = invoice_validated_rates([_rule()], test_directory)

        assert result[0]["invoice_validated_fsc_pct"] == 12.3
        assert result[0]["invoice_base_cost_ex_gst"] == 150.0
        assert result[0]["invoice_fuel_cost_ex_gst"] == pytest.approx(18.45)
        assert result[0]["invoice_evidence_count"] == 2
        assert "D05/DS1" in result[0]["invoice_validation_source"]
    finally:
        source.unlink(missing_ok=True)
        test_directory.rmdir()


def test_date_less_allied_evidence_is_not_matched_to_dated_rule() -> None:
    test_directory = Path("outputs/carrier_invoices/_test_fsc_validation_input")
    test_directory.mkdir(parents=True, exist_ok=True)
    source = test_directory / "allied_jobs.csv"
    try:
        source.write_text(
            "service_code,actual_base_cost_ex_gst,actual_fuel_surcharge_ex_gst\n"
            "R,100,10\n",
            encoding="utf-8",
        )

        result = invoice_validated_rates(
            [_rule(carrier="Allied Express", service="R")],
            test_directory,
        )

        assert result[0]["invoice_validated_fsc_pct"] is None
        assert result[0]["invoice_evidence_count"] == 0
    finally:
        source.unlink(missing_ok=True)
        test_directory.rmdir()


def test_gopeople_fuel_field_fallback_treats_value_as_dollars() -> None:
    test_directory = Path("outputs/carrier_invoices/_test_fsc_validation_input")
    test_directory.mkdir(parents=True, exist_ok=True)
    source = test_directory / "gopeople_charge_lines.csv"
    try:
        source.write_text(
            "consignment_date,charge_ex_gst,invoice_notes_or_fuel,actual_base_cost_ex_gst,actual_fuel_surcharge_ex_gst\n"
            "17/08/2026,3.81,0.22,,\n"
            "17/08/2026,3.59,,,0\n"
            "17/08/2026,4.00,,,\n",
            encoding="utf-8",
        )

        result = invoice_validated_rates(
            [_rule(carrier="GoPeople", service="GoEXPRESS", start_date="2026-08-17", end_date="2026-08-30", fuel_surcharge_pct="6.14")],
            test_directory,
        )

        assert result[0]["invoice_validated_fsc_pct"] == pytest.approx(6.128134)
        assert result[0]["invoice_base_cost_ex_gst"] == pytest.approx(3.59)
        assert result[0]["invoice_fuel_cost_ex_gst"] == pytest.approx(0.22)
    finally:
        source.unlink(missing_ok=True)
        test_directory.rmdir()
