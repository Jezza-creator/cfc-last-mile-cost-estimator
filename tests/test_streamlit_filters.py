import pandas as pd
import pytest
from pathlib import Path

from src.reconciliation_store import _combine_coverage_frames, _upsert_coverage

from app.streamlit_app import (
    _accounting_week_options,
    _air_packaging_diagnostics,
    _air_weekly_summary,
    _carrier_mix_daily_table,
    _filter_carrier,
    _filter_dashboard_weekly_history,
    _filter_historical_trend_year,
    _filter_reporting_period,
    _find_reconciliation_rows_for_invoices,
    _historical_matrix_coverage,
    _historical_zone_week_matrix,
    _quality_breakdown,
    _reconciliation_bucket_export,
    _reconciliation_coverage,
    _reconciliation_cost_frame,
    _reconciliation_bridge,
    _coverage_history_as_stage3,
    _reconciliation_detail_for_stage3,
    _reconciliation_physical_basis_detail,
    _reconciliation_physical_basis_summary,
    _reconciliation_physical_overview,
    _reconciliation_snapshot,
    _allocation_physical_evidence,
    _dashboard_weekly_history_matrix,
    _reconciliation_weekly_matrix,
    _reconciliation_weekly_summary,
    _rbu_mix,
    _stage3_invoice_matches,
    _slice_matrix,
    _weekly_history_trend,
)


def _sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "invoice_number": ["1", "2", "3", "4"],
            "kmart_accounting_year": [2027, 2027, 2027, 2027],
            "kmart_accounting_week_number_in_year": [3, 4, 4, 3],
            "despatch_date": ["2026-07-15", "2026-07-22", "2026-07-23", "2026-07-16"],
            "actual_carrier": ["Couriers Please", "eParcel", "Couriers Please", "eParcel"],
        }
    )


def test_stage3_invoice_lookup_accepts_excel_numeric_format() -> None:
    stage3 = pd.DataFrame(
        {
            "invoice_number": ["5801700011", "0005156520"],
            "actual_carrier": ["eParcel", "eParcel"],
        }
    )

    matches = _stage3_invoice_matches(stage3, "5.801700011E+09")

    assert matches["invoice_number"].tolist() == ["5801700011"]


def test_historical_trend_year_filter_starts_at_fy2027() -> None:
    coverage = pd.DataFrame(
        {
            "Accounting year": [2026, 2027, 2027],
            "Week": ["FY2026 W52", "FY2027 W01", "FY2027 W02"],
        }
    )
    weekly = pd.DataFrame(
        {
            "Week": ["FY2026 W52", "FY2027 W01"],
            "Value": [1, 2],
        }
    )

    assert _filter_historical_trend_year(coverage)["Week"].tolist() == [
        "FY2027 W01",
        "FY2027 W02",
    ]
    assert _filter_historical_trend_year(weekly)["Week"].tolist() == ["FY2027 W01"]


def test_allocation_physical_evidence_marks_unpersisted_sources_explicitly() -> None:
    invoice = pd.Series(
        {
            "invoice_number": "6559050711",
            "actual_parcel_count": 6,
            "actual_carrier": "eParcel",
            "actual_service": "eParcel",
            "shippit_dead_weight_kg": 29.04,
            "shippit_volumetric_weight_kg": 23.17,
            "shippit_chargeable_weight_kg": 29.04,
            "shippit_volumetric_cube_cm3": 92664,
            "shippit_chargeable_cube_cm3": 116160,
            "est_packed_sku_weight_kg": 27.84,
            "est_chargeable_weight_kg": 1607.79,
            "est_parcel_cube_total_m3": 6.431152,
            "despatch_date": "2026-08-31",
        }
    )
    assignment_lines = pd.DataFrame(
        [
            {
                "invoice_number": "6559050711",
                "sku_id": "42909484",
                "quantity_packed": 2,
                "cfc_height_cm": 63.1,
                "cfc_width_cm": 45.5,
                "cfc_length_cm": 70.0,
                "cfc_weight_kg": 0.87,
                "raw_packed_sku_cube_cm3_per_line": 401947.0,
                "assignment_source": "ORD_FULFILLMENT_DETAIL",
                "pack_timestamp": "2026-08-31 08:47:08.799000",
                "source_package_id": "PACKAGE1",
            }
        ]
    )
    physicals = pd.DataFrame(
        [
            {
                "estimated_height_cm": 70.0,
                "estimated_width_cm": 63.1,
                "estimated_length_cm": 91.0,
            }
        ]
    )
    plm_dimensions = pd.DataFrame(
        [
            {
                "sku_id": "42909484",
                "plm_shortest_cm": 18.0,
                "plm_median_cm": 18.0,
                "plm_longest_cm": 61.0,
                "plm_instore_gross_weight_kg": 0.90,
                "plm_gross_cube_cm3": 19764.0,
                "plm_volumetric_weight_kg": 3.29,
            }
        ]
    )

    evidence = _allocation_physical_evidence(
        invoice,
        physicals,
        assignment_lines,
        pd.DataFrame(),
        plm_dimensions,
    )

    latest = evidence.loc[evidence["Source"] == "Latest CFC SKU_ATTRIBUTE record"].iloc[0]
    assert latest["Evidence stage"] == "Current master data / post-pack linkage"
    assert latest["Status"].startswith("Available")
    assert evidence.loc[evidence["Source"] == "WHS allocation-time snapshot", "Status"].iloc[0].startswith("Not available")
    shippit = evidence.loc[evidence["Source"] == "Shippit / Catch webhook"].iloc[0]
    assert shippit["Evidence stage"] == "Shippit tracking / Catch article payload"
    assert shippit["Status"].startswith("Aggregate only")
    assert evidence.loc[evidence["Source"] == "Invoiced actual", "Status"].iloc[0].startswith("Not available")
    plm = evidence.loc[
        evidence["Source"] == "PLM keycode master (KEYCODE_CARTON_DSS_DIMENSIONS_VW)"
    ].iloc[0]
    assert "18.0 x 18.0 x 61.0 cm" in plm["Dimensions"]
    assert "0.039528 m3 total" in plm["Weight / cube"]
    assert "PLM matched 1 of 1 assigned keycode(s)" in plm["Allocation / parcel evidence"]
    assert "CFC cube versus PLM cube: +916.9%" in plm["Status"]


