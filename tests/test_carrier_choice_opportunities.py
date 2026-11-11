import pandas as pd

from src.carrier_choice_opportunities import (
    build_carrier_choice_detail,
    build_carrier_choice_summary,
)
from src.carrier_rules import carrier_rules_dataframe


def test_builds_one_primary_cheaper_option_per_consignment() -> None:
    stage3 = pd.DataFrame(
        {
            "invoice_number": ["1", "1", "2", "3", "4"],
            "ui_site": ["MOOREBANK"] * 5,
            "actual_carrier": ["eParcel"] * 5,
            "actual_service": ["eParcel"] * 5,
            "cheapest_alt_carrier": ["Couriers Please"] * 5,
            "cheapest_alt_service": ["Couriers Please"] * 5,
            "actual_est_cost_ex_gst": [100, 100, 100, 50, 0],
            "cheapest_alt_cost_ex_gst": [80, 80, 0, 60, 10],
            "ap_pricing_zone": ["Same State Metro"] * 5,
        }
    )

    detail = build_carrier_choice_detail(stage3)

    assert detail["invoice_number"].tolist() == ["1"]
    assert detail.iloc[0]["estimated_saving_ex_gst"] == 20
    assert detail.iloc[0]["estimated_saving_pct"] == 20


def test_summary_groups_by_current_and_next_best_option() -> None:
    detail = pd.DataFrame(
        {
            "opportunity_id": ["a", "b"],
            "ui_site": ["MOOREBANK", "MOOREBANK"],
            "current_carrier": ["eParcel", "eParcel"],
            "current_service": ["eParcel", "eParcel"],
            "next_best_carrier": ["Couriers Please", "Couriers Please"],
            "next_best_service": ["Couriers Please", "Couriers Please"],
            "ap_pricing_zone": ["Same State Metro", "Same State Metro"],
            "actual_parcel_count": [1, 2],
            "parcel_units_total": [2, 3],
            "current_modelled_cost_ex_gst": [100, 150],
            "next_best_modelled_cost_ex_gst": [80, 120],
            "estimated_saving_ex_gst": [20, 30],
        }
    )

    summary = build_carrier_choice_summary(detail)

    row = summary.iloc[0]
    assert row["eligible_consignments"] == 2
    assert row["eligible_parcels"] == 3
    assert row["eligible_units"] == 5
    assert row["estimated_saving_ex_gst"] == 50
    assert row["current_average_cost_per_consignment_ex_gst"] == 125
    assert row["next_best_average_cost_per_consignment_ex_gst"] == 100
    assert row["average_saving_per_consignment_ex_gst"] == 25
    assert row["estimated_saving_pct"] == 20


def test_excludes_gobundle_proxy_options() -> None:
    stage3 = pd.DataFrame(
        {
            "invoice_number": ["1"],
            "ui_site": ["TRUGANINA"],
            "actual_carrier": ["GoPeople"],
            "actual_service": ["GoBundle"],
            "cheapest_alt_carrier": ["Couriers Please"],
            "cheapest_alt_service": ["Couriers Please"],
            "actual_est_cost_ex_gst": [100],
            "cheapest_alt_cost_ex_gst": [80],
        }
    )

    assert build_carrier_choice_detail(stage3).empty


def test_classifies_known_carrier_limit_breaches_as_conditional() -> None:
    stage3 = pd.DataFrame(
        {
            "invoice_number": ["1", "2", "3"],
            "ui_site": ["MOOREBANK"] * 3,
            "actual_carrier": ["eParcel"] * 3,
            "actual_service": ["eParcel"] * 3,
            "cheapest_alt_carrier": ["Couriers Please"] * 3,
            "cheapest_alt_service": ["Couriers Please"] * 3,
            "actual_est_cost_ex_gst": [100, 100, 100],
            "cheapest_alt_cost_ex_gst": [80, 70, 60],
            "estimated_parcel_count": [8, 11, 8],
            "actual_parcel_count": [8, 11, 8],
            "shippit_dead_weight_kg": [10, 30, 10],
            "shippit_volumetric_weight_kg": [20, 20, 45],
        }
    )

    detail = build_carrier_choice_detail(stage3).set_index("invoice_number")

    assert detail.loc["1", "opportunity_bucket"] == "Realizable today"
    assert detail.loc["2", "opportunity_bucket"] == "Conditional if rule changed"
    assert "maximum 10 articles" in detail.loc["2", "eligibility_reason"]
    assert "dead-weight limit 25 kg" in detail.loc["2", "eligibility_reason"]
    assert detail.loc["3", "opportunity_bucket"] == "Conditional if rule changed"
    assert "volumetric-weight limit 40 kg" in detail.loc["3", "eligibility_reason"]


def test_carrier_rules_catalogue_keeps_unknown_limits_explicit() -> None:
    rules = carrier_rules_dataframe()

    cp = rules.loc[rules["carrier_service"].eq("Couriers Please / Couriers Please")].iloc[0]
    allied = rules.loc[rules["carrier_service"].eq("Allied Express")].iloc[0]

    assert cp["dead_weight_limit"] == "25 kg"
    assert cp["volumetric_weight_limit"] == "40 kg / 0.160 m3"
    assert cp["max_articles"] == "10"
    assert allied["dead_weight_limit"] == "Not confirmed"
