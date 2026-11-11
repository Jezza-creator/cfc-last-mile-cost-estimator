from src.carrier_invoice_cp_reconciliation import (
    _base_cost_variance_reason,
    _classify_root_cause as classify_cp_root_cause,
    _cp_service_mapping_status,
    _normalise_cp_service,
)
from src.carrier_invoice_gopeople_ingest import _fuel_amount
from src.carrier_invoice_gopeople_reconciliation import _classify_root_cause as classify_gopeople_root_cause, _result as gopeople_result


def test_cp_pwf_invoice_codes_map_to_pa_pricing_families():
    expected = {"PWF0": "PA0", "PWF1": "PA1", "PWF3": "PA3", "PWF5": "PA5"}

    for invoice_code, pricing_family in expected.items():
        assert _normalise_cp_service(invoice_code) == pricing_family
        assert _cp_service_mapping_status(invoice_code) == "PWF_TO_PA"

    assert _normalise_cp_service("PWF7") == "PWF7"
    assert _cp_service_mapping_status("PWF7") == "NOT_MAPPED"
    assert _cp_service_mapping_status("PA3") == "DIRECT_PA"


def test_cp_pwf_to_pa_mapping_does_not_create_service_mismatch():
    reason = _base_cost_variance_reason(
        {"origin_zone": "SYD", "destination_zone": "SYD", "service_code": "PWF3", "chargeable_weight_kg": "1"},
        {"site": "MOOREBANK", "cp_zone": "SYD", "cp_pricing_ratecard_id": "CP_PA3_20260504", "cp_pricing_chargeable_weight_kg": "1"},
        5.00,
        4.00,
    )

    assert reason == "CP_RATE_OR_CHARGE_MISMATCH"


def test_cp_physical_basis_bucket_combines_cube_and_weight_evidence():
    primary, _, shippit_status, root_cause, note = classify_cp_root_cause(
        {
            "manifest_reference": "",
            "origin_zone": "SYD",
            "destination_zone": "SYD",
            "service_code": "PA5",
        },
        {
            "site": "MOOREBANK",
            "cp_zone": "SYD",
            "cp_pricing_ratecard_id": "CP_PA5_20260504",
        },
        "TRACKING_NUMBER",
        69_000,
        66_188,
        12_716,
        17.25,
        3.18,
        4.58,
        3.35,
        0.52,
        0.38,
    )
    assert primary == "SHIPPIT_PHYSICAL_BASIS_MISMATCH"
    assert shippit_status == "SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER"
    assert "SHIPPIT_CUBE_UNDERSTATED" in root_cause
    assert "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH" in root_cause
    assert "CP chargeable weight" in note


def test_gopeople_physical_basis_bucket_exposes_cube_and_weight_gaps():
    primary, estimator_status, shippit_status, root_cause, note = classify_gopeople_root_cause(
        {"destination_postcode": "2061"},
        {"dest_postcode": "2061"},
        "TRACKING_NUMBER",
        1_700,
        5_784.06,
        12_000,
        1.08,
        3.0,
        5.95,
        5.52,
    )
    assert primary == "SHIPPIT_PHYSICAL_BASIS_MISMATCH"
    assert estimator_status == "ESTIMATOR_CUBE_OVERSTATED_VS_CARRIER"
    assert shippit_status == "SHIPPIT_CUBE_OVERSTATED_VS_CARRIER"
    assert "SHIPPIT_CUBE_OVERSTATED" in root_cause
    assert "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH" in root_cause
    assert "GoPeople chargeable weight" in note


def test_gopeople_invoice_fuel_field_is_a_dollar_amount():
    assert _fuel_amount("0.22") == 0.22

    result = gopeople_result(
        {
            "source_file": "invoice.xlsx",
            "source_sheet": "P1W3",
            "carrier_invoice_number": "123",
            "carrier_tracking_number": "TRACK1",
            "consignment_date": "2026-08-17",
            "actual_base_cost_ex_gst": "3.59",
            "actual_fuel_surcharge_ex_gst": "0.22",
            "actual_fuel_surcharge_pct": "6.128134",
            "actual_invoice_cost_ex_gst": "3.81",
            "actual_gst": "0.38",
            "invoice_notes_or_fuel": "0.22",
            "charged_weight_kg": "1",
            "customer_cubic_volume_m3": "0.001",
            "destination_postcode": "2000",
        },
        {
            "site": "MOOREBANK",
            "invoice_number": "ORDER1",
            "carrier_tracking_number": "TRACK1",
            "dest_postcode": "2000",
            "actual_est_base_cost_ex_gst": "3.35",
            "actual_est_fuel_surcharge_ex_gst": "0.10",
            "actual_est_fuel_surcharge_pct": "3.00",
            "actual_est_fuel_surcharge_source": "GoPeople FSC sheet",
            "actual_est_cost_ex_gst": "3.45",
            "cost_weight_basis_kg": "1",
        },
        "TRACKING_NUMBER",
    )

    assert result["actual_base_cost_ex_gst"] == "3.59"
    assert result["actual_fuel_surcharge_ex_gst"] == "0.22"
    assert result["actual_less_modelled_base_cost_ex_gst"] == "0.24"
    assert result["actual_less_modelled_fuel_surcharge_ex_gst"] == "0.12"
    assert "GOPEOPLE_FUEL_SURCHARGE_VARIANCE" in result["root_cause"]
    assert "GOPEOPLE_RATE_OR_CHARGE_MISMATCH" in result["root_cause"]