def test_coverage_history_can_supply_detail_lookup_keys() -> None:
    coverage = pd.DataFrame(
        [
            {
                "Shippit invoice number": "123",
                "Shippit tracking number": "TRACK123",
                "CFC": "TRUGANINA",
                "Shipped carrier": "Allied Express",
                "Shipped service": "Allied Express",
                "Despatch date": "2026-07-15",
                "Accounting year": 2027,
                "Accounting week": 3,
                "Modelled cost ex GST": 12.5,
                "Shippit parcel count": 1,
                "Shippit units": 4,
            }
        ]
    )

    history = _coverage_history_as_stage3(coverage)

    assert history.iloc[0]["invoice_number"] == "123"
    assert history.iloc[0]["carrier_tracking_number"] == "TRACK123"
    assert history.iloc[0]["ui_site"] == "TRUGANINA"
    assert history.iloc[0]["modelled_total_cost_ex_gst"] == pytest.approx(12.5)


def test_historical_matrix_uses_invoice_cost_then_estimator_fallback() -> None:
    coverage = pd.DataFrame(
        [
            {
                "Modelled cost ex GST": 10,
                "Invoiced actual cost ex GST": 12,
                "Invoice status": "Matched",
            },
            {
                "Modelled cost ex GST": 20,
                "Invoiced actual cost ex GST": None,
                "Invoice status": "Not yet matched",
            },
            {
                "Modelled cost ex GST": None,
                "Invoiced actual cost ex GST": 100,
                "Invoice status": "Matched",
            },
        ]
    )

    matrix_coverage = _historical_matrix_coverage(coverage)
    history = _coverage_history_as_stage3(
        matrix_coverage,
        prefer_invoiced_actual_cost=True,
    )

    assert len(matrix_coverage) == 2
    assert history["actual_est_cost_ex_gst"].tolist() == [12, 20]
    assert history["modelled_total_cost_ex_gst"].tolist() == [10, 20]


def test_reconciliation_detail_can_search_current_and_archived_extracts(monkeypatch) -> None:
    current = Path("app/streamlit_app.py")
    archived = Path("src/reconciliation_store.py")
    rows_by_name = {
        current.name: pd.DataFrame(
            [
                {
                    "invoice_number": "1",
                    "stage3_tracking_number": "TRACK1",
                    "site": "TRUGANINA",
                    "primary_cost_driver": "CURRENT",
                }
            ]
        ),
        archived.name: pd.DataFrame(
            [
                {
                    "invoice_number": "2",
                    "stage3_tracking_number": "TRACK2",
                    "site": "TRUGANINA",
                    "primary_cost_driver": "ARCHIVED",
                }
            ]
        ),
    }
    calls = []

    def fake_find(path_text, invoice_numbers, site, modified_ns, usecols=None):
        del invoice_numbers, site, modified_ns, usecols
        calls.append(Path(path_text).name)
        return rows_by_name[Path(path_text).name].copy()

    monkeypatch.setattr("app.streamlit_app._find_reconciliation_rows_for_invoices", fake_find)
    stage3 = pd.DataFrame(
        [
            {"invoice_number": "1", "carrier_tracking_number": "TRACK1", "actual_carrier": "Allied Express"},
            {"invoice_number": "2", "carrier_tracking_number": "TRACK2", "actual_carrier": "Allied Express"},
        ]
    )

    detail = _reconciliation_detail_for_stage3(
        stage3,
        "TRUGANINA",
        {"allied": (current, archived)},
    )

    assert calls == [current.name, archived.name]
    assert sorted(detail["primary_cost_driver"].tolist()) == ["ARCHIVED", "CURRENT"]


def test_reconciliation_detail_aggregates_multiple_carrier_rows_per_invoice(monkeypatch) -> None:
    source = Path("app/streamlit_app.py")
    rows = pd.DataFrame(
        [
            {
                "stage3_invoice_number": "1",
                "stage3_tracking_number": "TRACK1",
                "carrier_tracking_number": "CARRIER1",
                "site": "MOOREBANK",
                "actual_invoice_cost_ex_gst": "10",
                "actual_base_cost_ex_gst": "8",
                "actual_fuel_surcharge_ex_gst": "2",
                "actual_chargeable_weight_kg": "1",
                "actual_billed_cube_cm3": "1000",
            },
            {
                "stage3_invoice_number": "1",
                "stage3_tracking_number": "TRACK1",
                "carrier_tracking_number": "CARRIER2",
                "site": "MOOREBANK",
                "actual_invoice_cost_ex_gst": "5",
                "actual_base_cost_ex_gst": "4",
                "actual_fuel_surcharge_ex_gst": "1",
                "actual_chargeable_weight_kg": "2",
                "actual_billed_cube_cm3": "2000",
            },
        ]
    )

    def fake_find(path_text, invoice_numbers, site, modified_ns, usecols=None):
        del path_text, invoice_numbers, site, modified_ns, usecols
        return rows.copy()

    monkeypatch.setattr("app.streamlit_app._find_reconciliation_rows_for_invoices", fake_find)
    stage3 = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "carrier_tracking_number": "TRACK1",
                "actual_carrier": "eParcel",
                "modelled_total_cost_ex_gst": "15",
            }
        ]
    )

    detail = _reconciliation_detail_for_stage3(
        stage3,
        "MOOREBANK",
        {"australia_post": (source,)},
    )

    assert len(detail) == 1
    assert detail.iloc[0]["actual_invoice_cost_ex_gst"] == pytest.approx(15)
    assert detail.iloc[0]["actual_chargeable_weight_kg"] == pytest.approx(3)
    assert detail.iloc[0]["actual_billed_cube_cm3"] == pytest.approx(3000)
    assert _reconciliation_cost_frame(detail).iloc[0]["modelled_total_cost_ex_gst"] == pytest.approx(15)


def test_reconciliation_detail_matcher_excludes_blank_site_rows() -> None:
    path = Path(__file__).parent / "fixtures" / "site_filter_reconciliation.csv"

    detail = _find_reconciliation_rows_for_invoices(
        str(path),
        ("TRACK1",),
        "MOOREBANK",
        path.stat().st_mtime_ns,
    )

    assert detail["site"].tolist() == ["MOOREBANK"]
    assert detail["primary_cost_driver"].tolist() == ["MATCHED"]


def test_reporting_period_uses_latest_and_previous_accounting_weeks() -> None:
    frame = _sample_frame()

    current, current_label = _filter_reporting_period(
        frame,
        "Current accounting week to date",
        as_of_date="2026-07-23",
    )
    previous, previous_label = _filter_reporting_period(frame, "Previous accounting week")

    assert current_label == "FY2027 W04 to date"
    assert current["invoice_number"].tolist() == ["2", "3"]
    assert previous_label == "FY2027 W03"
    assert previous["invoice_number"].tolist() == ["1", "4"]


