from pathlib import Path

from src.carrier_invoice_australia_post_ingest import _aggregate_consignments, _charge_component
from src.carrier_invoice_australia_post_reconciliation import (
    MIN_FUEL_NORM_SAMPLE_COUNT,
    _build_fuel_norms,
    _classify_root_cause,
    _configured_fuel_norm,
    _find_stage3_match,
    _zone_comparison,
)
from src.carrier_invoice_australia_post_sample import _allocate_quotas, _candidate_counts_query, _selected_source_rows_query
from src.invoice_filter import invoice_filter_clause


def _line(**overrides: str) -> dict[str, str]:
    row = {
        "invoice_no": "AP1",
        "billing_date": "2026-07-20",
        "service_date": "2026-07-15",
        "billing_doc": "BD1",
        "assignment_no": "A1",
        "customer_ref_1": "Order 653897174",
        "customer_ref_2": "",
        "customer_ref_3": "",
        "sales_order": "",
        "consignment_id": "T1",
        "material": "B5S",
        "description": "Parcel Post with Signature",
        "charge_code": "3D55",
        "lodgement_zone": "N0",
        "destination_zone": "N0",
        "origin_city": "MOOREBANK",
        "origin_state": "NSW",
        "origin_postcode": "2170",
        "destination_city": "ZETLAND",
        "destination_state": "NSW",
        "destination_postcode": "2017",
        "actual_weight_kg": "1",
        "declared_weight_kg": "1",
        "billed_weight_kg": "1",
        "cubic_weight_kg": "1",
        "billed_length_m": "0.1",
        "billed_width_m": "0.1",
        "billed_height_m": "0.1",
        "declared_length_cm": "10",
        "declared_width_cm": "10",
        "declared_height_cm": "10",
        "actual_length_cm": "0",
        "actual_width_cm": "0",
        "actual_height_cm": "0",
        "amount_excl_tax": "10",
        "tax_amount": "1",
        "amount_incl_tax": "11",
        "invoice_total_excl_tax": "0",
        "invoice_total_incl_tax": "0",
        "actual_base_cost_ex_gst": "0",
        "actual_fuel_surcharge_ex_gst": "0",
        "actual_other_surcharge_ex_gst": "0",
        "actual_invoice_cost_ex_gst": "0",
        "actual_tax_amount": "0",
        "actual_fuel_rate_pct": "",
        "source_invoice_total_excl_tax": "0",
        "source_invoice_total_incl_tax": "0",
        "source_table": "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST",
        "carrier": "Australia Post",
        "source_row_number": "1",
        "customer_ref_1": "Order 653897174",
        "customer_ref_2": "",
        "customer_ref_3": "",
        "sales_order_item": "",
        "article_id": "",
        "qty": "1",
        "total_qty": "1",
        "fuel_surcharge_field_value": "0",
        "fuel_surcharge_discount": "0",
        "fuel_gst": "0",
        "charge_zone": "N0",
        "lodgement_date": "2026-07-15",
        "origin_name": "Kmart",
        "destination_name": "Customer",
        "row_hash": "",
        "charge_component": "BASE",
    }
    row.update(overrides)
    return row


def test_charge_component_uses_material_codes_for_ap_lines():
    assert _charge_component("B5S", "Parcel Post", "3D55") == "BASE"
    assert _charge_component("D05", "AP Parcels Domestic Fuel Surcharge", "") == "FUEL"
    assert _charge_component("BO1", "Over Maximum Limits Fee", "") == "OTHER"


def test_assignment_fuel_is_not_duplicated_when_assignment_has_multiple_consignments():
    rows = [
        _line(consignment_id="T1", amount_excl_tax="10"),
        _line(consignment_id="T2", amount_excl_tax="8", source_row_number="2"),
        _line(consignment_id="", material="D05", description="AP Parcels Domestic Fuel Surcharge", charge_component="FUEL", amount_excl_tax="1", source_row_number="3"),
    ]
    groups = _aggregate_consignments(rows)
    assert sum(float(row["actual_invoice_cost_ex_gst"]) for row in groups) == 19
    assert sum(float(row["actual_fuel_surcharge_ex_gst"]) for row in groups) == 1
    assert len([row for row in groups if row["owner_type"] == "ASSIGNMENT"]) == 1
    assert len([row for row in groups if row["owner_type"] == "TRACKING_NUMBER"]) == 2