def test_current_accounting_week_to_date_excludes_future_days(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "invoice_number": ["monday", "tuesday", "wednesday", "thursday"],
            "kmart_accounting_year": [2027, 2027, 2027, 2027],
            "kmart_accounting_week_number_in_year": [9, 9, 9, 9],
            "despatch_date": [
                "2026-08-31",
                "2026-09-01",
                "2026-09-02",
                "2026-09-03",
            ],
        }
    )
    selected, label = _filter_reporting_period(
        frame,
        "Current accounting week to date",
        as_of_date="2026-09-01",
    )

    assert label == "FY2027 W09 to date"
    assert selected["invoice_number"].tolist() == ["monday", "tuesday"]


def test_reporting_period_can_filter_to_one_selected_accounting_week() -> None:
    frame = _sample_frame()

    options = _accounting_week_options(frame)
    selected, label = _filter_reporting_period(
        frame,
        "Single accounting week",
        "FY2027 W03",
    )

    assert options == ["FY2027 W04", "FY2027 W03"]
    assert label == "FY2027 W03"
    assert selected["invoice_number"].tolist() == ["1", "4"]


def test_carrier_mix_daily_table_has_totals_and_wow_rows() -> None:
    current = pd.DataFrame(
        {
            "actual_carrier": ["Couriers Please", "Couriers Please", "eParcel", "eParcel"],
            "despatch_date": ["2026-08-31", "2026-09-01", "2026-08-31", "2026-09-01"],
        }
    )
    previous = pd.DataFrame(
        {
            "actual_carrier": ["Couriers Please", "Couriers Please", "eParcel", "eParcel"],
            "despatch_date": ["2026-08-24", "2026-08-25", "2026-08-25", "2026-08-25"],
        }
    )

    table = _carrier_mix_daily_table(
        current,
        previous,
        "Consignments",
        elapsed_weekdays=1,
    )

    assert table.loc["Total", "Mon\n31/08"] == 2
    assert table.loc["Total", "Tue\n01/09"] == 2
    assert table.loc["Total", "Total"] == 4
    assert table.loc["WoW %", "Mon\n31/08"] == pytest.approx(100)
    assert table.loc["WoW %", "Tue\n01/09"] == pytest.approx(-33.333333)
    assert pd.isna(table.loc["WoW %", "Wed\n02/09"])
    assert table.loc["WoW %", "Total"] == pytest.approx(0)


def test_carrier_mix_daily_table_percentages_show_carrier_share() -> None:
    current = pd.DataFrame(
        {
            "actual_carrier": [
                "Couriers Please",
                "Couriers Please",
                "Couriers Please",
                "eParcel",
            ],
            "despatch_date": [
                "2026-08-31",
                "2026-08-31",
                "2026-09-01",
                "2026-08-31",
            ],
        }
    )
    previous = pd.DataFrame(
        {
            "actual_carrier": ["Couriers Please", "eParcel"],
            "despatch_date": ["2026-08-24", "2026-08-24"],
        }
    )

    table = _carrier_mix_daily_table(
        current,
        previous,
        "Consignments %",
        elapsed_weekdays=1,
    )

    assert table.loc["Couriers Please", "Mon\n31/08"] == pytest.approx(66.666667)
    assert table.loc["eParcel", "Mon\n31/08"] == pytest.approx(33.333333)
    assert table.loc["Total", "Mon\n31/08"] == pytest.approx(100)
    assert table.loc["Couriers Please", "Total"] == pytest.approx(75)
    assert table.loc["eParcel", "Total"] == pytest.approx(25)
    assert table.loc["Total", "Total"] == pytest.approx(100)
    assert table.loc["WoW %", "Mon\n31/08"] == pytest.approx(50)
    assert pd.isna(table.loc["WoW %", "Wed\n02/09"])


def test_air_packaging_diagnostics_uses_best_physical_basis_and_mean_target() -> None:
    coverage = pd.DataFrame(
        {
            "Shippit invoice number": ["invoice", "fallback-a", "fallback-b"],
            "Shippit tracking number": ["track", "track-a", "track-b"],
            "CFC": ["MOOREBANK"] * 3,
            "Shipped carrier": ["Couriers Please"] * 3,
            "Shipped service": ["Couriers Please"] * 3,
            "Week": ["FY2027 W03"] * 3,
            "Invoice status": ["Matched", "Not yet matched", "Not yet matched"],
            "Invoiced actual cube m3": [0.0235, None, None],
            "Shippit chargeable cube m3": [0.0200, 0.0200, 0.0100],
            "Invoiced actual dead weight kg": [2.41, None, None],
            "Shippit dead weight kg": [2.00, 1.00, 1.00],
            "Invoiced actual cubic weight kg": [5.87, None, None],
            "Shippit volumetric weight kg": [5.00, 5.00, 2.50],
            "Invoiced actual chargeable weight kg": [5.87, None, None],
            "Shippit chargeable weight kg": [5.00, 5.00, 2.50],
            "Estimator packed SKU cube m3": [0.0152, 0.0150, 0.0090],
            "Estimator cube m3": [0.0152, 0.0150, 0.0090],
            "Estimator packed SKU weight kg": [1.37, 1.00, 0.80],
            "Invoiced actual cost ex GST": [10.00, None, None],
            "Modelled cost ex GST": [9.00, 20.00, 4.00],
            "Invoiced actual base cost ex GST": [8.00, None, None],
            "Invoiced actual fuel surcharge ex GST": [1.00, None, None],
            "Estimator packed SKU cube imputed": ["", "Y", ""],
        }
    )

    detail, summary = _air_packaging_diagnostics(coverage)

    invoice_row = detail.loc[
        detail["Shippit invoice number"].eq("invoice")
    ].iloc[0]
    assert invoice_row["Cube physical basis"] == "Invoiced actual"
    assert invoice_row["Dead-weight physical basis"] == "Invoiced actual"
    assert invoice_row["Air cube m3"] == pytest.approx(0.0083)
    assert invoice_row["Air share %"] == pytest.approx(0.0083 / 0.0235 * 100)
    assert invoice_row["Indicative value of reducing excess air ex GST"] > 0
    assert detail["Indicative value of reducing excess air ex GST"].gt(0).sum() == 2
    assert summary.iloc[0]["Outlier records"] == 2


def test_air_weekly_summary_surfaces_consistency_and_lumpy_weeks() -> None:
    detail = pd.DataFrame(
        {
            "Week": ["FY2027 W01", "FY2027 W01", "FY2027 W02"],
            "Invoice status": ["Matched", "Not yet matched", "Matched"],
            "Air share %": [20.0, 40.0, 80.0],
            "Air cube m3": [0.1, 0.2, 0.3],
            "Volume-led": ["Y", "", "Y"],
            "Added packaging/dunnage weight kg": [0.2, 0.4, 0.3],
            "Added packaging/dunnage weight %": [10.0, 20.0, 15.0],
            "Indicative value of reducing excess air ex GST": [2.0, 0.0, 5.0],
        }
    )

    weekly = _air_weekly_summary(detail)

    assert weekly["Week"].tolist() == ["FY2027 W01", "FY2027 W02"]
    first_week = weekly.iloc[0]
    assert first_week["Records"] == 2
    assert first_week["Usable physical-basis records"] == 2
    assert first_week["Invoice verified %"] == pytest.approx(50.0)
    assert first_week["Mean air share %"] == pytest.approx(30.0)
    assert first_week["Median air share %"] == pytest.approx(30.0)
    assert first_week["Positive excess-air records %"] == pytest.approx(100.0)
    assert first_week["Volume-led records %"] == pytest.approx(50.0)
    assert first_week["Outlier records"] == 1
    assert first_week["Indicative value of reducing excess air ex GST"] == pytest.approx(2.0)


def test_reporting_period_can_return_all_rows_and_filter_carrier() -> None:
    frame = _sample_frame()

    all_rows, label = _filter_reporting_period(frame, "All available weeks")

    assert label == "All available refreshed data"
    assert len(all_rows) == len(frame)
    assert _filter_carrier(frame, "Couriers Please")["invoice_number"].tolist() == ["1", "3"]


@pytest.mark.parametrize(
    ("metric_key", "expected_local_share", "expected_local_total"),
    [
        ("consignments_pct", 50.0, 75.0),
        ("shipped_parcels_pct", 3 / 7 * 100, 4 / 7 * 100),
        ("shipped_units_pct", 60.0, 70.0),
    ],
)
def test_slice_matrix_percentage_measures_use_grand_total(
    metric_key: str, expected_local_share: float, expected_local_total: float
) -> None:
    frame = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "0-1 kg",
                "actual_parcel_count": 1,
                "parcel_units_total": 2,
            },
            {
                "invoice_number": "2",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "0-1 kg",
                "actual_parcel_count": 2,
                "parcel_units_total": 4,
            },
            {
                "invoice_number": "3",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "1-2 kg",
                "actual_parcel_count": 1,
                "parcel_units_total": 1,
            },
            {
                "invoice_number": "4",
                "ap_pricing_zone": "Near State Metro",
                "reporting_weight_band": "0-1 kg",
                "actual_parcel_count": 3,
                "parcel_units_total": 3,
            },
        ]
    )

    matrix = _slice_matrix(frame, metric_key)

    assert matrix.loc["0-1 kg", "Same State Local"] == pytest.approx(expected_local_share)
    assert matrix.drop(index="Total").sum(skipna=True).sum() == pytest.approx(100)
    assert matrix.loc["Total", "Same State Local"] == pytest.approx(expected_local_total)


@pytest.mark.parametrize(
    "metric_key",
    ["cost_per_consignment", "cost_per_parcel", "cost_per_unit"],
)
def test_slice_matrix_unit_cost_totals_are_weighted_averages_by_zone(metric_key: str) -> None:
    frame = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "0-1 kg",
                "actual_est_cost_ex_gst": 10,
                "actual_parcel_count": 1,
                "parcel_units_total": 2,
            },
            {
                "invoice_number": "2",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "1-2 kg",
                "actual_est_cost_ex_gst": 30,
                "actual_parcel_count": 1,
                "parcel_units_total": 1,
            },
            {
                "invoice_number": "3",
                "ap_pricing_zone": "Near State Metro",
                "reporting_weight_band": "0-1 kg",
                "actual_est_cost_ex_gst": 100,
                "actual_parcel_count": 1,
                "parcel_units_total": 1,
            },
        ]
    )

    matrix = _slice_matrix(frame, metric_key)

    expected_local_total = {
        "cost_per_consignment": 20,
        "cost_per_parcel": 20,
        "cost_per_unit": 40 / 3,
    }[metric_key]
    assert matrix.loc["Total", "Same State Local"] == pytest.approx(expected_local_total)
    assert matrix.loc["Total", "Near State Metro"] == pytest.approx(100)


def test_historical_zone_week_matrix_filters_zone_and_keeps_week_order() -> None:
    frame = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "0-1 kg",
                "Week": "FY2027 W01",
                "actual_est_cost_ex_gst": 10,
                "actual_parcel_count": 1,
                "parcel_units_total": 2,
            },
            {
                "invoice_number": "2",
                "ap_pricing_zone": "Same State Local",
                "reporting_weight_band": "0-1 kg",
                "Week": "FY2027 W02",
                "actual_est_cost_ex_gst": 20,
                "actual_parcel_count": 2,
                "parcel_units_total": 4,
            },
            {
                "invoice_number": "3",
                "ap_pricing_zone": "Near State Metro",
                "reporting_weight_band": "0-1 kg",
                "Week": "FY2027 W01",
                "actual_est_cost_ex_gst": 100,
                "actual_parcel_count": 1,
                "parcel_units_total": 1,
            },
        ]
    )

    matrix = _historical_zone_week_matrix(
        frame,
        "cost_per_unit",
        "Same State Local",
        ["FY2027 W02", "FY2027 W01"],
    )

    assert matrix.columns.tolist() == ["FY2027 W02", "FY2027 W01"]
    assert matrix.loc["0-1 kg", "FY2027 W02"] == pytest.approx(5)
    assert matrix.loc["0-1 kg", "FY2027 W01"] == pytest.approx(5)
    assert matrix.loc["Total", "FY2027 W02"] == pytest.approx(5)
    assert matrix.loc["Total", "FY2027 W01"] == pytest.approx(5)


def test_reconciliation_snapshot_aggregates_actual_cost_and_physical_evidence() -> None:
    rows = pd.DataFrame(
        [
            {
                "actual_chargeable_weight_kg": "4",
                "actual_billed_cube_m3": "0.010",
                "actual_base_cost_ex_gst": "10.00",
                "actual_fuel_surcharge_ex_gst": "1.20",
                "actual_other_surcharges_ex_gst": "0.30",
                "actual_invoice_cost_ex_gst": "11.50",
            },
            {
                "actual_chargeable_weight_kg": "2",
                "actual_billed_cube_m3": "0.005",
                "actual_base_cost_ex_gst": "5.00",
                "actual_fuel_surcharge_ex_gst": "0.60",
                "actual_other_surcharges_ex_gst": "0.10",
                "actual_invoice_cost_ex_gst": "5.70",
            },
        ]
    )

    snapshot = _reconciliation_snapshot(rows)

    assert snapshot["chargeable_weight_kg"] == 6
    assert snapshot["cube_m3"] == pytest.approx(0.015)
    assert snapshot["base_cost_ex_gst"] == pytest.approx(15)
    assert snapshot["fuel_cost_ex_gst"] == pytest.approx(1.8)
    assert snapshot["other_cost_ex_gst"] == pytest.approx(0.4)
    assert snapshot["total_cost_ex_gst"] == pytest.approx(17.2)