def test_assignment_level_fuel_is_not_attached_to_a_selected_tracking_number():
    rows = [
        _line(consignment_id="T1", amount_excl_tax="28.36", fuel_surcharge_field_value="3.48"),
        _line(
            consignment_id="",
            material="D05",
            description="AP Parcels Domestic Fuel Surcharge",
            charge_component="FUEL",
            amount_excl_tax="12.49",
            total_qty="8",
            source_row_number="2",
        ),
    ]
    groups = _aggregate_consignments(rows)
    tracking = next(row for row in groups if row["owner_type"] == "TRACKING_NUMBER")
    assignment = next(row for row in groups if row["owner_type"] == "ASSIGNMENT")
    assert tracking["actual_fuel_surcharge_ex_gst"] == "3.48"
    assert tracking["fuel_allocation_status"] == "TRACKING_LINE_FUEL_FIELD"
    assert assignment["actual_fuel_surcharge_ex_gst"] == "12.49"
    assert assignment["fuel_allocation_status"] == "ASSIGNMENT_LEVEL_UNALLOCATED"


def test_article_measurements_are_summed_to_consignment_level():
    rows = [
        _line(
            article_id=f"ARTICLE-{index}",
            billed_length_m="0.34",
            billed_width_m="0.22",
            billed_height_m="0.17",
            cubic_weight_kg="3.18",
            billed_weight_kg="11.12",
            source_row_number=str(index),
        )
        for index in range(1, 7)
    ]

    result = _aggregate_consignments(rows)

    assert len(result) == 1
    consignment = result[0]
    assert consignment["physical_aggregation_method"] == "SUM_UNIQUE_ARTICLE_MEASUREMENTS"
    assert consignment["physical_distinct_article_count"] == "6"
    assert consignment["physical_article_duplicate_row_count"] == "0"
    assert consignment["billed_cube_cm3"] == "76320"
    assert consignment["physical_article_cube_max_cm3"] == "12720"
    assert consignment["billed_weight_kg"] == "66.72"
    assert consignment["actual_chargeable_weight_kg"] == "66.72"


def test_duplicate_article_measurements_are_not_double_counted():
    rows = [
        _line(article_id="ARTICLE-1", cubic_weight_kg="0", amount_excl_tax="10"),
        _line(article_id="ARTICLE-1", material="BO1", description="Over Maximum Limits Fee", charge_component="OTHER", cubic_weight_kg="0", amount_excl_tax="2", source_row_number="2"),
        _line(article_id="ARTICLE-2", cubic_weight_kg="0", amount_excl_tax="10", source_row_number="3"),
    ]

    result = _aggregate_consignments(rows)

    assert result[0]["physical_distinct_article_count"] == "2"
    assert result[0]["physical_article_duplicate_row_count"] == "1"
    assert result[0]["billed_cube_cm3"] == "2000"
    assert result[0]["actual_invoice_cost_ex_gst"] == "22"


def test_groups_without_article_ids_are_explicitly_flagged():
    result = _aggregate_consignments([_line(article_id="", billed_cube_cm3="")])

    assert result[0]["physical_aggregation_method"] == "MAX_WITHOUT_ARTICLE_ID"
    assert result[0]["physical_distinct_article_count"] == "0"


def test_fuel_norm_requires_thirty_samples_and_uses_median():
    rows = [_line(actual_base_cost_ex_gst="100", actual_fuel_surcharge_ex_gst=str(index), actual_other_surcharge_ex_gst="0", actual_invoice_cost_ex_gst=str(100 + index), owner_type="TRACKING_NUMBER", material_pattern="B5S", lodgement_zone_pattern="N0", destination_zone_pattern="N0") for index in range(MIN_FUEL_NORM_SAMPLE_COUNT)]
    output_path = Path("outputs/carrier_invoices/_test_australia_post_fuel_norms.csv")
    try:
        norms, fallback = _build_fuel_norms(rows, output_path)
        assert fallback.sample_count == MIN_FUEL_NORM_SAMPLE_COUNT
        assert fallback.p50_pct == 14.5
        assert norms[("LANE_DATE", "AP_DOMESTIC", "N0", "N0", "2026-07-15")].sample_count == MIN_FUEL_NORM_SAMPLE_COUNT
    finally:
        output_path.unlink(missing_ok=True)


def test_published_stage3_fsc_overrides_invoice_norm_fallback():
    norm = _configured_fuel_norm(
        {
            "actual_est_fsc_rate_pct": "12.3",
            "actual_est_fsc_source": "Australia Post published FSC schedule",
        }
    )
    assert norm is not None
    assert norm.sample_count == 0
    assert norm.p50_pct == 12.3
    assert norm.source == "Australia Post published FSC schedule"


def test_parent_order_fallback_is_available_when_tracking_is_missing():
    actual = _line(consignment_id="", owner_type="BILLING_DOCUMENT")
    model = {"invoice_number": "6538971741", "despatch_date": "2026-07-15", "dest_postcode": "2017", "carrier_tracking_number": "T1", "actual_carrier": "eParcel"}
    matched, method = _find_stage3_match(actual, {"T1": []}, {"6538971741": [model]}, {"653897174": [model]})
    assert matched == model
    assert method == "PARENT_ORDER_ID"


def test_zone_comparison_does_not_treat_ap_code_and_area_label_as_mismatch():
    assert _zone_comparison("N0", "Sydney") == "NOT_COMPARABLE_SOURCE_CODE_VS_MODEL_AREA"
    assert _zone_comparison("N0", "N1") == "COMPARABLE_MISMATCH"


def test_shippit_physical_basis_mismatch_combines_cube_and_weight_evidence():
    primary, root_cause, note = _classify_root_cause(
        {"destination_zone_pattern": "N0", "material_pattern": "B5S"},
        {"ap_zone": "N0"},
        "TRACKING_NUMBER",
        actual_cube=69_000,
        estimator_cube=66_000,
        shippit_cube=12_700,
        actual_weight=17.25,
        model_weight=3.18,
        shippit_weight=3.18,
        actual_base=5.0,
        model_base=5.0,
        actual_fuel=0.615,
        predicted_fuel=0.615,
        actual_other=0.0,
        zone_status="COMPARABLE_MATCH",
        estimator_status="ESTIMATOR_CUBE_ALIGNS_WITH_CARRIER",
        shippit_status="SHIPPIT_CUBE_UNDERSTATED_VS_CARRIER",
    )
    assert primary == "SHIPPIT_PHYSICAL_BASIS_MISMATCH"
    assert "SHIPPIT_CUBE_UNDERSTATED" in root_cause
    assert "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH" in root_cause
    assert "AP billed cube" in note
    assert "AP chargeable weight" in note


def test_sample_quota_is_exact_and_keeps_each_stratum_represented():
    counts = [
        {"SITE": "MOOREBANK", "SERVICE_MATERIAL": "B5S", "WEIGHT_BAND": "0-1 kg", "SERVICE_WEEK": "2026-07-13", "POPULATION_COUNT": 700},
        {"SITE": "MOOREBANK", "SERVICE_MATERIAL": "B5S", "WEIGHT_BAND": "2-5 kg", "SERVICE_WEEK": "2026-07-13", "POPULATION_COUNT": 200},
        {"SITE": "TRUGANINA", "SERVICE_MATERIAL": "B48", "WEIGHT_BAND": "5-10 kg", "SERVICE_WEEK": "2026-07-20", "POPULATION_COUNT": 100},
    ]
    quotas = _allocate_quotas(counts, 100)
    assert sum(quotas.values()) == 100
    assert all(value >= 1 for value in quotas.values())
    assert quotas[("MOOREBANK", "B5S", "0-1 kg", "2026-07-13")] > quotas[("TRUGANINA", "B48", "5-10 kg", "2026-07-20")]


def test_invoice_filter_clause_is_optional_and_escapes_values():
    assert invoice_filter_clause("invoice_number", []) == ""
    assert invoice_filter_clause("invoice_number", ["123", "O'9"]) == "AND invoice_number IN ('123', 'O''9')"


def test_sample_candidate_query_uses_both_ap_and_shippit_date_bounds():
    query = _candidate_counts_query("2026-07-15", "2026-07-31")
    assert "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST" in query
    assert "TO_DATE('2026-07-15')" in query
    assert "TO_DATE('2026-07-31')" in query
    assert "LIKE '%EPARCEL%'" in query


def test_sample_source_query_deduplicates_selected_assignments():
    candidates = [
        {"carrier_tracking_number": "T1", "ap_invoice_no": "AP1", "assignment_no": "A1"},
        {"carrier_tracking_number": "T2", "ap_invoice_no": "AP1", "assignment_no": "A1"},
    ]
    query = _selected_source_rows_query("2026-07-15", "2026-07-31", candidates)
    assert "selected_tracking AS" in query
    assert "selected_assignments AS" in query
    assert "SELECT DISTINCT carrier_tracking_number" in query
    assert "SELECT DISTINCT ap_invoice_no, assignment_no" in query
    assert "JOIN selected_assignments s" in query
    assert "FROM source_rows" in query
    assert "ORDER BY SERVICE_DATE" in query
    assert "JOIN selected_tracking s" in query