def test_reconciliation_physical_overview_is_a_directional_signed_summary() -> None:
    summary = pd.DataFrame(
        [
            {
                "_summary_carrier": "gopeople",
                "site": "TRUGANINA",
                "primary_cost_driver": "SHIPPIT_PHYSICAL_BASIS_MISMATCH",
                "billed_consignments": 80,
                "actual_invoice_cost_ex_gst": 180,
                "modelled_goexpress_cost_ex_gst": 100,
            },
            {
                "_summary_carrier": "gopeople",
                "site": "TRUGANINA",
                "primary_cost_driver": "GOPEOPLE_RATE_OR_CHARGE_MISMATCH",
                "billed_consignments": 20,
                "actual_invoice_cost_ex_gst": 40,
                "modelled_goexpress_cost_ex_gst": 50,
            },
        ]
    )

    overview = _reconciliation_physical_overview(
        summary,
        "TRUGANINA",
        "All shipped carriers",
    )

    row = overview.iloc[0]
    assert row["Complete-cost records"] == pytest.approx(100)
    assert row["Total observed variance ex GST"] == pytest.approx(70)
    assert row["Total observed variance %"] == pytest.approx(70 / 150 * 100)
    assert row["Dim/weight records"] == pytest.approx(80)
    assert row["Dim/weight variance ex GST"] == pytest.approx(80)
    assert row["Dim/weight share of signed variance %"] == pytest.approx(80 / 70 * 100)
    assert row["Residual variance ex GST"] == pytest.approx(-10)


def test_reconciliation_bridge_groups_variance_by_primary_driver() -> None:
    rows = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "primary_cost_driver": "CP_CHARGEABLE_WEIGHT_MISMATCH",
                "modelled_cp_base_cost_ex_gst": "10",
                "modelled_cp_cost_including_fuel_ex_gst": "11",
                "predicted_fuel_surcharge_ex_gst": "1",
                "actual_base_cost_ex_gst": "12",
                "actual_fuel_surcharge_ex_gst": "1.2",
                "actual_other_surcharges_ex_gst": "0.3",
                "actual_invoice_cost_ex_gst": "13.5",
            }
        ]
    )

    bridge, totals = _reconciliation_bridge(rows)

    assert totals["matched_invoices"] == 1
    assert totals["modelled_cost"] == pytest.approx(11)
    assert totals["actual_cost"] == pytest.approx(13.5)
    assert totals["variance"] == pytest.approx(2.5)
    assert bridge.iloc[0]["primary_root_cause"] == "CP_CHARGEABLE_WEIGHT_MISMATCH"
    assert bridge.iloc[0]["total_variance_ex_gst"] == pytest.approx(2.5)


def test_reconciliation_bridge_adds_allied_surcharge_components() -> None:
    rows = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "primary_cost_driver": "ALLIED_CHARGEABLE_WEIGHT_MISMATCH",
                "modelled_allied_base_cost_ex_gst": "10",
                "modelled_allied_cost_including_fuel_ex_gst": "11",
                "predicted_fuel_surcharge_ex_gst": "1",
                "actual_base_cost_ex_gst": "8",
                "actual_fuel_surcharge_ex_gst": "1",
                "actual_excess_weight_surcharge_ex_gst": "3",
                "actual_delivery_surcharge_ex_gst": "0.25",
                "actual_other_surcharge_ex_gst": "0.25",
                "actual_invoice_cost_ex_gst": "12.5",
            }
        ]
    )

    bridge, totals = _reconciliation_bridge(rows)

    assert totals["variance"] == pytest.approx(1.5)
    assert bridge.iloc[0]["base_variance_ex_gst"] == pytest.approx(-2)
    assert bridge.iloc[0]["fuel_variance_ex_gst"] == pytest.approx(0)
    assert bridge.iloc[0]["other_variance_ex_gst"] == pytest.approx(3.5)
    assert bridge.iloc[0]["total_variance_ex_gst"] == pytest.approx(1.5)


def test_reconciliation_bridge_excludes_incomplete_cost_pairs() -> None:
    rows = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "primary_cost_driver": "CP_RATE_OR_CHARGE_MISMATCH",
                "modelled_cp_cost_including_fuel_ex_gst": "10",
                "actual_invoice_cost_ex_gst": "12",
            },
            {
                "invoice_number": "",
                "primary_cost_driver": "MATCHING_GAP",
                "modelled_cp_cost_including_fuel_ex_gst": "",
                "actual_invoice_cost_ex_gst": "99",
            },
            {
                "invoice_number": "3",
                "primary_cost_driver": "MATCHING_GAP",
                "modelled_cp_cost_including_fuel_ex_gst": "0",
                "actual_invoice_cost_ex_gst": "99",
            },
        ]
    )

    bridge, totals = _reconciliation_bridge(rows)

    assert totals["matched_records"] == 1
    assert totals["modelled_cost"] == pytest.approx(10)
    assert totals["actual_cost"] == pytest.approx(12)
    assert totals["variance"] == pytest.approx(2)
    assert bridge["primary_root_cause"].tolist() == ["CP_RATE_OR_CHARGE_MISMATCH"]


def test_reconciliation_cost_frame_adds_primary_root_cause_for_bucket_review() -> None:
    rows = pd.DataFrame(
        [
            {
                "primary_cost_driver": "SHIPPIT_PHYSICAL_BASIS_MISMATCH",
                "root_cause": "SHIPPIT_CUBE_UNDERSTATED",
                "modelled_cp_base_cost_ex_gst": "10",
                "modelled_cp_cost_including_fuel_ex_gst": "11",
                "predicted_fuel_surcharge_ex_gst": "1",
                "actual_base_cost_ex_gst": "12",
                "actual_fuel_surcharge_ex_gst": "1.2",
                "actual_invoice_cost_ex_gst": "13.2",
            }
        ]
    )

    cost_detail = _reconciliation_cost_frame(rows)

    assert cost_detail.iloc[0]["primary_root_cause"] == "SHIPPIT_PHYSICAL_BASIS_MISMATCH"


def test_reconciliation_physical_basis_cohorts_separate_weight_and_cube_evidence() -> None:
    rows = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "primary_cost_driver": "SHIPPIT_PHYSICAL_BASIS_MISMATCH",
                "root_cause": "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH | SHIPPIT_CUBE_UNDERSTATED",
                "shippit_chargeable_weight_kg": "3",
                "actual_chargeable_weight_kg": "8",
                "shippit_chargeable_cube_cm3": "10000",
                "actual_total_volume_m3": "0.020",
                "estimator_parcel_cube_cm3": "19000",
                "modelled_cp_base_cost_ex_gst": "10",
                "modelled_cp_cost_including_fuel_ex_gst": "11",
                "predicted_fuel_surcharge_ex_gst": "1",
                "actual_base_cost_ex_gst": "12",
                "actual_fuel_surcharge_ex_gst": "1.2",
                "actual_invoice_cost_ex_gst": "13.2",
            },
            {
                "invoice_number": "2",
                "primary_cost_driver": "CP_RATE_OR_CHARGE_MISMATCH",
                "root_cause": "CP_RATE_OR_CHARGE_MISMATCH",
                "shippit_chargeable_weight_kg": "3",
                "actual_chargeable_weight_kg": "3.2",
                "shippit_chargeable_cube_cm3": "10000",
                "actual_total_volume_m3": "0.020",
                "estimator_parcel_cube_cm3": "9500",
                "modelled_cp_base_cost_ex_gst": "10",
                "modelled_cp_cost_including_fuel_ex_gst": "11",
                "predicted_fuel_surcharge_ex_gst": "1",
                "actual_base_cost_ex_gst": "11",
                "actual_fuel_surcharge_ex_gst": "1.1",
                "actual_invoice_cost_ex_gst": "12.1",
            },
        ]
    )

    detail = _reconciliation_physical_basis_detail(rows)
    summary = _reconciliation_physical_basis_summary(rows).set_index(
        "Shippit physical-basis cohort"
    )

    assert detail.iloc[0]["shippit_physical_basis_cohort"] == "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH"
    assert detail.iloc[1]["shippit_physical_basis_cohort"] == "SHIPPIT_CUBE_MISMATCH_ONLY"
    assert summary.loc["SHIPPIT_CUBE_AND_WEIGHT_MISMATCH", "Observed variance ex GST"] == pytest.approx(2.2)
    assert summary.loc["SHIPPIT_CUBE_MISMATCH_ONLY", "Observed variance ex GST"] == pytest.approx(1.1)
    assert summary.loc["SHIPPIT_CUBE_MISMATCH_ONLY", "% with invoiced weight above Shippit"] == pytest.approx(100)


def test_reconciliation_bucket_export_uses_explicit_sources_units_and_cost_precision() -> None:
    rows = pd.DataFrame(
        [
            {
                "invoice_number": "6388302371",
                "stage3_tracking_number": "CPCAH0Z02103471",
                "actual_service_name": "I99 - 25KG Block",
                "actual_service_code": "I99",
                "actual_chargeable_weight_kg": "17.25",
                "actual_rounded_chargeable_weight_kg": "18",
                "shippit_chargeable_weight_kg": "3.18",
                "modelled_cp_chargeable_weight_kg": "4",
                "actual_cp_destination_zone": "SYD",
                "modelled_cp_destination_zone": "SYD",
                "estimator_parcel_cube_cm3": "66187.98",
                "shippit_chargeable_cube_cm3": "12716",
                "actual_total_volume_m3": "0.069",
                "actual_invoice_cost_ex_gst": "5.1",
                "modelled_total_cost_ex_gst": "3.730682",
                "actual_less_modelled_cost_including_fuel_ex_gst": "1.369318",
            }
        ]
    )

    export = _reconciliation_bucket_export(rows)

    assert export.iloc[0]["shippit_invoice_number"] == "6388302371"
    assert export.iloc[0]["invoiced_actual_service_code"] == "I99"
    assert export.iloc[0]["estimator_parcel_cube_m3"] == "0.0662"
    assert export.iloc[0]["shippit_chargeable_cube_m3"] == "0.0127"
    assert export.iloc[0]["invoiced_actual_total_volume_m3"] == "0.0690"
    assert export.iloc[0]["invoiced_actual_cost_ex_gst"] == "5.10"
    assert export.iloc[0]["modelled_total_cost_ex_gst"] == "3.73"
    assert export.iloc[0]["invoiced_actual_less_modelled_cost_including_fuel_ex_gst"] == "1.37"


def test_reconciliation_coverage_keeps_unmatched_consignments_and_matches_by_tracking() -> None:
    stage3 = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "carrier_tracking_number": "TRK1",
                "actual_carrier": "Couriers Please",
                "actual_service": "Couriers Please",
                "ui_site": "MOOREBANK",
                "despatch_date": "2026-07-15",
                "kmart_accounting_year": 2027,
                "kmart_accounting_week_number_in_year": 3,
                "actual_est_cost_ex_gst": "10",
                "parcel_units_total": 2,
                "ap_pricing_zone": "Same State Metro",
                "reporting_weight_band": "1-2 kg",
            },
            {
                "invoice_number": "2",
                "carrier_tracking_number": "TRK2",
                "actual_carrier": "eParcel",
                "actual_service": "Metro",
                "ui_site": "MOOREBANK",
                "despatch_date": "2026-07-22",
                "kmart_accounting_year": 2027,
                "kmart_accounting_week_number_in_year": 4,
                "actual_est_cost_ex_gst": "20",
                "parcel_units_total": 3,
            },
        ]
    )
    detail = pd.DataFrame(
        [
            {
                "stage3_tracking_number": "TRK1",
                "actual_base_cost_ex_gst": "10",
                "actual_fuel_surcharge_ex_gst": "2",
                "actual_invoice_cost_ex_gst": "12",
                "primary_cost_driver": "BASE_RATE_OR_WEIGHT_VARIANCE",
            }
        ]
    )

    coverage, _ = _reconciliation_coverage(stage3, detail)
    weekly = _reconciliation_weekly_summary(coverage).set_index("Week")

    assert coverage["Invoice status"].tolist() == [
        "Matched by tracking number",
        "Not yet matched",
    ]
    assert coverage.iloc[0]["Invoiced actual cost ex GST"] == pytest.approx(12)
    assert coverage.iloc[0]["Variance ex GST"] == pytest.approx(2)
    assert coverage.iloc[0]["AP pricing zone"] == "Same State Metro"
    assert coverage.iloc[0]["Reporting weight band"] == "1-2 kg"
    assert weekly.loc["FY2027 W03", "Shippit consignments"] == 1
    assert weekly.loc["FY2027 W03", "Matched consignments"] == 1
    assert weekly.loc["FY2027 W03", "Invoice coverage %"] == pytest.approx(100)
    assert weekly.loc["FY2027 W03", "Variance %"] == pytest.approx(20)
    assert weekly.loc["FY2027 W03", "Matched Shippit units"] == pytest.approx(2)
    assert weekly.loc["FY2027 W03", "Modelled CPU ex GST"] == pytest.approx(5)
    assert weekly.loc["FY2027 W03", "Invoiced actual CPU ex GST"] == pytest.approx(6)
    assert weekly.loc["FY2027 W03", "Variance CPU ex GST"] == pytest.approx(1)
    assert weekly.loc["FY2027 W03", "Estimator cost per consignment ex GST"] == pytest.approx(10)
    assert weekly.loc["FY2027 W03", "Estimator cost per unit ex GST"] == pytest.approx(5)
    assert weekly.loc["FY2027 W03", "Invoiced actual cost per consignment ex GST"] == pytest.approx(12)
    assert weekly.loc["FY2027 W03", "Invoiced actual cost per unit ex GST"] == pytest.approx(6)
    assert weekly.loc["FY2027 W04", "Shippit consignments"] == 1
    assert weekly.loc["FY2027 W04", "Shipped carrier"] == "eParcel"
    assert weekly.loc["FY2027 W04", "Shipped service"] == "Metro"
    assert weekly.loc["FY2027 W04", "Matched consignments"] == 0
    assert weekly.loc["FY2027 W04", "Invoice coverage %"] == pytest.approx(0)
    assert pd.isna(weekly.loc["FY2027 W04", "Invoiced actual cost ex GST"])


def test_reconciliation_weekly_matrix_orders_weeks_newest_first() -> None:
    weekly = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "Metro",
                "Week": "FY2027 W03",
                "Invoice coverage %": 25.0,
            },
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "Metro",
                "Week": "FY2027 W04",
                "Invoice coverage %": 75.0,
            },
        ]
    )

    matrix = _reconciliation_weekly_matrix(weekly, "Invoice coverage %")

    assert matrix.columns.tolist() == [
        "CFC",
        "Shipped carrier",
        "Shipped service",
        "FY2027 W04",
        "FY2027 W03",
    ]
    assert matrix.iloc[0]["FY2027 W04"] == pytest.approx(75.0)
    assert matrix.iloc[0]["FY2027 W03"] == pytest.approx(25.0)


def test_dashboard_weekly_history_matrix_starts_with_invoice_verification() -> None:
    weekly = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "eParcel",
                "Week": "FY2027 W03",
                "Shippit consignments": 10,
                "Matched consignments": 5,
                "Estimator cost ex GST": 100.0,
            },
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "eParcel",
                "Week": "FY2027 W04",
                "Shippit consignments": 20,
                "Matched consignments": 20,
                "Estimator cost ex GST": 240.0,
            },
        ]
    )

    matrix = _dashboard_weekly_history_matrix(weekly, "Estimator cost ex GST")

    assert matrix.columns.tolist() == [
        "CFC",
        "Shipped carrier",
        "Shipped service",
        "FY2027 W04",
        "FY2027 W03",
    ]
    assert matrix.iloc[0]["CFC"] == "Invoice verified %"
    assert matrix.iloc[0]["FY2027 W04"] == pytest.approx(100.0)
    assert matrix.iloc[0]["FY2027 W03"] == pytest.approx(50.0)
    assert matrix.iloc[1]["CFC"] == "MOOREBANK"
    assert matrix.iloc[1]["FY2027 W04"] == pytest.approx(240.0)
    assert matrix.iloc[1]["FY2027 W03"] == pytest.approx(100.0)


def test_coverage_retains_estimator_shippit_and_invoiced_physical_evidence() -> None:
    stage3 = pd.DataFrame(
        [
            {
                "invoice_number": "1",
                "actual_carrier": "Couriers Please",
                "actual_service": "Couriers Please",
                "carrier_tracking_number": "TRACK1",
                "ui_site": "MOOREBANK",
                "despatch_date": "2026-07-15",
                "kmart_accounting_year": 2027,
                "kmart_accounting_week_number_in_year": 3,
                "actual_est_cost_ex_gst": 10.0,
                "est_chargeable_weight_kg": 7.0,
                "est_packed_sku_weight_kg": 2.0,
                "est_volumetric_weight_kg": 7.0,
                "est_parcel_cube_total_cm3": 20000.0,
                "shippit_chargeable_weight_kg": 3.0,
                "shippit_dead_weight_kg": 2.5,
                "shippit_volumetric_weight_kg": 3.0,
                "shippit_chargeable_cube_cm3": 12000.0,
                "actual_parcel_count": 1,
                "parcel_units_total": 2,
            }
        ]
    )
    detail = pd.DataFrame(
        [
            {
                "stage3_invoice_number": "1",
                "stage3_tracking_number": "TRACK1",
                "actual_invoice_cost_ex_gst": 12.0,
                "actual_base_cost_ex_gst": 12.0,
                "actual_chargeable_weight_kg": 8.0,
                "actual_rounded_chargeable_weight_kg": 8.0,
                "actual_billed_weight_kg": 8.0,
                "actual_cubic_weight_kg": 6.0,
                "actual_total_volume_m3": 0.03,
            }
        ]
    )

    coverage, _ = _reconciliation_coverage(stage3, detail)
    row = coverage.iloc[0]

    assert row["Estimator cube m3"] == pytest.approx(0.02)
    assert row["Estimator chargeable weight kg"] == pytest.approx(7.0)
    assert row["Shippit chargeable cube m3"] == pytest.approx(0.012)
    assert row["Shippit chargeable weight kg"] == pytest.approx(3.0)
    assert row["Invoiced actual cube m3"] == pytest.approx(0.03)
    assert row["Invoiced actual chargeable weight kg"] == pytest.approx(8.0)


def test_combined_history_coalesces_physical_evidence_from_preferred_rows() -> None:
    matched = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRACK1",
                "Invoice status": "Matched",
                "Modelled cost ex GST": 10.0,
                "Estimator cube m3": float("nan"),
                "coverage_refresh_timestamp_utc": "2026-07-20T00:00:00Z",
            }
        ]
    )
    live = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRACK1",
                "Invoice status": "Not yet matched",
                "Modelled cost ex GST": 10.0,
                "Estimator cube m3": 0.02,
                "Shippit chargeable cube m3": 0.012,
                "coverage_refresh_timestamp_utc": "2026-09-01T00:00:00Z",
            }
        ]
    )

    combined = _combine_coverage_frames([matched, live])

    assert len(combined) == 1
    assert combined.iloc[0]["Invoice status"] == "Matched"
    assert combined.iloc[0]["Estimator cube m3"] == pytest.approx(0.02)
    assert combined.iloc[0]["Shippit chargeable cube m3"] == pytest.approx(0.012)


def test_dashboard_weekly_history_trend_uses_appropriate_aggregates() -> None:
    weekly = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "eParcel",
                "Week": "FY2027 W03",
                "Shippit consignments": 10,
                "Matched consignments": 5,
                "Invoice coverage %": 50.0,
                "Estimator cost ex GST": 100.0,
                "Estimator Shippit units": 20.0,
                "Matched modelled cost ex GST": 40.0,
                "Invoiced actual cost ex GST": 50.0,
                "Variance ex GST": 10.0,
                "Estimator costed consignments": 10,
                "Estimator costed parcels": 10,
                "Matched Shippit units": 10.0,
                "Matched Shippit parcels": 5.0,
                "Matched records with cost": 5,
            },
            {
                "CFC": "MOOREBANK",
                "Shipped carrier": "eParcel",
                "Shipped service": "eParcel",
                "Week": "FY2027 W04",
                "Shippit consignments": 20,
                "Matched consignments": 10,
                "Invoice coverage %": 50.0,
                "Estimator cost ex GST": 240.0,
                "Estimator Shippit units": 40.0,
                "Matched modelled cost ex GST": 120.0,
                "Invoiced actual cost ex GST": 132.0,
                "Variance ex GST": 12.0,
                "Estimator costed consignments": 20,
                "Estimator costed parcels": 20,
                "Matched Shippit units": 20.0,
                "Matched Shippit parcels": 10.0,
                "Matched records with cost": 10,
            },
        ]
    )

    filtered = _filter_dashboard_weekly_history(weekly, "MOOREBANK", "eParcel")

    assert len(filtered) == 2
    assert _weekly_history_trend(filtered, "Invoice coverage %")["Value"].tolist() == [50.0, 50.0]
    assert _weekly_history_trend(filtered, "Variance %")["Value"].tolist() == [25.0, 10.0]
    assert _weekly_history_trend(filtered, "Estimator CPU ex GST")["Value"].tolist() == [5.0, 6.0]
    assert _weekly_history_trend(filtered, "Estimator cost per consignment ex GST")["Value"].tolist() == [10.0, 12.0]
    assert _weekly_history_trend(filtered, "Invoiced actual cost per parcel ex GST")["Value"].tolist() == [10.0, 13.2]


def test_reconciliation_store_upsert_retains_old_rows_and_replaces_refreshed_rows() -> None:
    existing = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Invoice status": "Not yet matched",
            },
            {
                "CFC": "TRUGANINA",
                "Shippit invoice number": "2",
                "Shippit tracking number": "TRK2",
                "Invoice status": "Matched",
            },
        ]
    )
    current = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Invoice status": "Matched",
            },
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "3",
                "Shippit tracking number": "TRK3",
                "Invoice status": "Not yet matched",
            },
        ]
    )

    result = _upsert_coverage(existing, current)

    assert len(result) == 3
    assert result.loc[result["Shippit invoice number"] == "1", "Invoice status"].iloc[0] == "Matched"
    assert "2" in result["Shippit invoice number"].tolist()
    assert "3" in result["Shippit invoice number"].tolist()


def test_reconciliation_store_upsert_retains_existing_physical_evidence_when_refresh_is_blank() -> None:
    existing = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Estimator cube m3": 0.12,
                "Invoiced actual cube m3": 0.18,
            }
        ]
    )
    current = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Estimator cube m3": 0.15,
                "Invoiced actual cube m3": float("nan"),
            }
        ]
    )

    result = _upsert_coverage(existing, current)

    assert result.iloc[0]["Estimator cube m3"] == pytest.approx(0.15)
    assert result.iloc[0]["Invoiced actual cube m3"] == pytest.approx(0.18)


def test_reconciliation_store_upsert_retains_columns_missing_from_refresh() -> None:
    existing = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Invoice status": "Matched",
                "Invoiced actual cost ex GST": 12.5,
            }
        ]
    )
    current = pd.DataFrame(
        [
            {
                "CFC": "MOOREBANK",
                "Shippit invoice number": "1",
                "Shippit tracking number": "TRK1",
                "Invoice status": "Not yet matched",
            }
        ]
    )

    result = _upsert_coverage(existing, current)

    assert result.iloc[0]["Invoiced actual cost ex GST"] == pytest.approx(12.5)


def test_quality_breakdown_summarises_weight_cube_and_imputation_signals() -> None:
    frame = pd.DataFrame(
        [
            {
                "ui_site": "MOOREBANK",
                "shippit_carrier": "Couriers Please",
                "est_process_path": "TOTEABLE",
                "est_parcel_count_match": "Y",
                "est_weight_gap_vs_shippit_chargeable_kg": "-4.0",
                "est_cube_gap_vs_shippit_cm3": "-19024",
                "shippit_volumetric_cube_cm3": "49632",
                "cfc_cube_imputed_unit_count": "2",
                "est_parcel_units_total": "3",
            },
            {
                "ui_site": "MOOREBANK",
                "shippit_carrier": "Couriers Please",
                "est_process_path": "TOTEABLE",
                "est_parcel_count_match": "N",
                "est_weight_gap_vs_shippit_chargeable_kg": "2.0",
                "est_cube_gap_vs_shippit_cm3": "0",
                "shippit_volumetric_cube_cm3": "2000",
                "cfc_cube_imputed_unit_count": "0",
                "est_parcel_units_total": "1",
            },
        ]
    )

    breakdown = _quality_breakdown(frame, "shippit_carrier")

    assert breakdown.iloc[0]["Review slice"] == "Couriers Please"
    assert breakdown.iloc[0]["Consignments"] == 2
    assert breakdown.iloc[0]["Parcel match %"] == pytest.approx(50.0)
    assert breakdown.iloc[0]["Median abs chargeable gap kg"] == pytest.approx(3.0)
    assert breakdown.iloc[0]["Imputed units"] == pytest.approx(2.0)
    assert breakdown.iloc[0]["Packed units"] == pytest.approx(4.0)


def test_rbu_mix_counts_units_and_distinct_keycodes_by_size_category() -> None:
    lines = pd.DataFrame(
        [
            {"rbu_description": "LIVING-HOME", "size_category": "Toteable", "sku_id": "100", "quantity_packed": 2},
            {"rbu_description": "LIVING-HOME", "size_category": "Toteable", "sku_id": "100", "quantity_packed": 1},
            {"rbu_description": "LIVING-HOME", "size_category": "Bulky", "sku_id": "200", "quantity_packed": 3},
            {"rbu_description": "APPAREL", "size_category": "No dims", "sku_id": "300", "quantity_packed": 4},
        ]
    )

    mix = _rbu_mix(lines).set_index("RBU description")

    assert mix.loc["LIVING-HOME", "Packed units"] == pytest.approx(6)
    assert mix.loc["LIVING-HOME", "Toteable units"] == pytest.approx(3)
    assert mix.loc["LIVING-HOME", "Bulky units"] == pytest.approx(3)
    assert mix.loc["LIVING-HOME", "Distinct keycodes"] == pytest.approx(2)
    assert mix.loc["APPAREL", "No dims units"] == pytest.approx(4)
