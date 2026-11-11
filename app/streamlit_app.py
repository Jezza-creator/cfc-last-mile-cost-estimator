from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
import importlib
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import altair as alt
import pandas as pd
import streamlit as st

from src.fuel_surcharge_validation import (
    FSC_RULE_COLUMNS,
    invoice_validated_rates,
    load_model_rules,
    validate_model_rules,
)
from src.opportunity_register import (
    DEFAULT_PATH as OPPORTUNITY_REGISTER_PATH,
    read_opportunity_register,
)
# Streamlit reruns the app script without always reloading imported local
# modules. Reload these small rule/classification modules so a UI refresh
# cannot retain an older opportunity schema and mislabel candidates.
import src.carrier_rules as carrier_rules_module
import src.carrier_choice_opportunities as carrier_choice_module

importlib.reload(carrier_rules_module)
importlib.reload(carrier_choice_module)

from src.carrier_choice_opportunities import (
    build_carrier_choice_detail,
    build_carrier_choice_summary,
)
from src.carrier_rules import carrier_rules_dataframe
from src.invoice_refresh import CARRIER_LABELS, check_invoice_availability


st.set_page_config(page_title="Kmart CFC Last Mile Cost Estimator", page_icon="LM", layout="wide")

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
FSC_MODEL_INPUT_PATH = ROOT / "data" / "reference" / "carrier_fuel_surcharge_rates.csv"
FSC_INVOICE_DIRECTORY = OUTPUTS / "carrier_invoices"
REFRESH_STATUS_PATH = OUTPUTS / "refresh_status.json"
INVOICE_REFRESH_STATUS_PATH = OUTPUTS / "invoice_refresh_status.json"
HISTORY_REFRESH_STATUS_PATH = OUTPUTS / "history_refresh_status.json"
REFRESH_LOCK_PATH = OUTPUTS / "refresh_live.lock"
FUTURE_DEVELOPMENT_PATH = ROOT / "data" / "reference" / "future_development_register.csv"
PLM_DIMENSION_COLUMNS = (
    "sku_id",
    "plm_item_name",
    "plm_item_description",
    "plm_shortest_cm",
    "plm_median_cm",
    "plm_longest_cm",
    "plm_instore_gross_weight_kg",
    "plm_gross_cube_cm3",
    "plm_volumetric_weight_kg",
    "plm_size_category",
)
SITE_FILES = {
    "MOOREBANK": {
        "stage2": OUTPUTS / "stage2_moorebank_parcelizer_validation.csv",
        "physicals": OUTPUTS / "stage2_moorebank_parcel_physicals_full.csv",
        "air_norms": OUTPUTS / "stage2_moorebank_air_ratio_norms.csv",
        "keycode_review": OUTPUTS / "stage2_moorebank_parcelizer_keycode_review.csv",
        "assignment_lines": OUTPUTS / "stage1_moorebank_parcel_assignment_lines.csv",
        "plm_dimensions": OUTPUTS / "cfc_plm_variance_report.csv",
        "carrier_validation": OUTPUTS / "stage3_moorebank_carrier_validation.csv",
        "stage3": OUTPUTS / "stage3_moorebank_cost_estimates_full.csv",
    },
    "TRUGANINA": {
        "stage2": OUTPUTS / "stage2_truganina_parcelizer_validation.csv",
        "physicals": OUTPUTS / "stage2_truganina_parcel_physicals_full.csv",
        "air_norms": OUTPUTS / "stage2_truganina_air_ratio_norms.csv",
        "keycode_review": OUTPUTS / "stage2_truganina_parcelizer_keycode_review.csv",
        "assignment_lines": OUTPUTS / "stage1_truganina_parcel_assignment_lines.csv",
        "plm_dimensions": OUTPUTS / "cfc_plm_variance_report.csv",
        "carrier_validation": OUTPUTS / "stage3_truganina_carrier_validation.csv",
        "stage3": OUTPUTS / "stage3_truganina_cost_estimates_full.csv",
    },
}

# Keep operational views column-pruned. Consignment Explorer reads one saved
# history fact row and fetches detailed Stage 1-3 evidence only on demand.
STAGE3_CONTROL_COLUMNS = (
    "invoice_number",
    "shippit_invoice_number",
    "actual_carrier",
    "shippit_carrier",
    "despatch_date",
    "kmart_accounting_year",
    "kmart_accounting_week_number_in_year",
)
STAGE3_DASHBOARD_COLUMNS = (
    *STAGE3_CONTROL_COLUMNS,
    "actual_service",
    "shippit_service",
    "actual_est_service",
    "actual_parcel_count",
    "estimated_parcel_count",
    "parcel_units_total",
    "shippit_dead_weight_kg",
    "shippit_volumetric_weight_kg",
    "actual_est_cost_ex_gst",
    "modelled_total_cost_ex_gst",
    "ap_pricing_zone",
    "reporting_weight_band",
    "est_saving_ex_gst",
    "cheapest_alt_carrier",
    "cheapest_alt_service",
    "cheapest_alt_cost_ex_gst",
)
STAGE3_RECONCILIATION_COLUMNS = (
    *STAGE3_DASHBOARD_COLUMNS,
    "carrier_tracking_number",
    "shippit_tracking_number",
    "shippit_parcel_count",
    "shippit_units_total",
    "est_chargeable_weight_kg",
    "est_packed_sku_weight_kg",
    "est_packed_sku_cube_total_cm3",
    "est_packed_sku_cube_total_m3",
    "est_packed_sku_cube_imputed_flag",
    "est_volumetric_weight_kg",
    "est_parcel_cube_total_cm3",
    "est_parcel_cube_total_m3",
    "shippit_chargeable_weight_kg",
    "shippit_dead_weight_kg",
    "shippit_volumetric_weight_kg",
    "shippit_chargeable_cube_cm3",
    "shippit_volumetric_cube_cm3",
)
EXPLORER_HISTORY_COLUMNS = (
    "Shippit invoice number",
    "Shippit tracking number",
    "CFC",
    "Shipped carrier",
    "Shipped service",
    "Despatch date",
    "Accounting year",
    "Accounting week",
    "Accounting period number",
    "Modelled cost ex GST",
    "Modelled base cost ex GST",
    "Modelled fuel surcharge ex GST",
    "Modelled fuel surcharge %",
    "Modelled fuel surcharge source",
    "Modelled cost weight basis kg",
    "Modelled Allied zone",
    "Modelled Allied total cost ex GST",
    "Modelled AP zone",
    "Modelled Couriers Please zone",
    "Modelled GoExpress zone",
    "Destination suburb",
    "Destination state",
    "Destination postcode",
    "Shippit parcel count",
    "Shippit units",
    "Estimator chargeable weight kg",
    "Estimator packed SKU weight kg",
    "Estimator packed SKU cube m3",
    "Estimator packed SKU cube imputed",
    "Estimator volumetric weight kg",
    "Estimator cube m3",
    "Shippit chargeable weight kg",
    "Shippit dead weight kg",
    "Shippit volumetric weight kg",
    "Shippit chargeable cube m3",
    "AP pricing zone",
    "Reporting weight band",
)
STAGE3_EXPLORER_COLUMNS = (
    "invoice_number",
    "shippit_invoice_number",
    "carrier_tracking_number",
    "shippit_tracking_number",
    "actual_carrier",
    "shippit_carrier",
    "actual_service",
    "shippit_service",
    "actual_est_service",
    "despatch_date",
    "kmart_accounting_year",
    "kmart_accounting_week_number_in_year",
    "kmart_accounting_period_number",
    "actual_est_cost_ex_gst",
    "modelled_total_cost_ex_gst",
    "actual_est_base_cost_ex_gst",
    "actual_est_fuel_surcharge_ex_gst",
    "actual_est_fuel_surcharge_pct",
    "actual_est_fuel_surcharge_source",
    "actual_parcel_count",
    "shippit_parcel_count",
    "parcel_units_total",
    "shippit_units_total",
    "est_chargeable_weight_kg",
    "est_packed_sku_weight_kg",
    "est_packed_sku_cube_total_cm3",
    "est_packed_sku_cube_total_m3",
    "est_packed_sku_cube_imputed_flag",
    "est_parcel_cube_total_cm3",
    "est_parcel_cube_total_m3",
    "est_volumetric_weight_kg",
    "shippit_chargeable_weight_kg",
    "shippit_chargeable_cube_cm3",
    "shippit_dead_weight_kg",
    "shippit_volumetric_weight_kg",
    "shippit_volumetric_cube_cm3",
    "ap_pricing_zone",
    "reporting_weight_band",
    "dest_suburb",
    "dest_state",
    "dest_postcode",
    "cost_weight_basis_kg",
    "allied_zone",
    "allied_total_cost_ex_gst",
    "ap_zone",
    "cp_zone",
    "goexpress_zone",
    "cheapest_alt_cost_ex_gst",
    "cheapest_alt_carrier",
    "cheapest_alt_service",
    "est_saving_ex_gst",
    "cp_pricing_basis_note",
)
STAGE2_DASHBOARD_COLUMNS = (
    "invoice_number",
    "shippit_invoice_number",
    "est_parcel_count_match",
    "est_weight_gap_vs_shippit_chargeable_kg",
    "est_rectangularized_air_ratio_pct_vs_shippit_cube",
)
STAGE2_QUALITY_COLUMNS = (
    *STAGE2_DASHBOARD_COLUMNS,
    "shippit_carrier",
    "est_process_path",
    "est_parcel_contents_size_mix",
    "est_cube_gap_vs_shippit_cm3",
    "shippit_volumetric_cube_cm3",
    "cfc_cube_imputed_unit_count",
    "est_parcel_units_total",
    "est_flag_odd_allocation",
    "est_shape_source_pattern",
)
ASSIGNMENT_QUALITY_COLUMNS = (
    "invoice_number",
    "shippit_invoice_number",
    "rbu_description",
    "size_category",
    "quantity_packed",
    "sku_id",
)
RECONCILIATION_FILES = {
    "couriers_please": OUTPUTS / "carrier_invoices" / "couriers_please_reconciliation.csv",
    "australia_post": OUTPUTS / "carrier_invoices" / "australia_post_reconciliation.csv",
    "gopeople": OUTPUTS / "carrier_invoices" / "gopeople_reconciliation.csv",
    "allied": OUTPUTS / "carrier_invoices" / "allied_reconciliation.csv",
}
RECONCILIATION_SUMMARY_FILES = {
    "couriers_please": OUTPUTS / "carrier_invoices" / "couriers_please_reconciliation_summary.csv",
    "australia_post": OUTPUTS / "carrier_invoices" / "australia_post_reconciliation_summary.csv",
    "gopeople": OUTPUTS / "carrier_invoices" / "gopeople_reconciliation_summary.csv",
    "allied": OUTPUTS / "carrier_invoices" / "allied_reconciliation_summary.csv",
}
HISTORICAL_RECONCILIATION_SUMMARY_FILES = {
    "couriers_please": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "couriers_please_reconciliation_summary.csv",
    "australia_post": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "australia_post_reconciliation_summary.csv",
    "gopeople": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "gopeople_reconciliation_summary.csv",
    "allied": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "allied_reconciliation_summary.csv",
}
ALL_CARRIER_DETAIL_RECONCILIATION_FILES = {
    "couriers_please": (
        RECONCILIATION_FILES["couriers_please"],
        OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "couriers_please_reconciliation.csv",
        OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026_physical_basis" / "couriers_please_reconciliation.csv",
    ),
    "australia_post": (
        RECONCILIATION_FILES["australia_post"],
        OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "australia_post_reconciliation.csv",
        OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000_physical_basis" / "australia_post_reconciliation.csv",
    ),
    "gopeople": (
        RECONCILIATION_FILES["gopeople"],
        OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "gopeople_reconciliation.csv",
    ),
    "allied": (
        RECONCILIATION_FILES["allied"],
        OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "allied_reconciliation.csv",
    ),
}
ALL_CARRIER_HISTORY_STORE = OUTPUTS / "reconciliation_store" / "live"
FINANCE_CALENDAR_PATH = OUTPUTS / "finance_period_week_map.csv"
VALIDATION_DATASETS = {
    "All carrier history": {
        "description": "Complete persisted consignment history, updated in place as shipments and invoices change.",
        "stage3": None,
        "reconciliation": RECONCILIATION_FILES,
        "reconciliation_summary": HISTORICAL_RECONCILIATION_SUMMARY_FILES,
        "detail_reconciliation": ALL_CARRIER_DETAIL_RECONCILIATION_FILES,
        "persisted": {
            "coverage": ALL_CARRIER_HISTORY_STORE / "consignment_invoice_coverage.csv",
            "weekly": ALL_CARRIER_HISTORY_STORE / "weekly_invoice_summary.csv",
        },
    },
    "Live rolling 14-day outputs": {
        "description": "Current operational Stage 3 outputs and live reconciliation files.",
        "stage3": None,
        "reconciliation": RECONCILIATION_FILES,
        "reconciliation_summary": RECONCILIATION_SUMMARY_FILES,
        "persisted": {
            "coverage": OUTPUTS / "reconciliation_store" / "live" / "consignment_invoice_coverage.csv",
            "weekly": OUTPUTS / "reconciliation_store" / "live" / "weekly_invoice_summary.csv",
        },
    },
    "CP July 2026 validation snapshot": {
        "description": "Historical Stage 3 and CP reconciliation outputs retained for validation.",
        "stage3": {
            "MOOREBANK": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026" / "stage3_moorebank_cost_estimates_full.csv",
            "TRUGANINA": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026" / "stage3_truganina_cost_estimates_full.csv",
        },
        "reconciliation": {
            "couriers_please": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026_physical_basis" / "couriers_please_reconciliation.csv",
        },
        "reconciliation_summary": {
            "couriers_please": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026_physical_basis" / "couriers_please_reconciliation_summary.csv",
        },
        "persisted": {
            "coverage": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026_physical_basis" / "reconciliation_store" / "cp_july_2026" / "consignment_invoice_coverage.csv",
            "weekly": OUTPUTS / "carrier_invoice_snapshots" / "cp_july_2026_physical_basis" / "reconciliation_store" / "cp_july_2026" / "weekly_invoice_summary.csv",
        },
    },
    "Australia Post July 2026 sample snapshot": {
        "description": "Historical Stage 3 and 1,000-consignment AP sample retained for validation.",
        "stage3": {
            "MOOREBANK": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000" / "stage_outputs" / "stage3_moorebank_cost_estimates_full.csv",
            "TRUGANINA": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000" / "stage_outputs" / "stage3_truganina_cost_estimates_full.csv",
        },
        "reconciliation": {
            "australia_post": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000_physical_basis" / "australia_post_reconciliation.csv",
        },
        "reconciliation_summary": {
            "australia_post": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000_physical_basis" / "australia_post_reconciliation_summary.csv",
        },
        "persisted": {
            "coverage": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000_physical_basis" / "reconciliation_store" / "consignment_invoice_coverage.csv",
            "weekly": OUTPUTS / "carrier_invoice_snapshots" / "australia_post_sample_2026-07-15_2026-07-31_1000_physical_basis" / "reconciliation_store" / "weekly_invoice_summary.csv",
        },
    },
    "FY2027 P1W1-P2W3 historical snapshot": {
        "description": "Historical Stage 1-3 outputs and carrier invoice validation for Finance periods P1W1-P2W3.",
        "stage3": {
            "MOOREBANK": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "stage_outputs" / "stage3_moorebank_cost_estimates_full.csv",
            "TRUGANINA": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "stage_outputs" / "stage3_truganina_cost_estimates_full.csv",
        },
        "reconciliation": {
            "couriers_please": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "couriers_please_reconciliation.csv",
            "australia_post": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "australia_post_reconciliation.csv",
            "gopeople": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "gopeople_reconciliation.csv",
            "allied": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "carrier_invoices" / "allied_reconciliation.csv",
        },
        "reconciliation_summary": HISTORICAL_RECONCILIATION_SUMMARY_FILES,
        "persisted": {
            "coverage": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "reconciliation_store" / "fy2027_p1w1_p2w3" / "consignment_invoice_coverage.csv",
            "weekly": OUTPUTS / "carrier_invoice_snapshots" / "fy2027_p1w1_p2w3" / "reconciliation_store" / "fy2027_p1w1_p2w3" / "weekly_invoice_summary.csv",
        },
    },
}
AP_PRICING_ZONE_ORDER = [
    "Same State Local",
    "Same State Metro",
    "Same State Remote",
    "Near State Capital",
    "Near State Metro",
    "Near State Remote",
    "Distant State Capital",
    "Distant State Metro",
    "Distant State Remote",
]
REPORTING_WEIGHT_BAND_ORDER = [
    "0-1 kg",
    "1-2 kg",
    "2-5 kg",
    "5-10 kg",
    "10-15 kg",
    "15-22 kg",
    "22-32 kg",
    "32-45 kg",
    "45+ kg",
]
METRIC_OPTIONS = {
    "Consignments": "consignments",
    "Consignments %": "consignments_pct",
    "Shipped parcels": "shipped_parcels",
    "Shipped parcels %": "shipped_parcels_pct",
    "Shipped units": "shipped_units",
    "Shipped units %": "shipped_units_pct",
    "Units per consignment": "units_per_consignment",
    "Units per parcel": "units_per_parcel",
    "Parcels per consignment": "parcels_per_consignment",
    "Cost per consignment": "cost_per_consignment",
    "Cost per parcel": "cost_per_parcel",
    "Cost per unit": "cost_per_unit",
    "Modelled shipped-carrier cost ex GST": "modelled_cost",
}
REPORTING_PERIOD_OPTIONS = [
    "Current accounting week to date",
    "Previous accounting week",
    "Single accounting week",
    "All available weeks",
]


def _inject_style() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Manrope:wght@400;500;600;700;800&display=swap');
        html, body, [class*="css"] { font-family: 'Manrope', sans-serif; }
        [data-testid="stSidebar"] { background: #102a2b; }
        [data-testid="stSidebar"] * { color: #f7fbf8; }
        [data-testid="stSidebar"] [data-baseweb="select"],
        [data-testid="stSidebar"] [data-baseweb="select"] > div,
        [data-testid="stSidebar"] [data-baseweb="input"],
        [data-testid="stSidebar"] [role="combobox"] { background: #e7efe9 !important; }
        [data-testid="stSidebar"] [data-baseweb="select"] *,
        [data-testid="stSidebar"] [data-baseweb="input"] *,
        [data-testid="stSidebar"] [role="combobox"] *,
        [data-testid="stSidebar"] input { color: #0b2021 !important; -webkit-text-fill-color: #0b2021 !important; opacity: 1 !important; }
        [data-testid="stSidebar"] [data-testid="stButton"] button,
        [data-testid="stSidebar"] [data-testid="stButton"] button * { color: #0b2021 !important; -webkit-text-fill-color: #0b2021 !important; background: #e7efe9; font-weight: 700; }
        .eyebrow { color: #dc6e3f; font-family: 'DM Mono', monospace; font-size: 0.78rem; letter-spacing: 0.13em; text-transform: uppercase; }
        .title { color: #102a2b; font-size: 2.45rem; font-weight: 800; line-height: 1.05; margin: 0.15rem 0 0.5rem; }
        .subtitle { color: #526766; font-size: 1.02rem; max-width: 52rem; }
        [data-testid="stMetric"] { background: #f7f3ea; border: 1px solid #e8dfd1; border-radius: 10px; padding: 0.75rem; }
        [data-testid="stMetricValue"] { color: #102a2b; font-weight: 800; }
        .reconciliation-metric-card { border: 1px solid; border-radius: 10px; padding: 0.75rem; min-height: 7.1rem; margin-bottom: 0.5rem; }
        .reconciliation-metric-card.physical { background: #fff1df; border-color: #e5bd8a; }
        .reconciliation-metric-card.residual { background: #e8f1f5; border-color: #a8c4d0; }
        .reconciliation-metric-label { color: #234744; font-size: 0.9rem; line-height: 1.25; }
        .reconciliation-metric-value { color: #102a2b; font-size: 2.25rem; font-weight: 800; line-height: 1.15; margin-top: 0.45rem; }
        .reconciliation-metric-delta { color: #234744; font-size: 0.88rem; margin-top: 0.35rem; }
        .panel { background: #eef5f0; border-left: 4px solid #2c8674; border-radius: 0 10px 10px 0; padding: 0.8rem 1rem; color: #234744; }
        .future-banner { background: #fff7e6; border: 1px solid #e5bd8a; border-left: 4px solid #dc6e3f; border-radius: 0 10px 10px 0; padding: 0.85rem 1rem; color: #5b3d20; }
        .future-status { display: inline-block; background: #f4ead8; border: 1px solid #d9bf94; border-radius: 999px; color: #5b3d20; font-size: 0.82rem; font-weight: 700; padding: 0.2rem 0.65rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_reconciliation_metric_card(
    column: object,
    label: str,
    value: str,
    delta: str = "",
    tone: str = "residual",
) -> None:
    """Render a tinted reconciliation card without changing global metric styling."""
    delta_markup = (
        f'<div class="reconciliation-metric-delta">{escape(delta)}</div>'
        if delta
        else ""
    )
    column.markdown(
        f"""
        <div class="reconciliation-metric-card {escape(tone)}">
            <div class="reconciliation-metric-label">{escape(label)}</div>
            <div class="reconciliation-metric-value">{escape(value)}</div>
            {delta_markup}
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False)
def _read_csv(
    path_text: str,
    modified_ns: int | None,
    usecols: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    # Include the file timestamp in the cache key so refreshed pipeline outputs appear automatically.
    del modified_ns
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()
    read_kwargs: dict[str, object] = {
        "low_memory": False,
        "dtype": {"invoice_number": "string", "shippit_invoice_number": "string"},
    }
    if usecols is not None:
        try:
            available_columns = set(pd.read_csv(path, nrows=0).columns)
        except (OSError, pd.errors.ParserError):
            return pd.DataFrame()
        selected_columns = [column for column in usecols if column in available_columns]
        if not selected_columns:
            return pd.DataFrame()
        read_kwargs["usecols"] = selected_columns
    try:
        return pd.read_csv(path, **read_kwargs)
    except (OSError, pd.errors.ParserError, ValueError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False, max_entries=128)
def _saved_consignment_history(
    path_text: str,
    modified_ns: int,
    file_size: int,
    site: str,
    invoice_number: str,
) -> pd.DataFrame:
    """Read one consignment from the durable history store without loading all history."""
    del modified_ns, file_size
    path = Path(path_text)
    if not path.exists() or not invoice_number:
        return pd.DataFrame()
    try:
        available = set(pd.read_csv(path, nrows=0).columns)
        usecols = [column for column in EXPLORER_HISTORY_COLUMNS if column in available]
        if not {"Shippit invoice number", "CFC"}.issubset(usecols):
            return pd.DataFrame()
        dtype = {
            column: "string"
            for column in ("Shippit invoice number", "Shippit tracking number", "CFC")
            if column in usecols
        }
        for chunk in pd.read_csv(
            path,
            low_memory=False,
            usecols=usecols,
            dtype=dtype,
            chunksize=100_000,
        ):
            invoice_mask = chunk["Shippit invoice number"].map(
                _normalise_consignment_number
            ).eq(invoice_number)
            site_mask = chunk["CFC"].fillna("").astype(str).str.upper().eq(site)
            matches = chunk.loc[invoice_mask & site_mask]
            if not matches.empty:
                return _coverage_history_as_stage3(matches)
    except (OSError, pd.errors.ParserError, ValueError, UnicodeDecodeError):
        return pd.DataFrame()
    return pd.DataFrame()


@st.cache_data(show_spinner=False, max_entries=128)
def _current_stage3_consignment(
    path_text: str,
    modified_ns: int,
    file_size: int,
    site: str,
    invoice_number: str,
) -> pd.DataFrame:
    """Find one current rolling-window Stage 3 row without loading the full export."""
    del modified_ns, file_size
    path = Path(path_text)
    if not path.exists() or not invoice_number:
        return pd.DataFrame()
    try:
        available = set(pd.read_csv(path, nrows=0).columns)
        usecols = [column for column in STAGE3_EXPLORER_COLUMNS if column in available]
        invoice_column = "invoice_number" if "invoice_number" in usecols else "shippit_invoice_number"
        if invoice_column not in usecols:
            return pd.DataFrame()
        dtype = {
            column: "string"
            for column in (
                "invoice_number",
                "shippit_invoice_number",
                "carrier_tracking_number",
                "shippit_tracking_number",
            )
            if column in usecols
        }
        for chunk in pd.read_csv(
            path,
            low_memory=False,
            usecols=usecols,
            dtype=dtype,
            chunksize=100_000,
        ):
            matches = chunk.loc[
                chunk[invoice_column].map(_normalise_consignment_number).eq(invoice_number)
            ]
            if not matches.empty:
                matches = matches.copy()
                matches["ui_site"] = site
                return matches
    except (OSError, pd.errors.ParserError, ValueError, UnicodeDecodeError):
        return pd.DataFrame()
    return pd.DataFrame()


def _number(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(0.0, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").fillna(0.0)


def _load_status_file(path: Path) -> dict[str, object]:
    candidates = [path, path.with_name(f"{path.stem}.fallback{path.suffix}")]
    existing = sorted(
        (candidate for candidate in candidates if candidate.exists()),
        key=lambda candidate: candidate.stat().st_mtime_ns,
        reverse=True,
    )
    for candidate in existing:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _load_refresh_status() -> dict[str, object]:
    return _load_status_file(REFRESH_STATUS_PATH)


def _refresh_lock_is_active() -> bool:
    """Return whether the shared refresh lock appears to be live."""
    if not REFRESH_LOCK_PATH.exists():
        return False
    try:
        age_seconds = pd.Timestamp.now(tz="UTC").timestamp() - REFRESH_LOCK_PATH.stat().st_mtime
    except OSError:
        return True
    return age_seconds <= 24 * 60 * 60


def _start_refresh_job(
    module: str,
    arguments: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Launch a refresh module independently of the Streamlit request."""
    if _refresh_lock_is_active():
        return False, "Another refresh is already running. Check the refresh status above before starting a new one."

    command = [sys.executable, "-m", module, *arguments]
    popen_kwargs: dict[str, object] = {
        "cwd": ROOT,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except OSError as error:
        return False, f"Could not start {module}: {error}"

    # The running process writes its own status and log. Clear cached CSV
    # reads so the next app rerun picks up the completed output files.
    st.cache_data.clear()
    return True, f"Started {module} in the background (process {process.pid})."


def _render_refresh_status() -> None:
    daily = _load_refresh_status()
    if daily:
        state = str(daily.get("status", "")).lower()
        start_date = daily.get("extraction_start_date", "")
        end_date = daily.get("extraction_end_date", "")
        window = f"{start_date} to {end_date}" if start_date and end_date else ""
        if state == "running":
            st.info(
                f"Daily refresh in progress ({daily.get('completed_steps', 0)}/"
                f"{daily.get('total_steps', 0)} steps): {daily.get('current_step', 'starting')}."
            )
        elif state == "failed":
            st.warning(
                f"Last daily refresh failed at {daily.get('current_step', 'an unknown step')}. "
                f"Check the refresh log: {daily.get('log_file', 'not recorded')}."
            )
        elif state == "succeeded":
            st.caption(
                f"Last daily refresh: {daily.get('finished_at_utc', 'unknown')} UTC"
                + (f" | extraction window: {window}" if window else "")
            )


def _render_invoice_refresh_controls() -> None:
    """Render availability checks and carrier-specific invoice refresh actions."""
    st.subheader("Invoice data refresh")
    st.caption(
        "Check each carrier's invoice source before refreshing. Australia Post is checked in Snowflake; "
        "Allied, Couriers Please, and GoPeople are checked from their Excel workbooks. "
        "A carrier refresh matches new actuals to retained shipment history and updates the Finance coverage summary without rerunning Stage 1-3."
    )

    carrier_keys = list(CARRIER_LABELS)
    selected_carrier = st.selectbox(
        "Invoice carrier to refresh",
        carrier_keys,
        format_func=lambda value: CARRIER_LABELS[value],
        key="invoice_refresh_carrier",
    )
    check_col, refresh_col = st.columns(2)
    with check_col:
        check_clicked = st.button(
            "Check for new invoice data",
            key="check_invoice_availability",
            help="Query AP and inspect the Excel carrier input folders without ingesting or changing reconciliation outputs.",
            width="stretch",
        )
    if check_clicked:
        with st.spinner("Checking AP Snowflake and carrier invoice workbooks..."):
            try:
                st.session_state["invoice_availability_status"] = check_invoice_availability()
                st.session_state["invoice_availability_checked_at"] = pd.Timestamp.now(tz="UTC").strftime(
                    "%d/%m/%Y %H:%M UTC"
                )
            except Exception as error:
                st.session_state["invoice_availability_status"] = []
                st.error(f"Invoice availability check failed: {error}")

    statuses = st.session_state.get("invoice_availability_status", [])
    if statuses:
        status_rows = pd.DataFrame(
            [
                {
                    "Carrier": row.get("carrier", ""),
                    "Source": row.get("source_type", ""),
                    "Status": row.get("status", ""),
                    "New data": "Yes" if row.get("new_available") else "No",
                    "Latest service / source date": row.get("latest_source_date", ""),
                    "Latest billing date": row.get("latest_billing_date", ""),
                    "Last processed": row.get("last_processed", ""),
                    "Detail": row.get("detail", ""),
                }
                for row in statuses
            ]
        )
        st.dataframe(status_rows, width="stretch", hide_index=True)
        checked_at = st.session_state.get("invoice_availability_checked_at")
        if checked_at:
            st.caption(f"Availability last checked: {checked_at}.")

        new_carriers = [row.get("carrier", "") for row in statuses if row.get("new_available")]
        if new_carriers:
            st.warning(
                "New invoice information is available for: " + ", ".join(new_carriers) + ". "
                "Select the relevant carrier and run its reconciliation refresh."
            )
        elif all(str(row.get("status", "")).endswith("failed") for row in statuses):
            st.warning("No invoice source could be checked successfully.")
        else:
            st.success("No new invoice information was detected in the last check.")
    else:
        st.info("Run the availability check before starting a carrier invoice refresh.")

    selected_status = next(
        (row for row in statuses if row.get("carrier_key") == selected_carrier),
        None,
    )
    refresh_enabled = bool(selected_status and selected_status.get("can_refresh"))
    with refresh_col:
        refresh_clicked = st.button(
            f"Refresh {CARRIER_LABELS[selected_carrier]} reconciliation",
            key="refresh_selected_invoice_carrier",
            type="primary",
            disabled=not refresh_enabled,
            help="Run only the selected carrier's ingest, reconciliation, and live coverage update.",
            width="stretch",
        )
    if refresh_clicked and selected_status:
        arguments = ["--carrier", selected_carrier]
        if selected_carrier == "australia_post":
            start_date = str(selected_status.get("refresh_start_date") or "")
            end_date = str(selected_status.get("refresh_end_date") or "")
            if start_date and end_date:
                arguments.extend(["--start-date", start_date, "--end-date", end_date])
        started, message = _start_refresh_job(
            "src.invoice_refresh",
            tuple(arguments),
        )
        if started:
            st.success(message)
        else:
            st.warning(message)

    refresh_status = _load_status_file(INVOICE_REFRESH_STATUS_PATH)
    if refresh_status:
        refresh_state = str(refresh_status.get("status", "")).lower()
        refresh_carrier = refresh_status.get("carrier", "selected carrier")
        if refresh_state == "running":
            st.info(
                f"{refresh_carrier} invoice refresh in progress "
                f"({refresh_status.get('completed_steps', 0)}/{refresh_status.get('total_steps', 0)} steps): "
                f"{refresh_status.get('current_step', 'starting')}."
            )
        elif refresh_state == "failed":
            st.warning(
                f"Last {refresh_carrier} invoice refresh failed at "
                f"{refresh_status.get('current_step', 'an unknown step')}. "
                f"Check the log: {refresh_status.get('log_file', 'not recorded')}."
            )
        elif refresh_state == "succeeded":
            st.caption(
                f"Last {refresh_carrier} invoice refresh: "
                f"{refresh_status.get('finished_at_utc', 'unknown')} UTC."
            )


@st.cache_data(show_spinner=False)
def _site_frame_cached(
    site: str,
    dataset: str,
    usecols: tuple[str, ...] | None,
    signatures: tuple[tuple[str, int, int], ...],
) -> pd.DataFrame:
    del signatures
    if site == "ALL":
        frames = []
        for site_key, paths in SITE_FILES.items():
            path = paths[dataset]
            modified_ns = path.stat().st_mtime_ns if path.exists() else None
            frame = _read_csv(str(path), modified_ns, usecols).copy()
            if not frame.empty:
                frame["ui_site"] = site_key
                frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    path = SITE_FILES[site][dataset]
    modified_ns = path.stat().st_mtime_ns if path.exists() else None
    frame = _read_csv(str(path), modified_ns, usecols).copy()
    if not frame.empty:
        frame["ui_site"] = site
    return frame


def _site_frame(
    site: str,
    dataset: str,
    usecols: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    site_keys = list(SITE_FILES) if site == "ALL" else [site]
    signatures = []
    for site_key in site_keys:
        path = SITE_FILES[site_key][dataset]
        if path.exists():
            stat = path.stat()
            signatures.append((str(path), stat.st_mtime_ns, stat.st_size))
        else:
            signatures.append((str(path), 0, 0))
    return _site_frame_cached(site, dataset, usecols, tuple(signatures))


def _validation_stage3(
    site: str,
    dataset_name: str,
    usecols: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Load the Stage 3 outputs aligned to the selected reconciliation dataset."""
    dataset = VALIDATION_DATASETS[dataset_name]
    stage3_paths = dataset["stage3"]
    if stage3_paths is None:
        return _site_frame(site, "stage3", usecols)

    site_keys = list(stage3_paths) if site == "ALL" else [site]
    frames = []
    for site_key in site_keys:
        path = stage3_paths[site_key]
        modified_ns = path.stat().st_mtime_ns if path.exists() else None
        frame = _read_csv(str(path), modified_ns, usecols).copy()
        if not frame.empty:
            frame["ui_site"] = site_key
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _available_validation_datasets() -> list[str]:
    available = []
    for name, dataset in VALIDATION_DATASETS.items():
        stage3_paths = dataset["stage3"]
        stage3_available = stage3_paths is None or all(path.exists() for path in stage3_paths.values())
        reconciliation_available = any(path.exists() for path in dataset["reconciliation"].values())
        persisted_coverage = dataset.get("persisted", {}).get("coverage")
        persisted_available = persisted_coverage is not None and persisted_coverage.exists()
        if stage3_available and (reconciliation_available or persisted_available):
            available.append(name)
    return available


def _load_persisted_reconciliation(
    dataset_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    persisted = VALIDATION_DATASETS[dataset_name].get("persisted", {})
    coverage_path = persisted.get("coverage")
    weekly_path = persisted.get("weekly")
    coverage = (
        _read_csv(str(coverage_path), coverage_path.stat().st_mtime_ns)
        if coverage_path is not None and coverage_path.exists()
        else pd.DataFrame()
    )
    weekly = (
        _read_csv(str(weekly_path), weekly_path.stat().st_mtime_ns)
        if weekly_path is not None and weekly_path.exists()
        else pd.DataFrame()
    )
    return coverage, weekly


def _load_reconciliation_summaries(dataset_name: str) -> pd.DataFrame:
    """Load small saved carrier summaries for fast aggregate diagnostics."""
    summary_files = VALIDATION_DATASETS[dataset_name].get("reconciliation_summary", {})
    frames = []
    for carrier, path in summary_files.items():
        if not path.exists():
            continue
        frame = _read_csv(str(path), path.stat().st_mtime_ns).copy()
        if not frame.empty:
            frame["_summary_carrier"] = carrier
            frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _reconciliation_physical_overview(
    summary: pd.DataFrame,
    site: str,
    shipped_carrier: str,
    coverage: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return a fast carrier/CFC view of dim/weight variance contribution."""
    columns = [
        "CFC",
        "Carrier",
        "Complete-cost records",
        "Total observed variance ex GST",
        "Total observed variance %",
        "Dim/weight records",
        "Dim/weight variance ex GST",
        "Dim/weight share of signed variance %",
        "Residual variance ex GST",
    ]
    if summary.empty or "site" not in summary:
        return pd.DataFrame(columns=columns)

    work = summary.copy()
    site_values = work["site"].fillna("").astype(str).str.strip()
    work = work.loc[site_values.ne("") & ~site_values.str.upper().eq("UNMATCHED")].copy()
    if site != "ALL":
        work = work.loc[work["site"].fillna("").astype(str).str.upper().eq(site)]
    if work.empty:
        return pd.DataFrame(columns=columns)

    carrier_labels = {
        "couriers_please": "Couriers Please",
        "australia_post": "Australia Post",
        "gopeople": "GoPeople",
        "allied": "Allied Express",
    }
    work["Carrier"] = work["_summary_carrier"].map(carrier_labels).fillna(
        work["_summary_carrier"]
    )
    if shipped_carrier != "All shipped carriers":
        carrier_aliases = {
            "Australia Post": {"Australia Post", "eParcel", "eParcel Metro"},
            "eParcel": {"Australia Post", "eParcel"},
            "eParcel Metro": {"Australia Post", "eParcel Metro"},
        }
        allowed = carrier_aliases.get(shipped_carrier, {shipped_carrier})
        work = work.loc[work["Carrier"].isin(allowed)]
    if work.empty:
        return pd.DataFrame(columns=columns)

    actual = _numeric_from_columns(work, ("actual_invoice_cost_ex_gst",))
    modelled = _numeric_from_columns(
        work,
        (
            "modelled_cp_cost_including_fuel_ex_gst",
            "modelled_ap_cost_including_fuel_ex_gst",
            "modelled_allied_cost_ex_gst",
            "modelled_goexpress_cost_ex_gst",
        ),
    )
    records = _numeric_from_columns(
        work,
        (
            "matched_consignment_groups",
            "billed_consignment_groups",
            "billed_consignments",
            "billed_jobs",
        ),
    )
    primary = work.get(
        "primary_cost_driver", pd.Series("", index=work.index)
    ).fillna("").astype(str).str.upper()
    physical = primary.str.contains(
        "PHYSICAL_BASIS|CHARGEABLE_WEIGHT_MISMATCH|CUBE_MISMATCH",
        regex=True,
        na=False,
    )
    work["_actual"] = actual
    work["_modelled"] = modelled
    work["_variance"] = actual - modelled
    work["_records"] = records
    work["_physical"] = physical
    work = work.loc[work["_actual"].notna() & work["_modelled"].notna()].copy()
    if work.empty:
        return pd.DataFrame(columns=columns)

    coverage_totals: dict[tuple[str, str], dict[str, float]] = {}
    if coverage is not None and not coverage.empty:
        coverage_work = coverage.copy()
        coverage_modelled = pd.to_numeric(
            coverage_work.get("Modelled cost ex GST"), errors="coerce"
        )
        coverage_actual = pd.to_numeric(
            coverage_work.get("Invoiced actual cost ex GST"), errors="coerce"
        )
        coverage_status = coverage_work.get(
            "Invoice status",
            pd.Series("Not yet matched", index=coverage_work.index),
        ).astype(str)
        complete = coverage_status.ne("Not yet matched") & coverage_modelled.gt(0) & coverage_actual.notna()
        if complete.any() and "CFC" in coverage_work and "Shipped carrier" in coverage_work:
            coverage_work = coverage_work.loc[complete].copy()
            coverage_work["_coverage_modelled"] = coverage_modelled.loc[complete]
            coverage_work["_coverage_actual"] = coverage_actual.loc[complete]
            coverage_work["_coverage_carrier"] = coverage_work["Shipped carrier"].astype(str).str.strip()
            coverage_work.loc[
                coverage_work["_coverage_carrier"].isin({"eParcel", "Australia Post"}),
                "_coverage_carrier",
            ] = "Australia Post"
            for (coverage_cfc, coverage_carrier), group in coverage_work.groupby(
                ["CFC", "_coverage_carrier"], dropna=False
            ):
                modelled_total = float(group["_coverage_modelled"].sum())
                actual_total = float(group["_coverage_actual"].sum())
                coverage_totals[(str(coverage_cfc).strip().upper(), str(coverage_carrier))] = {
                    "records": float(len(group)),
                    "modelled": modelled_total,
                    "variance": actual_total - modelled_total,
                }

    rows = []
    for (cfc, carrier), group in work.groupby(["site", "Carrier"], dropna=False, sort=True):
        record_total = group["_records"].sum(min_count=1)
        if pd.isna(record_total):
            record_total = float(len(group))
        physical_group = group.loc[group["_physical"]]
        physical_records = physical_group["_records"].sum(min_count=1)
        if pd.isna(physical_records):
            physical_records = float(len(physical_group))
        summary_total_variance = float(group["_variance"].sum())
        summary_record_total = float(record_total)
        summary_physical_variance = (
            float(physical_group["_variance"].sum()) if not physical_group.empty else 0.0
        )
        total_variance = summary_total_variance
        modelled_total = float(group["_modelled"].sum())
        physical_variance = summary_physical_variance
        coverage_total = coverage_totals.get((str(cfc).strip().upper(), str(carrier)))
        if coverage_total is not None:
            modelled_total = coverage_total["modelled"]
            total_variance = coverage_total["variance"]
            record_total = coverage_total["records"]
            if summary_total_variance:
                physical_share = summary_physical_variance / summary_total_variance
                physical_variance = total_variance * physical_share
                physical_records = (
                    record_total * physical_records / summary_record_total
                    if summary_record_total
                    else 0.0
                )
            else:
                physical_variance = 0.0
                physical_records = 0.0
        rows.append(
            {
                "CFC": cfc,
                "Carrier": carrier,
                "Complete-cost records": record_total,
                "Total observed variance ex GST": total_variance,
                "Total observed variance %": (
                    total_variance / modelled_total * 100
                    if modelled_total
                    else None
                ),
                "Dim/weight records": physical_records,
                "Dim/weight variance ex GST": physical_variance,
                "Dim/weight share of signed variance %": (
                    physical_variance / total_variance * 100
                    if total_variance
                    else None
                ),
                "Residual variance ex GST": total_variance - physical_variance,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _filter_persisted_coverage(
    coverage: pd.DataFrame,
    site: str,
    shipped_carrier: str,
    parcel_scope: str,
    reporting_period_label: str,
) -> pd.DataFrame:
    if coverage.empty:
        return coverage

    filtered = coverage.copy()
    if site != "ALL" and "CFC" in filtered:
        filtered = filtered.loc[filtered["CFC"].fillna("").astype(str).str.upper() == site]
    if shipped_carrier != "All shipped carriers" and "Shipped carrier" in filtered:
        carrier_aliases = {
            "Australia Post": {"Australia Post", "eParcel", "eParcel Metro"},
            "eParcel": {"Australia Post", "eParcel"},
            "eParcel Metro": {"Australia Post", "eParcel Metro"},
        }
        allowed = carrier_aliases.get(shipped_carrier, {shipped_carrier})
        filtered = filtered.loc[
            filtered["Shipped carrier"].fillna("").astype(str).str.strip().isin(allowed)
        ]
    if parcel_scope != "All consignments" and "Shippit parcel count" in filtered:
        parcel_count = pd.to_numeric(filtered["Shippit parcel count"], errors="coerce")
        if parcel_scope == "Single parcel":
            filtered = filtered.loc[parcel_count == 1]
        else:
            filtered = filtered.loc[parcel_count > 1]
    if reporting_period_label == "FY2027 W01 onward" and "Week" in filtered:
        week_parts = filtered["Week"].astype(str).str.extract(r"FY(\d+)\s+W(\d+)")
        years = pd.to_numeric(week_parts[0], errors="coerce")
        weeks = pd.to_numeric(week_parts[1], errors="coerce")
        filtered = filtered.loc[(years.gt(2027)) | (years.eq(2027) & weeks.ge(1))]
    elif reporting_period_label not in {"All available refreshed data"} and not reporting_period_label.startswith("Historical validation snapshot:") and "Week" in filtered:
        filtered = filtered.loc[filtered["Week"].astype(str) == reporting_period_label]
    return filtered


def _filter_persisted_weekly(
    weekly: pd.DataFrame,
    site: str,
    shipped_carrier: str,
    reporting_period_label: str,
) -> pd.DataFrame:
    if weekly.empty:
        return weekly

    filtered = weekly.copy()
    if site != "ALL" and "CFC" in filtered:
        filtered = filtered.loc[filtered["CFC"].fillna("").astype(str).str.upper() == site]
    if shipped_carrier != "All shipped carriers" and "Shipped carrier" in filtered:
        carrier_aliases = {
            "Australia Post": {"Australia Post", "eParcel"},
            "eParcel": {"Australia Post", "eParcel"},
        }
        allowed = carrier_aliases.get(shipped_carrier, {shipped_carrier})
        filtered = filtered.loc[
            filtered["Shipped carrier"].fillna("").astype(str).str.strip().isin(allowed)
        ]
    if reporting_period_label == "FY2027 W01 onward" and "Week" in filtered:
        week_parts = filtered["Week"].astype(str).str.extract(r"FY(\d+)\s+W(\d+)")
        years = pd.to_numeric(week_parts[0], errors="coerce")
        weeks = pd.to_numeric(week_parts[1], errors="coerce")
        filtered = filtered.loc[(years.gt(2027)) | (years.eq(2027) & weeks.ge(1))]
    elif reporting_period_label not in {"All available refreshed data"} and not reporting_period_label.startswith("Historical validation snapshot:") and "Week" in filtered:
        filtered = filtered.loc[filtered["Week"].astype(str) == reporting_period_label]
    return filtered


def _accounting_week_options(frame: pd.DataFrame) -> list[str]:
    """Return available accounting weeks newest first for a scope selector."""
    if frame.empty:
        return []
    if "Week" in frame:
        labels = frame["Week"].fillna("").astype(str).str.strip()
        parts = labels.str.extract(r"(\d{4}).*?W\s*(\d+)", expand=True)
        sort_key = pd.to_numeric(parts[0], errors="coerce") * 100 + pd.to_numeric(
            parts[1], errors="coerce"
        )
        options = (
            pd.DataFrame({"label": labels, "sort_key": sort_key})
            .loc[labels.ne("")]
            .drop_duplicates("label")
            .sort_values("sort_key", ascending=False, na_position="last")["label"]
            .tolist()
        )
        return options

    required = {"kmart_accounting_year", "kmart_accounting_week_number_in_year"}
    if required.issubset(frame.columns):
        calendar = pd.DataFrame(
            {
                "year": pd.to_numeric(frame["kmart_accounting_year"], errors="coerce"),
                "week": pd.to_numeric(
                    frame["kmart_accounting_week_number_in_year"], errors="coerce"
                ),
            }
        ).dropna()
        return [
            f"FY{int(row.year)} W{int(row.week):02d}"
            for row in calendar.drop_duplicates()
            .sort_values(["year", "week"], ascending=False)
            .itertuples(index=False)
        ]

    dates = pd.to_datetime(
        frame.get("despatch_date", pd.Series(index=frame.index)), errors="coerce"
    )
    week_starts = dates - pd.to_timedelta(dates.dt.weekday, unit="D")
    return sorted(
        {
            f"Week of {date:%d %b %Y} to {(date + pd.Timedelta(days=6)):%d %b %Y}"
            for date in week_starts.dropna()
        },
        reverse=True,
    )


def _filter_historical_trend_year(
    frame: pd.DataFrame,
    minimum_accounting_year: int = 2027,
) -> pd.DataFrame:
    """Keep the historical trend workspace within the requested financial year."""
    if frame.empty:
        return frame

    year = pd.Series(index=frame.index, dtype="float64")
    for column in ("Accounting year", "kmart_accounting_year"):
        if column in frame:
            year = pd.to_numeric(frame[column], errors="coerce")
            if year.notna().any():
                break

    if not year.notna().any() and "Week" in frame:
        week_parts = frame["Week"].fillna("").astype(str).str.extract(
            r"(\d{4}).*?W\s*\d+", expand=False
        )
        year = pd.to_numeric(week_parts, errors="coerce")

    if not year.notna().any():
        return frame
    return frame.loc[year.ge(minimum_accounting_year).fillna(False)].copy()


def _invoice_column(frame: pd.DataFrame) -> str:
    return "invoice_number" if "invoice_number" in frame else "shippit_invoice_number"


def _normalise_consignment_number(value: object) -> str:
    """Normalise pasted numeric invoice values without changing leading-zero IDs."""
    text = _normalise_lookup(value)
    if not text or ("." not in text and "e" not in text.lower()):
        return text
    try:
        numeric = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    return str(int(numeric)) if numeric == numeric.to_integral_value() else text


def _stage3_invoice_matches(frame: pd.DataFrame, invoice_number: object) -> pd.DataFrame:
    """Find a consignment while tolerating Excel-style .0 or scientific notation."""
    if frame.empty:
        return pd.DataFrame()
    invoice_col = _invoice_column(frame)
    selected = _normalise_consignment_number(invoice_number)
    values = frame[invoice_col].map(_normalise_consignment_number)
    return frame.loc[values.eq(selected)]


def _current_sydney_date() -> pd.Timestamp:
    """Return today's date in the business timezone used by the dashboard."""
    return pd.Timestamp.now(tz="Australia/Sydney").tz_localize(None).normalize()


def _filter_reporting_period(
    frame: pd.DataFrame,
    reporting_period: str,
    selected_week: str | None = None,
    as_of_date: pd.Timestamp | str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Filter to a Kmart accounting week, keeping the latest week as the default."""
    if frame.empty or reporting_period == "All available weeks":
        return frame, "All available refreshed data"

    if reporting_period == "Single accounting week":
        options = _accounting_week_options(frame)
        if not options:
            return frame, "Accounting week unavailable - showing all refreshed data"
        target = selected_week if selected_week in options else options[0]
        required = {"kmart_accounting_year", "kmart_accounting_week_number_in_year"}
        if required.issubset(frame.columns) and target.startswith("FY"):
            parts = target.replace("FY", "").split(" W")
            year_value, week_value = int(parts[0]), int(parts[1])
            calendar = pd.DataFrame(
                {
                    "year": pd.to_numeric(frame["kmart_accounting_year"], errors="coerce"),
                    "week": pd.to_numeric(
                        frame["kmart_accounting_week_number_in_year"], errors="coerce"
                    ),
                },
                index=frame.index,
            )
            selected = frame.loc[
                (calendar["year"] == year_value) & (calendar["week"] == week_value)
            ]
        else:
            dates = pd.to_datetime(
                frame.get("despatch_date", pd.Series(index=frame.index)), errors="coerce"
            )
            week_starts = dates - pd.to_timedelta(dates.dt.weekday, unit="D")
            start_text = target.removeprefix("Week of ").split(" to ")[0]
            week_start = pd.to_datetime(start_text, format="%d %b %Y", errors="coerce")
            selected = frame.loc[week_starts == week_start]
        return selected, target

    required = {"kmart_accounting_year", "kmart_accounting_week_number_in_year"}
    if not required.issubset(frame.columns):
        dates = pd.to_datetime(frame.get("despatch_date", pd.Series(index=frame.index)), errors="coerce")
        dated_rows = dates.dropna()
        if dated_rows.empty:
            return frame, "Accounting week unavailable - showing all refreshed data"
        current_to_date = reporting_period == "Current accounting week to date"
        if current_to_date:
            as_of = (
                pd.Timestamp(as_of_date).normalize()
                if as_of_date is not None
                else _current_sydney_date()
            )
            week_start = as_of - pd.Timedelta(days=as_of.weekday())
            week_end = as_of
        else:
            latest_date = dated_rows.max()
            week_start = latest_date - pd.Timedelta(days=latest_date.weekday())
            week_end = week_start + pd.Timedelta(days=6)
        if reporting_period == "Previous accounting week":
            week_start -= pd.Timedelta(days=7)
            week_end = week_start + pd.Timedelta(days=6)
        selected = frame.loc[
            (dates.reindex(frame.index) >= week_start)
            & (dates.reindex(frame.index) <= week_end)
        ]
        label = (
            f"Week of {week_start:%d %b %Y} to date"
            if current_to_date
            else f"Week of {week_start:%d %b %Y} to {week_end:%d %b %Y}"
        )
        return selected, label

    calendar = pd.DataFrame(
        {
            "year": pd.to_numeric(frame["kmart_accounting_year"], errors="coerce"),
            "week": pd.to_numeric(frame["kmart_accounting_week_number_in_year"], errors="coerce"),
        },
        index=frame.index,
    )
    valid_calendar = calendar.dropna()
    if valid_calendar.empty:
        return frame, "Accounting week unavailable - showing all refreshed data"

    week_keys = valid_calendar[["year", "week"]].drop_duplicates().sort_values(["year", "week"])
    current_dates = pd.to_datetime(
        frame.get("despatch_date", pd.Series(index=frame.index)), errors="coerce"
    )
    if reporting_period == "Current accounting week to date":
        as_of = (
            pd.Timestamp(as_of_date).normalize()
            if as_of_date is not None
            else _current_sydney_date()
        )
        current_week_start = as_of - pd.Timedelta(days=as_of.weekday())
        current_week_rows = valid_calendar.loc[
            current_dates.loc[valid_calendar.index].between(current_week_start, as_of)
        ]
        if current_week_rows.empty:
            return frame.iloc[0:0].copy(), "Current accounting week to date"
        current_week_keys = (
            current_week_rows[["year", "week"]]
            .drop_duplicates()
            .sort_values(["year", "week"])
        )
        current_year, current_week = current_week_keys.iloc[-1]
        selected = frame.loc[
            (calendar["year"] == current_year) & (calendar["week"] == current_week)
        ]
        selected_dates = current_dates.loc[selected.index]
        selected = selected.loc[selected_dates.le(as_of)]
        return selected, f"FY{int(current_year)} W{int(current_week):02d} to date"

    target_position = -2 if reporting_period == "Previous accounting week" else -1
    if len(week_keys) < abs(target_position):
        target_position = -1
    current_year, current_week = week_keys.iloc[target_position]
    selected = frame.loc[(calendar["year"] == current_year) & (calendar["week"] == current_week)]
    return selected, f"FY{int(current_year)} W{int(current_week):02d}"


def _filter_carrier(frame: pd.DataFrame, carrier: str) -> pd.DataFrame:
    if carrier == "All shipped carriers" or frame.empty:
        return frame
    carrier_column = "actual_carrier" if "actual_carrier" in frame else "shippit_carrier"
    values = frame[carrier_column].fillna("").astype(str).str.strip()
    return frame.loc[values == carrier]


def _reconciliation_paths(
    carrier: str,
    reconciliation_files: dict[str, Path | tuple[Path, ...]] | None = None,
) -> tuple[Path, ...]:
    files = RECONCILIATION_FILES if reconciliation_files is None else reconciliation_files
    carrier_key = carrier.strip().lower()
    path: Path | tuple[Path, ...] | None = None
    if carrier_key == "couriers please":
        path = files.get("couriers_please")
    elif carrier_key in {"eparcel", "eparcel metro", "australia post", "auspost"}:
        path = files.get("australia_post")
    elif carrier_key in {"goexpress", "gobundle", "gopeople"}:
        path = files.get("gopeople")
    elif carrier_key == "allied express":
        path = files.get("allied")
    if path is None:
        return ()
    if isinstance(path, tuple):
        return path
    return (path,)


def _reconciliation_path(carrier: str, reconciliation_files: dict[str, Path] | None = None) -> Path | None:
    """Return the primary reconciliation path for status/reporting compatibility."""
    return next(iter(_reconciliation_paths(carrier, reconciliation_files)), None)


def _normalise_lookup(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _coalesced_lookup(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    result = pd.Series("", index=frame.index, dtype="string")
    for column in columns:
        if column not in frame:
            continue
        values = frame[column].fillna("").astype(str).str.strip()
        result = result.mask(result.eq(""), values)
    return result


@st.cache_data(show_spinner=False)
def _find_reconciliation_rows(
    path_text: str,
    invoice_number: str,
    tracking_number: str,
    modified_ns: int | None,
) -> pd.DataFrame:
    """Read only matching rows from a large reconciliation export."""
    del modified_ns
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()

    lookups = {
        value for value in (_normalise_lookup(invoice_number), _normalise_lookup(tracking_number)) if value
    }
    if not lookups:
        return pd.DataFrame()

    matches = []
    try:
        chunks = pd.read_csv(path, chunksize=50_000, low_memory=False, dtype="string")
        for chunk in chunks:
            match = pd.Series(False, index=chunk.index)
            for column in (
                "invoice_number",
                "stage3_invoice_number",
                "carrier_tracking_number",
                "stage3_tracking_number",
                "carrier_consignment_reference",
                "consignment_id",
            ):
                if column in chunk:
                    match = match | chunk[column].fillna("").astype(str).str.strip().isin(lookups)
            if match.any():
                matches.append(chunk.loc[match])
    except (OSError, pd.errors.ParserError):
        return pd.DataFrame()
    return pd.concat(matches, ignore_index=True) if matches else pd.DataFrame()


@st.cache_data(show_spinner=False)
def _find_reconciliation_rows_for_invoices(
    path_text: str,
    invoice_numbers: tuple[str, ...],
    site: str,
    modified_ns: int | None,
    usecols: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Read the reconciliation records for the currently selected invoice set."""
    del modified_ns
    path = Path(path_text)
    if not path.exists() or not invoice_numbers:
        return pd.DataFrame()

    lookups = {str(value).strip() for value in invoice_numbers if str(value).strip()}
    if not lookups:
        return pd.DataFrame()

    matches = []
    try:
        read_kwargs = {"chunksize": 50_000, "low_memory": False, "dtype": "string"}
        if usecols is not None:
            available_columns = set(pd.read_csv(path, nrows=0).columns)
            read_kwargs["usecols"] = [column for column in usecols if column in available_columns]
        chunks = pd.read_csv(path, **read_kwargs)
        for chunk in chunks:
            match = pd.Series(False, index=chunk.index)
            for column in (
                "invoice_number",
                "stage3_invoice_number",
                "carrier_tracking_number",
                "stage3_tracking_number",
                "carrier_consignment_reference",
                "consignment_id",
            ):
                if column in chunk:
                    match = match | chunk[column].fillna("").astype(str).str.strip().isin(lookups)
            if site != "ALL" and "site" in chunk:
                site_values = chunk["site"].fillna("").astype(str).str.strip().str.upper()
                match = match & site_values.eq(site)
            if match.any():
                matches.append(chunk.loc[match])
    except (OSError, pd.errors.ParserError):
        return pd.DataFrame()
    return pd.concat(matches, ignore_index=True) if matches else pd.DataFrame()


_RECONCILIATION_ADDITIVE_COLUMNS = (
    "actual_invoice_cost_ex_gst",
    "actual_base_cost_ex_gst",
    "actual_fuel_surcharge_ex_gst",
    "actual_other_surcharges_ex_gst",
    "actual_other_surcharge_ex_gst",
    "actual_delivery_surcharge_ex_gst",
    "actual_excess_weight_surcharge_ex_gst",
    "actual_remote_area_surcharge_ex_gst",
    "actual_oversized_item_surcharge_ex_gst",
    "actual_overweight_surcharge_ex_gst",
    "actual_manual_measurement_fee_ex_gst",
    "actual_reverse_logistics_surcharge_ex_gst",
    "actual_additional_services_ex_gst",
    "actual_insurance_ex_gst",
    "actual_chargeable_weight_kg",
    "actual_rounded_chargeable_weight_kg",
    "actual_weight_kg",
    "actual_declared_weight_kg",
    "actual_cubic_weight_kg",
    "actual_billed_weight_kg",
    "actual_billed_cube_cm3",
    "actual_chargeable_cube_cm3",
    "actual_billed_cube_m3",
    "actual_total_volume_m3",
    "actual_declared_cube_cm3",
    "actual_declared_cube_m3",
)


def _aggregate_reconciliation_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Aggregate carrier parcel rows to one Shippit invoice before source dedupe."""
    if rows.empty:
        return rows

    raw_key = _coalesced_lookup(
        rows,
        (
            "stage3_invoice_number",
            "invoice_number",
            "stage3_tracking_number",
            "carrier_tracking_number",
            "carrier_consignment_reference",
            "consignment_id",
        ),
    )
    keyed = raw_key.ne("")
    dedupe_key = raw_key
    if "site" in rows:
        dedupe_key = rows["site"].fillna("").astype(str).str.strip() + "|" + dedupe_key
    if not keyed.any():
        return rows

    representatives = (
        rows.loc[keyed]
        .assign(_detail_dedupe_key=dedupe_key.loc[keyed])
        .drop_duplicates("_detail_dedupe_key", keep="first")
    )
    representative_keys = representatives["_detail_dedupe_key"]
    for column in _RECONCILIATION_ADDITIVE_COLUMNS:
        if column not in rows:
            continue
        values = pd.to_numeric(rows.loc[keyed, column], errors="coerce")
        totals = values.groupby(dedupe_key.loc[keyed], dropna=False).sum(min_count=1)
        representatives[column] = representative_keys.map(totals)

    representatives = representatives.drop(columns="_detail_dedupe_key")
    return pd.concat([rows.loc[~keyed], representatives], axis=0).sort_index().reset_index(drop=True)


def _reconciliation_detail_for_stage3(
    stage3: pd.DataFrame,
    site: str,
    reconciliation_files: dict[str, Path | tuple[Path, ...]] | None = None,
    detail_columns: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    if stage3.empty:
        return pd.DataFrame()
    invoice_column = _invoice_column(stage3)
    carrier_column = "actual_carrier" if "actual_carrier" in stage3 else "shippit_carrier"
    tracking_column = "carrier_tracking_number" if "carrier_tracking_number" in stage3 else "shippit_tracking_number"
    path_invoices: dict[Path, set[str]] = {}
    for carrier, rows in stage3.groupby(carrier_column, dropna=False):
        paths = _reconciliation_paths(_normalise_lookup(carrier), reconciliation_files)
        if not paths:
            continue
        lookups = set(rows[invoice_column].dropna().astype(str).str.strip())
        if tracking_column in rows:
            lookups.update(rows[tracking_column].dropna().astype(str).str.strip())
        for path in paths:
            path_invoices.setdefault(path, set()).update(value for value in lookups if value)

    frames = []
    for path, invoice_numbers in path_invoices.items():
        modified_ns = path.stat().st_mtime_ns if path.exists() else None
        rows = _find_reconciliation_rows_for_invoices(
            str(path), tuple(sorted(invoice_numbers)), site, modified_ns, detail_columns
        )
        if not rows.empty:
            rows["reconciliation_source"] = path.name
            frames.append(_aggregate_reconciliation_rows(rows))
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    dedupe_key = pd.Series("", index=combined.index, dtype="string")
    for column in (
        "stage3_invoice_number",
        "invoice_number",
        "stage3_tracking_number",
        "carrier_tracking_number",
        "carrier_consignment_reference",
        "consignment_id",
    ):
        if column in combined:
            values = combined[column].fillna("").astype(str).str.strip()
            dedupe_key = dedupe_key.mask(dedupe_key.eq(""), values)
    if "site" in combined:
        dedupe_key = combined["site"].fillna("").astype(str).str.strip() + "|" + dedupe_key
    keyed = dedupe_key.ne("") & dedupe_key.ne("|")
    if keyed.any():
        combined = pd.concat(
            [
                combined.loc[~keyed],
                combined.loc[keyed].assign(_detail_dedupe_key=dedupe_key.loc[keyed]).drop_duplicates(
                    "_detail_dedupe_key", keep="first"
                ).drop(columns="_detail_dedupe_key"),
            ],
            ignore_index=True,
        )
    stage3_modelled = _numeric_from_columns(
        stage3,
        ("modelled_total_cost_ex_gst", "actual_est_cost_ex_gst"),
    )
    if stage3_modelled.notna().any():
        stage3_invoice_keys = _coalesced_lookup(stage3, ("invoice_number", "shippit_invoice_number"))
        stage3_tracking_keys = _coalesced_lookup(
            stage3,
            ("carrier_tracking_number", "shippit_tracking_number"),
        )

        def first_value_map(keys: pd.Series) -> pd.Series:
            values = pd.DataFrame({"key": keys, "value": stage3_modelled})
            values = values.loc[values["key"].ne("") & values["value"].notna()]
            if values.empty:
                return pd.Series(dtype="float64")
            return values.drop_duplicates("key", keep="first").set_index("key")["value"]

        invoice_modelled = first_value_map(stage3_invoice_keys)
        tracking_modelled = first_value_map(stage3_tracking_keys)
        detail_invoice_keys = _coalesced_lookup(
            combined,
            ("stage3_invoice_number", "invoice_number"),
        )
        detail_tracking_keys = _coalesced_lookup(
            combined,
            (
                "stage3_tracking_number",
                "carrier_tracking_number",
                "carrier_consignment_reference",
                "consignment_id",
            ),
        )
        aligned_modelled = detail_invoice_keys.map(invoice_modelled)
        aligned_modelled = aligned_modelled.where(
            aligned_modelled.notna(), detail_tracking_keys.map(tracking_modelled)
        )
        if aligned_modelled.notna().any():
            combined["_stage3_modelled_total_cost_ex_gst"] = aligned_modelled
    return combined


def _reconciliation_file_status(reconciliation_files: dict[str, Path]) -> pd.DataFrame:
    rows = []
    for carrier, path in sorted(reconciliation_files.items()):
        row = {
            "Carrier": carrier.replace("_", " ").title(),
            "Status": "Available" if path.exists() else "Missing",
            "File": path.name,
            "Path": str(path.relative_to(ROOT)),
        }
        if path.exists():
            stat = path.stat()
            row["Last updated"] = pd.to_datetime(stat.st_mtime, unit="s").strftime("%Y-%m-%d %H:%M")
            row["Size MB"] = stat.st_size / 1_000_000
        else:
            row["Last updated"] = ""
            row["Size MB"] = pd.NA
        rows.append(row)
    return pd.DataFrame(rows)


def _numeric_from_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    result = pd.Series(float("nan"), index=frame.index, dtype="float64")
    for column in columns:
        if column in frame:
            values = pd.to_numeric(frame[column], errors="coerce")
            result = result.fillna(values)
    return result


def _summed_numeric_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    present = [column for column in columns if column in frame]
    if not present:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    values = pd.DataFrame(
        {column: pd.to_numeric(frame[column], errors="coerce") for column in present},
        index=frame.index,
    )
    return values.sum(axis=1, min_count=1)


def _reconciliation_cost_frame(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame()

    work = detail.copy()
    primary = work.get("primary_cost_driver", pd.Series("", index=work.index)).fillna("").astype(str).str.strip()
    root_cause = work.get("root_cause", pd.Series("", index=work.index)).fillna("").astype(str).str.strip()
    work["primary_root_cause"] = primary.where(primary != "", root_cause.where(root_cause != "", "UNCLASSIFIED"))
    work["modelled_base_cost_ex_gst"] = _numeric_from_columns(
        work,
        (
            "modelled_cp_base_cost_ex_gst",
            "modelled_ap_base_cost_ex_gst",
            "modelled_allied_base_cost_ex_gst",
            "modelled_goexpress_base_cost_ex_gst",
        ),
    )
    work["modelled_fuel_cost_ex_gst"] = _numeric_from_columns(
        work,
        ("predicted_fuel_surcharge_ex_gst", "modelled_goexpress_fuel_surcharge_ex_gst"),
    )
    work["modelled_total_cost_ex_gst"] = _numeric_from_columns(
        work,
        (
            "_stage3_modelled_total_cost_ex_gst",
            "modelled_cp_cost_including_fuel_ex_gst",
            "modelled_ap_cost_including_fuel_ex_gst",
            "modelled_allied_cost_including_fuel_ex_gst",
            "modelled_goexpress_cost_ex_gst",
        ),
    )
    work["modelled_total_cost_ex_gst"] = work["modelled_total_cost_ex_gst"].fillna(
        work["modelled_base_cost_ex_gst"] + work["modelled_fuel_cost_ex_gst"].fillna(0.0)
    )
    work["actual_base_cost_ex_gst_std"] = _numeric_from_columns(
        work, ("actual_base_cost_ex_gst",)
    )
    work["actual_fuel_cost_ex_gst_std"] = _numeric_from_columns(
        work, ("actual_fuel_surcharge_ex_gst",)
    )
    if "actual_excess_weight_surcharge_ex_gst" in work:
        # Allied exposes surcharge components separately, so they must be added.
        work["actual_other_cost_ex_gst_std"] = _summed_numeric_columns(
            work,
            (
                "actual_excess_weight_surcharge_ex_gst",
                "actual_delivery_surcharge_ex_gst",
                "actual_other_surcharge_ex_gst",
            ),
        )
    else:
        work["actual_other_cost_ex_gst_std"] = _numeric_from_columns(
            work,
            (
                "actual_other_surcharges_ex_gst",
                "actual_other_surcharge_ex_gst",
                "actual_delivery_surcharge_ex_gst",
            ),
        )
    work["actual_total_cost_ex_gst_std"] = _numeric_from_columns(
        work, ("actual_invoice_cost_ex_gst",)
    )
    work["base_variance_ex_gst"] = work["actual_base_cost_ex_gst_std"] - work["modelled_base_cost_ex_gst"]
    work["fuel_variance_ex_gst"] = work["actual_fuel_cost_ex_gst_std"] - work["modelled_fuel_cost_ex_gst"]
    work["other_variance_ex_gst"] = work["actual_other_cost_ex_gst_std"]
    work["total_variance_ex_gst"] = work["actual_total_cost_ex_gst_std"] - work["modelled_total_cost_ex_gst"]
    return work


def _reconciliation_coverage(
    stage3: pd.DataFrame,
    detail: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return one coverage row per Shippit consignment and matched cost detail."""
    if stage3.empty:
        return pd.DataFrame(), pd.DataFrame()

    cost_detail = _reconciliation_cost_frame(detail)
    work = stage3.reset_index(drop=True).copy()
    invoice_keys = _coalesced_lookup(work, ("invoice_number", "shippit_invoice_number"))
    tracking_keys = _coalesced_lookup(work, ("carrier_tracking_number", "shippit_tracking_number"))

    detail_invoice_keys = _coalesced_lookup(cost_detail, ("stage3_invoice_number", "invoice_number"))
    detail_tracking_keys = _coalesced_lookup(
        cost_detail,
        (
            "stage3_tracking_number",
            "carrier_tracking_number",
            "carrier_consignment_reference",
            "consignment_id",
        ),
    )
    detail_value_columns = (
        "actual_total_cost_ex_gst_std",
        "actual_base_cost_ex_gst_std",
        "actual_fuel_cost_ex_gst_std",
        "actual_other_cost_ex_gst_std",
        "actual_chargeable_weight_kg",
        "actual_rounded_chargeable_weight_kg",
        "actual_weight_kg",
        "actual_declared_weight_kg",
        "actual_cubic_weight_kg",
        "actual_billed_weight_kg",
        "actual_billed_cube_cm3",
        "actual_chargeable_cube_cm3",
        "actual_billed_cube_m3",
        "actual_total_volume_m3",
        "actual_declared_cube_cm3",
        "actual_declared_cube_m3",
    )
    for column in detail_value_columns:
        if column not in cost_detail:
            cost_detail[column] = pd.Series(float("nan"), index=cost_detail.index)
        else:
            cost_detail[column] = pd.to_numeric(cost_detail[column], errors="coerce")

    def summarise(keys: pd.Series) -> pd.DataFrame:
        if cost_detail.empty:
            return pd.DataFrame(columns=[*detail_value_columns, "actual_invoice_rows"])
        valid = keys.ne("")
        if not valid.any():
            return pd.DataFrame(columns=[*detail_value_columns, "actual_invoice_rows"])
        keyed = cost_detail.loc[valid].copy()
        keyed["_match_key"] = keys.loc[keyed.index]
        grouped = keyed.groupby("_match_key", dropna=False)
        summary = grouped[list(detail_value_columns)].sum(min_count=1)
        summary["actual_invoice_rows"] = grouped.size()
        return summary

    invoice_summary = summarise(detail_invoice_keys)
    tracking_summary = summarise(detail_tracking_keys)
    invoice_matched = invoice_keys.isin(invoice_summary.index)
    tracking_matched = tracking_keys.isin(tracking_summary.index)
    matched = invoice_matched | tracking_matched

    def mapped_value(
        keys: pd.Series,
        summary: pd.DataFrame,
        column: str,
    ) -> pd.Series:
        if summary.empty:
            return pd.Series(float("nan"), index=work.index, dtype="float64")
        return pd.to_numeric(keys.map(summary[column]), errors="coerce")

    invoice_actual = mapped_value(invoice_keys, invoice_summary, "actual_total_cost_ex_gst_std")
    tracking_actual = mapped_value(tracking_keys, tracking_summary, "actual_total_cost_ex_gst_std")
    invoice_rows = mapped_value(invoice_keys, invoice_summary, "actual_invoice_rows")
    tracking_rows = mapped_value(tracking_keys, tracking_summary, "actual_invoice_rows")
    actual_cost = invoice_actual.where(invoice_matched, tracking_actual)
    actual_rows = invoice_rows.where(invoice_matched, tracking_rows)
    modelled_cost = _numeric_from_columns(work, ("actual_est_cost_ex_gst", "modelled_total_cost_ex_gst"))
    cost_matched = matched & actual_cost.notna() & modelled_cost.gt(0)
    variance = actual_cost - modelled_cost

    cfc = _coalesced_lookup(work, ("ui_site", "site"))
    carrier = _coalesced_lookup(work, ("actual_carrier", "shippit_carrier"))
    service = _coalesced_lookup(work, ("actual_service", "shippit_service", "actual_est_service"))

    year = pd.to_numeric(work.get("kmart_accounting_year", pd.Series(index=work.index)), errors="coerce")
    week = pd.to_numeric(
        work.get("kmart_accounting_week_number_in_year", pd.Series(index=work.index)),
        errors="coerce",
    )
    dates = pd.to_datetime(work.get("despatch_date", pd.Series(index=work.index)), errors="coerce")
    week_labels = pd.Series("Week unavailable", index=work.index, dtype="string")
    week_sort = pd.Series(float("nan"), index=work.index, dtype="float64")
    for index in work.index:
        if pd.notna(year.loc[index]) and pd.notna(week.loc[index]):
            year_value = int(year.loc[index])
            week_value = int(week.loc[index])
            week_labels.loc[index] = f"FY{year_value} W{week_value:02d}"
            week_sort.loc[index] = year_value * 100 + week_value
        elif pd.notna(dates.loc[index]):
            week_start = dates.loc[index] - pd.Timedelta(days=dates.loc[index].weekday())
            week_labels.loc[index] = f"Week of {week_start:%d %b %Y}"
            week_sort.loc[index] = week_start.toordinal()

    coverage = pd.DataFrame(index=work.index)
    coverage["Shippit invoice number"] = invoice_keys
    coverage["Shippit tracking number"] = tracking_keys
    coverage["CFC"] = cfc
    coverage["Shipped carrier"] = carrier
    coverage["Shipped service"] = service
    coverage["Shippit parcel count"] = _numeric_from_columns(work, ("actual_parcel_count", "shippit_parcel_count"))
    coverage["Shippit units"] = _numeric_from_columns(work, ("parcel_units_total", "shippit_units_total"))
    coverage["Despatch date"] = work.get("despatch_date", pd.Series(index=work.index)).astype("string")
    coverage["Accounting year"] = year
    coverage["Accounting week"] = week
    coverage["Week"] = week_labels
    coverage["Estimator chargeable weight kg"] = _numeric_from_columns(
        work, ("est_chargeable_weight_kg",)
    )
    coverage["Estimator packed SKU weight kg"] = _numeric_from_columns(
        work, ("est_packed_sku_weight_kg",)
    )
    estimator_packed_sku_cube_m3 = _numeric_from_columns(
        work, ("est_packed_sku_cube_total_m3",)
    )
    estimator_packed_sku_cube_cm3 = _numeric_from_columns(
        work, ("est_packed_sku_cube_total_cm3",)
    )
    coverage["Estimator packed SKU cube m3"] = estimator_packed_sku_cube_m3.fillna(
        estimator_packed_sku_cube_cm3 / 1_000_000
    )
    coverage["Estimator packed SKU cube imputed"] = work.get(
        "est_packed_sku_cube_imputed_flag",
        pd.Series("", index=work.index, dtype="string"),
    ).fillna("").astype("string")
    coverage["Estimator volumetric weight kg"] = _numeric_from_columns(
        work, ("est_volumetric_weight_kg",)
    )
    estimator_cube_m3 = _numeric_from_columns(work, ("est_parcel_cube_total_m3",))
    estimator_cube_cm3 = _numeric_from_columns(work, ("est_parcel_cube_total_cm3",))
    coverage["Estimator cube m3"] = estimator_cube_m3.fillna(estimator_cube_cm3 / 1_000_000)
    coverage["Shippit chargeable weight kg"] = _numeric_from_columns(
        work, ("shippit_chargeable_weight_kg",)
    )
    coverage["Shippit dead weight kg"] = _numeric_from_columns(
        work, ("shippit_dead_weight_kg",)
    )
    coverage["Shippit volumetric weight kg"] = _numeric_from_columns(
        work, ("shippit_volumetric_weight_kg",)
    )
    shippit_cube_cm3 = _numeric_from_columns(
        work, ("shippit_chargeable_cube_cm3", "shippit_volumetric_cube_cm3")
    )
    coverage["Shippit chargeable cube m3"] = shippit_cube_cm3 / 1_000_000
    coverage["AP pricing zone"] = work.get(
        "ap_pricing_zone", pd.Series("", index=work.index, dtype="string")
    ).fillna("").astype("string")
    coverage["Reporting weight band"] = work.get(
        "reporting_weight_band", pd.Series("", index=work.index, dtype="string")
    ).fillna("").astype("string")
    coverage["Accounting period number"] = work.get(
        "kmart_accounting_period_number", pd.Series("", index=work.index, dtype="string")
    ).fillna("").astype("string")
    for source_column, coverage_column in (
        ("dest_suburb", "Destination suburb"),
        ("dest_state", "Destination state"),
        ("dest_postcode", "Destination postcode"),
        ("actual_est_fuel_surcharge_source", "Modelled fuel surcharge source"),
        ("allied_zone", "Modelled Allied zone"),
        ("ap_zone", "Modelled AP zone"),
        ("cp_zone", "Modelled Couriers Please zone"),
        ("goexpress_zone", "Modelled GoExpress zone"),
    ):
        coverage[coverage_column] = work.get(
            source_column, pd.Series("", index=work.index, dtype="string")
        ).fillna("").astype("string")
    for source_column, coverage_column in (
        ("cost_weight_basis_kg", "Modelled cost weight basis kg"),
        ("actual_est_base_cost_ex_gst", "Modelled base cost ex GST"),
        ("actual_est_fuel_surcharge_ex_gst", "Modelled fuel surcharge ex GST"),
        ("actual_est_fuel_surcharge_pct", "Modelled fuel surcharge %"),
        ("allied_total_cost_ex_gst", "Modelled Allied total cost ex GST"),
    ):
        coverage[coverage_column] = _numeric_from_columns(work, (source_column,))
    coverage["Invoice status"] = pd.Series("Not yet matched", index=work.index, dtype="string")
    coverage.loc[tracking_matched, "Invoice status"] = "Matched by tracking number"
    coverage.loc[invoice_matched, "Invoice status"] = "Matched"
    coverage["Match source"] = pd.Series("Not matched", index=work.index, dtype="string")
    coverage.loc[tracking_matched, "Match source"] = "Carrier tracking number"
    coverage.loc[invoice_matched, "Match source"] = "Shippit invoice number"
    coverage["Actual invoice rows"] = actual_rows
    coverage["Modelled cost ex GST"] = modelled_cost
    coverage["Invoiced actual cost ex GST"] = actual_cost
    coverage["Variance ex GST"] = variance.where(cost_matched)
    coverage["Variance %"] = (variance.div(modelled_cost.replace(0, pd.NA)).mul(100)).where(cost_matched)

    def mapped_detail_value(column: str) -> pd.Series:
        invoice_values = mapped_value(invoice_keys, invoice_summary, column)
        tracking_values = mapped_value(tracking_keys, tracking_summary, column)
        return invoice_values.where(invoice_matched, tracking_values)

    coverage["Invoiced actual chargeable weight kg"] = mapped_detail_value(
        "actual_chargeable_weight_kg"
    )
    coverage["Invoiced actual rounded chargeable weight kg"] = mapped_detail_value(
        "actual_rounded_chargeable_weight_kg"
    )
    coverage["Invoiced actual billed weight kg"] = mapped_detail_value(
        "actual_billed_weight_kg"
    )
    coverage["Invoiced actual declared weight kg"] = mapped_detail_value(
        "actual_declared_weight_kg"
    )
    coverage["Invoiced actual dead weight kg"] = mapped_detail_value("actual_weight_kg")
    coverage["Invoiced actual cubic weight kg"] = mapped_detail_value(
        "actual_cubic_weight_kg"
    )
    invoiced_cube_m3 = mapped_detail_value("actual_billed_cube_m3")
    invoiced_cube_m3 = invoiced_cube_m3.fillna(mapped_detail_value("actual_total_volume_m3"))
    invoiced_cube_cm3 = mapped_detail_value("actual_billed_cube_cm3")
    invoiced_cube_cm3 = invoiced_cube_cm3.fillna(
        mapped_detail_value("actual_chargeable_cube_cm3")
    )
    coverage["Invoiced actual cube m3"] = invoiced_cube_m3.fillna(invoiced_cube_cm3 / 1_000_000)
    coverage["Invoiced actual base cost ex GST"] = mapped_detail_value(
        "actual_base_cost_ex_gst_std"
    )
    coverage["Invoiced actual fuel surcharge ex GST"] = mapped_detail_value(
        "actual_fuel_cost_ex_gst_std"
    )
    coverage["Invoiced actual other surcharges ex GST"] = mapped_detail_value(
        "actual_other_cost_ex_gst_std"
    )
    coverage["_accuracy_matched"] = cost_matched
    coverage["_week_sort"] = week_sort
    return coverage, cost_detail


def _coverage_history_as_stage3(
    coverage: pd.DataFrame,
    prefer_invoiced_actual_cost: bool = False,
) -> pd.DataFrame:
    """Create lookup-only Stage 3 keys from persisted coverage history."""
    if coverage.empty:
        return pd.DataFrame()

    def column(name: str) -> pd.Series:
        return coverage[name] if name in coverage else pd.Series(
            "", index=coverage.index, dtype="string"
        )

    history = pd.DataFrame(index=coverage.index)
    history["invoice_number"] = column("Shippit invoice number")
    history["carrier_tracking_number"] = column("Shippit tracking number")
    history["actual_carrier"] = column("Shipped carrier")
    history["actual_service"] = column("Shipped service")
    history["ui_site"] = column("CFC")
    history["despatch_date"] = column("Despatch date")
    history["kmart_accounting_year"] = column("Accounting year")
    history["kmart_accounting_week_number_in_year"] = column("Accounting week")
    history["kmart_accounting_period_number"] = column("Accounting period number")
    history["Week"] = column("Week")
    modelled_cost = pd.to_numeric(column("Modelled cost ex GST"), errors="coerce")
    if prefer_invoiced_actual_cost:
        invoiced_cost = pd.to_numeric(
            column("Invoiced actual cost ex GST"), errors="coerce"
        )
        invoice_status = column("Invoice status").fillna("Not yet matched").astype(str)
        invoice_available = invoice_status.ne("Not yet matched") & invoiced_cost.notna()
        history["actual_est_cost_ex_gst"] = invoiced_cost.where(
            invoice_available, modelled_cost
        )
    else:
        history["actual_est_cost_ex_gst"] = column("Modelled cost ex GST")
    history["modelled_total_cost_ex_gst"] = column("Modelled cost ex GST")
    history["actual_est_base_cost_ex_gst"] = column("Modelled base cost ex GST")
    history["actual_est_fuel_surcharge_ex_gst"] = column("Modelled fuel surcharge ex GST")
    history["actual_est_fuel_surcharge_pct"] = column("Modelled fuel surcharge %")
    history["actual_est_fuel_surcharge_source"] = column("Modelled fuel surcharge source")
    history["dest_suburb"] = column("Destination suburb")
    history["dest_state"] = column("Destination state")
    history["dest_postcode"] = column("Destination postcode")
    history["cost_weight_basis_kg"] = column("Modelled cost weight basis kg")
    history["allied_zone"] = column("Modelled Allied zone")
    history["allied_total_cost_ex_gst"] = column("Modelled Allied total cost ex GST")
    history["ap_zone"] = column("Modelled AP zone")
    history["cp_zone"] = column("Modelled Couriers Please zone")
    history["goexpress_zone"] = column("Modelled GoExpress zone")
    history["actual_parcel_count"] = column("Shippit parcel count")
    history["parcel_units_total"] = column("Shippit units")
    history["est_chargeable_weight_kg"] = column("Estimator chargeable weight kg")
    history["est_packed_sku_weight_kg"] = column("Estimator packed SKU weight kg")
    history["est_packed_sku_cube_total_m3"] = column("Estimator packed SKU cube m3")
    history["est_packed_sku_cube_total_cm3"] = pd.to_numeric(
        column("Estimator packed SKU cube m3"), errors="coerce"
    ) * 1_000_000
    history["est_packed_sku_cube_imputed_flag"] = column(
        "Estimator packed SKU cube imputed"
    )
    history["est_volumetric_weight_kg"] = column("Estimator volumetric weight kg")
    history["est_parcel_cube_total_m3"] = column("Estimator cube m3")
    history["est_parcel_cube_total_cm3"] = pd.to_numeric(
        column("Estimator cube m3"), errors="coerce"
    ) * 1_000_000
    history["shippit_chargeable_weight_kg"] = column("Shippit chargeable weight kg")
    history["shippit_dead_weight_kg"] = column("Shippit dead weight kg")
    history["shippit_volumetric_weight_kg"] = column("Shippit volumetric weight kg")
    history["shippit_chargeable_cube_cm3"] = pd.to_numeric(
        column("Shippit chargeable cube m3"), errors="coerce"
    ) * 1_000_000
    history["shippit_volumetric_cube_cm3"] = history["shippit_chargeable_cube_cm3"]
    history["ap_pricing_zone"] = column("AP pricing zone")
    history["reporting_weight_band"] = column("Reporting weight band")
    return history.reset_index(drop=True)


def _reconciliation_weekly_summary(coverage: pd.DataFrame) -> pd.DataFrame:
    if coverage.empty:
        return pd.DataFrame()

    work = coverage.copy()
    if "_accuracy_matched" not in work:
        modelled = pd.to_numeric(work.get("Modelled cost ex GST"), errors="coerce")
        invoiced = pd.to_numeric(work.get("Invoiced actual cost ex GST"), errors="coerce")
        work["_accuracy_matched"] = (
            work.get("Invoice status", pd.Series("Not yet matched", index=work.index))
            .astype(str)
            .ne("Not yet matched")
            & modelled.notna()
            & invoiced.notna()
        )
    if "_week_sort" not in work:
        year = pd.to_numeric(work.get("Accounting year"), errors="coerce")
        week = pd.to_numeric(work.get("Accounting week"), errors="coerce")
        work["_week_sort"] = year * 100 + week
        fallback = pd.Series(
            pd.factorize(work.get("Week", pd.Series(index=work.index)).astype(str))[0],
            index=work.index,
            dtype="float64",
        )
        work["_week_sort"] = work["_week_sort"].fillna(fallback)
    if "CFC" not in work:
        work["CFC"] = ""
    if "Shippit units" not in work:
        work["Shippit units"] = pd.Series(float("nan"), index=work.index, dtype="float64")
    if "Shippit parcel count" not in work:
        work["Shippit parcel count"] = pd.Series(float("nan"), index=work.index, dtype="float64")

    rows = []
    for (cfc, carrier, service, week_sort, week_label), group in work.groupby(
        ["CFC", "Shipped carrier", "Shipped service", "_week_sort", "Week"], dropna=False, sort=True
    ):
        matched = group["Invoice status"].ne("Not yet matched")
        accuracy = group["_accuracy_matched"].fillna(False).astype(bool)
        estimator_modelled = pd.to_numeric(
            group["Modelled cost ex GST"]
            if "Modelled cost ex GST" in group
            else pd.Series(index=group.index, dtype="float64"),
            errors="coerce",
        )
        estimator_costed = estimator_modelled.notna()
        modelled = pd.to_numeric(group.loc[accuracy, "Modelled cost ex GST"], errors="coerce")
        invoiced = pd.to_numeric(group.loc[accuracy, "Invoiced actual cost ex GST"], errors="coerce")
        variance = pd.to_numeric(group.loc[accuracy, "Variance ex GST"], errors="coerce")
        units = pd.to_numeric(group.loc[accuracy, "Shippit units"], errors="coerce")
        estimator_units = pd.to_numeric(
            group.loc[estimator_costed, "Shippit units"], errors="coerce"
        )
        estimator_modelled_total = (
            float(estimator_modelled.sum()) if estimator_modelled.notna().any() else None
        )
        estimator_units_total = (
            float(estimator_units.sum())
            if estimator_units.notna().any() and estimator_units.notna().all()
            else None
        )
        estimator_costed_consignments = int(estimator_costed.sum())
        estimator_parcels = pd.to_numeric(
            group.loc[estimator_costed, "Shippit parcel count"], errors="coerce"
        )
        estimator_parcels_total = (
            float(estimator_parcels.sum())
            if estimator_parcels.notna().any() and estimator_parcels.notna().all()
            else None
        )
        modelled_total = float(modelled.sum()) if modelled.notna().any() else None
        invoiced_total = float(invoiced.sum()) if invoiced.notna().any() else None
        variance_total = float(variance.sum()) if variance.notna().any() else None
        matched_parcels = pd.to_numeric(
            group.loc[accuracy, "Shippit parcel count"], errors="coerce"
        )
        matched_parcels_total = (
            float(matched_parcels.sum())
            if matched_parcels.notna().any() and matched_parcels.notna().all()
            else None
        )
        units_total = (
            float(units.sum())
            if units.notna().any() and units.notna().all()
            else None
        )
        matched_accuracy_count = int(accuracy.sum())
        rows.append(
            {
                "_week_sort": week_sort,
                "CFC": cfc,
                "Shipped carrier": carrier,
                "Shipped service": service,
                "Week": week_label,
                "Shippit consignments": len(group),
                "Matched consignments": int(matched.sum()),
                "Invoice coverage %": matched.mean() * 100,
                "Not yet matched": int((~matched).sum()),
                "Estimator cost ex GST": estimator_modelled_total,
                "Estimator costed consignments": estimator_costed_consignments,
                "Estimator costed parcels": estimator_parcels_total,
                "Estimator Shippit units": estimator_units_total,
                "Estimator cost per consignment ex GST": (
                    estimator_modelled_total / estimator_costed_consignments
                    if estimator_modelled_total is not None and estimator_costed_consignments
                    else None
                ),
                "Estimator cost per parcel ex GST": (
                    estimator_modelled_total / estimator_parcels_total
                    if estimator_modelled_total is not None
                    and estimator_parcels_total not in (None, 0)
                    else None
                ),
                "Estimator cost per unit ex GST": (
                    estimator_modelled_total / estimator_units_total
                    if estimator_modelled_total is not None
                    and estimator_units_total not in (None, 0)
                    else None
                ),
                "Estimator CPU ex GST": (
                    estimator_modelled_total / estimator_units_total
                    if estimator_modelled_total is not None
                    and estimator_units_total not in (None, 0)
                    else None
                ),
                "Matched modelled cost ex GST": modelled_total,
                "Invoiced actual cost ex GST": invoiced_total,
                "Variance ex GST": variance_total,
                "Matched Shippit parcels": matched_parcels_total,
                "Matched Shippit units": units_total,
                "Invoiced actual cost per consignment ex GST": (
                    invoiced_total / matched_accuracy_count
                    if invoiced_total is not None and matched_accuracy_count
                    else None
                ),
                "Invoiced actual cost per parcel ex GST": (
                    invoiced_total / matched_parcels_total
                    if invoiced_total is not None
                    and matched_parcels_total not in (None, 0)
                    else None
                ),
                "Invoiced actual cost per unit ex GST": (
                    invoiced_total / units_total
                    if invoiced_total is not None and units_total not in (None, 0)
                    else None
                ),
                "Modelled CPU ex GST": (
                    modelled_total / units_total
                    if modelled_total is not None and units_total not in (None, 0)
                    else None
                ),
                "Invoiced actual CPU ex GST": (
                    invoiced_total / units_total
                    if invoiced_total is not None and units_total not in (None, 0)
                    else None
                ),
                "Variance CPU ex GST": (
                    variance_total / units_total
                    if variance_total is not None and units_total not in (None, 0)
                    else None
                ),
                "Variance %": (
                    variance_total / modelled_total * 100
                    if variance_total is not None and modelled_total not in (None, 0)
                    else None
                ),
                "Matched records with cost": matched_accuracy_count,
                "Mean absolute variance per matched consignment ex GST": (
                    float(variance.abs().sum()) / matched_accuracy_count
                    if matched_accuracy_count
                    else None
                ),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values(
            ["CFC", "Shipped carrier", "Shipped service", "_week_sort"],
            na_position="last",
        )
        .drop(columns="_week_sort")
    )


def _reconciliation_weekly_matrix(weekly: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Pivot weekly coverage metrics with the newest accounting week first."""
    if weekly.empty or metric not in weekly:
        return pd.DataFrame()

    work = weekly.copy()
    week_labels = work.get("Week", pd.Series("", index=work.index)).astype(str)
    week_parts = week_labels.str.extract(r"(\d{4}).*?W\s*(\d+)", expand=True)
    work["_week_sort"] = (
        pd.to_numeric(week_parts[0], errors="coerce") * 100
        + pd.to_numeric(week_parts[1], errors="coerce")
    )
    fallback = pd.Series(
        pd.factorize(week_labels)[0], index=work.index, dtype="float64"
    )
    work["_week_sort"] = work["_week_sort"].fillna(fallback)
    week_order = (
        work[["Week", "_week_sort"]]
        .drop_duplicates()
        .sort_values("_week_sort", ascending=False, na_position="last")["Week"]
        .tolist()
    )

    row_columns = ["CFC", "Shipped carrier", "Shipped service"]
    for column in row_columns:
        if column not in work:
            work[column] = ""
    matrix = work.pivot_table(
        index=row_columns,
        columns="Week",
        values=metric,
        aggfunc="first",
        sort=False,
    )
    matrix = matrix.reindex(columns=week_order).reset_index()
    matrix.columns.name = None
    return matrix


def _dashboard_weekly_history_matrix(
    weekly: pd.DataFrame,
    metric: str,
) -> pd.DataFrame:
    """Add aggregate invoice verification above the selected weekly measure."""
    matrix = _reconciliation_weekly_matrix(weekly, metric)
    coverage = _weekly_history_trend(weekly, "Invoice coverage %")
    if coverage.empty:
        return matrix

    row_columns = ["CFC", "Shipped carrier", "Shipped service"]
    if matrix.empty:
        week_columns = coverage["Week"].astype(str).drop_duplicates().iloc[::-1].tolist()
        matrix = pd.DataFrame(columns=row_columns + week_columns)
    else:
        week_columns = [column for column in matrix.columns if column not in row_columns]

    coverage_by_week = coverage.set_index("Week")["Value"]
    coverage_row = {column: "" for column in matrix.columns}
    coverage_row["CFC"] = "Invoice verified %"
    for week in week_columns:
        coverage_row[week] = coverage_by_week.get(week, pd.NA)

    return pd.concat([pd.DataFrame([coverage_row]), matrix], ignore_index=True)


def _filter_dashboard_weekly_history(
    weekly: pd.DataFrame,
    site: str,
    shipped_carrier: str,
) -> pd.DataFrame:
    """Filter the persisted weekly history without loading consignment detail."""
    if weekly.empty:
        return weekly

    filtered = weekly.copy()
    if site != "ALL" and "CFC" in filtered:
        filtered = filtered.loc[
            filtered["CFC"].fillna("").astype(str).str.upper().eq(site)
        ]
    if shipped_carrier != "All shipped carriers" and "Shipped carrier" in filtered:
        carrier_aliases = {
            "Australia Post": {"Australia Post", "eParcel"},
            "eParcel": {"Australia Post", "eParcel"},
        }
        allowed = carrier_aliases.get(shipped_carrier, {shipped_carrier})
        filtered = filtered.loc[
            filtered["Shipped carrier"].fillna("").astype(str).str.strip().isin(allowed)
        ]
    return filtered


def _weekly_history_trend(weekly: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Aggregate weekly history into one trend line for the selected scope."""
    derived_metrics = {
        "Invoice coverage %",
        "Variance %",
        "Estimator CPU ex GST",
        "Invoiced actual CPU ex GST",
        "Variance CPU ex GST",
        "Estimator cost per consignment ex GST",
        "Estimator cost per parcel ex GST",
        "Estimator cost per unit ex GST",
        "Invoiced actual cost per consignment ex GST",
        "Invoiced actual cost per parcel ex GST",
        "Invoiced actual cost per unit ex GST",
    }
    if weekly.empty or (metric not in weekly and metric not in derived_metrics):
        return pd.DataFrame(columns=["Week", "Value"])

    work = weekly.copy()
    week_values = work["Week"] if "Week" in work else pd.Series("", index=work.index)
    work["Week"] = week_values.fillna("").astype(str)
    work["_week_sort"] = pd.to_numeric(
        work.get("_week_sort", pd.Series(index=work.index)), errors="coerce"
    )
    if work["_week_sort"].isna().all():
        parts = work["Week"].str.extract(r"(\d{4}).*?W\s*(\d+)", expand=True)
        work["_week_sort"] = pd.to_numeric(parts[0], errors="coerce") * 100 + pd.to_numeric(
            parts[1], errors="coerce"
        )

    def numeric(column: str) -> pd.Series:
        return pd.to_numeric(
            work[column]
            if column in work
            else pd.Series(index=work.index, dtype="float64"),
            errors="coerce",
        )

    if metric == "Invoice coverage %":
        numerator = numeric("Matched consignments")
        denominator = numeric("Shippit consignments")
        work["_value"] = numerator
        grouped = work.groupby(["Week", "_week_sort"], dropna=False)["_value"].sum()
        denominator_by_week = denominator.groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum()
        result = grouped.div(denominator_by_week.replace(0, pd.NA)).mul(100)
    elif metric == "Variance %":
        numerator = numeric("Variance ex GST")
        denominator = numeric("Matched modelled cost ex GST")
        grouped = numerator.groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum()
        denominator_by_week = denominator.groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum()
        result = grouped.div(denominator_by_week.replace(0, pd.NA)).mul(100)
    elif metric in {
        "Estimator CPU ex GST",
        "Invoiced actual CPU ex GST",
        "Variance CPU ex GST",
        "Estimator cost per consignment ex GST",
        "Estimator cost per parcel ex GST",
        "Estimator cost per unit ex GST",
        "Invoiced actual cost per consignment ex GST",
        "Invoiced actual cost per parcel ex GST",
        "Invoiced actual cost per unit ex GST",
    }:
        numerator_column = {
            "Estimator CPU ex GST": "Estimator cost ex GST",
            "Invoiced actual CPU ex GST": "Invoiced actual cost ex GST",
            "Variance CPU ex GST": "Variance ex GST",
            "Estimator cost per consignment ex GST": "Estimator cost ex GST",
            "Estimator cost per parcel ex GST": "Estimator cost ex GST",
            "Estimator cost per unit ex GST": "Estimator cost ex GST",
            "Invoiced actual cost per consignment ex GST": "Invoiced actual cost ex GST",
            "Invoiced actual cost per parcel ex GST": "Invoiced actual cost ex GST",
            "Invoiced actual cost per unit ex GST": "Invoiced actual cost ex GST",
        }[metric]
        denominator_column = {
            "Estimator CPU ex GST": "Estimator Shippit units",
            "Invoiced actual CPU ex GST": "Matched Shippit units",
            "Variance CPU ex GST": "Matched Shippit units",
            "Estimator cost per consignment ex GST": "Estimator costed consignments",
            "Estimator cost per parcel ex GST": "Estimator costed parcels",
            "Estimator cost per unit ex GST": "Estimator Shippit units",
            "Invoiced actual cost per consignment ex GST": "Matched records with cost",
            "Invoiced actual cost per parcel ex GST": "Matched Shippit parcels",
            "Invoiced actual cost per unit ex GST": "Matched Shippit units",
        }[metric]
        numerator = numeric(numerator_column).groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum()
        denominator = numeric(denominator_column).groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum()
        result = numerator.div(denominator.replace(0, pd.NA))
    else:
        values = numeric(metric)
        result = values.groupby(
            [work["Week"], work["_week_sort"]], dropna=False
        ).sum(min_count=1)

    trend = result.rename("Value").reset_index()
    return trend.sort_values("_week_sort").drop(columns="_week_sort")


def _dashboard_weekly_history(
    weekly: pd.DataFrame,
    site: str,
    shipped_carrier: str,
    selected_weeks: list[str] | None = None,
) -> None:
    """Render the complete weekly history from the persisted aggregate."""
    st.subheader("Weekly history and trend")
    st.caption(
        "This view uses the durable saved history and is independent of the operational "
        "14-day dashboard window. Estimator cost includes costed estimator rows; invoiced "
        "actual cost and variance use matched invoice records."
    )
    history = _filter_dashboard_weekly_history(weekly, site, shipped_carrier)
    if selected_weeks is not None and "Week" in history:
        history = history.loc[history["Week"].astype(str).isin(selected_weeks)]
    if history.empty:
        st.info("No weekly history is available for this scope yet. Run the daily refresh to initialize the durable store.")
        return

    metric_options = [
        ("Shippit consignments", "Shippit consignments"),
        ("Matched consignments", "Matched consignments"),
        ("Invoice coverage %", "Invoice coverage %"),
        ("Estimator cost ex GST", "Estimator cost ex GST"),
        ("Matched estimator cost ex GST", "Matched modelled cost ex GST"),
        ("Invoiced actual cost ex GST", "Invoiced actual cost ex GST"),
        ("Variance ex GST", "Variance ex GST"),
        ("Estimator cost per consignment ex GST", "Estimator cost per consignment ex GST"),
        ("Estimator cost per parcel ex GST", "Estimator cost per parcel ex GST"),
        ("Estimator cost per unit ex GST", "Estimator cost per unit ex GST"),
        ("Invoiced actual cost per consignment ex GST", "Invoiced actual cost per consignment ex GST"),
        ("Invoiced actual cost per parcel ex GST", "Invoiced actual cost per parcel ex GST"),
        ("Invoiced actual cost per unit ex GST", "Invoiced actual cost per unit ex GST"),
        ("Estimator CPU ex GST", "Estimator CPU ex GST"),
        ("Invoiced actual CPU ex GST", "Invoiced actual CPU ex GST"),
        ("Variance %", "Variance %"),
    ]
    available_options = [option for option in metric_options if option[1] in history]
    if not available_options:
        st.info("No weekly trend measures are available in the durable history store yet.")
        return
    option_labels = [label for label, _ in available_options]
    selected_label = st.selectbox(
        "Weekly history measure",
        option_labels,
        index=option_labels.index("Estimator cost ex GST")
        if "Estimator cost ex GST" in option_labels
        else 0,
    )
    metric = dict(available_options)[selected_label]
    trend = _weekly_history_trend(history, metric)
    if trend.empty:
        st.info("No values are available for this weekly history measure.")
        return

    is_percent = metric in {"Invoice coverage %", "Variance %"}
    is_currency = "cost" in selected_label.lower() or "cpu" in selected_label.lower()
    value_format = (
        "{:,.1f}%"
        if is_percent
        else "${:,.2f}"
        if is_currency
        else "{:,.0f}"
    )
    chart = (
        alt.Chart(trend)
        .mark_line(point=True, strokeWidth=3)
        .encode(
            x=alt.X(
                "Week:N",
                title="Accounting week",
                sort=trend["Week"].tolist(),
            ),
            y=alt.Y(
                "Value:Q",
                title=selected_label,
                axis=alt.Axis(
                    format=(
                        ",.1f"
                        if is_percent
                        else "$,.0f"
                        if is_currency
                        else ",.0f"
                    )
                ),
            ),
            tooltip=[
                alt.Tooltip("Week:N", title="Accounting week"),
                alt.Tooltip(
                    "Value:Q",
                    title=selected_label,
                    format=(
                        ",.1f"
                        if is_percent
                        else "$,.2f"
                        if is_currency
                        else ",.0f"
                    ),
                ),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, width="stretch")

    history_matrix = _dashboard_weekly_history_matrix(history, metric)
    value_columns = [
        column
        for column in history_matrix.columns
        if column not in {"CFC", "Shipped carrier", "Shipped service"}
    ]
    matrix_style = history_matrix.style
    if len(history_matrix) > 1:
        matrix_style = matrix_style.format(
            {column: value_format for column in value_columns},
            subset=pd.IndexSlice[1:, value_columns],
            na_rep="-",
        )
    matrix_style = matrix_style.format(
        {column: "{:,.1f}%" for column in value_columns},
        subset=pd.IndexSlice[0:0, value_columns],
        na_rep="-",
    )
    matrix_style = matrix_style.apply(
        lambda row: [
            "font-weight: 700; background-color: #eef5ef;"
            if row.name == 0
            else ""
            for _ in row
        ],
        axis=1,
    )
    st.dataframe(
        matrix_style,
        width="stretch",
        hide_index=True,
    )


def _air_packaging_diagnostics(
    coverage: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calculate air, packaging-weight and directional opportunity diagnostics."""
    detail_columns = [
        "Shippit invoice number",
        "Shippit tracking number",
        "CFC",
        "Shipped carrier",
        "Shipped service",
        "Week",
        "Invoice status",
        "Cube physical basis",
        "Dead-weight physical basis",
        "Physical cube m3",
        "Estimator parcel cube m3",
        "Estimator packed SKU cube m3",
        "Signed cube gap m3",
        "Air cube m3",
        "Air share %",
        "Mean air share target %",
        "Physical dead weight kg",
        "Estimator packed SKU weight kg",
        "Added packaging/dunnage weight kg",
        "Added packaging/dunnage weight %",
        "Physical volumetric weight kg",
        "Physical chargeable weight kg",
        "Target physical cube m3",
        "Target volumetric weight kg",
        "Target chargeable weight kg",
        "Chargeable weight reduction kg",
        "Current cost ex GST",
        "Cost basis",
        "Indicative value of reducing excess air ex GST",
        "Volume-led",
        "Estimator packed SKU cube imputed",
    ]
    if coverage.empty:
        return pd.DataFrame(columns=detail_columns), pd.DataFrame()

    work = coverage.copy()

    def numeric(column: str) -> pd.Series:
        return pd.to_numeric(
            work.get(column, pd.Series(float("nan"), index=work.index)),
            errors="coerce",
        )

    def text(column: str, default: str = "") -> pd.Series:
        return work.get(
            column,
            pd.Series(default, index=work.index, dtype="string"),
        ).fillna(default).astype("string")

    invoice_status = text("Invoice status", "Not yet matched")
    verified = invoice_status.ne("Not yet matched")
    invoice_cube = numeric("Invoiced actual cube m3")
    shippit_cube = numeric("Shippit chargeable cube m3")
    use_invoice_cube = verified & invoice_cube.gt(0)
    physical_cube = invoice_cube.where(use_invoice_cube, shippit_cube)
    cube_basis = pd.Series("Shippit fallback", index=work.index, dtype="string")
    cube_basis.loc[use_invoice_cube] = "Invoiced actual"

    invoice_dead_weight = numeric("Invoiced actual dead weight kg")
    shippit_dead_weight = numeric("Shippit dead weight kg")
    use_invoice_dead_weight = verified & invoice_dead_weight.notna()
    physical_dead_weight = invoice_dead_weight.where(
        use_invoice_dead_weight, shippit_dead_weight
    )
    dead_weight_basis = pd.Series("Shippit fallback", index=work.index, dtype="string")
    dead_weight_basis.loc[use_invoice_dead_weight] = "Invoiced actual"

    invoice_volumetric_weight = numeric("Invoiced actual cubic weight kg")
    shippit_volumetric_weight = numeric("Shippit volumetric weight kg")
    use_invoice_volumetric_weight = verified & invoice_volumetric_weight.notna()
    physical_volumetric_weight = invoice_volumetric_weight.where(
        use_invoice_volumetric_weight, shippit_volumetric_weight
    )

    invoice_chargeable_weight = numeric("Invoiced actual chargeable weight kg")
    shippit_chargeable_weight = numeric("Shippit chargeable weight kg")
    use_invoice_chargeable_weight = verified & invoice_chargeable_weight.gt(0)
    physical_chargeable_weight = invoice_chargeable_weight.where(
        use_invoice_chargeable_weight, shippit_chargeable_weight
    )
    inferred_chargeable_weight = pd.concat(
        [physical_dead_weight, physical_volumetric_weight], axis=1
    ).max(axis=1, skipna=True)
    physical_chargeable_weight = physical_chargeable_weight.where(
        physical_chargeable_weight.gt(0), inferred_chargeable_weight
    )

    packed_sku_cube = numeric("Estimator packed SKU cube m3")
    estimator_parcel_cube = numeric("Estimator cube m3")
    packed_sku_weight = numeric("Estimator packed SKU weight kg")
    usable = physical_cube.gt(0) & packed_sku_cube.gt(0)
    signed_cube_gap = physical_cube - packed_sku_cube
    air_cube = signed_cube_gap.clip(lower=0).where(usable)
    air_share_pct = air_cube.div(physical_cube.replace(0, pd.NA)).mul(100).where(usable)

    group_columns = ["CFC", "Shipped carrier", "Shipped service"]
    for column in group_columns:
        if column not in work:
            work[column] = ""
        work[column] = work[column].fillna("").astype("string")
    work["_usable"] = usable
    work["_verified"] = verified.astype(int)
    work["_air_share_pct"] = air_share_pct
    mean_air_share_pct = work.groupby(group_columns, dropna=False)["_air_share_pct"].transform("mean")
    target_air_ratio = (mean_air_share_pct / 100).clip(lower=0, upper=0.95)
    target_cube = packed_sku_cube.div((1 - target_air_ratio).replace(0, pd.NA))
    target_cube = target_cube.where(usable & mean_air_share_pct.notna())

    cube_to_volumetric_ratio = physical_volumetric_weight.div(
        physical_cube.replace(0, pd.NA)
    )
    target_volumetric_weight = target_cube.mul(cube_to_volumetric_ratio)
    target_volumetric_weight = target_volumetric_weight.fillna(target_cube * 250.0)
    target_chargeable_weight = pd.concat(
        [physical_dead_weight, target_volumetric_weight], axis=1
    ).max(axis=1, skipna=True)
    chargeable_reduction = (
        physical_chargeable_weight - target_chargeable_weight
    ).clip(lower=0)
    volume_led = (
        physical_volumetric_weight.gt(physical_dead_weight + 0.01)
        & physical_volumetric_weight.notna()
        & physical_dead_weight.notna()
    )
    chargeable_reduction = chargeable_reduction.where(volume_led & usable, 0.0)

    actual_cost = numeric("Invoiced actual cost ex GST")
    modelled_cost = numeric("Modelled cost ex GST")
    use_actual_cost = verified & actual_cost.notna()
    current_cost = actual_cost.where(use_actual_cost, modelled_cost)
    cost_basis = pd.Series("Modelled estimate", index=work.index, dtype="string")
    cost_basis.loc[use_actual_cost] = "Invoiced actual"
    actual_base = numeric("Invoiced actual base cost ex GST")
    actual_fuel = numeric("Invoiced actual fuel surcharge ex GST")
    invoice_volume_sensitive_cost = actual_base + actual_fuel
    use_invoice_volume_sensitive_cost = (
        use_actual_cost & actual_base.notna() & actual_fuel.notna()
    )
    volume_sensitive_cost = invoice_volume_sensitive_cost.where(
        use_invoice_volume_sensitive_cost, modelled_cost
    )
    opportunity = volume_sensitive_cost.mul(
        chargeable_reduction.div(physical_chargeable_weight.replace(0, pd.NA))
    ).clip(lower=0)

    work["_air_cube"] = air_cube
    work["_volume_led"] = volume_led
    work["_opportunity"] = opportunity

    added_packaging_weight = (
        physical_dead_weight - packed_sku_weight
    ).clip(lower=0).where(physical_dead_weight.notna() & packed_sku_weight.notna())
    added_packaging_weight_pct = added_packaging_weight.div(
        packed_sku_weight.replace(0, pd.NA)
    ).mul(100)

    imputed = text("Estimator packed SKU cube imputed")
    detail = pd.DataFrame(index=work.index)
    detail["Shippit invoice number"] = text("Shippit invoice number")
    detail["Shippit tracking number"] = text("Shippit tracking number")
    detail["CFC"] = text("CFC")
    detail["Shipped carrier"] = text("Shipped carrier")
    detail["Shipped service"] = text("Shipped service")
    detail["Week"] = text("Week")
    detail["Invoice status"] = invoice_status
    detail["Cube physical basis"] = cube_basis
    detail["Dead-weight physical basis"] = dead_weight_basis
    detail["Physical cube m3"] = physical_cube
    detail["Estimator parcel cube m3"] = estimator_parcel_cube
    detail["Estimator packed SKU cube m3"] = packed_sku_cube
    detail["Signed cube gap m3"] = signed_cube_gap.where(usable)
    detail["Air cube m3"] = air_cube
    detail["Air share %"] = air_share_pct
    detail["Mean air share target %"] = mean_air_share_pct
    detail["Physical dead weight kg"] = physical_dead_weight
    detail["Estimator packed SKU weight kg"] = packed_sku_weight
    detail["Added packaging/dunnage weight kg"] = added_packaging_weight
    detail["Added packaging/dunnage weight %"] = added_packaging_weight_pct
    detail["Physical volumetric weight kg"] = physical_volumetric_weight
    detail["Physical chargeable weight kg"] = physical_chargeable_weight
    detail["Target physical cube m3"] = target_cube
    detail["Target volumetric weight kg"] = target_volumetric_weight
    detail["Target chargeable weight kg"] = target_chargeable_weight
    detail["Chargeable weight reduction kg"] = chargeable_reduction
    detail["Current cost ex GST"] = current_cost
    detail["Cost basis"] = cost_basis
    detail["Indicative value of reducing excess air ex GST"] = opportunity
    detail["Volume-led"] = volume_led.map({True: "Y", False: ""})
    detail["Estimator packed SKU cube imputed"] = imputed

    group = work.groupby(group_columns, dropna=False, sort=True)
    summary = group.agg(
        Records=("_usable", "size"),
        **{
            "Usable physical basis records": ("_usable", "sum"),
            "Invoice verified %": ("_verified", "mean"),
            "Mean air share %": ("_air_share_pct", "mean"),
            "Air cube m3": ("_air_cube", "sum"),
            "Volume-led records": ("_volume_led", "sum"),
            "Outlier records": ("_opportunity", lambda values: (values > 0).sum()),
            "Indicative value of reducing excess air ex GST": ("_opportunity", "sum"),
        },
    ).reset_index()
    summary["Invoice verified %"] = summary["Invoice verified %"] * 100
    summary["Mean air share %"] = summary["Mean air share %"].round(1)
    summary["Air cube m3"] = summary["Air cube m3"].round(4)
    summary["Indicative value of reducing excess air ex GST"] = summary[
        "Indicative value of reducing excess air ex GST"
    ].round(2)
    return detail[detail_columns], summary


def _air_weekly_summary(air_detail: pd.DataFrame) -> pd.DataFrame:
    """Summarise air and packaging signals by accounting week for trend review."""
    columns = [
        "Week",
        "Records",
        "Usable physical-basis records",
        "Invoice verified %",
        "Mean air share %",
        "Median air share %",
        "P90 air share %",
        "Positive excess-air records %",
        "Air cube m3",
        "Volume-led records",
        "Volume-led records %",
        "Median added packaging/dunnage kg",
        "Median added packaging/dunnage %",
        "Outlier records",
        "Indicative value of reducing excess air ex GST",
    ]
    if air_detail.empty or "Week" not in air_detail:
        return pd.DataFrame(columns=columns)

    def numeric(group: pd.DataFrame, column: str) -> pd.Series:
        return pd.to_numeric(
            group.get(column, pd.Series(float("nan"), index=group.index)),
            errors="coerce",
        )

    weeks = air_detail["Week"].fillna("").astype(str)
    week_parts = weeks.str.extract(r"(\d{4}).*?W\s*(\d+)", expand=True)
    week_sort = pd.to_numeric(week_parts[0], errors="coerce") * 100 + pd.to_numeric(
        week_parts[1], errors="coerce"
    )
    rows = []
    for (week, week_key), group in air_detail.assign(_week_sort=week_sort).groupby(
        ["Week", "_week_sort"], dropna=False, sort=True
    ):
        air_share = numeric(group, "Air share %")
        usable = air_share.notna()
        positive_air = air_share.gt(0)
        verified = group.get(
            "Invoice status",
            pd.Series("Not yet matched", index=group.index),
        ).astype(str).ne("Not yet matched")
        volume_led = group.get(
            "Volume-led", pd.Series("", index=group.index, dtype="string")
        ).astype(str).eq("Y")
        opportunity = numeric(
            group, "Indicative value of reducing excess air ex GST"
        )
        packaging_weight = numeric(group, "Added packaging/dunnage weight kg")
        packaging_pct = numeric(group, "Added packaging/dunnage weight %")
        rows.append(
            {
                "Week": str(week),
                "_week_sort": week_key,
                "Records": len(group),
                "Usable physical-basis records": int(usable.sum()),
                "Invoice verified %": float(verified.mean() * 100),
                "Mean air share %": float(air_share.mean()) if usable.any() else None,
                "Median air share %": float(air_share.median()) if usable.any() else None,
                "P90 air share %": float(air_share.quantile(0.90)) if usable.any() else None,
                "Positive excess-air records %": float(
                    positive_air.sum() / usable.sum() * 100
                )
                if usable.any()
                else None,
                "Air cube m3": float(numeric(group, "Air cube m3").sum(min_count=1))
                if numeric(group, "Air cube m3").notna().any()
                else None,
                "Volume-led records": int((volume_led & usable).sum()),
                "Volume-led records %": float((volume_led & usable).sum() / usable.sum() * 100)
                if usable.any()
                else None,
                "Median added packaging/dunnage kg": float(packaging_weight.median())
                if packaging_weight.notna().any()
                else None,
                "Median added packaging/dunnage %": float(packaging_pct.median())
                if packaging_pct.notna().any()
                else None,
                "Outlier records": int(opportunity.gt(0).sum()),
                "Indicative value of reducing excess air ex GST": float(
                    opportunity.sum(min_count=1)
                )
                if opportunity.notna().any()
                else None,
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values("_week_sort").drop(columns="_week_sort")[
        columns
    ]


def _historical_trends_workspace(
    coverage: pd.DataFrame,
    weekly: pd.DataFrame,
    site: str,
    shipped_carrier: str,
    parcel_scope: str,
) -> None:
    """Render persisted weekly trends and invoice/estimator historical lane analysis."""
    st.subheader("Historical trends")
    st.caption(
        "This workspace uses the combined saved history rather than the operational rolling window. "
        "Weekly trend measures distinguish estimator outputs from matched invoiced actuals. "
        "The current view begins at FY2027 W01."
    )

    scoped_coverage = _filter_persisted_coverage(
        coverage,
        site,
        shipped_carrier,
        parcel_scope,
        "All available refreshed data",
    )
    scoped_coverage = _filter_historical_trend_year(scoped_coverage)
    if scoped_coverage.empty:
        st.info("No saved historical coverage is available for this scope yet.")
        return

    week_options = _accounting_week_options(scoped_coverage)
    selected_weeks = st.multiselect(
        "Historical weeks",
        week_options,
        default=week_options,
        help="Choose the accounting weeks represented in the trend and lane/weight matrix.",
    )
    if not selected_weeks:
        st.info("Select at least one accounting week to review historical trends.")
        return

    selected_coverage = scoped_coverage.loc[
        scoped_coverage["Week"].astype(str).isin(selected_weeks)
    ].copy()
    selected_weekly = _filter_dashboard_weekly_history(
        _filter_historical_trend_year(weekly),
        site,
        shipped_carrier,
    )
    selected_weekly = selected_weekly.loc[
        selected_weekly["Week"].astype(str).isin(selected_weeks)
    ] if "Week" in selected_weekly else selected_weekly
    required_weekly_metrics = {
        "Estimator cost per consignment ex GST",
        "Estimator cost per parcel ex GST",
        "Estimator cost per unit ex GST",
        "Invoiced actual cost per consignment ex GST",
        "Invoiced actual cost per parcel ex GST",
        "Invoiced actual cost per unit ex GST",
    }
    if parcel_scope != "All consignments" or not required_weekly_metrics.issubset(selected_weekly.columns):
        selected_weekly = _reconciliation_weekly_summary(selected_coverage)
    _dashboard_weekly_history(
        selected_weekly,
        site,
        shipped_carrier,
        selected_weeks,
    )

    st.subheader("Weekly air and packaging trend")
    st.caption(
        "Physical cube and dead weight use invoiced actuals where available and Shippit as a fallback. "
        "These weekly measures are intended to show whether air and packaging behaviour is consistent or lumpy. "
        "Observed excess-air share is clipped at 0% when packed SKU cube is greater than physical cube, so "
        "the median is 0% whenever at least half of usable records have no positive excess-air gap. "
        "The indicative opportunity estimates the value of reducing excess air to the mean air share for the "
        "selected cohort; it is directional and should be validated with Operations."
    )
    air_detail, _ = _air_packaging_diagnostics(selected_coverage)
    usable_air = air_detail["Air share %"].notna() if not air_detail.empty else pd.Series(dtype=bool)
    if air_detail.empty or not usable_air.any():
        st.info(
            "No usable packed SKU cube and physical cube are available for the selected weeks. "
            "Rerun Stage 3 and refresh the saved history after the packed SKU cube fields are available."
        )
    else:
        air_weekly = _air_weekly_summary(air_detail)
        air_metric_options = [
            ("Observed excess-air share mean %", "Mean air share %"),
            ("Observed excess-air share median %", "Median air share %"),
            ("Observed excess-air share P90 %", "P90 air share %"),
            ("Positive excess-air records %", "Positive excess-air records %"),
            ("Air cube m3", "Air cube m3"),
            ("Volume-led records %", "Volume-led records %"),
            ("Indicative air-reduction opportunity", "Indicative value of reducing excess air ex GST"),
        ]
        air_metric_label = st.selectbox(
            "Weekly air trend measure",
            [label for label, _ in air_metric_options],
            index=0,
            key="historical_air_weekly_metric",
            help="Choose a weekly air measure to identify stable patterns or lumpy weeks.",
        )
        air_metric = dict(air_metric_options)[air_metric_label]
        air_trend = air_weekly[["Week", air_metric]].dropna()
        if not air_trend.empty:
            air_is_percent = air_metric.endswith("%")
            air_is_currency = "opportunity" in air_metric_label.lower()
            air_axis_format = ",.1f" if air_is_percent else "$,.0f" if air_is_currency else ",.4f" if air_metric == "Air cube m3" else ",.0f"
            air_tooltip_format = ",.1f" if air_is_percent else "$,.2f" if air_is_currency else ",.4f" if air_metric == "Air cube m3" else ",.0f"
            air_chart = (
                alt.Chart(air_trend)
                .mark_line(point=True, strokeWidth=3)
                .encode(
                    x=alt.X("Week:N", title="Accounting week", sort=air_trend["Week"].tolist()),
                    y=alt.Y(f"{air_metric}:Q", title=air_metric_label, axis=alt.Axis(format=air_axis_format)),
                    tooltip=[
                        alt.Tooltip("Week:N", title="Accounting week"),
                        alt.Tooltip(air_metric, title=air_metric_label, format=air_tooltip_format),
                    ],
                )
                .properties(height=260)
            )
            st.altair_chart(air_chart, width="stretch")

        air_weekly_labels = {
            "Mean air share %": "Observed excess-air share mean %",
            "Median air share %": "Observed excess-air share median %",
            "P90 air share %": "Observed excess-air share P90 %",
        }
        air_weekly_display = (
            air_weekly.set_index("Week")
            .reindex(selected_weeks)
            .T.rename(index=air_weekly_labels)
        )
        air_weekly_display.index.name = "Metric"
        air_weekly_display.columns.name = "Accounting week"
        air_weekly_formats = {
            "Records": "{:,.0f}",
            "Usable physical-basis records": "{:,.0f}",
            "Invoice verified %": "{:.1f}%",
            "Observed excess-air share mean %": "{:.1f}%",
            "Observed excess-air share median %": "{:.1f}%",
            "Observed excess-air share P90 %": "{:.1f}%",
            "Positive excess-air records %": "{:.1f}%",
            "Air cube m3": "{:.4f}",
            "Volume-led records": "{:,.0f}",
            "Volume-led records %": "{:.1f}%",
            "Median added packaging/dunnage kg": "{:.2f}",
            "Median added packaging/dunnage %": "{:.1f}%",
            "Outlier records": "{:,.0f}",
            "Indicative value of reducing excess air ex GST": "${:,.2f}",
        }
        air_weekly_style = air_weekly_display.style
        for metric, number_format in air_weekly_formats.items():
            if metric in air_weekly_display.index:
                air_weekly_style = air_weekly_style.format(
                    number_format,
                    subset=pd.IndexSlice[[metric], :],
                    na_rep="-",
                )
        st.dataframe(air_weekly_style, width="stretch", hide_index=False)

        opportunity = pd.to_numeric(
            air_detail["Indicative value of reducing excess air ex GST"], errors="coerce"
        )
        outlier_mask = opportunity.gt(0)
        outliers = air_detail.loc[outlier_mask].copy().sort_values(
            ["Indicative value of reducing excess air ex GST", "Air share %"],
            ascending=[False, False],
        )
        with st.expander("Air and packaging outliers", expanded=False):
            st.caption("Use this consignment-level list to investigate the weeks and cohorts highlighted above.")
            if outliers.empty:
                st.info("No volume-led air-reduction outliers were identified for the selected scope.")
            else:
                outlier_limit = st.selectbox(
                    "Air outlier list size",
                    [25, 50, 100],
                    index=1,
                    key="historical_air_outlier_limit",
                )
                outlier_display = outliers.head(outlier_limit)
                outlier_style = outlier_display.style.format(
                    {
                        "Physical cube m3": "{:.4f}",
                        "Estimator parcel cube m3": "{:.4f}",
                        "Estimator packed SKU cube m3": "{:.4f}",
                        "Signed cube gap m3": "{:+.4f}",
                        "Air cube m3": "{:.4f}",
                        "Air share %": "{:.1f}%",
                        "Mean air share target %": "{:.1f}%",
                        "Physical dead weight kg": "{:.2f}",
                        "Estimator packed SKU weight kg": "{:.2f}",
                        "Added packaging/dunnage weight kg": "{:.2f}",
                        "Added packaging/dunnage weight %": "{:.1f}%",
                        "Physical volumetric weight kg": "{:.2f}",
                        "Physical chargeable weight kg": "{:.2f}",
                        "Target physical cube m3": "{:.4f}",
                        "Target volumetric weight kg": "{:.2f}",
                        "Target chargeable weight kg": "{:.2f}",
                        "Chargeable weight reduction kg": "{:.2f}",
                        "Current cost ex GST": "${:,.2f}",
                        "Indicative value of reducing excess air ex GST": "${:,.2f}",
                    },
                    na_rep="-",
                )
                st.dataframe(outlier_style, width="stretch", hide_index=True)
                st.download_button(
                    "Download air and packaging outliers CSV",
                    outliers.to_csv(index=False).encode("utf-8"),
                    file_name="historical_air_packaging_outliers.csv",
                    mime="text/csv",
                )

    st.subheader("Historical lane and weight matrix")
    st.caption(
        "The matrix uses all selected Shippit consignments with a Stage 3 modelled cost. "
        "Where an invoice is matched, invoiced actual cost is used; otherwise the estimator cost "
        "is used as the fallback. Rows without an estimator cost are excluded. AP pricing zone and "
        "reporting weight band remain the Stage 3 model classifications."
    )
    matrix_coverage = _historical_matrix_coverage(selected_coverage)
    if matrix_coverage.empty:
        st.info("No selected consignments have a Stage 3 modelled cost for the historical matrix.")
        return

    matrix_carrier_series = matrix_coverage.get(
        "Shipped carrier", pd.Series("", index=matrix_coverage.index)
    ).fillna("").astype(str).str.strip()
    matrix_carrier_values = sorted(value for value in matrix_carrier_series.unique() if value)
    matrix_carrier_options = ["All carriers", *matrix_carrier_values]
    matrix_filter_controls = st.columns((1, 1.8))
    with matrix_filter_controls[0]:
        selected_matrix_carrier = st.selectbox(
            "Historical matrix carrier",
            matrix_carrier_options,
            key="historical_matrix_carrier",
            help="Choose one carrier or keep All carriers for the combined matrix.",
        )
    if selected_matrix_carrier != "All carriers":
        matrix_coverage = matrix_coverage.loc[
            matrix_carrier_series.eq(selected_matrix_carrier)
        ]

    matrix_service_series = matrix_coverage.get(
        "Shipped service", pd.Series("", index=matrix_coverage.index)
    ).fillna("").astype(str).str.strip()
    matrix_service_values = sorted(value for value in matrix_service_series.unique() if value)
    service_key = (
        "historical_matrix_services_"
        + selected_matrix_carrier.lower().replace(" ", "_")
    )
    with matrix_filter_controls[1]:
        selected_matrix_services = st.multiselect(
            "Historical matrix carrier services",
            matrix_service_values,
            default=matrix_service_values,
            key=service_key,
            help="Select one or more services to include in the matrix.",
        )
    if not selected_matrix_services:
        st.info("Select at least one carrier service to display the historical matrix.")
        return
    matrix_coverage = matrix_coverage.loc[matrix_service_series.isin(selected_matrix_services)]
    if matrix_coverage.empty:
        st.info("No selected consignments match the chosen carrier services.")
        return

    matrix_invoice_status = matrix_coverage.get(
        "Invoice status",
        pd.Series("Not yet matched", index=matrix_coverage.index),
    ).fillna("Not yet matched").astype(str)
    matrix_invoiced_cost = pd.to_numeric(
        matrix_coverage.get(
            "Invoiced actual cost ex GST",
            pd.Series(index=matrix_coverage.index, dtype="float64"),
        ),
        errors="coerce",
    )
    matrix_invoice_rows = int(
        (matrix_invoice_status.ne("Not yet matched") & matrix_invoiced_cost.notna()).sum()
    )
    matrix_fallback_rows = len(matrix_coverage) - matrix_invoice_rows
    matrix_excluded_rows = len(selected_coverage) - len(matrix_coverage)
    st.caption(
        f"Cost basis: {matrix_invoice_rows:,} rows use invoiced actuals and "
        f"{matrix_fallback_rows:,} use estimator fallback. "
        f"{matrix_excluded_rows:,} rows without estimator cost are excluded."
    )

    required_dimensions = {"AP pricing zone", "Reporting weight band"}
    if not required_dimensions.issubset(matrix_coverage.columns):
        st.warning(
            "The selected historical coverage predates the saved AP pricing-zone and reporting-weight-band fields. "
            "Rebuild the relevant reconciliation store before using this matrix."
        )
        return

    nonblank_dimensions = {
        column: matrix_coverage[column].fillna("").astype(str).str.strip().ne("").any()
        for column in required_dimensions
    }
    if not all(nonblank_dimensions.values()):
        st.warning(
            "The selected historical coverage does not yet contain usable AP pricing zones and reporting weight bands. "
            "Refresh the reconciliation store after the Stage 3 lineage fields are available."
        )
        return
    has_dimensions = (
        matrix_coverage["AP pricing zone"].fillna("").astype(str).str.strip().ne("")
        & matrix_coverage["Reporting weight band"].fillna("").astype(str).str.strip().ne("")
    )
    missing_dimension_count = int((~has_dimensions).sum())
    if missing_dimension_count:
        st.warning(
            f"{missing_dimension_count:,} selected consignment(s) do not have "
            "aligned Stage 3 lane/weight fields and will appear as 'Unmapped / review required'."
        )

    matrix_rows = _coverage_history_as_stage3(
        matrix_coverage,
        prefer_invoiced_actual_cost=True,
    )
    matrix_controls = st.columns((1, 1, 1, 1.5))
    with matrix_controls[0]:
        matrix_cfc = st.selectbox(
            "Historical matrix CFC",
            ["Both CFCs", "Moorebank CFC", "Truganina CFC"],
        )
    if matrix_cfc == "Moorebank CFC":
        matrix_rows = matrix_rows.loc[matrix_rows["ui_site"] == "MOOREBANK"]
    elif matrix_cfc == "Truganina CFC":
        matrix_rows = matrix_rows.loc[matrix_rows["ui_site"] == "TRUGANINA"]
    with matrix_controls[1]:
        pricing_zone_options = ["All zones"] + _ordered_values(
            matrix_rows, "ap_pricing_zone", AP_PRICING_ZONE_ORDER
        )
        selected_pricing_zone = st.selectbox(
            "AP pricing zone",
            pricing_zone_options,
            key="historical_matrix_ap_pricing_zone",
            help="All zones keeps the cross-zone comparison. Select one zone to trend its weight bands by week.",
        )
    with matrix_controls[2]:
        matrix_family = st.selectbox(
            "Historical measure family",
            ["Volume", "Cost"],
        )
    volume_metrics = [
        "Consignments",
        "Consignments %",
        "Shipped parcels",
        "Shipped parcels %",
        "Shipped units",
        "Shipped units %",
    ]
    cost_metric_options = {
        "Invoiced actual / estimator fallback cost ex GST": "modelled_cost",
        "Cost per consignment": "cost_per_consignment",
        "Cost per parcel": "cost_per_parcel",
        "Cost per unit": "cost_per_unit",
    }
    cost_metrics = list(cost_metric_options)
    with matrix_controls[3]:
        selected_metric_label = st.selectbox(
            "Historical matrix measure",
            volume_metrics if matrix_family == "Volume" else cost_metrics,
        )

    selected_metric_key = (
        cost_metric_options[selected_metric_label]
        if matrix_family == "Cost"
        else METRIC_OPTIONS[selected_metric_label]
    )
    if selected_pricing_zone == "All zones":
        matrix = _slice_matrix(matrix_rows, selected_metric_key)
        matrix_caption = (
            "All zones: cells show the selected-week aggregate by AP pricing zone and reporting weight band."
        )
    else:
        matrix = _historical_zone_week_matrix(
            matrix_rows,
            selected_metric_key,
            selected_pricing_zone,
            selected_weeks,
        )
        matrix_caption = (
            f"{selected_pricing_zone}: cells show the selected measure by reporting weight band and accounting week."
        )
    st.caption(matrix_caption)
    st.dataframe(
        matrix.style.format(_metric_format(selected_metric_key), na_rep="-").apply(
            lambda row: [
                "font-weight: bold;" if row.name == "Total" else ""
                for _ in row
            ],
            axis=1,
        ),
        width="stretch",
    )
    st.download_button(
        "Download historical lane and weight matrix CSV",
        matrix.to_csv().encode("utf-8"),
        file_name=(
            f"historical_lane_weight_matrix_"
            f"{selected_metric_key}_{matrix_cfc.lower().replace(' ', '_')}_"
            f"{selected_pricing_zone.lower().replace(' ', '_')}.csv"
        ),
        mime="text/csv",
    )


def _reconciliation_bridge(detail: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float | None]]:
    work = _reconciliation_cost_frame(detail)
    if work.empty:
        return pd.DataFrame(), {}

    complete_cost = (
        work["modelled_total_cost_ex_gst"].notna()
        & work["modelled_total_cost_ex_gst"].gt(0)
        & work["actual_total_cost_ex_gst_std"].notna()
    )
    work = work.loc[complete_cost].copy()
    if work.empty:
        return pd.DataFrame(), {
            "matched_records": 0.0,
            "matched_invoices": 0.0,
            "modelled_cost": None,
            "actual_cost": None,
            "variance": None,
            "variance_pct": None,
        }

    def total(column: str) -> float | None:
        values = pd.to_numeric(work[column], errors="coerce")
        return float(values.sum()) if values.notna().any() else None

    invoice_ids = _coalesced_lookup(work, ("stage3_invoice_number", "invoice_number"))
    totals = {
        "matched_records": float(len(work)),
        "matched_invoices": float(invoice_ids[invoice_ids.ne("")].nunique()),
        "modelled_cost": total("modelled_total_cost_ex_gst"),
        "actual_cost": total("actual_total_cost_ex_gst_std"),
    }
    if totals["modelled_cost"] is not None and totals["actual_cost"] is not None:
        totals["variance"] = totals["actual_cost"] - totals["modelled_cost"]
        totals["variance_pct"] = totals["variance"] / totals["modelled_cost"] * 100 if totals["modelled_cost"] else None
    else:
        totals["variance"] = None
        totals["variance_pct"] = None

    grouped = work.groupby("primary_root_cause", dropna=False)
    bridge = grouped.size().rename("matched_records").to_frame()
    for source_column in (
        "base_variance_ex_gst",
        "fuel_variance_ex_gst",
        "other_variance_ex_gst",
        "total_variance_ex_gst",
        "actual_total_cost_ex_gst_std",
        "modelled_total_cost_ex_gst",
    ):
        bridge[source_column] = grouped[source_column].sum(min_count=1)
    bridge = bridge.reset_index().rename(
        columns={
            "actual_total_cost_ex_gst_std": "actual_cost_ex_gst",
            "modelled_total_cost_ex_gst": "modelled_cost_ex_gst",
        }
    )
    bridge["variance_pct"] = bridge["total_variance_ex_gst"] / bridge["modelled_cost_ex_gst"].replace(0, pd.NA) * 100
    return bridge.sort_values("total_variance_ex_gst", key=lambda values: values.abs(), ascending=False), totals


def _numeric_cube_cm3(
    frame: pd.DataFrame,
    cm3_columns: tuple[str, ...],
    m3_columns: tuple[str, ...],
) -> pd.Series:
    """Coalesce carrier cube fields into a single cubic-centimetre series."""
    result = pd.Series(float("nan"), index=frame.index, dtype="float64")
    for column in cm3_columns:
        if column in frame:
            result = result.fillna(pd.to_numeric(frame[column], errors="coerce"))
    for column in m3_columns:
        if column in frame:
            values = pd.to_numeric(frame[column], errors="coerce") * 1_000_000
            result = result.fillna(values)
    return result


def _reconciliation_physical_basis_detail(detail: pd.DataFrame) -> pd.DataFrame:
    """Add an exclusive Shippit physical-basis cohort and comparison measures."""
    if detail.empty:
        return pd.DataFrame()

    work = _reconciliation_cost_frame(detail)
    actual_weight = _numeric_from_columns(work, ("actual_chargeable_weight_kg",))
    shippit_weight = _numeric_from_columns(work, ("shippit_chargeable_weight_kg",))
    actual_cube = _numeric_cube_cm3(
        work,
        (
            "actual_billed_cube_cm3",
            "actual_chargeable_cube_cm3",
            "actual_declared_cube_cm3",
        ),
        (
            "actual_billed_cube_m3",
            "actual_total_volume_m3",
            "actual_cubic_volume_m3",
            "actual_declared_cube_m3",
        ),
    )
    shippit_cube = _numeric_from_columns(
        work,
        ("shippit_chargeable_cube_cm3", "shippit_volumetric_cube_cm3"),
    )
    estimator_cube = _numeric_cube_cm3(
        work,
        ("estimator_parcel_cube_cm3", "est_parcel_cube_total_cm3"),
        ("estimator_parcel_cube_m3",),
    )

    weight_available = actual_weight.gt(0) & shippit_weight.gt(0)
    weight_gap = actual_weight - shippit_weight
    weight_mismatch = weight_available & weight_gap.abs().gt(0.5)
    cube_available = actual_cube.gt(0) & shippit_cube.gt(0)
    invoiced_less_shippit_cube_pct = (
        (actual_cube - shippit_cube).div(actual_cube.replace(0, pd.NA)).mul(100)
    )
    cube_mismatch = cube_available & invoiced_less_shippit_cube_pct.abs().gt(25)
    cohort = pd.Series("NO_SHIPPIT_PHYSICAL_MISMATCH", index=work.index, dtype="string")
    cohort.loc[weight_mismatch & cube_mismatch] = "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH"
    cohort.loc[weight_mismatch & ~cube_mismatch] = "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY"
    cohort.loc[~weight_mismatch & cube_mismatch] = "SHIPPIT_CUBE_MISMATCH_ONLY"
    unavailable = ~(weight_available | cube_available)
    cohort.loc[unavailable] = "PHYSICAL_EVIDENCE_UNAVAILABLE"

    work["shippit_physical_basis_cohort"] = cohort
    work["invoiced_actual_less_shippit_chargeable_weight_kg"] = weight_gap.where(weight_available)
    work["invoiced_actual_less_shippit_cube_cm3"] = (actual_cube - shippit_cube).where(cube_available)
    work["invoiced_actual_less_shippit_cube_pct"] = invoiced_less_shippit_cube_pct.where(cube_available)
    work["estimator_less_invoiced_actual_cube_pct"] = (
        (estimator_cube - actual_cube).div(actual_cube.replace(0, pd.NA)).mul(100)
    ).where(actual_cube.gt(0) & estimator_cube.gt(0))
    work["weight_gap_actual_above_shippit"] = (weight_gap > 0).where(weight_available)
    work["estimator_cube_within_15pct_of_invoiced_actual"] = (
        work["estimator_less_invoiced_actual_cube_pct"].abs().le(15)
    ).where(actual_cube.gt(0) & estimator_cube.gt(0))
    return work


def _reconciliation_physical_basis_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Summarise observed variance by exclusive physical-basis cohort."""
    work = _reconciliation_physical_basis_detail(detail)
    if work.empty:
        return pd.DataFrame()
    complete_cost = (
        work["modelled_total_cost_ex_gst"].notna()
        & work["modelled_total_cost_ex_gst"].gt(0)
        & work["actual_total_cost_ex_gst_std"].notna()
    )
    work = work.loc[complete_cost].copy()
    if work.empty:
        return pd.DataFrame()

    rows = []
    for cohort, group in work.groupby("shippit_physical_basis_cohort", dropna=False, sort=False):
        weight_gap = pd.to_numeric(
            group["invoiced_actual_less_shippit_chargeable_weight_kg"], errors="coerce"
        )
        cube_gap = pd.to_numeric(group["invoiced_actual_less_shippit_cube_pct"], errors="coerce")
        estimator_gap = pd.to_numeric(
            group["estimator_less_invoiced_actual_cube_pct"], errors="coerce"
        )
        weight_direction = group["weight_gap_actual_above_shippit"].dropna()
        estimator_alignment = group["estimator_cube_within_15pct_of_invoiced_actual"].dropna()
        modelled = pd.to_numeric(group["modelled_total_cost_ex_gst"], errors="coerce")
        invoiced = pd.to_numeric(group["actual_total_cost_ex_gst_std"], errors="coerce")
        variance = pd.to_numeric(group["total_variance_ex_gst"], errors="coerce")
        modelled_total = float(modelled.sum()) if modelled.notna().any() else None
        invoiced_total = float(invoiced.sum()) if invoiced.notna().any() else None
        variance_total = float(variance.sum()) if variance.notna().any() else None
        rows.append(
            {
                "Shippit physical-basis cohort": cohort,
                "Matched records": len(group),
                "Modelled cost ex GST": modelled_total,
                "Invoiced actual cost ex GST": invoiced_total,
                "Observed variance ex GST": variance_total,
                "Observed variance %": (
                    variance_total / modelled_total * 100
                    if variance_total is not None and modelled_total not in (None, 0)
                    else None
                ),
                "Median invoiced less Shippit weight kg": (
                    float(weight_gap.median()) if weight_gap.notna().any() else None
                ),
                "% with invoiced weight above Shippit": (
                    float(weight_direction.mean() * 100) if not weight_direction.empty else None
                ),
                "Median invoiced less Shippit cube %": (
                    float(cube_gap.median()) if cube_gap.notna().any() else None
                ),
                "Median estimator less invoiced cube %": (
                    float(estimator_gap.median()) if estimator_gap.notna().any() else None
                ),
                "% estimator cube within 15% of invoiced": (
                    float(estimator_alignment.mean() * 100)
                    if not estimator_alignment.empty
                    else None
                ),
            }
        )
    order = {
        "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH": 0,
        "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY": 1,
        "SHIPPIT_CUBE_MISMATCH_ONLY": 2,
        "NO_SHIPPIT_PHYSICAL_MISMATCH": 3,
        "PHYSICAL_EVIDENCE_UNAVAILABLE": 4,
    }
    return (
        pd.DataFrame(rows)
        .assign(_order=lambda frame: frame["Shippit physical-basis cohort"].map(order).fillna(99))
        .sort_values("_order")
        .drop(columns="_order")
        .reset_index(drop=True)
    )


def _reconciliation_variance_readout(
    detail: pd.DataFrame,
    residual_bridge: pd.DataFrame,
    residual_totals: dict[str, float | None],
) -> list[str]:
    """Create a short, source-aware narrative for the reconciliation reviewer."""
    if detail.empty:
        return []

    complete_cost = (
        detail["modelled_total_cost_ex_gst"].notna()
        & detail["modelled_total_cost_ex_gst"].gt(0)
        & detail["actual_total_cost_ex_gst_std"].notna()
    )
    detail = detail.loc[complete_cost].copy()
    if detail.empty:
        return []

    cohort = detail["shippit_physical_basis_cohort"].astype(str)
    physical_cohorts = {
        "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH",
        "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY",
        "SHIPPIT_CUBE_MISMATCH_ONLY",
    }
    physical_mask = cohort.isin(physical_cohorts)
    weight_mask = cohort.isin(
        {"SHIPPIT_CUBE_AND_WEIGHT_MISMATCH", "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY"}
    )
    cube_mask = cohort.isin(
        {"SHIPPIT_CUBE_AND_WEIGHT_MISMATCH", "SHIPPIT_CUBE_MISMATCH_ONLY"}
    )
    total_variance = pd.to_numeric(detail["total_variance_ex_gst"], errors="coerce")
    modelled = pd.to_numeric(detail["modelled_total_cost_ex_gst"], errors="coerce")
    physical_variance = total_variance.loc[physical_mask]
    modelled_total = float(modelled.sum()) if modelled.notna().any() else None
    physical_total = float(physical_variance.sum()) if physical_variance.notna().any() else 0.0
    physical_count = int(physical_mask.sum())
    total_count = len(detail)

    readout = [
        f"Shippit dim/weight basis: {physical_count:,} of {total_count:,} matched records "
        f"({physical_count / total_count * 100:.1f}%) have a material cube and/or chargeable-weight mismatch, "
        f"representing ${physical_total:+,.0f} of observed variance."
    ]
    if weight_mask.any() or cube_mask.any():
        readout.append(
            f"The dim/weight evidence splits into {int(weight_mask.sum()):,} records with a Shippit weight mismatch "
            f"and {int(cube_mask.sum()):,} with a Shippit cube mismatch; records can appear in both groups."
        )

    estimator_alignment = pd.to_numeric(
        detail["estimator_cube_within_15pct_of_invoiced_actual"], errors="coerce"
    ).dropna()
    shippit_cube_gap = pd.to_numeric(
        detail["invoiced_actual_less_shippit_cube_pct"], errors="coerce"
    ).dropna()
    if not estimator_alignment.empty:
        estimator_pct = float(estimator_alignment.mean() * 100)
        shippit_pct = (
            float(shippit_cube_gap.abs().le(15).mean() * 100)
            if not shippit_cube_gap.empty
            else None
        )
        shippit_text = ""
        if shippit_pct is not None:
            shippit_text = f" Shippit is within 15% for {shippit_pct:.1f}% of those records."
        readout.append(
            f"Cube comparison: the independent estimator is within 15% of the invoiced actual cube for "
            f"{estimator_pct:.1f}% of {len(estimator_alignment):,} records with both measures.{shippit_text}"
        )

    actual_fuel = pd.to_numeric(detail["actual_fuel_cost_ex_gst_std"], errors="coerce")
    modelled_fuel = pd.to_numeric(detail["modelled_fuel_cost_ex_gst"], errors="coerce")
    fuel_valid = actual_fuel.notna() & modelled_fuel.notna()
    if fuel_valid.any():
        actual_fuel_total = float(actual_fuel.loc[fuel_valid].sum())
        modelled_fuel_total = float(modelled_fuel.loc[fuel_valid].sum())
        fuel_variance = actual_fuel_total - modelled_fuel_total
        actual_base = pd.to_numeric(
            detail.loc[fuel_valid, "actual_base_cost_ex_gst_std"], errors="coerce"
        ).sum(min_count=1)
        modelled_base = pd.to_numeric(
            detail.loc[fuel_valid, "modelled_base_cost_ex_gst"], errors="coerce"
        ).sum(min_count=1)
        actual_rate = (
            float(actual_fuel_total / actual_base * 100)
            if pd.notna(actual_base) and actual_base
            else None
        )
        modelled_rate = (
            float(modelled_fuel_total / modelled_base * 100)
            if pd.notna(modelled_base) and modelled_base
            else None
        )
        rate_text = ""
        if actual_rate is not None and modelled_rate is not None:
            rate_text = f" ({actual_rate:.1f}% of invoiced base versus {modelled_rate:.1f}% modelled)"
        if abs(fuel_variance) > 0.05 or abs(actual_fuel_total) > 0.05:
            readout.append(
                f"Fuel surcharge: invoiced actual fuel is ${actual_fuel_total:,.0f} versus "
                f"${modelled_fuel_total:,.0f} modelled, a ${fuel_variance:+,.0f} variance{rate_text}. "
                "Fuel is shown separately because the current fuel input is an assumption until carrier rules are confirmed."
            )
    readout.append(
        "Weight comparison currently measures invoiced actual chargeable weight against Shippit chargeable weight. "
        "An independent estimator chargeable-weight field is not yet carried into the reconciliation detail, so estimator-versus-carrier weight performance is not quantified here."
    )

    residual_variance = residual_totals.get("variance")
    residual_pct = residual_totals.get("variance_pct")
    if residual_variance is not None:
        readout.append(
            f"Comparable-basis residual: after excluding material Shippit physical mismatches, the remaining observed variance is "
            f"${residual_variance:+,.0f} ({residual_pct:+.1f}% of modelled cost) across "
            f"{int(residual_totals.get('matched_records') or 0):,} records."
        )
        if not residual_bridge.empty:
            top = residual_bridge.iloc[0]
            readout.append(
                f"The largest remaining primary driver is {top['primary_root_cause']} "
                f"at ${float(top['total_variance_ex_gst']):+,.0f}."
            )
            residual_fuel = pd.to_numeric(
                residual_bridge["fuel_variance_ex_gst"], errors="coerce"
            ).sum(min_count=1)
            if pd.notna(residual_fuel) and abs(float(residual_fuel)) > 0.05:
                readout.append(
                    f"Fuel surcharge remains a separate residual cost element at ${float(residual_fuel):+,.0f}; "
                    "it should continue to be treated as an explicit assumption until carrier rules are confirmed."
                )
    return readout


def _reconciliation_bucket_export(bucket_detail: pd.DataFrame) -> pd.DataFrame:
    """Prepare a review-friendly bucket extract with explicit data-source names and units."""
    columns = [
        "invoice_number",
        "stage3_tracking_number",
        "site",
        "primary_root_cause",
        "root_cause",
        "match_method",
        "actual_service_name",
        "actual_service_code",
        "actual_chargeable_weight_kg",
        "actual_rounded_chargeable_weight_kg",
        "shippit_chargeable_weight_kg",
        "modelled_cp_chargeable_weight_kg",
        "modelled_ap_chargeable_weight_kg",
        "modelled_goexpress_chargeable_weight_kg",
        "modelled_allied_cost_weight_basis_kg",
        "actual_cp_origin_zone",
        "actual_cp_destination_zone",
        "actual_destination_zone",
        "modelled_ap_zone",
        "modelled_ap_pricing_zone",
        "modelled_cp_origin_zone",
        "modelled_cp_destination_zone",
        "modelled_goexpress_zone",
        "modelled_allied_zone",
        "estimator_parcel_cube_cm3",
        "shippit_chargeable_cube_cm3",
        "actual_total_volume_m3",
        "shippit_physical_basis_cohort",
        "invoiced_actual_less_shippit_chargeable_weight_kg",
        "invoiced_actual_less_shippit_cube_pct",
        "estimator_less_invoiced_actual_cube_pct",
        "actual_invoice_cost_ex_gst",
        "modelled_total_cost_ex_gst",
        "actual_less_modelled_cost_including_fuel_ex_gst",
        "estimator_dimension_diagnostic",
        "shippit_dimension_diagnostic",
        "reconciliation_note",
    ]
    export = bucket_detail[[column for column in columns if column in bucket_detail]].copy()
    export = export.rename(
        columns={
            "invoice_number": "shippit_invoice_number",
            "stage3_tracking_number": "shippit_tracking_number",
            "actual_service_name": "invoiced_actual_service_name",
            "actual_service_code": "invoiced_actual_service_code",
            "actual_chargeable_weight_kg": "invoiced_actual_chargeable_weight_kg",
            "actual_rounded_chargeable_weight_kg": "invoiced_actual_rounded_chargeable_weight_kg",
            "actual_cp_destination_zone": "invoiced_actual_cp_destination_zone",
            "actual_destination_zone": "invoiced_actual_destination_zone",
            "actual_cp_origin_zone": "invoiced_actual_cp_origin_zone",
            "modelled_ap_zone": "modelled_ap_destination_zone",
            "modelled_ap_pricing_zone": "modelled_ap_pricing_zone",
            "modelled_cp_origin_zone": "modelled_cp_origin_zone",
            "modelled_cp_destination_zone": "modelled_cp_destination_zone",
            "shippit_physical_basis_cohort": "shippit_physical_basis_cohort",
            "invoiced_actual_less_shippit_chargeable_weight_kg": "invoiced_actual_less_shippit_chargeable_weight_kg",
            "invoiced_actual_less_shippit_cube_pct": "invoiced_actual_less_shippit_cube_pct",
            "estimator_less_invoiced_actual_cube_pct": "estimator_less_invoiced_actual_cube_pct",
            "estimator_parcel_cube_cm3": "estimator_parcel_cube_m3",
            "shippit_chargeable_cube_cm3": "shippit_chargeable_cube_m3",
            "actual_total_volume_m3": "invoiced_actual_total_volume_m3",
            "actual_invoice_cost_ex_gst": "invoiced_actual_cost_ex_gst",
            "actual_less_modelled_cost_including_fuel_ex_gst": "invoiced_actual_less_modelled_cost_including_fuel_ex_gst",
        }
    )
    cube_columns = [
        "estimator_parcel_cube_m3",
        "shippit_chargeable_cube_m3",
        "invoiced_actual_total_volume_m3",
    ]
    for column in cube_columns:
        if column in export:
            values = pd.to_numeric(export[column], errors="coerce")
            if column != "invoiced_actual_total_volume_m3" and column.endswith("_cube_m3"):
                values = values / 1_000_000
            export[column] = values.map(lambda value: f"{value:.4f}" if pd.notna(value) else "")

    cost_columns = [column for column in export if "cost" in column and "ex_gst" in column]
    for column in cost_columns:
        values = pd.to_numeric(export[column], errors="coerce")
        export[column] = values.map(lambda value: f"{value:.2f}" if pd.notna(value) else "")
    return export


@st.cache_data(show_spinner=False, max_entries=2)
def _finance_report_data_cached(
    path_text: str,
    modified_ns: int,
    file_size: int,
    calendar_path_text: str,
    calendar_modified_ns: int,
    calendar_file_size: int,
) -> pd.DataFrame:
    """Aggregate Finance measures from the durable coverage file in chunks."""
    del modified_ns, file_size, calendar_modified_ns, calendar_file_size
    path = Path(path_text)
    if not path.exists():
        return pd.DataFrame()
    try:
        calendar_path = Path(calendar_path_text)
        period_by_week: dict[str, str] = {}
        if calendar_path.exists():
            calendar = pd.read_csv(
                calendar_path,
                dtype={
                    "accounting_year": "string",
                    "accounting_week_number_in_year": "string",
                    "accounting_period_number": "string",
                },
            )
            if set(
                (
                    "accounting_year",
                    "accounting_week_number_in_year",
                    "accounting_period_number",
                )
            ).issubset(calendar.columns):
                calendar_year = calendar["accounting_year"].fillna("").astype(str).str.replace(
                    r"\.0$", "", regex=True
                )
                calendar_week = pd.to_numeric(
                    calendar["accounting_week_number_in_year"], errors="coerce"
                ).map(lambda value: str(int(value)) if pd.notna(value) else "")
                calendar_period = calendar["accounting_period_number"].fillna("").astype(str).str.strip()
                for year, week, period in zip(calendar_year, calendar_week, calendar_period):
                    if year and week and period:
                        period_by_week[f"{year}|{week}"] = period

        available = set(pd.read_csv(path, nrows=0).columns)
        wanted = (
            "CFC",
            "Shipped carrier",
            "Accounting year",
            "Accounting week",
            "Accounting period number",
            "AP pricing zone",
            "Invoiced actual rounded chargeable weight kg",
            "Invoiced actual chargeable weight kg",
            "Invoiced actual billed weight kg",
            "Shippit parcel count",
            "Shippit units",
            "Modelled cost ex GST",
            "Invoiced actual cost ex GST",
        )
        usecols = [column for column in wanted if column in available]
        if not {"CFC", "Shipped carrier"}.issubset(usecols):
            return pd.DataFrame()
        string_columns = [
            column
            for column in (
                "CFC",
                "Shipped carrier",
                "Accounting year",
                "Accounting week",
                "Accounting period number",
                "AP pricing zone",
            )
            if column in usecols
        ]
        partials: list[pd.DataFrame] = []
        for chunk in pd.read_csv(
            path,
            low_memory=False,
            encoding="utf-8-sig",
            usecols=usecols,
            dtype={column: "string" for column in string_columns},
            chunksize=100_000,
        ):
            fiscal_year = chunk.get(
                "Accounting year", pd.Series("", index=chunk.index, dtype="string")
            ).fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
            period_number = chunk.get(
                "Accounting period number", pd.Series("", index=chunk.index, dtype="string")
            ).fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
            week_number = pd.to_numeric(
                chunk.get("Accounting week", pd.Series(float("nan"), index=chunk.index)),
                errors="coerce",
            )
            week_suffix = week_number.map(
                lambda value: str(int(value)) if pd.notna(value) else ""
            )
            calendar_period = (fiscal_year + "|" + week_suffix).map(period_by_week)
            period_number = (
                period_number.replace("", pd.NA).fillna(calendar_period).fillna("")
            )
            period_suffix = period_number.str[-2:].where(period_number.str.len().ge(2), "")
            accounting_week = week_number.map(
                lambda value: f"W{int(value):02d}" if pd.notna(value) else "Week unavailable"
            )
            finance_period = "FY" + fiscal_year + " P" + period_suffix
            missing_period = period_suffix.eq("")
            finance_period.loc[missing_period] = (
                "FY" + fiscal_year[missing_period] + " W" + week_number[missing_period].fillna(0).astype(int).astype(str).str.zfill(2)
            )
            finance_period = finance_period.where(
                fiscal_year.ne(""), "Finance period unavailable"
            )

            actual_cost = pd.to_numeric(
                chunk.get("Invoiced actual cost ex GST", pd.Series(float("nan"), index=chunk.index)),
                errors="coerce",
            )
            modelled_cost = pd.to_numeric(
                chunk.get("Modelled cost ex GST", pd.Series(float("nan"), index=chunk.index)),
                errors="coerce",
            )
            finance_cost = actual_cost.combine_first(modelled_cost)
            invoice_weight = pd.Series(float("nan"), index=chunk.index)
            for weight_column in (
                "Invoiced actual rounded chargeable weight kg",
                "Invoiced actual chargeable weight kg",
                "Invoiced actual billed weight kg",
            ):
                if weight_column in chunk:
                    invoice_weight = invoice_weight.combine_first(
                        pd.to_numeric(chunk[weight_column], errors="coerce")
                    )
            weight_band_edges = (
                (0.0, 1.0, "0-1 kg"),
                (1.0, 2.0, "1-2 kg"),
                (2.0, 5.0, "2-5 kg"),
                (5.0, 10.0, "5-10 kg"),
                (10.0, 15.0, "10-15 kg"),
                (15.0, 22.0, "15-22 kg"),
                (22.0, 32.0, "22-32 kg"),
                (32.0, 45.0, "32-45 kg"),
                (45.0, float("inf"), "45+ kg"),
            )
            invoiced_weight_band = pd.Series(
                "Invoiced weight unavailable", index=chunk.index, dtype="string"
            )
            for lower, upper, label in weight_band_edges:
                band_mask = invoice_weight.ge(lower) & invoice_weight.lt(upper)
                invoiced_weight_band.loc[band_mask] = label
            parcel_count = pd.to_numeric(
                chunk.get("Shippit parcel count", pd.Series(float("nan"), index=chunk.index)),
                errors="coerce",
            )
            unit_count = pd.to_numeric(
                chunk.get("Shippit units", pd.Series(float("nan"), index=chunk.index)),
                errors="coerce",
            )
            cost_available = finance_cost.notna()
            work = pd.DataFrame(
                {
                    "Finance period": finance_period,
                    "Accounting week": accounting_week,
                    "CFC": chunk["CFC"].fillna("Unknown CFC").astype(str).str.upper(),
                    "Carrier": chunk["Shipped carrier"].fillna("Unknown carrier").astype(str),
                    "Destination zone": chunk.get(
                        "AP pricing zone", pd.Series("", index=chunk.index, dtype="string")
                    ).fillna("").astype(str).str.strip().replace("", "Zone unavailable"),
                    "Invoiced weight band": invoiced_weight_band,
                    "Finance cost ex GST": finance_cost,
                    "Cost source": "No cost available",
                    "Consignments": 1,
                    "Costed consignments": cost_available.astype(int),
                    "Parcel count": parcel_count,
                    "Costed parcel count": parcel_count.where(cost_available),
                    "Unit count": unit_count,
                    "Costed unit count": unit_count.where(cost_available),
                    "Invoiced actual consignments": actual_cost.notna().astype(int),
                }
            )
            work.loc[modelled_cost.notna(), "Cost source"] = "Model estimate"
            work.loc[actual_cost.notna(), "Cost source"] = "Invoiced actual"
            group_columns = [
                "Finance period",
                "Accounting week",
                "CFC",
                "Carrier",
                "Destination zone",
                "Invoiced weight band",
                "Cost source",
            ]
            partials.append(
                work.groupby(group_columns, dropna=False).agg(
                    **{
                        "Finance cost ex GST": ("Finance cost ex GST", lambda values: values.sum(min_count=1)),
                        "Consignments": ("Consignments", "sum"),
                        "Costed consignments": ("Costed consignments", "sum"),
                        "Parcel count": ("Parcel count", lambda values: values.sum(min_count=1)),
                        "Costed parcel count": ("Costed parcel count", lambda values: values.sum(min_count=1)),
                        "Unit count": ("Unit count", lambda values: values.sum(min_count=1)),
                        "Costed unit count": ("Costed unit count", lambda values: values.sum(min_count=1)),
                        "Invoiced actual consignments": ("Invoiced actual consignments", "sum"),
                    }
                ).reset_index()
            )
        if not partials:
            return pd.DataFrame()
        combined = pd.concat(partials, ignore_index=True, sort=False)
        group_columns = [
            "Finance period",
            "Accounting week",
            "CFC",
            "Carrier",
            "Destination zone",
            "Invoiced weight band",
            "Cost source",
        ]
        return combined.groupby(group_columns, dropna=False).agg(
            **{
                "Finance cost ex GST": ("Finance cost ex GST", lambda values: values.sum(min_count=1)),
                "Consignments": ("Consignments", "sum"),
                "Costed consignments": ("Costed consignments", "sum"),
                "Parcel count": ("Parcel count", lambda values: values.sum(min_count=1)),
                "Costed parcel count": ("Costed parcel count", lambda values: values.sum(min_count=1)),
                "Unit count": ("Unit count", lambda values: values.sum(min_count=1)),
                "Costed unit count": ("Costed unit count", lambda values: values.sum(min_count=1)),
                "Invoiced actual consignments": ("Invoiced actual consignments", "sum"),
            }
        ).reset_index()
    except (OSError, pd.errors.ParserError, ValueError, UnicodeDecodeError):
        return pd.DataFrame()


def _finance_report_data() -> pd.DataFrame:
    path = ALL_CARRIER_HISTORY_STORE / "consignment_invoice_coverage.csv"
    if not path.exists():
        return pd.DataFrame()
    stat = path.stat()
    calendar_path = FINANCE_CALENDAR_PATH
    calendar_stat = calendar_path.stat() if calendar_path.exists() else None
    return _finance_report_data_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        str(calendar_path),
        calendar_stat.st_mtime_ns if calendar_stat else 0,
        calendar_stat.st_size if calendar_stat else 0,
    )


def _finance_report_xlsx(summary: pd.DataFrame) -> bytes:
    """Build an Excel export with numeric values stored as numeric cells."""
    buffer = BytesIO()
    numeric_formats = {
        "Cost": '$#,##0.00;[Red]-$#,##0.00',
        "Cost per consignment": '$#,##0.00;[Red]-$#,##0.00',
        "Cost per parcel": '$#,##0.00;[Red]-$#,##0.00',
        "Cost per unit": '$#,##0.00;[Red]-$#,##0.00',
        "Cost coverage %": '0.0"%"',
        "Invoice coverage %": '0.0"%"',
    }
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Finance Report")
        worksheet = writer.sheets["Finance Report"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_index, column_name in enumerate(summary.columns, start=1):
            column_letter = worksheet.cell(row=1, column=column_index).column_letter
            if column_name in numeric_formats:
                cell_format = numeric_formats[column_name]
                for row_index in range(2, worksheet.max_row + 1):
                    worksheet.cell(row=row_index, column=column_index).number_format = cell_format
            elif column_name in {
                "Consignment Count", "Costed Consignment Count", "Parcel Count",
                "Costed Parcel Count", "Unit Count", "Costed Unit Count",
                "Invoiced actual consignments",
            }:
                for row_index in range(2, worksheet.max_row + 1):
                    worksheet.cell(row=row_index, column=column_index).number_format = "#,##0.##"
            values = [
                len(str(worksheet.cell(row=row_index, column=column_index).value or ""))
                for row_index in range(1, worksheet.max_row + 1)
            ]
            width = min(max(max(values, default=0) + 2, 12), 42)
            worksheet.column_dimensions[column_letter].width = width
    return buffer.getvalue()


def _finance_report_workspace(site: str) -> None:
    st.subheader("Finance Report")
    st.caption(
        "Finance period costs use matched carrier actuals where available and the saved model estimate otherwise. "
        "Costs and counts come from durable shipment history; Finance periods use the Snowflake date-dimension calendar. "
        "Destination grouping starts with the app's AP pricing zone labels. Costs are ex GST."
    )
    with st.spinner("Aggregating Finance period totals from saved shipment history..."):
        data = _finance_report_data()
    if data.empty:
        st.warning("The durable shipment history or its Finance fields are not available yet. Run the daily refresh to initialize the store.")
        return
    if site != "ALL":
        data = data.loc[data["CFC"].eq(site)]
    periods = sorted(data["Finance period"].dropna().unique().tolist())
    selected_periods = st.multiselect("Finance periods", periods, default=periods)
    if not selected_periods:
        st.info("Select at least one Finance period.")
        return
    period_data = data.loc[data["Finance period"].isin(selected_periods)].copy()
    expected_weeks: dict[str, set[str]] = {}
    if FINANCE_CALENDAR_PATH.exists():
        calendar = pd.read_csv(FINANCE_CALENDAR_PATH, low_memory=False)
        required_calendar_columns = {
            "accounting_year",
            "accounting_week_number_in_year",
            "accounting_period_number",
        }
        if required_calendar_columns.issubset(calendar.columns):
            calendar_year = calendar["accounting_year"].fillna("").astype(str).str.replace(
                r"\.0$", "", regex=True
            )
            calendar_period = calendar["accounting_period_number"].fillna("").astype(str).str.strip()
            calendar_week = pd.to_numeric(
                calendar["accounting_week_number_in_year"], errors="coerce"
            )
            calendar_period_suffix = calendar_period.str[-2:].where(
                calendar_period.str.len().ge(2), ""
            )
            calendar_labels = "FY" + calendar_year + " P" + calendar_period_suffix
            calendar_weeks = calendar_week.map(
                lambda value: f"W{int(value):02d}" if pd.notna(value) else ""
            )
            calendar_frame = pd.DataFrame(
                {"Finance period": calendar_labels, "Accounting week": calendar_weeks}
            )
            expected_weeks = {
                period: set(weeks.dropna().astype(str))
                for period, weeks in calendar_frame.groupby("Finance period")["Accounting week"]
            }
    actual_weeks = period_data.groupby("Finance period")["Accounting week"].agg(
        lambda values: set(values.dropna().astype(str))
    )
    missing_week_details = []
    for period, weeks in actual_weeks.items():
        expected = expected_weeks.get(period)
        missing = sorted(expected - weeks) if expected else []
        if missing:
            missing_week_details.append(f"{period}: {', '.join(missing)}")
    if missing_week_details:
        details = "; ".join(missing_week_details)
        st.warning(
            "The selected Finance period(s) have no stored shipment rows for these calendar weeks: "
            f"{details}. Confirm these weeks had no shipments or refresh the source before treating the totals as complete."
        )
    filter_columns = st.columns(3)
    carriers = sorted(period_data["Carrier"].dropna().astype(str).unique().tolist())
    zones = sorted(period_data["Destination zone"].dropna().astype(str).unique().tolist())
    weight_bands = [
        "0-1 kg", "1-2 kg", "2-5 kg", "5-10 kg", "10-15 kg",
        "15-22 kg", "22-32 kg", "32-45 kg", "45+ kg", "Invoiced weight unavailable",
    ]
    weight_bands = [band for band in weight_bands if band in set(period_data["Invoiced weight band"])]
    with filter_columns[0]:
        selected_carriers = st.multiselect("Carrier", carriers, default=carriers)
    with filter_columns[1]:
        selected_zones = st.multiselect("Destination zone", zones, default=zones)
    with filter_columns[2]:
        selected_weight_bands = st.multiselect(
            "Invoiced weight band", weight_bands, default=weight_bands
        )
    data = period_data.loc[
        period_data["Carrier"].isin(selected_carriers)
        & period_data["Destination zone"].isin(selected_zones)
        & period_data["Invoiced weight band"].isin(selected_weight_bands)
    ].copy()
    if data.empty:
        st.info("No rows match the selected Finance periods and filters.")
        return
    summary = data.groupby(
        ["Finance period", "CFC", "Carrier", "Destination zone", "Invoiced weight band"],
        dropna=False,
    ).agg(
        Cost=("Finance cost ex GST", lambda values: values.sum(min_count=1)),
        **{
            "Weeks included": ("Accounting week", lambda values: ", ".join(sorted(set(values.dropna().astype(str))))),
            "Consignment Count": ("Consignments", "sum"),
            "Costed Consignment Count": ("Costed consignments", "sum"),
            "Parcel Count": ("Parcel count", lambda values: values.sum(min_count=1)),
            "Costed Parcel Count": ("Costed parcel count", lambda values: values.sum(min_count=1)),
            "Unit Count": ("Unit count", lambda values: values.sum(min_count=1)),
            "Costed Unit Count": ("Costed unit count", lambda values: values.sum(min_count=1)),
        },
        **{"Invoiced actual consignments": ("Invoiced actual consignments", "sum")},
    ).reset_index()
    summary = summary.rename(columns={"Finance period": "Finance Period", "CFC": "Origin CFC"})
    summary["Cost coverage %"] = (
        summary["Costed Consignment Count"]
        .div(summary["Consignment Count"].replace(0, pd.NA))
        .mul(100)
    )
    summary["Invoice coverage %"] = (
        summary["Invoiced actual consignments"]
        .div(summary["Consignment Count"].replace(0, pd.NA))
        .mul(100)
    )
    summary["Cost per consignment"] = summary["Cost"] / summary["Costed Consignment Count"].replace(0, pd.NA)
    summary["Cost per parcel"] = summary["Cost"] / summary["Costed Parcel Count"].replace(0, pd.NA)
    summary["Cost per unit"] = summary["Cost"] / summary["Costed Unit Count"].replace(0, pd.NA)
    numeric_columns = [
        "Cost", "Consignment Count", "Costed Consignment Count", "Parcel Count",
        "Costed Parcel Count", "Unit Count", "Costed Unit Count",
        "Invoiced actual consignments", "Cost coverage %", "Invoice coverage %",
        "Cost per consignment", "Cost per parcel", "Cost per unit",
    ]
    summary[numeric_columns] = summary[numeric_columns].apply(pd.to_numeric, errors="coerce")
    st.caption(
        "Rows are grouped by Finance period, origin CFC, shipped carrier, AP pricing zone, and invoiced weight band. "
        "Weight band uses invoiced rounded chargeable weight where available, then invoiced chargeable weight, then invoiced billed weight; "
        "missing invoice weight is shown as Invoiced weight unavailable. "
        "Cost-per rates use only consignments with an actual or modelled cost; the costed counts show each rate's denominator."
    )
    st.dataframe(
        summary.style.format({
            "Cost": "${:,.2f}", "Cost per consignment": "${:,.2f}",
            "Cost per parcel": "${:,.2f}", "Cost per unit": "${:,.2f}",
            "Consignment Count": "{:,.0f}", "Costed Consignment Count": "{:,.0f}",
            "Parcel Count": "{:,.0f}", "Unit Count": "{:,.0f}",
            "Invoiced actual consignments": "{:,.0f}",
            "Cost coverage %": "{:.1f}%",
            "Invoice coverage %": "{:.1f}%",
        }, na_rep="-"),
        width="stretch", hide_index=True,
    )
    st.caption("Use the Excel download to retain numeric cell types. CSV is a text format and does not retain cell types.")
    export_columns = st.columns(2)
    with export_columns[0]:
        st.download_button(
            "Download Finance Report Excel", _finance_report_xlsx(summary),
            file_name="finance_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    with export_columns[1]:
        st.download_button(
            "Download Finance Report CSV", summary.to_csv(index=False).encode("utf-8"),
            file_name="finance_report.csv", mime="text/csv", width="stretch",
        )
    with st.expander("Cost basis and coverage detail"):
        basis = data.groupby(
            ["Finance period", "CFC", "Carrier", "Destination zone", "Invoiced weight band", "Cost source"],
            dropna=False,
        ).agg(
            Consignments=("Consignments", "sum"),
            Cost=("Finance cost ex GST", lambda values: values.sum(min_count=1)),
        ).reset_index()
        st.dataframe(basis, width="stretch", hide_index=True)
    st.caption(
        "Finance period numbers come from saved coverage, filled from the Snowflake accounting calendar where legacy rows lack a period. "
        "Any week still missing a calendar match remains week-labelled. Zones follow the app's AP pricing zone labels."
    )

def _refresh_guide_workspace() -> None:
    st.subheader("Refresh Guide")
    st.caption(
        "Use the source update that matches the data you need. Daily shipment refreshes, carrier invoice refreshes, "
        "and the weekly history rollup are separate jobs."
    )

    def status_row(label: str, path: Path) -> dict[str, str]:
        status = _load_status_file(path)
        state = str(status.get("status") or "Not recorded").replace("_", " ").title()
        finished = status.get("finished_at_utc") or status.get("last_successful_refresh_utc")
        finished_text = "—"
        if finished:
            parsed = pd.to_datetime(finished, utc=True, errors="coerce")
            finished_text = (
                parsed.tz_convert("Australia/Sydney").strftime("%d %b %Y %H:%M Sydney")
                if pd.notna(parsed)
                else str(finished)
            )
        details = str(status.get("error") or status.get("current_step") or "")
        return {
            "Process": label,
            "Last status": state,
            "Last completed": finished_text,
            "Status detail": details,
        }

    st.dataframe(
        pd.DataFrame(
            [
                status_row("Daily shipment and estimator refresh", REFRESH_STATUS_PATH),
                status_row("Carrier invoice refresh", INVOICE_REFRESH_STATUS_PATH),
                status_row("All-carrier history rollup", HISTORY_REFRESH_STATUS_PATH),
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("### Refresh cadence and trigger")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Process": "Daily shipment and estimator refresh",
                    "Typical frequency": "Daily",
                    "Run when": "New despatches or corrected Shippit shipment facts should appear in the operational dashboard.",
                    "What updates": "Stage 1–3 outputs and the live rolling shipment and invoice-coverage store.",
                },
                {
                    "Process": "Carrier invoice refresh",
                    "Typical frequency": "After each source update; AP is usually monthly and carrier workbooks weekly or more often.",
                    "Run when": "The availability check reports new AP data or a new/changed carrier workbook.",
                    "What updates": "The selected carrier's actual invoice costs, matches, and live coverage summary.",
                },
                {
                    "Process": "All-carrier history rollup",
                    "Typical frequency": "Weekly, after the day's shipment and invoice updates.",
                    "Run when": "Finance or Operations needs the combined weekly and Finance-period history refreshed.",
                    "What updates": "The combined All carrier history store used by Historical trends and Finance Report.",
                },
                {
                    "Process": "Consignment explorer",
                    "Typical frequency": "On demand.",
                    "Run when": "A user searches for a consignment.",
                    "What updates": "The explorer queries the requested consignment from Snowflake; no bulk refresh is needed.",
                },
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("### Steps to run an update")
    st.markdown(
        "**1. Refresh daily shipment data**\n\n"
        "Open the Dashboard and select **Run daily refresh** in the sidebar. It uses the standard 14-day overlap, "
        "runs Stage 1–3, updates Finance calendar fields, and persists the live rolling history. Wait for the status "
        "to show **Succeeded** before starting another job."
    )
    st.code(r".\.venv\Scripts\python.exe -m src.daily_refresh", language="powershell")

    st.markdown(
        "**2. Refresh carrier invoices when new source data is available**\n\n"
        "Open **Invoice reconciliation** and select **Check for new invoice data**. For Australia Post, the check "
        "queries Snowflake. For Allied, Couriers Please, and GoPeople, it compares workbook timestamps with the last "
        "processed control summary. If the selected carrier shows **New data: Yes**, select that carrier and press "
        "**Refresh … reconciliation**. Wait for the invoice refresh status to show **Succeeded**, then review the "
        "coverage period and matched counts in the same workspace. The invoice refresh does not rerun Stage 1–3."
    )

    st.markdown(
        "**3. Rebuild combined history after weekly updates**\n\n"
        "After the daily and any invoice refreshes have succeeded, run this from the project directory when the "
        "Historical trends or Finance Report workspaces need the latest combined history."
    )
    st.code(r".\.venv\Scripts\python.exe -m src.history_refresh", language="powershell")

    with st.expander("If the data looks stale, a week is incomplete, or a refresh fails"):
        st.markdown(
            "- **Refresh cached data** in the sidebar reloads changed local files in the app. It does not query "
            "Snowflake or process carrier workbooks. Use it after an update has succeeded if the displayed page is stale.\n"
            "- A **No new invoice information** result is a source-change signal, not proof that every historical week "
            "is complete. If one week has much lower coverage than adjacent weeks, check the carrier service dates "
            "around both week boundaries. For example, Shippit FY2027 W11 records matched AP invoice rows dated "
            "14–16 September, just after the Shippit week ended.\n"
            "- A historical AP gap needs a controlled date-range backfill and merge, followed by reconciliation and "
            "a scoped coverage rematch. The ordinary latest-14-day refresh is for recent arrivals; ask the app "
            "maintainer to run a historical backfill rather than using it to replace the saved AP history.\n"
            "- If a refresh is already running, wait for its status and log before starting another. Daily, invoice, "
            "and history refreshes share one lock. If a run fails, check its displayed log path and resolve the error "
            "before retrying.\n"
            "- Use a full historical rematch only for backdated invoice corrections or matching-logic changes; it is "
            "slower than a normal carrier refresh."
        )


def _reconciliation_workspace(
    site: str,
    stage3: pd.DataFrame,
    parcel_scope: str,
    reporting_period_label: str,
    shipped_carrier: str,
    validation_dataset: str,
    reconciliation_files: dict[str, Path | tuple[Path, ...]],
) -> None:
    st.subheader("Invoice reconciliation")
    _render_invoice_refresh_controls()
    persisted_coverage, persisted_weekly = _load_persisted_reconciliation(validation_dataset)
    has_persisted_coverage = not persisted_coverage.empty
    coverage_period_display = reporting_period_label
    coverage_period_filter = reporting_period_label
    if has_persisted_coverage:
        coverage_period_choice = st.selectbox(
            "Invoice coverage period",
            ["FY2027 W01 onward", "Selected reporting period", "All available weeks"],
            index=0,
            key="invoice_reconciliation_coverage_period",
            help=(
                "The default includes every saved coverage week from FY2027 W01 onward. "
                "Consignment, matched, and coverage metrics use this same week range."
            ),
        )
        if coverage_period_choice == "FY2027 W01 onward":
            coverage_period_display = "FY2027 W01 onward"
            coverage_period_filter = coverage_period_choice
        elif coverage_period_choice == "All available weeks":
            coverage_period_display = "All available weeks"
            coverage_period_filter = "All available refreshed data"
        else:
            coverage_period_display = reporting_period_label
            coverage_period_filter = reporting_period_label
    st.caption(
        f"Coverage period: **{coverage_period_display}** | CFC: **{'All sites' if site == 'ALL' else site.title()}** | "
        f"Carrier: **{shipped_carrier}** | Parcel scope: **{parcel_scope}** | Validation dataset: **{validation_dataset}**"
    )
    load_key = (
        f"reconciliation_loaded_{site}_{coverage_period_filter}_{shipped_carrier}_{parcel_scope}_{validation_dataset}"
        .lower()
        .replace(" ", "_")
        .replace("/", "_")
    )
    detail_reconciliation_files = VALIDATION_DATASETS[validation_dataset].get(
        "detail_reconciliation",
        reconciliation_files,
    )
    if stage3.empty and not has_persisted_coverage:
        st.info("No Stage 3 invoices match the current controls. Try All available weeks or another carrier.")
        return
    loaded_detail = bool(st.session_state.get(load_key))
    if not has_persisted_coverage and not loaded_detail:
        st.info(
            "No saved invoice coverage is available for this dataset yet. "
            "Load matching reconciliation records to calculate a temporary view, or run the persistence command first."
        )
        if st.button("Load detailed reconciliation and variance analysis", type="primary"):
            st.session_state[load_key] = True
            st.rerun()
        return

    detail = pd.DataFrame()
    if loaded_detail:
        with st.spinner("Loading matching carrier reconciliation records..."):
            detail_stage3 = stage3
            if has_persisted_coverage:
                persisted_detail_stage3 = _filter_persisted_coverage(
                    persisted_coverage,
                    site,
                    shipped_carrier,
                    parcel_scope,
                    coverage_period_filter,
                )
                detail_stage3 = _coverage_history_as_stage3(persisted_detail_stage3)
            detail = _reconciliation_detail_for_stage3(
                detail_stage3,
                site,
                detail_reconciliation_files,
            )

    if has_persisted_coverage:
        coverage = _filter_persisted_coverage(
            persisted_coverage,
            site,
            shipped_carrier,
            parcel_scope,
            coverage_period_filter,
        )
        cost_detail = _reconciliation_cost_frame(detail) if loaded_detail else pd.DataFrame()
        weekly = (
            _filter_persisted_weekly(persisted_weekly, site, shipped_carrier, coverage_period_filter)
            if parcel_scope == "All consignments"
            else _reconciliation_weekly_summary(coverage)
        )
    else:
        coverage, cost_detail = _reconciliation_coverage(stage3, detail)
        weekly = _reconciliation_weekly_summary(coverage)

    st.subheader("1. Invoice coverage")
    st.caption(
        "Coverage is measured against the selected Shippit consignments. A consignment is matched by its Shippit invoice number or carrier tracking number; "
        "accuracy uses only matched records with both modelled and invoiced costs."
    )
    coverage_cards = st.columns(3)
    matched_mask = (
        coverage["Invoice status"].ne("Not yet matched")
        if "Invoice status" in coverage
        else pd.Series(False, index=coverage.index)
    )
    coverage_cards[0].metric("Shippit consignments", f"{len(coverage):,}")
    coverage_cards[1].metric("Matched to an invoice record", f"{int(matched_mask.sum()):,}")
    coverage_cards[2].metric(
        "Invoice coverage",
        "-" if coverage.empty else f"{matched_mask.mean() * 100:.1f}%",
    )
    if weekly.empty:
        st.info("No weekly coverage rows are available for this selection.")
    else:
        matrix_metrics = {
            "Invoice coverage %": "Invoice coverage %",
            "Shippit consignments": "Shippit consignments",
            "Matched consignments": "Matched consignments",
            "Not yet matched": "Not yet matched",
        }
        selected_metric = st.selectbox(
            "Coverage matrix metric",
            list(matrix_metrics),
            index=0,
            help="Choose the weekly coverage measure shown in the matrix.",
        )
        weekly_matrix = _reconciliation_weekly_matrix(
            weekly, matrix_metrics[selected_metric]
        )
        week_columns = [
            column
            for column in weekly_matrix.columns
            if column not in {"CFC", "Shipped carrier", "Shipped service"}
        ]
        weekly_format = {
            column: ("{:,.1f}%" if selected_metric == "Invoice coverage %" else "{:,.0f}")
            for column in week_columns
        }
        st.caption(
            "Rows show the CFC and shipped carrier/service. Columns run from newest to oldest within the selected coverage period."
        )
        weekly_matrix_height = min(
            900,
            max(180, 38 * (len(weekly_matrix) + 1) + 20),
        )
        st.dataframe(
            weekly_matrix.style.format(weekly_format, na_rep="-"),
            width="stretch",
            height=weekly_matrix_height,
            hide_index=True,
        )
        st.download_button(
            "Download weekly coverage matrix CSV",
            weekly_matrix.to_csv(index=False).encode("utf-8"),
            file_name="weekly_invoice_coverage_matrix.csv",
            mime="text/csv",
        )

        cost_matrix_metrics = {
            "Variance %": "Variance %",
            "Modelled cost ex GST": "Matched modelled cost ex GST",
            "Modelled CPU ex GST": "Modelled CPU ex GST",
            "Invoiced actual cost ex GST": "Invoiced actual cost ex GST",
            "Invoiced actual CPU ex GST": "Invoiced actual CPU ex GST",
            "Variance ex GST": "Variance ex GST",
            "Variance CPU ex GST": "Variance CPU ex GST",
        }
        selected_cost_metric = st.selectbox(
            "Cost and CPU matrix metric",
            list(cost_matrix_metrics),
            index=0,
            help=(
                "Cost and CPU measures use matched invoice records only. CPU is cost per packed Shippit unit; "
                "variance is invoiced actual less modelled."
            ),
        )
        cost_matrix = _reconciliation_weekly_matrix(
            weekly, cost_matrix_metrics[selected_cost_metric]
        )
        cost_week_columns = [
            column
            for column in cost_matrix.columns
            if column not in {"CFC", "Shipped carrier", "Shipped service"}
        ]
        cost_is_pct = selected_cost_metric == "Variance %"
        cost_matrix_format = {
            column: ("{:,.1f}%" if cost_is_pct else "${:,.2f}")
            for column in cost_week_columns
        }
        st.caption(
            "Cost and CPU are calculated from matched invoice records; CPU uses matched packed Shippit units as the denominator."
        )
        st.dataframe(
            cost_matrix.style.format(cost_matrix_format, na_rep="-"),
            width="stretch",
            height=weekly_matrix_height,
            hide_index=True,
        )
        st.download_button(
            "Download weekly cost and CPU matrix CSV",
            cost_matrix.to_csv(index=False).encode("utf-8"),
            file_name="weekly_invoice_cost_cpu_matrix.csv",
            mime="text/csv",
        )

    st.subheader("Fast dim/weight variance signal")
    st.caption(
        "This high-level view avoids loading the large invoice-level detail files. Observed variance and complete-cost records are aligned to the saved coverage matrix. "
        "The dim/weight share is directional: it applies the saved carrier-summary physical mix to that aligned variance; the detailed loader provides the exact invoice-level evidence."
    )
    physical_overview = _reconciliation_physical_overview(
        _load_reconciliation_summaries(validation_dataset),
        site,
        shipped_carrier,
        coverage,
    )
    if physical_overview.empty:
        st.info("No saved high-level dim/weight summary is available for this selection.")
    else:
        st.caption(
            "The dim/weight amount and record count are directional estimates in this fast view. The signed percentage can exceed 100% when a dim/weight overrun is partly offset by another variance driver. "
            "Use the detailed reconciliation loader for exact invoice-level evidence and root-cause review."
        )
        st.dataframe(
            physical_overview.style.format(
                {
                    "Complete-cost records": "{:,.0f}",
                    "Total observed variance ex GST": "${:+,.0f}",
                    "Total observed variance %": "{:+,.1f}%",
                    "Dim/weight records": "{:,.0f}",
                    "Dim/weight variance ex GST": "${:+,.0f}",
                    "Dim/weight share of signed variance %": "{:+,.1f}%",
                    "Residual variance ex GST": "${:+,.0f}",
                },
                na_rep="-",
            ),
            width="stretch",
            hide_index=True,
        )

    with st.expander("Shippit consignment invoice coverage", expanded=False):
        coverage_columns = [
            "Week",
            "Shippit invoice number",
            "Shippit tracking number",
            "CFC",
            "Shipped carrier",
            "Shipped service",
            "Despatch date",
            "Invoice status",
            "Match source",
            "Actual invoice rows",
            "Modelled cost ex GST",
            "Invoiced actual cost ex GST",
            "Variance ex GST",
            "Variance %",
        ]
        coverage_columns = [column for column in coverage_columns if column in coverage]
        coverage_sort_columns = [
            column for column in ["Week", "Shippit invoice number"] if column in coverage
        ]
        coverage_display = (
            coverage.sort_values(coverage_sort_columns, na_position="last")[coverage_columns]
            if coverage_sort_columns
            else coverage[coverage_columns]
        )
        st.caption(
            "The table identifies every selected Shippit consignment, including records not yet present in the exposed carrier invoice files."
        )
        st.dataframe(
            coverage_display.head(500).style.format(
                {
                    "Actual invoice rows": "{:,.0f}",
                    "Modelled cost ex GST": "${:,.2f}",
                    "Invoiced actual cost ex GST": "${:,.2f}",
                    "Variance ex GST": "${:+,.2f}",
                    "Variance %": "{:+,.1f}%",
                },
                na_rep="-",
            ),
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "Download consignment invoice coverage CSV",
            coverage_display.to_csv(index=False).encode("utf-8"),
            file_name="consignment_invoice_coverage.csv",
            mime="text/csv",
        )

    with st.expander("Reconciliation files available to this dataset", expanded=False):
        file_status = _reconciliation_file_status(reconciliation_files)
        st.dataframe(
            file_status.style.format({"Size MB": "{:,.1f}"}, na_rep="-"),
            width="stretch",
            hide_index=True,
        )

    if not loaded_detail:
        st.info(
            "The saved coverage and weekly accuracy are shown above. Load matching reconciliation records to open the cost-driver bridge and bucket detail."
        )
        if st.button("Load detailed reconciliation and variance analysis", type="primary"):
            st.session_state[load_key] = True
            st.rerun()
        return

    cost_detail = _reconciliation_physical_basis_detail(cost_detail)
    bridge, totals = _reconciliation_bridge(cost_detail)
    if cost_detail.empty:
        st.info(
            "No matched reconciliation records were found for this selection. "
            "The reconciliation feed may not cover this accounting period yet."
        )
        st.caption("Try **All available weeks** to review historical carrier invoices.")
        return

    modelled_cost = totals.get("modelled_cost")
    actual_cost = totals.get("actual_cost")
    variance = totals.get("variance")
    variance_pct = totals.get("variance_pct")

    st.subheader("2. Observed variance: invoiced actual versus model")
    st.caption(
        "Positive variance means the invoiced actual cost is above the modelled estimate. "
        "This is the total observed gap before separating Shippit dim/weight evidence from residual model and rate variance."
    )
    cards = st.columns(5)
    cards[0].metric("Invoices with complete costs", f"{int(totals['matched_invoices'] or 0):,}")
    cards[1].metric("Records with complete costs", f"{int(totals['matched_records'] or 0):,}")
    cards[2].metric("Model estimated cost", "-" if modelled_cost is None else f"${modelled_cost:,.0f}")
    cards[3].metric("Actual invoiced cost", "-" if actual_cost is None else f"${actual_cost:,.0f}")
    cards[4].metric(
        "Actual less modelled",
        "-" if variance is None else f"${variance:,.0f}",
        "-" if variance_pct is None else f"{variance_pct:+.1f}%",
    )
    complete_cost_records = int(totals.get("matched_records") or 0)
    incomplete_cost_records = max(0, len(cost_detail) - complete_cost_records)
    st.caption(
        f"Complete-cost invoices means unique Shippit invoice numbers in {complete_cost_records:,} reconciliation records "
        "where both an invoiced actual cost and a positive modelled cost are present. "
        "This is a subset of all invoice statuses marked as matched in the coverage matrix."
    )
    if incomplete_cost_records:
        st.info(
            f"{incomplete_cost_records:,} loaded reconciliation record(s) do not have both cost values. "
            "They are excluded from the observed variance total and retained for matching follow-up."
        )
    detail_source = (
        "the same persisted historical coverage rows used by the weekly matrices, resolved against available current and archived carrier extracts"
        if has_persisted_coverage
        else "the selected live Stage 3 rows"
    )
    st.caption(f"Detailed reconciliation source: {detail_source}.")
    if has_persisted_coverage:
        st.caption(
            "The bridge rows are the invoice records resolved for those same persisted coverage keys; "
            "archived extracts are included so historical weeks are not limited to the current reconciliation file."
        )

    physical_cohort_names = {
        "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH",
        "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY",
        "SHIPPIT_CUBE_MISMATCH_ONLY",
    }
    complete_cost_mask = (
        pd.to_numeric(cost_detail["modelled_total_cost_ex_gst"], errors="coerce").gt(0)
        & pd.to_numeric(cost_detail["actual_total_cost_ex_gst_std"], errors="coerce").notna()
    )
    complete_cost_detail = cost_detail.loc[complete_cost_mask].copy()
    dim_weight_detail = complete_cost_detail.loc[
        complete_cost_detail["shippit_physical_basis_cohort"].isin(physical_cohort_names)
    ].copy()
    comparable_detail = complete_cost_detail.loc[
        complete_cost_detail["shippit_physical_basis_cohort"] == "NO_SHIPPIT_PHYSICAL_MISMATCH"
    ].copy()
    unavailable_physical_detail = complete_cost_detail.loc[
        complete_cost_detail["shippit_physical_basis_cohort"] == "PHYSICAL_EVIDENCE_UNAVAILABLE"
    ].copy()

    st.subheader("Observed variance bridge by primary cost driver")
    st.caption(
        "Each matched record is assigned to one primary driver so the rows reconcile to the observed total variance. "
        "The component columns show whether the gap comes from base/rate, fuel or other surcharges. "
        "A component is shown as unavailable when that carrier feed does not provide the split."
    )
    st.caption(
        "Sign convention: positive means invoiced actual cost is above the modelled cost; "
        "negative means invoiced actual cost is below the modelled cost. Other surcharge variance "
        "includes carrier charges outside base freight and fuel; for Allied this includes excess-weight, "
        "delivery, and other surcharges."
    )
    st.caption(
        "Colour key: amber marks the Shippit dim/weight-basis portion shown in section 3; "
        "blue marks the complementary comparable-basis portion shown in section 4."
    )
    display_bridge = bridge.rename(
        columns={
            "primary_root_cause": "Primary cost driver",
            "matched_records": "Matched records",
            "base_variance_ex_gst": "Base / rate variance ex GST",
            "fuel_variance_ex_gst": "Fuel variance ex GST",
            "other_variance_ex_gst": "Other surcharge variance ex GST",
            "total_variance_ex_gst": "Total variance ex GST",
            "variance_pct": "Variance %",
        }
    )
    bridge_columns = [
        "Primary cost driver",
        "Matched records",
        "Base / rate variance ex GST",
        "Fuel variance ex GST",
        "Other surcharge variance ex GST",
        "Total variance ex GST",
        "Variance %",
    ]
    bridge_table = display_bridge[bridge_columns].copy()
    bridge_table.loc[len(bridge_table)] = {
        "Primary cost driver": "Total observed variance",
        "Matched records": bridge_table["Matched records"].sum(),
        "Base / rate variance ex GST": bridge_table["Base / rate variance ex GST"].sum(min_count=1),
        "Fuel variance ex GST": bridge_table["Fuel variance ex GST"].sum(min_count=1),
        "Other surcharge variance ex GST": bridge_table["Other surcharge variance ex GST"].sum(min_count=1),
        "Total variance ex GST": bridge_table["Total variance ex GST"].sum(min_count=1),
        "Variance %": variance_pct,
    }
    def bridge_row_style(row: pd.Series) -> list[str]:
        if row.name == bridge_table.index[-1]:
            return ["font-weight: bold; background-color: #dfe7eb;"] * len(row)
        if row.get("Primary cost driver") == "SHIPPIT_PHYSICAL_BASIS_MISMATCH":
            return ["background-color: #fff1df;"] * len(row)
        return ["background-color: #e8f1f5;"] * len(row)

    bridge_style = bridge_table.style.format(
        {
            "Matched records": "{:,.0f}",
            "Base / rate variance ex GST": "${:+,.0f}",
            "Fuel variance ex GST": "${:+,.0f}",
            "Other surcharge variance ex GST": "${:+,.0f}",
            "Total variance ex GST": "${:+,.0f}",
            "Variance %": "{:+.1f}%",
        },
        na_rep="-",
    ).apply(bridge_row_style, axis=1)
    st.dataframe(
        bridge_style,
        width="stretch",
        hide_index=True,
    )

    st.subheader("3. Shippit dim/weight diagnostic")
    st.caption(
        "This exclusive split shows which matched consignments have a material difference between the invoiced carrier dimensions/weight and Shippit's weight or cube. "
        "The tinted cards below are the dim/weight portion of section 2. Section 4 is the complementary comparable-basis portion, so the two sections reconcile to section 2."
    )
    physical_summary = _reconciliation_physical_basis_summary(cost_detail)
    physical_labels = {
        "SHIPPIT_CUBE_AND_WEIGHT_MISMATCH": "Shippit cube and weight mismatch",
        "SHIPPIT_CHARGEABLE_WEIGHT_MISMATCH_ONLY": "Shippit chargeable weight mismatch only",
        "SHIPPIT_CUBE_MISMATCH_ONLY": "Shippit cube mismatch only",
        "NO_SHIPPIT_PHYSICAL_MISMATCH": "No material Shippit dim/weight mismatch",
        "PHYSICAL_EVIDENCE_UNAVAILABLE": "Dim/weight evidence unavailable",
    }
    physical_display = physical_summary.copy()
    physical_display["Cohort"] = physical_display["Shippit physical-basis cohort"].map(
        physical_labels
    ).fillna(physical_display["Shippit physical-basis cohort"])
    physical_headline_columns = [
        "Cohort",
        "Matched records",
        "Observed variance ex GST",
        "Observed variance %",
    ]
    st.dataframe(
        physical_display[physical_headline_columns].style.format(
            {
                "Matched records": "{:,.0f}",
                "Observed variance ex GST": "${:+,.0f}",
                "Observed variance %": "{:+.1f}%",
            },
            na_rep="-",
        ),
        width="stretch",
        hide_index=True,
    )
    dim_weight_bridge, dim_weight_totals = _reconciliation_bridge(dim_weight_detail)
    dim_weight_variance = dim_weight_totals.get("variance")
    dim_weight_variance_pct = dim_weight_totals.get("variance_pct")
    dim_weight_cards = st.columns(4)
    _render_reconciliation_metric_card(
        dim_weight_cards[0],
        "Dim/weight-basis records",
        f"{int(dim_weight_totals.get('matched_records') or 0):,}",
        tone="physical",
    )
    _render_reconciliation_metric_card(
        dim_weight_cards[1],
        "Dim/weight-basis modelled cost",
        f"${dim_weight_totals.get('modelled_cost', 0) or 0:,.0f}",
        tone="physical",
    )
    _render_reconciliation_metric_card(
        dim_weight_cards[2],
        "Dim/weight-basis invoiced cost",
        f"${dim_weight_totals.get('actual_cost', 0) or 0:,.0f}",
        tone="physical",
    )
    _render_reconciliation_metric_card(
        dim_weight_cards[3],
        "Dim/weight-basis variance",
        "-" if dim_weight_variance is None else f"${dim_weight_variance:+,.0f}",
        "" if dim_weight_variance_pct is None else f"{dim_weight_variance_pct:+.1f}%",
        tone="physical",
    )
    if unavailable_physical_detail.empty:
        st.caption(
            "Dim/weight-basis plus comparable-basis records, costs and variance reconcile to the complete-cost totals in section 2."
        )
    else:
        st.caption(
            f"{len(unavailable_physical_detail):,} complete-cost record(s) have no usable physical evidence and are excluded from both tinted cohorts; "
            "they remain included in section 2 and the bridge total."
        )
    with st.expander("Dim/weight evidence detail", expanded=False):
        physical_detail_columns = [
            "Cohort",
            "Matched records",
            "Modelled cost ex GST",
            "Invoiced actual cost ex GST",
            "Median invoiced less Shippit weight kg",
            "% with invoiced weight above Shippit",
        ]
        st.dataframe(
            physical_display[physical_detail_columns].style.format(
                {
                    "Matched records": "{:,.0f}",
                    "Modelled cost ex GST": "${:,.0f}",
                    "Invoiced actual cost ex GST": "${:,.0f}",
                    "Median invoiced less Shippit weight kg": "{:+.2f}",
                    "% with invoiced weight above Shippit": "{:.1f}%",
                },
                na_rep="-",
            ),
            width="stretch",
            hide_index=True,
        )
    st.download_button(
        "Download Shippit dim/weight diagnostic CSV",
        physical_summary.to_csv(index=False).encode("utf-8"),
        file_name="shippit_physical_basis_diagnostic.csv",
        mime="text/csv",
    )

    residual_bridge = pd.DataFrame()
    residual_totals: dict[str, float | None] = {}
    st.subheader("4. Residual model and rate variance")
    if comparable_detail.empty:
        st.info(
            "There are no matched records with comparable Shippit dimensions/weight in this selection. "
            "The residual model/rate view will appear when the matched data includes such records."
        )
    else:
        residual_bridge, residual_totals = _reconciliation_bridge(comparable_detail)
        residual_variance = residual_totals.get("variance")
        residual_variance_pct = residual_totals.get("variance_pct")
        st.caption(
            f"Observed variance for {len(comparable_detail):,} matched record(s) where Shippit is not materially different from the invoiced dimensions/weight. "
            "This is the controlled residual cohort, not a counterfactual repricing of the physical-mismatch records. The residual cards and rows use the complementary blue tone."
        )
        residual_cards = st.columns(4)
        _render_reconciliation_metric_card(
            residual_cards[0],
            "Comparable-basis records",
            f"{int(residual_totals.get('matched_records') or 0):,}",
            tone="residual",
        )
        _render_reconciliation_metric_card(
            residual_cards[1],
            "Comparable-basis modelled cost",
            f"${residual_totals.get('modelled_cost', 0) or 0:,.0f}",
            tone="residual",
        )
        _render_reconciliation_metric_card(
            residual_cards[2],
            "Comparable-basis invoiced cost",
            f"${residual_totals.get('actual_cost', 0) or 0:,.0f}",
            tone="residual",
        )
        _render_reconciliation_metric_card(
            residual_cards[3],
            "Residual variance",
            "-" if residual_variance is None else f"${residual_variance:+,.0f}",
            "-" if residual_variance_pct is None else f"{residual_variance_pct:+.1f}%",
            tone="residual",
        )
        residual_display = residual_bridge.rename(
            columns={
                "primary_root_cause": "Residual driver",
                "matched_records": "Matched records",
                "base_variance_ex_gst": "Base / rate variance ex GST",
                "fuel_variance_ex_gst": "Fuel variance ex GST",
                "other_variance_ex_gst": "Other surcharge variance ex GST",
                "total_variance_ex_gst": "Residual variance ex GST",
                "variance_pct": "Variance %",
            }
        )
        residual_columns = [
            "Residual driver",
            "Matched records",
            "Base / rate variance ex GST",
            "Fuel variance ex GST",
            "Other surcharge variance ex GST",
            "Residual variance ex GST",
            "Variance %",
        ]
        st.dataframe(
            residual_display[residual_columns].style.format(
                {
                    "Matched records": "{:,.0f}",
                    "Base / rate variance ex GST": "${:+,.0f}",
                    "Fuel variance ex GST": "${:+,.0f}",
                    "Other surcharge variance ex GST": "${:+,.0f}",
                    "Residual variance ex GST": "${:+,.0f}",
                    "Variance %": "{:+.1f}%",
                },
                na_rep="-",
            ),
            width="stretch",
            hide_index=True,
        )

    readout = _reconciliation_variance_readout(
        cost_detail,
        residual_bridge,
        residual_totals,
    )
    if readout:
        st.subheader("Variance read-out")
        st.markdown("\n".join(f"- {line}" for line in readout))

    st.subheader("Review a variance bucket")
    bucket_options = ["All primary cost drivers"] + bridge["primary_root_cause"].astype(str).tolist()
    selected_bucket = st.selectbox("Primary cost driver", bucket_options)
    bucket_detail = cost_detail if selected_bucket == "All primary cost drivers" else cost_detail[cost_detail["primary_root_cause"].astype(str) == selected_bucket]
    diagnostic_columns = [
        column
        for column in [
            "invoice_number",
            "stage3_tracking_number",
            "site",
            "primary_root_cause",
            "root_cause",
            "match_method",
            "actual_service_name",
            "actual_service_material",
            "actual_service_code",
            "actual_chargeable_weight_kg",
            "actual_rounded_chargeable_weight_kg",
            "shippit_chargeable_weight_kg",
            "modelled_cp_chargeable_weight_kg",
            "modelled_ap_chargeable_weight_kg",
            "actual_destination_zone",
            "actual_cp_destination_zone",
            "modelled_ap_zone",
            "modelled_ap_pricing_zone",
            "modelled_cp_destination_zone",
            "modelled_goexpress_zone",
            "modelled_allied_zone",
            "estimator_parcel_cube_cm3",
            "shippit_chargeable_cube_cm3",
            "actual_billed_cube_m3",
            "actual_total_volume_m3",
            "actual_cubic_volume_m3",
            "actual_invoice_cost_ex_gst",
            "modelled_total_cost_ex_gst",
            "actual_less_modelled_cost_including_fuel_ex_gst",
            "estimator_dimension_diagnostic",
            "shippit_dimension_diagnostic",
            "reconciliation_note",
        ]
        if column in bucket_detail
    ]
    st.caption(f"Showing {len(bucket_detail):,} reconciliation record(s) in the selected bucket.")
    bucket_export = _reconciliation_bucket_export(bucket_detail)
    st.dataframe(bucket_export.head(500), width="stretch", hide_index=True)
    st.download_button(
        "Download selected reconciliation bucket CSV",
        bucket_export.to_csv(index=False).encode("utf-8"),
        file_name=f"reconciliation_{selected_bucket.lower().replace(' ', '_')}.csv",
        mime="text/csv",
    )


def _first_value(row: pd.Series, columns: tuple[str, ...]) -> object:
    for column in columns:
        if column in row.index:
            value = row.get(column)
            if value is not None and not pd.isna(value) and str(value).strip():
                return value
    return None


def _reconciliation_snapshot(rows: pd.DataFrame) -> dict[str, object]:
    if rows.empty:
        return {}

    def number(columns: tuple[str, ...], aggregate: str = "sum") -> float | None:
        for column in columns:
            if column not in rows:
                continue
            values = pd.to_numeric(rows[column], errors="coerce")
            if not values.notna().any():
                continue
            return float(values.sum() if aggregate == "sum" else values.dropna().iloc[0])
        return None

    billed_cube_m3 = number(
        (
            "actual_billed_cube_m3",
            "actual_total_volume_m3",
            "actual_cubic_volume_m3",
    "actual_declared_cube_m3",
    "actual_chargeable_cube_cm3",
)
    )
    if billed_cube_m3 is None:
        billed_cube_cm3 = number(("actual_billed_cube_cm3", "actual_declared_cube_cm3"))
        billed_cube_m3 = billed_cube_cm3 / 1_000_000 if billed_cube_cm3 is not None else None

    volumetric_weight = number(("actual_cubic_weight_kg", "actual_volumetric_weight_kg"))
    if volumetric_weight is None and billed_cube_m3 is not None:
        volumetric_weight = billed_cube_m3 * 4_000

    return {
        "service": _first_value(rows.iloc[0], ("actual_service_name", "actual_service_material", "actual_service_code", "stage3_service")),
        "chargeable_weight_kg": number(("actual_chargeable_weight_kg", "actual_rounded_chargeable_weight_kg")),
        "declared_weight_kg": number(("actual_declared_weight_kg", "actual_weight_kg", "actual_dead_weight_kg")),
        "volumetric_weight_kg": volumetric_weight,
        "cube_m3": billed_cube_m3,
        "base_cost_ex_gst": number(("actual_base_cost_ex_gst", "actual_modelled_base_cost_ex_gst")),
        "fuel_cost_ex_gst": number(("actual_fuel_surcharge_ex_gst",)),
        "other_cost_ex_gst": number(("actual_other_surcharges_ex_gst", "actual_other_surcharge_ex_gst", "actual_delivery_surcharge_ex_gst")),
        "total_cost_ex_gst": number(("actual_invoice_cost_ex_gst",)),
        "fuel_rate_pct": number(("actual_fuel_rate_pct", "fuel_norm_p50_pct"), aggregate="first"),
        "primary_cost_driver": _first_value(rows.iloc[0], ("primary_cost_driver", "root_cause")),
        "reconciliation_note": _first_value(rows.iloc[0], ("reconciliation_note",)),
    }


def _filter_parcel_scope(frame: pd.DataFrame, parcel_scope: str) -> pd.DataFrame:
    if parcel_scope == "All consignments" or "actual_parcel_count" not in frame:
        return frame
    parcel_count = pd.to_numeric(frame["actual_parcel_count"], errors="coerce")
    if parcel_scope == "Single parcel":
        return frame.loc[parcel_count == 1]
    return frame.loc[parcel_count > 1]


def _cost_series(frame: pd.DataFrame) -> pd.Series:
    if "actual_est_cost_ex_gst" not in frame:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame["actual_est_cost_ex_gst"], errors="coerce")


def _metric_value(frame: pd.DataFrame, metric_key: str) -> float:
    if frame.empty:
        return float("nan")
    invoices = frame[_invoice_column(frame)].dropna().astype(str).nunique()
    parcels = _number(frame, "actual_parcel_count").sum()
    units = _number(frame, "parcel_units_total").sum()
    costs = _cost_series(frame)
    costed = frame.loc[costs.notna()]
    modelled_cost = costs.sum(skipna=True)
    costed_invoices = costed[_invoice_column(costed)].dropna().astype(str).nunique()
    costed_parcels = _number(costed, "actual_parcel_count").sum()
    costed_units = _number(costed, "parcel_units_total").sum()
    values = {
        "consignments": float(invoices),
        "shipped_parcels": parcels,
        "shipped_units": units,
        "units_per_consignment": units / invoices if invoices else float("nan"),
        "units_per_parcel": units / parcels if parcels else float("nan"),
        "parcels_per_consignment": parcels / invoices if invoices else float("nan"),
        "cost_per_consignment": modelled_cost / costed_invoices if costed_invoices else float("nan"),
        "cost_per_parcel": modelled_cost / costed_parcels if costed_parcels else float("nan"),
        "cost_per_unit": modelled_cost / costed_units if costed_units else float("nan"),
        "modelled_cost": modelled_cost,
    }
    return values[metric_key]


def _metric_format(metric_key: str) -> str:
    if metric_key.endswith("_pct"):
        return "{:,.1f}%"
    if metric_key == "modelled_cost":
        return "${:,.0f}"
    if metric_key.startswith("cost_"):
        return "${:,.2f}"
    if metric_key in {"consignments", "shipped_parcels", "shipped_units"}:
        return "{:,.0f}"
    return "{:,.2f}"


def _ordered_values(frame: pd.DataFrame, column: str, preferred: list[str]) -> list[str]:
    values = frame.get(column, pd.Series(dtype="string")).fillna("").astype(str).str.strip()
    extras = sorted(value for value in values.unique() if value and value not in preferred)
    return preferred + extras + (["Unmapped / review required"] if (values == "").any() else [])


def _quality_breakdown(frame: pd.DataFrame, group_column: str) -> pd.DataFrame:
    """Summarise Stage 2 model-quality signals by a review slice."""
    if frame.empty:
        return pd.DataFrame()

    work = frame.copy()
    groups = work.get(group_column, pd.Series("", index=work.index)).fillna("").astype(str).str.strip()
    work["review_slice"] = groups.replace("", "Unmapped / review required")
    work["parcel_match"] = (
        work.get("est_parcel_count_match", pd.Series("", index=work.index))
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("Y")
        .astype(float)
    )
    work["absolute_chargeable_gap_kg"] = _number(work, "est_weight_gap_vs_shippit_chargeable_kg").abs()
    shippit_cube = _number(work, "shippit_volumetric_cube_cm3").replace(0, pd.NA)
    work["absolute_cube_gap_pct"] = (
        _number(work, "est_cube_gap_vs_shippit_cm3").abs().div(shippit_cube).mul(100).fillna(0)
    )
    work["imputed_units"] = _number(work, "cfc_cube_imputed_unit_count")
    work["packed_units"] = _number(work, "est_parcel_units_total")

    summary = (
        work.groupby("review_slice", dropna=False, sort=False)
        .agg(
            Consignments=("review_slice", "size"),
            Parcel_match_pct=("parcel_match", "mean"),
            Median_abs_chargeable_gap_kg=("absolute_chargeable_gap_kg", "median"),
            Median_abs_cube_gap_pct=("absolute_cube_gap_pct", "median"),
            Imputed_units=("imputed_units", "sum"),
            Packed_units=("packed_units", "sum"),
        )
        .reset_index()
    )
    p95 = work.groupby("review_slice", dropna=False, sort=False)["absolute_chargeable_gap_kg"].quantile(0.95)
    summary["P95_abs_chargeable_gap_kg"] = summary["review_slice"].map(p95)
    summary["Parcel_match_pct"] = summary["Parcel_match_pct"] * 100.0
    return summary.rename(
        columns={
            "review_slice": "Review slice",
            "Consignments": "Consignments",
            "Parcel_match_pct": "Parcel match %",
            "Median_abs_chargeable_gap_kg": "Median abs chargeable gap kg",
            "P95_abs_chargeable_gap_kg": "P95 abs chargeable gap kg",
            "Median_abs_cube_gap_pct": "Median abs cube gap %",
            "Imputed_units": "Imputed units",
            "Packed_units": "Packed units",
        }
    )


def _rbu_mix(assignment_lines: pd.DataFrame) -> pd.DataFrame:
    """Create an RBU x size-category exposure table from Stage 1 lines."""
    if assignment_lines.empty or "rbu_description" not in assignment_lines:
        return pd.DataFrame()

    work = assignment_lines.copy()
    work["RBU description"] = (
        work["rbu_description"].fillna("").astype(str).str.strip().replace("", "Unmapped / review required")
    )
    work["Size category"] = (
        work.get("size_category", pd.Series("", index=work.index))
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", "No dims")
    )
    work["Packed units"] = _number(work, "quantity_packed")
    work["sku_id"] = work.get("sku_id", pd.Series("", index=work.index)).fillna("").astype(str).str.strip()
    unit_mix = work.pivot_table(
        index="RBU description",
        columns="Size category",
        values="Packed units",
        aggfunc="sum",
        fill_value=0,
    )
    for category in ["Toteable", "Non-Toteable", "Bulky", "No dims"]:
        if category not in unit_mix:
            unit_mix[category] = 0
    unit_mix["Packed units"] = unit_mix[["Toteable", "Non-Toteable", "Bulky", "No dims"]].sum(axis=1)
    keycode_mix = (
        work.loc[work["sku_id"].ne("")]
        .drop_duplicates(["RBU description", "Size category", "sku_id"])
        .pivot_table(index="RBU description", columns="Size category", values="sku_id", aggfunc="nunique", fill_value=0)
    )
    for category in ["Toteable", "Non-Toteable", "Bulky", "No dims"]:
        if category not in keycode_mix:
            keycode_mix[category] = 0
    keycode_mix["Distinct keycodes"] = keycode_mix[["Toteable", "Non-Toteable", "Bulky", "No dims"]].sum(axis=1)
    result = unit_mix[["Packed units", "Toteable", "Non-Toteable", "Bulky", "No dims"]].rename(
        columns={category: f"{category} units" for category in ["Toteable", "Non-Toteable", "Bulky", "No dims"]}
    )
    result = result.join(keycode_mix[["Distinct keycodes"]])
    result = result.reset_index()
    result = result.sort_values(["Packed units", "RBU description"], ascending=[False, True])
    return result[["RBU description", "Packed units", "Distinct keycodes", "Toteable units", "Non-Toteable units", "Bulky units", "No dims units"]]


def _data_readiness(site: str) -> pd.DataFrame:
    rows = []
    labels = {
        "stage1": "Stage 1 assignment lines",
        "stage2": "Stage 2 parcel validation",
        "physicals": "Stage 2 parcel physicals",
        "keycode_review": "Stage 2 keycode review",
        "air_norms": "Stage 2 air-ratio norms",
        "stage3": "Stage 3 cost estimates",
        "carrier_validation": "Stage 3 carrier validation",
    }
    site_keys = list(SITE_FILES) if site == "ALL" else [site]
    for site_key in site_keys:
        for dataset, label in labels.items():
            path = SITE_FILES[site_key].get("assignment_lines" if dataset == "stage1" else dataset)
            if path is None:
                continue
            if path.exists():
                stat = path.stat()
                updated = pd.to_datetime(stat.st_mtime, unit="s").strftime("%d %b %Y %H:%M")
                rows.append(
                    {
                        "CFC": site_key.title(),
                        "Output": label,
                        "Status": "Available",
                        "Last modified": updated,
                        "Size MB": round(stat.st_size / 1_000_000, 1),
                    }
                )
            else:
                rows.append(
                    {
                        "CFC": site_key.title(),
                        "Output": label,
                        "Status": "Missing",
                        "Last modified": "",
                        "Size MB": 0.0,
                    }
                )
    return pd.DataFrame(rows)


def _quality_assumptions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["Parcel shape", "Stacked rectangular envelope; tall tote-like stacks reflow to a 60 cm x 40 cm footprint where applicable.", "Review if physical dimensions remain implausibly tall."],
            ["Chargeable weight", "Stage 2 uses max(parcel weight, parcel cube / 4,000 cm3 per kg) and compares it with Shippit.", "Large gaps indicate a dim/weight or Shippit-data review, not automatically a ratecard error."],
            ["Pricing basis", "Stage 3 scales the parcel estimate to the Shippit chargeable cube before carrier comparison.", "This keeps the cost model aligned to the current Shippit source of truth."],
            ["CFC origin", "Moorebank is modelled from Sydney/NSW; Truganina is modelled from Melbourne/VIC for Australia Post zone selection.", "Origin-aware zones should be checked when comparing CFCs."],
            ["Fuel surcharge", "Effective-dated carrier FSC rules are applied as a separate Stage 3 cost element where available.", "The rule and source are assumptions to validate against carrier invoices."],
            ["Reconciliation", "Carrier invoices validate the model after shipment; they do not feed back into the daily Stage 3 estimate automatically.", "Use reconciliation root causes as a controlled feedback loop for ratecards and data."],
        ],
        columns=["Assumption", "Current treatment", "Review implication"],
    )


@st.cache_data(show_spinner=False)
def _fsc_model_rules_cached(path_text: str, modified_ns: int | None) -> pd.DataFrame:
    del modified_ns
    rows = load_model_rules(Path(path_text))
    return pd.DataFrame(rows, columns=FSC_RULE_COLUMNS)


def _fsc_model_rules() -> pd.DataFrame:
    modified_ns = FSC_MODEL_INPUT_PATH.stat().st_mtime_ns if FSC_MODEL_INPUT_PATH.exists() else None
    return _fsc_model_rules_cached(str(FSC_MODEL_INPUT_PATH), modified_ns)


@st.cache_data(show_spinner="Reading invoice FSC evidence...")
def _fsc_validation_cached(
    rule_rows: tuple[tuple[str, ...], ...],
    invoice_signatures: tuple[tuple[str, int, int], ...],
) -> pd.DataFrame:
    del invoice_signatures
    rules = [dict(zip(FSC_RULE_COLUMNS, row)) for row in rule_rows]
    return pd.DataFrame(invoice_validated_rates(rules, FSC_INVOICE_DIRECTORY))


def _fsc_validation(rules: pd.DataFrame) -> pd.DataFrame:
    rule_rows = tuple(
        tuple("" if pd.isna(value) else str(value) for value in row)
        for row in rules.loc[:, FSC_RULE_COLUMNS].itertuples(index=False, name=None)
    )
    signatures = []
    for path in (
        FSC_INVOICE_DIRECTORY / "australia_post_charge_lines.csv",
        FSC_INVOICE_DIRECTORY / "gopeople_charge_lines.csv",
        FSC_INVOICE_DIRECTORY / "couriers_please_charge_lines.csv",
        FSC_INVOICE_DIRECTORY / "allied_jobs.csv",
    ):
        if path.exists():
            stat = path.stat()
            signatures.append((str(path), stat.st_mtime_ns, stat.st_size))
        else:
            signatures.append((str(path), 0, 0))
    return _fsc_validation_cached(rule_rows, tuple(signatures))


def _save_fsc_model_rules(rows: list[dict[str, object]]) -> None:
    FSC_MODEL_INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = FSC_MODEL_INPUT_PATH.with_suffix(".csv.tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = pd.DataFrame(rows, columns=FSC_RULE_COLUMNS).fillna("")
        writer.to_csv(handle, index=False)
    temporary_path.replace(FSC_MODEL_INPUT_PATH)


def _fsc_editor_frame(rules: pd.DataFrame, validation: pd.DataFrame) -> pd.DataFrame:
    display = rules.copy()
    if validation.empty:
        validation = pd.DataFrame(columns=FSC_RULE_COLUMNS)
    validation_columns = [
        "invoice_validated_fsc_pct",
        "invoice_base_cost_ex_gst",
        "invoice_fuel_cost_ex_gst",
        "invoice_evidence_count",
        "invoice_validation_source",
    ]
    for column in validation_columns:
        display[column] = validation[column].tolist() if column in validation else None
    return display[
        [
            *FSC_RULE_COLUMNS,
            "invoice_validated_fsc_pct",
            "invoice_base_cost_ex_gst",
            "invoice_fuel_cost_ex_gst",
            "invoice_evidence_count",
            "invoice_validation_source",
        ]
    ]


def _fsc_model_inputs() -> None:
    st.subheader("FSC model inputs")
    saved_message = st.session_state.pop("fsc_saved_message", "")
    if saved_message:
        st.success(saved_message)
    st.write(
        "Maintain the effective-dated fuel surcharge assumptions used by Stage 3. "
        "The model rate is editable; the invoice-validated average is read-only evidence."
    )
    st.info(
        "Save changes here, then rerun both Stage 3 cost-estimator scripts for the new FSC "
        "assumption to affect model outputs. Existing reconciliation files are not rewritten "
        "by this page."
    )

    rules = _fsc_model_rules()
    validation = _fsc_validation(rules)
    editor_frame = _fsc_editor_frame(rules, validation)
    validation_columns = [
        "invoice_validated_fsc_pct",
        "invoice_base_cost_ex_gst",
        "invoice_fuel_cost_ex_gst",
        "invoice_evidence_count",
        "invoice_validation_source",
    ]
    editor_version = st.session_state.get("fsc_editor_version", 0)
    edited = st.data_editor(
        editor_frame,
        key=f"fsc_model_rules_editor_{editor_version}",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        disabled=validation_columns,
        column_config={
            "carrier": st.column_config.TextColumn("Carrier", required=True),
            "service": st.column_config.TextColumn("Service", required=True),
            "start_date": st.column_config.TextColumn("Start date (YYYY-MM-DD)", required=True),
            "end_date": st.column_config.TextColumn("End date (YYYY-MM-DD)", required=True),
            "fuel_surcharge_pct": st.column_config.NumberColumn("Model FSC rate %", format="%.2f", min_value=0.0, max_value=100.0, required=True),
            "source_note": st.column_config.TextColumn("Model source / note"),
            "invoice_validated_fsc_pct": st.column_config.NumberColumn("Invoice-validated avg %", format="%.2f"),
            "invoice_base_cost_ex_gst": st.column_config.NumberColumn("Invoice base evidence ex GST", format="$%,.2f"),
            "invoice_fuel_cost_ex_gst": st.column_config.NumberColumn("Invoice fuel evidence ex GST", format="$%,.2f"),
            "invoice_evidence_count": st.column_config.NumberColumn("Invoice evidence rows", format="%,.0f"),
            "invoice_validation_source": st.column_config.TextColumn("Validation basis"),
        },
    )
    st.caption(
        "Invoice-validated average is weighted as invoice fuel evidence divided by invoice base "
        "evidence for the same carrier, service and date range. It is not used by Stage 3."
    )
    if st.button("Save FSC model inputs", type="primary", width="stretch"):
        model_rows = edited.loc[:, FSC_RULE_COLUMNS].to_dict("records")
        errors = validate_model_rules(model_rows)
        if errors:
            for error in errors:
                st.error(error)
        else:
            _save_fsc_model_rules(model_rows)
            st.cache_data.clear()
            st.session_state["fsc_saved_message"] = (
                "FSC model inputs saved. Rerun Stage 3 to apply the updated assumptions."
            )
            st.session_state["fsc_editor_version"] = editor_version + 1
            st.rerun()

    with st.expander("How validation is calculated"):
        st.markdown(
            "- **Australia Post:** uses the fuel amount on the B48/B5S/B5R base service row; "
            "D05/DS1 fuel rows are retained as a control representation and are not added again.\n"
            "- **Couriers Please:** uses the parsed fuel component divided by base freight.\n"
            "- **GoPeople:** uses the parsed fuel field where present.\n"
            "- **Allied Express:** validation requires dated invoice evidence before it can be "
            "matched to a date-bounded rule."
        )
    st.download_button(
        "Download FSC model input CSV",
        FSC_MODEL_INPUT_PATH.read_bytes() if FSC_MODEL_INPUT_PATH.exists() else b"",
        FSC_MODEL_INPUT_PATH.name,
        "text/csv",
        width="stretch",
        disabled=not FSC_MODEL_INPUT_PATH.exists(),
    )


def _slice_matrix(frame: pd.DataFrame, metric_key: str) -> pd.DataFrame:
    percentage = metric_key.endswith("_pct")
    base_metric_key = metric_key.removesuffix("_pct") if percentage else metric_key
    work = frame.copy()
    work["ui_zone"] = work.get("ap_pricing_zone", pd.Series("", index=work.index)).fillna("").astype(str).str.strip().replace("", "Unmapped / review required")
    work["ui_band"] = work.get("reporting_weight_band", pd.Series("", index=work.index)).fillna("").astype(str).str.strip().replace("", "Unmapped / review required")
    zones = _ordered_values(work, "ap_pricing_zone", AP_PRICING_ZONE_ORDER)
    bands = _ordered_values(work, "reporting_weight_band", REPORTING_WEIGHT_BAND_ORDER)
    matrix = pd.DataFrame(index=bands, columns=zones, dtype="float64")
    for band in bands:
        for zone in zones:
            matrix.loc[band, zone] = _metric_value(
                work[(work["ui_band"] == band) & (work["ui_zone"] == zone)],
                base_metric_key,
            )
    if percentage:
        grand_total = matrix.sum(skipna=True).sum()
        if grand_total:
            matrix = matrix.div(grand_total).mul(100)
    if not percentage and base_metric_key in {
        "cost_per_consignment",
        "cost_per_parcel",
        "cost_per_unit",
    }:
        # Unit-cost totals are weighted averages for each pricing-zone column,
        # rather than sums of the ratios shown in the weight-band rows.
        matrix.loc["Total"] = pd.Series(
            {
                zone: _metric_value(work[work["ui_zone"] == zone], base_metric_key)
                for zone in zones
            },
            dtype="float64",
        )
    else:
        matrix.loc["Total"] = matrix.sum(axis=0, skipna=True)
    matrix.index.name = "Reporting weight band"
    return matrix


def _historical_matrix_coverage(coverage: pd.DataFrame) -> pd.DataFrame:
    """Keep historical matrix rows that have an estimator cost to fall back to."""
    if coverage.empty:
        return coverage.copy()
    modelled_cost = pd.to_numeric(
        coverage["Modelled cost ex GST"]
        if "Modelled cost ex GST" in coverage
        else pd.Series(index=coverage.index, dtype="float64"),
        errors="coerce",
    )
    return coverage.loc[modelled_cost.notna()].copy()


def _historical_zone_week_matrix(
    frame: pd.DataFrame,
    metric_key: str,
    pricing_zone: str,
    week_order: list[str],
) -> pd.DataFrame:
    """Pivot a selected AP pricing zone by reporting weight band and week."""
    percentage = metric_key.endswith("_pct")
    base_metric_key = metric_key.removesuffix("_pct") if percentage else metric_key
    work = frame.copy()
    work["ui_zone"] = (
        work.get("ap_pricing_zone", pd.Series("", index=work.index))
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", "Unmapped / review required")
    )
    work["ui_band"] = (
        work.get("reporting_weight_band", pd.Series("", index=work.index))
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", "Unmapped / review required")
    )
    work["ui_week"] = (
        work.get("Week", pd.Series("", index=work.index))
        .fillna("")
        .astype(str)
        .str.strip()
    )
    if pricing_zone != "All zones":
        work = work.loc[work["ui_zone"] == pricing_zone]

    weeks = [str(week) for week in week_order]
    bands = _ordered_values(work, "reporting_weight_band", REPORTING_WEIGHT_BAND_ORDER)
    matrix = pd.DataFrame(index=bands, columns=weeks, dtype="float64")
    for band in bands:
        for week in weeks:
            matrix.loc[band, week] = _metric_value(
                work[(work["ui_band"] == band) & (work["ui_week"] == week)],
                base_metric_key,
            )
    if percentage:
        grand_total = matrix.sum(skipna=True).sum()
        if grand_total:
            matrix = matrix.div(grand_total).mul(100)
    if not percentage and base_metric_key in {
        "cost_per_consignment",
        "cost_per_parcel",
        "cost_per_unit",
    }:
        matrix.loc["Total"] = pd.Series(
            {
                week: _metric_value(work[work["ui_week"] == week], base_metric_key)
                for week in weeks
            },
            dtype="float64",
        )
    else:
        matrix.loc["Total"] = matrix.sum(axis=0, skipna=True)
    matrix.index.name = "Reporting weight band"
    return matrix


def _download(label: str, path: Path, mime: str = "text/csv") -> None:
    if path.exists():
        st.download_button(label, path.read_bytes(), path.name, mime=mime, width="stretch")
    else:
        st.caption(f"Not yet available: `{path.name}`")


def _generate_invoice_drilldown(site: str, invoice_number: str) -> tuple[bool, str]:
    command = [
        sys.executable,
        "-m",
        "src.invoice_drilldown_report",
        "--invoice-number",
        str(invoice_number),
        "--site",
        site,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "The workbook generator exceeded five minutes. Check the Snowflake authentication window, then try again."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "No diagnostic output was returned.").strip()
        return False, detail[-1200:]
    return True, ""


def _carrier_mix_values(frame: pd.DataFrame, metric: str) -> pd.Series:
    base_metric = metric.removesuffix(" %")
    if base_metric == "Consignments":
        return pd.Series(1.0, index=frame.index, dtype="float64")
    if base_metric == "Parcels":
        return _numeric_from_columns(frame, ("actual_parcel_count", "shippit_parcel_count"))
    return _numeric_from_columns(frame, ("parcel_units_total", "shippit_units_total"))


def _carrier_mix_daily_table(
    current: pd.DataFrame,
    previous: pd.DataFrame,
    metric: str,
    elapsed_weekdays: int | None = None,
) -> pd.DataFrame:
    """Build a carrier-by-weekday table with totals and a comparable WoW row."""
    weekday_columns = list(range(7))
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    current_dates = pd.to_datetime(
        current.get("despatch_date", pd.Series(index=current.index)), errors="coerce"
    ).dropna()
    if current_dates.empty:
        day_labels = [f"{name}\n--" for name in day_names]
    else:
        latest_week_start = current_dates.max().normalize() - pd.Timedelta(
            days=current_dates.max().weekday()
        )
        day_labels = [
            f"{name}\n{latest_week_start + pd.Timedelta(days=weekday):%d/%m}"
            for weekday, name in enumerate(day_names)
        ]
    display_columns = [
        *day_labels,
        "Total",
    ]

    def daily(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=weekday_columns, dtype="float64")
        carrier_column = "actual_carrier" if "actual_carrier" in frame else "shippit_carrier"
        dates = pd.to_datetime(
            frame.get("despatch_date", pd.Series(index=frame.index)), errors="coerce"
        )
        work = pd.DataFrame(
            {
                "_carrier": frame[carrier_column].fillna("Unassigned").replace("", "Unassigned"),
                "_weekday": dates.dt.dayofweek,
                "_value": _carrier_mix_values(frame, metric),
            },
            index=frame.index,
        ).dropna(subset=["_weekday"])
        if work.empty:
            return pd.DataFrame(columns=weekday_columns, dtype="float64")
        return (
            work.pivot_table(
                index="_carrier",
                columns="_weekday",
                values="_value",
                aggfunc="sum",
                fill_value=0.0,
            )
            .reindex(columns=weekday_columns, fill_value=0.0)
            .sort_index()
        )

    current_daily = daily(current)
    previous_daily = daily(previous)
    carrier_index = current_daily.index.union(previous_daily.index)
    current_daily = current_daily.reindex(
        index=carrier_index, columns=weekday_columns, fill_value=0.0
    )
    previous_daily = previous_daily.reindex(
        index=carrier_index, columns=weekday_columns, fill_value=0.0
    )

    current_total_by_carrier = current_daily.sum(axis=1)
    current_total = current_daily.sum(axis=0)
    prior_total = previous_daily.sum(axis=0)
    wow = current_total.subtract(prior_total).div(prior_total.replace(0, pd.NA)).mul(100)
    prior_period_total = prior_total.sum()
    wow["Total"] = (
        (current_total.sum() - prior_period_total) / prior_period_total * 100
        if prior_period_total
        else pd.NA
    )
    if elapsed_weekdays is not None:
        future_columns = [column for column in weekday_columns if column > elapsed_weekdays]
        wow.loc[future_columns] = pd.NA

    if metric.endswith(" %"):
        day_denominators = current_total.replace(0, pd.NA)
        period_denominator = current_total_by_carrier.sum()
        current_display = current_daily.div(day_denominators, axis=1).mul(100)
        current_display["Total"] = current_total_by_carrier.div(
            period_denominator if period_denominator else pd.NA
        ).mul(100)
        total = current_display.sum(axis=0)
    else:
        current_display = current_daily.copy()
        current_display["Total"] = current_total_by_carrier
        total = current_display.sum(axis=0)

    table = current_display.rename(columns=dict(zip(weekday_columns, display_columns[:-1])))
    table = table.reindex(columns=display_columns)
    table.loc["Total"] = total.rename(
        index={**dict(zip(weekday_columns, display_columns[:-1])), "Total": "Total"}
    )
    table.loc["WoW %"] = wow.rename(
        index={**dict(zip(weekday_columns, display_columns[:-1])), "Total": "Total"}
    )
    table.index.name = "Carrier"
    return table


def _dashboard(
    stage3: pd.DataFrame,
    stage2: pd.DataFrame,
    parcel_scope: str,
    reporting_period_label: str,
    reporting_period: str,
    comparison_stage3: pd.DataFrame | None = None,
    *,
    site: str = "ALL",
    shipped_carrier: str = "All shipped carriers",
) -> None:
    st.subheader("Metrics dashboard")
    _render_refresh_status()
    if stage3.empty:
        if reporting_period == "Current accounting week to date":
            st.warning(
                f"No Stage 3 rows are available for {reporting_period_label}. "
                "Refresh the Stage 1-3 outputs before reviewing the current week."
            )
        else:
            st.warning("No Stage 3 outputs are available. Run the pipeline before using the dashboard.")
        return
    stage3 = _filter_parcel_scope(stage3, parcel_scope)
    if stage3.empty:
        st.info(f"No {parcel_scope.lower()} records are available for {reporting_period_label}.")
        return
    if not stage2.empty:
        stage2_invoice_col = _invoice_column(stage2)
        stage2 = stage2[stage2[stage2_invoice_col].astype(str).isin(stage3[_invoice_column(stage3)].astype(str))]
    st.caption(f"Reporting period: **{reporting_period_label}** | Parcel scope: **{parcel_scope}**")
    match_rate = 0.0 if stage2.empty else (stage2.get("est_parcel_count_match", pd.Series(dtype="string")).astype(str).str.upper() == "Y").mean()
    core_metrics = [
        ("Consignments", "consignments"),
        ("Shipped parcels", "shipped_parcels"),
        ("Shipped units", "shipped_units"),
        ("Units per consignment", "units_per_consignment"),
        ("Units per parcel", "units_per_parcel"),
        ("Parcels per consignment", "parcels_per_consignment"),
        ("Cost per consignment", "cost_per_consignment"),
        ("Cost per parcel", "cost_per_parcel"),
        ("Cost per unit", "cost_per_unit"),
        ("Modelled shipped-carrier cost ex GST", "modelled_cost"),
    ]
    for row_metrics in [core_metrics[:5], core_metrics[5:]]:
        cards = st.columns(5)
        for card, (label, metric_key) in zip(cards, row_metrics):
            card.metric(label, _metric_format(metric_key).format(_metric_value(stage3, metric_key)))
    st.caption(f"Parcel-count match for the same invoice set: {match_rate:.1%}. Cost ratios exclude invoices where the modelled carrier cost is missing.")

    st.subheader("Modelled cost by shipped carrier")
    carrier_col = "actual_carrier" if "actual_carrier" in stage3 else "shippit_carrier"
    mix_metric = st.selectbox(
        "Carrier mix measure",
        ["Consignments", "Consignments %", "Parcels", "Parcels %", "Units", "Units %"],
        index=0,
    )
    if reporting_period == "Current accounting week to date":
        wow_note = (
            "For percentage views, the daily and Total rows show carrier share; the WoW row "
            "shows volume change for the selected measure. "
            if mix_metric.endswith(" %")
            else ""
        )
        st.caption(
            f"{wow_note}WoW compares the elapsed weekdays with the same weekdays in the prior "
            "accounting week; future weekdays remain blank."
        )
    comparison_source = comparison_stage3 if comparison_stage3 is not None else pd.DataFrame()
    previous_stage3 = pd.DataFrame()
    elapsed_weekdays = None
    if reporting_period == "Current accounting week to date":
        as_of_date = _current_sydney_date()
        current_week_start = as_of_date - pd.Timedelta(days=as_of_date.weekday())
        previous_week_start = current_week_start - pd.Timedelta(days=7)
        previous_week_end = previous_week_start + pd.Timedelta(days=as_of_date.weekday())
        comparison_dates = pd.to_datetime(
            comparison_source.get(
                "despatch_date", pd.Series(index=comparison_source.index)
            ),
            errors="coerce",
        )
        previous_stage3 = comparison_source.loc[
            comparison_dates.between(previous_week_start, previous_week_end)
        ]
        elapsed_weekdays = as_of_date.weekday()
    moorebank, truganina = st.columns(2)
    for container, site_key, colour in [(moorebank, "MOOREBANK", "#2c8674"), (truganina, "TRUGANINA", "#dc6e3f")]:
        with container:
            site_rows = stage3[stage3["ui_site"] == site_key]
            st.caption(f"{site_key.title()} CFC")
            if site_rows.empty:
                st.info("No refreshed current-week output.")
            else:
                chart_rows = site_rows[[carrier_col]].copy()
                chart_rows["modelled_cost"] = _cost_series(site_rows)
                chart_summary = (
                    chart_rows.assign(
                        carrier=chart_rows[carrier_col].fillna("Unassigned").replace("", "Unassigned")
                    )
                    .groupby("carrier", as_index=False)
                    .agg(
                        modelled_cost=("modelled_cost", "sum"),
                        total_consignments=("modelled_cost", "size"),
                        costed_consignments=("modelled_cost", "count"),
                    )
                    .sort_values("modelled_cost", ascending=False)
                )
                chart = (
                    alt.Chart(chart_summary)
                    .mark_bar()
                    .encode(
                        x=alt.X("carrier:N", title=None, sort="-y"),
                        y=alt.Y(
                            "modelled_cost:Q",
                            title="Modelled cost ex GST",
                            axis=alt.Axis(format="$,.0f"),
                        ),
                        color=alt.value(colour),
                        tooltip=[
                            alt.Tooltip("carrier:N", title="Carrier"),
                            alt.Tooltip(
                                "modelled_cost:Q",
                                title="Modelled cost ex GST",
                                format="$,.2f",
                            ),
                            alt.Tooltip(
                                "costed_consignments:Q", title="Costed consignments", format=",.0f"
                            ),
                            alt.Tooltip(
                                "total_consignments:Q", title="Total consignments", format=",.0f"
                            ),
                        ],
                    )
                    .properties(height=300)
                )
                st.altair_chart(chart, width="stretch")
                incomplete = chart_summary[
                    chart_summary["costed_consignments"] < chart_summary["total_consignments"]
                ]
                if not incomplete.empty:
                    missing_text = "; ".join(
                        f"{row.carrier}: {int(row.total_consignments - row.costed_consignments):,} of "
                        f"{int(row.total_consignments):,} consignments uncosted"
                        for row in incomplete.itertuples()
                    )
                    st.warning(
                        "Some carrier bars are incomplete because the rate model returned no cost: "
                        f"{missing_text}. Review the missing-cost detail before comparing totals."
                    )
                st.subheader(f"Carrier mix ({mix_metric.lower()}) by day")
                site_previous_rows = previous_stage3[
                    previous_stage3["ui_site"] == site_key
                ] if not previous_stage3.empty else pd.DataFrame()
                carrier_mix_dates = pd.to_datetime(
                    site_rows.get("despatch_date"), errors="coerce"
                )
                if carrier_mix_dates.dropna().empty:
                    st.info("No despatch dates are available.")
                else:
                    carrier_mix_table = _carrier_mix_daily_table(
                        site_rows,
                        site_previous_rows,
                        mix_metric,
                        elapsed_weekdays,
                    )
                    if mix_metric.endswith(" %"):
                        carrier_mix_style = carrier_mix_table.style.format(
                            {column: "{:,.1f}%" for column in carrier_mix_table.columns}
                        )
                    else:
                        carrier_mix_style = carrier_mix_table.style.format(
                            {column: "{:,.0f}" for column in carrier_mix_table.columns},
                            subset=pd.IndexSlice[carrier_mix_table.index[:-1], :],
                        ).format(
                            {column: "{:,.1f}%" for column in carrier_mix_table.columns},
                            subset=pd.IndexSlice[["WoW %"], :],
                        )
                    st.dataframe(
                        carrier_mix_style.apply(
                            lambda row: [
                                "font-weight: bold;"
                                if row.name == "Total"
                                else ""
                                for _ in row
                            ],
                            axis=1,
                        ),
                        width="stretch",
                        hide_index=False,
                        column_config={
                            "_index": st.column_config.TextColumn(
                                "Carrier", width="medium"
                            ),
                            **{
                                column: st.column_config.Column(column, width="small")
                                for column in carrier_mix_table.columns
                                if column != "Carrier"
                            },
                        },
                    )

    st.subheader("Carrier-choice opportunities")
    st.caption(
        "These are modelled savings signals using the same Shippit physical basis for the current and next-best carrier. "
        "The screen separates alternatives that pass the published article, dead-weight and volumetric-weight limits from "
        "conditional alternatives that require a rule change. It excludes carriers without a valid ratecard result and GoBUNDLE proxy pricing. "
        "Before acting, investigate each CFC's WHS logic for parcel count and chargeable weight, how WHS configures carrier ratecards and carrier choice, "
        "and whether Shippit's chargeable weight reflects the same basis."
    )
    all_carrier_choice_detail = build_carrier_choice_detail(stage3)
    classification_available = "opportunity_bucket" in all_carrier_choice_detail
    if not all_carrier_choice_detail.empty and not classification_available:
        st.warning(
            "Opportunity eligibility has not been classified in the loaded module. "
            "Restart the Streamlit process before treating any candidate as realizable."
        )
    weight_band_options = ["All weight bands"] + _ordered_values(
        stage3, "reporting_weight_band", REPORTING_WEIGHT_BAND_ORDER
    )
    selected_opportunity_weight_band = st.selectbox(
        "Opportunity weight band",
        weight_band_options,
        index=0,
    )
    if selected_opportunity_weight_band == "All weight bands":
        carrier_choice_detail = all_carrier_choice_detail
        opportunity_scope = stage3
    else:
        detail_bands = (
            all_carrier_choice_detail.get(
                "reporting_weight_band",
                pd.Series(index=all_carrier_choice_detail.index, dtype="string"),
            )
            .fillna("")
            .astype(str)
            .str.strip()
            .replace("", "Unmapped / review required")
        )
        carrier_choice_detail = all_carrier_choice_detail.loc[
            detail_bands.eq(selected_opportunity_weight_band)
        ].copy()
        scope_bands = (
            stage3.get(
                "reporting_weight_band", pd.Series(index=stage3.index, dtype="string")
            )
            .fillna("")
            .astype(str)
            .str.strip()
            .replace("", "Unmapped / review required")
        )
        opportunity_scope = stage3.loc[scope_bands.eq(selected_opportunity_weight_band)]
    opportunity_status_options = [
        "All opportunity statuses",
        "Realizable today",
        "Conditional if rule changed",
        "Needs validation",
    ]
    selected_opportunity_status = st.selectbox(
        "Opportunity status",
        opportunity_status_options,
        index=0,
        help="Realizable today passes the fast checks supported by the persisted Stage 3 fields. Conditional rows breach a published limit.",
    )
    weighted_opportunity_detail = carrier_choice_detail
    if selected_opportunity_status != "All opportunity statuses":
        status_values = weighted_opportunity_detail.get(
            "opportunity_bucket",
            pd.Series("Needs validation", index=weighted_opportunity_detail.index),
        )
        carrier_choice_detail = weighted_opportunity_detail.loc[
            status_values.eq(selected_opportunity_status)
        ].copy()
    rule_condition_source = carrier_choice_detail.get(
        "eligibility_reason",
        pd.Series(index=carrier_choice_detail.index, dtype="string"),
    ).fillna("Missing rule / condition").astype(str).str.strip()
    rule_condition_options = sorted(
        value for value in rule_condition_source.unique() if value
    )
    selected_rule_conditions = st.multiselect(
        "Rule / condition filter",
        rule_condition_options,
        default=[],
        help="Filter the opportunity tables and downloads to consignments with the selected rule or condition.",
    )
    if selected_rule_conditions:
        carrier_choice_detail = carrier_choice_detail.loc[
            rule_condition_source.isin(selected_rule_conditions)
        ].copy()
    opportunity_invoice_column = _invoice_column(opportunity_scope)
    opportunity_invoices = (
        opportunity_scope[opportunity_invoice_column]
        .fillna("")
        .astype(str)
        .str.strip()
    )
    opportunity_denominator = opportunity_invoices[opportunity_invoices.ne("")].nunique()
    candidate_count = len(weighted_opportunity_detail)
    realizable_detail = weighted_opportunity_detail.loc[
        weighted_opportunity_detail.get(
            "opportunity_bucket",
            pd.Series("Needs validation", index=weighted_opportunity_detail.index),
        ).eq("Realizable today")
    ]
    conditional_detail = weighted_opportunity_detail.loc[
        weighted_opportunity_detail.get(
            "opportunity_bucket",
            pd.Series("Needs validation", index=weighted_opportunity_detail.index),
        ).eq("Conditional if rule changed")
    ]
    needs_validation_detail = weighted_opportunity_detail.loc[
        weighted_opportunity_detail.get(
            "opportunity_bucket",
            pd.Series("Needs validation", index=weighted_opportunity_detail.index),
        ).eq("Needs validation")
    ]
    opportunity_rate = (
        candidate_count / opportunity_denominator * 100
        if opportunity_denominator
        else 0.0
    )
    opportunity_metrics = st.columns(5)
    opportunity_metrics[0].metric(
        "Candidate opportunities",
        f"{candidate_count:,}",
    )
    opportunity_metrics[1].metric(
        "Realizable today",
        f"{len(realizable_detail):,}",
        f"{(len(realizable_detail) / candidate_count * 100) if candidate_count else 0:,.1f}% of candidates",
    )
    opportunity_metrics[2].metric(
        "Conditional if rule changed",
        f"{len(conditional_detail):,}",
        f"{(len(conditional_detail) / candidate_count * 100) if candidate_count else 0:,.1f}% of candidates",
    )
    opportunity_metrics[3].metric(
        "Realizable saving",
        f"${realizable_detail['estimated_saving_ex_gst'].sum():,.0f}",
    )
    opportunity_metrics[4].metric(
        "Conditional saving signal",
        f"${conditional_detail['estimated_saving_ex_gst'].sum():,.0f}",
    )
    st.caption(
        f"The candidate opportunity rate is {candidate_count:,} consignments out of "
        f"{opportunity_denominator:,} consignments in the selected weight-band scope ({opportunity_rate:,.1f}%). "
        f"{len(needs_validation_detail):,} candidates need additional physical or carrier-rule validation. "
        "Conditional saving signals are not included in realizable saving. "
        "Status is applied to the persisted Stage 3 cheapest candidate; rerun Stage 3 with eligibility-aware ranking "
        "later if a different eligible second-best carrier is required."
    )
    if carrier_choice_detail.empty:
        st.info("No valid cheaper carrier-choice opportunities are available for this reporting slice.")
    else:
        carrier_choice_summary = build_carrier_choice_summary(carrier_choice_detail)
        opportunity_display = carrier_choice_summary.rename(
            columns={
                "ui_site": "CFC",
                "current_carrier": "Current shipped carrier",
                "next_best_carrier": "Next-best carrier",
                "ap_pricing_zone": "AP pricing zone",
                "opportunity_bucket": "Opportunity status",
                "eligibility_reason": "Rule / condition",
                "eligible_consignments": "Consignments",
                "eligible_parcels": "Parcels",
                "eligible_units": "Units",
                "current_modelled_cost_ex_gst": "Current modelled cost ex GST",
                "next_best_modelled_cost_ex_gst": "Next-best modelled cost ex GST",
                "current_average_cost_per_consignment_ex_gst": "Current Avg Cost per Consignment ex GST",
                "next_best_average_cost_per_consignment_ex_gst": "Next Best Avg Cost per Consignment ex GST",
                "estimated_saving_ex_gst": "Estimated saving ex GST",
                "average_saving_per_consignment_ex_gst": "Average saving / consignment ex GST",
                "estimated_saving_pct": "Saving %",
            }
        )
        st.dataframe(
            opportunity_display.style.format(
                {
                    "Consignments": "{:,.0f}",
                    "Parcels": "{:,.0f}",
                    "Units": "{:,.0f}",
                    "Current modelled cost ex GST": "${:,.2f}",
                    "Next-best modelled cost ex GST": "${:,.2f}",
                    "Current Avg Cost per Consignment ex GST": "${:,.2f}",
                    "Next Best Avg Cost per Consignment ex GST": "${:,.2f}",
                    "Estimated saving ex GST": "${:,.2f}",
                    "Average saving / consignment ex GST": "${:,.2f}",
                    "Saving %": "{:,.1f}%",
                },
                na_rep="-",
            ),
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "Download carrier-choice opportunity summary CSV",
            opportunity_display.to_csv(index=False).encode("utf-8"),
            file_name="carrier_choice_opportunity_summary.csv",
            mime="text/csv",
            width="stretch",
        )
        with st.expander("Consignment-level carrier-choice opportunities"):
            detail_display = carrier_choice_detail.rename(
                columns={
                    "opportunity_id": "Opportunity ID",
                    "ui_site": "CFC",
                    "invoice_number": "Shippit consignment",
                    "despatch_date": "Despatch date",
                    "kmart_accounting_year": "Accounting year",
                    "kmart_accounting_week_number_in_year": "Accounting week",
                    "current_carrier": "Current shipped carrier",
                    "current_service": "Current service",
                    "next_best_carrier": "Next-best carrier",
                    "next_best_service": "Next-best service",
                    "ap_pricing_zone": "AP pricing zone",
                    "reporting_weight_band": "Reporting weight band",
                    "opportunity_bucket": "Opportunity status",
                    "eligibility_reason": "Rule / condition",
                    "article_count_basis": "Article-count basis",
                    "opportunity_article_count": "Checked articles",
                    "opportunity_dead_weight_kg": "Shippit dead weight kg",
                    "opportunity_volumetric_weight_kg": "Shippit volumetric weight kg",
                    "actual_parcel_count": "Parcels",
                    "parcel_units_total": "Units",
                    "current_modelled_cost_ex_gst": "Current modelled cost ex GST",
                    "next_best_modelled_cost_ex_gst": "Next-best modelled cost ex GST",
                    "estimated_saving_ex_gst": "Estimated saving ex GST",
                    "estimated_saving_pct": "Saving %",
                }
            )
            st.caption(
                "Showing the top 100 by saving signal. The download contains every candidate in the selected slice."
            )
            st.dataframe(
                detail_display.head(100).style.format(
                    {
                        "Parcels": "{:,.0f}",
                        "Units": "{:,.0f}",
                        "Current modelled cost ex GST": "${:,.2f}",
                        "Next-best modelled cost ex GST": "${:,.2f}",
                        "Estimated saving ex GST": "${:,.2f}",
                        "Saving %": "{:,.1f}%",
                    },
                    na_rep="-",
                ),
                width="stretch",
                hide_index=True,
            )
            st.download_button(
                "Download consignment-level carrier-choice opportunities CSV",
                detail_display.to_csv(index=False).encode("utf-8"),
                file_name="carrier_choice_opportunities.csv",
                mime="text/csv",
                width="stretch",
            )

    st.subheader("Lane and weight matrix")
    matrix_controls = st.columns((1, 1, 1.5))
    with matrix_controls[0]:
        matrix_cfc = st.selectbox("Matrix CFC", ["Both CFCs", "Moorebank CFC", "Truganina CFC"])
    with matrix_controls[1]:
        matrix_family = st.selectbox("Measure family", ["Volume", "Cost"])
    volume_metrics = [
        "Consignments",
        "Consignments %",
        "Shipped parcels",
        "Shipped parcels %",
        "Shipped units",
        "Shipped units %",
    ]
    cost_metrics = ["Modelled shipped-carrier cost ex GST", "Cost per consignment", "Cost per parcel", "Cost per unit"]
    with matrix_controls[2]:
        selected_metric_label = st.selectbox("Matrix measure", volume_metrics if matrix_family == "Volume" else cost_metrics)
    selected_metric_key = METRIC_OPTIONS[selected_metric_label]
    if selected_metric_label.endswith(" %"):
        st.caption("Percentage values show each cell as a share of the grand total for the selected matrix slice.")
    matrix_rows = stage3
    if matrix_cfc == "Moorebank CFC":
        matrix_rows = matrix_rows[matrix_rows["ui_site"] == "MOOREBANK"]
    elif matrix_cfc == "Truganina CFC":
        matrix_rows = matrix_rows[matrix_rows["ui_site"] == "TRUGANINA"]
    matrix = _slice_matrix(matrix_rows, selected_metric_key)
    st.dataframe(
        matrix.style.format(_metric_format(selected_metric_key), na_rep="-").apply(
            lambda row: [
                "font-weight: bold;" if row.name == "Total" else ""
                for _ in row
            ],
            axis=1,
        ),
        width="stretch",
    )
    st.download_button(
        "Download matrix CSV",
        matrix.to_csv().encode("utf-8"),
        file_name=f"last_mile_{reporting_period_label.lower().replace(' ', '_')}_{matrix_cfc.lower().replace(' ', '_')}_{selected_metric_key}_matrix.csv",
        mime="text/csv",
    )

    st.caption("Current-week parcelizer comparison against Shippit")
    if not stage2.empty:
        gap = _number(stage2, "est_weight_gap_vs_shippit_chargeable_kg").abs()
        residual = _number(stage2, "est_rectangularized_air_ratio_pct_vs_shippit_cube").abs()
        st.dataframe(pd.DataFrame({"Median": [gap.median(), residual.median()], "P95": [gap.quantile(.95), residual.quantile(.95)]}, index=["Absolute chargeable gap kg", "Absolute cube residual %"]), width="stretch")

def _shippit_cube_cm3(invoice: pd.Series) -> float:
    """Use the recorded cube, with a volumetric fallback for older exports."""
    for column in ("shippit_chargeable_cube_cm3", "shippit_volumetric_cube_cm3"):
        value = pd.to_numeric(pd.Series([invoice.get(column)]), errors="coerce").iloc[0]
        if pd.notna(value) and value > 0:
            return float(value)
    volumetric_weight = pd.to_numeric(
        pd.Series([invoice.get("shippit_volumetric_weight_kg")]), errors="coerce"
    ).iloc[0]
    return float(volumetric_weight * 4000.0) if pd.notna(volumetric_weight) and volumetric_weight > 0 else 0.0


def _rbu_size_matrix(assignment_lines: pd.DataFrame, invoice_number: str) -> pd.DataFrame:
    if assignment_lines.empty or "invoice_number" not in assignment_lines:
        return pd.DataFrame()
    lines = assignment_lines[assignment_lines["invoice_number"].astype(str) == invoice_number].copy()
    required = {"quantity_packed", "size_category", "rbu_description"}
    if lines.empty or not required.issubset(lines.columns):
        return pd.DataFrame()

    category = lines["size_category"].fillna("No dims").astype(str).str.strip()
    lines["size_category"] = category.where(
        category.isin(["Toteable", "Non-Toteable", "Bulky"]), "No dims"
    )
    lines["rbu_description"] = lines["rbu_description"].fillna("Unclassified").replace("", "Unclassified")
    lines["quantity_packed"] = pd.to_numeric(lines["quantity_packed"], errors="coerce").fillna(0.0)
    matrix = lines.pivot_table(
        index="rbu_description",
        columns="size_category",
        values="quantity_packed",
        aggfunc="sum",
        fill_value=0.0,
    )
    matrix = matrix.reindex(columns=["Toteable", "Non-Toteable", "Bulky", "No dims"], fill_value=0.0)
    matrix["Total"] = matrix.sum(axis=1)
    return matrix.reset_index().rename(columns={"rbu_description": "RBU description"})


def _keycode_detail(assignment_lines: pd.DataFrame, invoice_number: str) -> pd.DataFrame:
    if assignment_lines.empty or "invoice_number" not in assignment_lines:
        return pd.DataFrame()
    lines = assignment_lines[assignment_lines["invoice_number"].astype(str) == invoice_number].copy()
    required = {"sku_id", "quantity_packed"}
    if lines.empty or not required.issubset(lines.columns):
        return pd.DataFrame()

    numeric_columns = [
        "quantity_packed",
        "cfc_height_cm",
        "cfc_width_cm",
        "cfc_length_cm",
        "cfc_weight_kg",
        "raw_packed_sku_cube_cm3_per_line",
    ]
    for column in numeric_columns:
        if column in lines:
            lines[column] = pd.to_numeric(lines[column], errors="coerce")
    for column, default in {"product_description": "", "rbu_description": "Unclassified"}.items():
        if column not in lines:
            lines[column] = default
        lines[column] = lines[column].fillna(default).replace("", default or "Description unavailable")

    detail = (
        lines.groupby(["sku_id", "product_description", "rbu_description"], dropna=False, as_index=False)
        .agg(
            quantity=("quantity_packed", "sum"),
            height_cm=("cfc_height_cm", "first"),
            width_cm=("cfc_width_cm", "first"),
            length_cm=("cfc_length_cm", "first"),
            weight_kg=("cfc_weight_kg", "first"),
            packed_cube_m3=("raw_packed_sku_cube_cm3_per_line", lambda values: values.sum() / 1_000_000),
        )
        .sort_values(["quantity", "sku_id"], ascending=[False, True])
        .rename(
            columns={
                "sku_id": "Keycode",
                "product_description": "Keycode description",
                "rbu_description": "RBU description",
                "quantity": "Quantity packed",
                "height_cm": "Height cm",
                "width_cm": "Width cm",
                "length_cm": "Length cm",
                "weight_kg": "Weight kg",
                "packed_cube_m3": "Packed cube m3",
            }
        )
    )
    return detail


def _dimension_text(frame: pd.DataFrame, columns: tuple[str, str, str], decimals: int = 1) -> str:
    if frame.empty or not all(column in frame for column in columns):
        return "Not available"
    values = frame.loc[:, list(columns)].apply(pd.to_numeric, errors="coerce").dropna(how="any").drop_duplicates()
    if values.empty:
        return "Not available"
    formatted = [
        " x ".join(f"{value:.{decimals}f}" for value in row)
        for row in values.itertuples(index=False, name=None)
    ]
    return f"{formatted[0]} cm" if len(formatted) == 1 else f"Multiple values ({len(formatted)})"


def _first_numeric(frame: pd.DataFrame, columns: tuple[str, ...]) -> float | None:
    for column in columns:
        if column not in frame:
            continue
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if not values.empty:
            return float(values.iloc[0])
    return None


def _numeric_sum(frame: pd.DataFrame, column: str) -> float | None:
    if column not in frame:
        return None
    values = pd.to_numeric(frame[column], errors="coerce")
    return float(values.sum()) if values.notna().any() else None


def _format_measure(value: float | None, unit: str, decimals: int = 2) -> str:
    return "Not available" if value is None else f"{value:,.{decimals}f} {unit}"


def _allocation_physical_evidence(
    invoice: pd.Series,
    physicals: pd.DataFrame,
    assignment_lines: pd.DataFrame,
    reconciliation_rows: pd.DataFrame,
    plm_dimensions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build lifecycle-labelled evidence without inferring missing snapshots.

    The post-pack article rows and the pre-pack carrier-allocation inputs are
    different events. Keeping that distinction visible prevents the explorer
    from presenting a later packing result as proof of the original decision.
    """
    rows: list[dict[str, str]] = []

    assigned = assignment_lines.copy()
    if "invoice_number" in assigned:
        selected = _normalise_consignment_number(invoice.get("invoice_number"))
        assigned = assigned[assigned["invoice_number"].map(_normalise_consignment_number).eq(selected)]

    assignment_sources = (
        sorted({str(value).strip() for value in assigned.get("assignment_source", pd.Series(dtype="string")) if str(value).strip()})
        or ["Not available"]
    )
    pack_timestamps = pd.to_datetime(assigned.get("pack_timestamp"), errors="coerce") if "pack_timestamp" in assigned else pd.Series(dtype="datetime64[ns]")
    timestamp_text = "Not available"
    valid_timestamps = pack_timestamps.dropna()
    if not valid_timestamps.empty:
        timestamp_text = f"{valid_timestamps.min():%Y-%m-%d %H:%M:%S} to {valid_timestamps.max():%H:%M:%S}"
    assigned_quantities = pd.to_numeric(assigned.get("quantity_packed"), errors="coerce").fillna(0) if "quantity_packed" in assigned else pd.Series(dtype="float64")
    assigned_weight = None
    if "cfc_weight_kg" in assigned:
        weights = pd.to_numeric(assigned["cfc_weight_kg"], errors="coerce")
        assigned_weight = float((weights * assigned_quantities).sum()) if weights.notna().any() else None
    assigned_cube = _numeric_sum(assigned, "raw_packed_sku_cube_cm3_per_line")
    assignment_pattern = "|".join(str(value) for value in assigned_quantities.astype(int).tolist()) if not assigned.empty else "Not available"
    source_packages = (
        assigned["source_package_id"].dropna().astype(str).replace("", pd.NA).dropna().nunique()
        if "source_package_id" in assigned
        else 0
    )
    rows.append(
        {
            "Evidence stage": "Current master data / post-pack linkage",
            "Source": "Latest CFC SKU_ATTRIBUTE record",
            "Dimensions": _dimension_text(assigned, ("cfc_height_cm", "cfc_width_cm", "cfc_length_cm")),
            "Weight / cube": f"{_format_measure(assigned_weight, 'kg')} / {_format_measure(None if assigned_cube is None else assigned_cube / 1_000_000, 'm3', 6)}",
            "Allocation / parcel evidence": f"Stage 1 package link: {source_packages or 'Not available'} source package(s); packed units {assignment_pattern}",
            "Timestamp": timestamp_text,
            "Status": f"Available; dimensions come from the latest KSFDA.CATCH_CFC.SKU_ATTRIBUTE record and package grouping comes from {', '.join(assignment_sources)}. This is not proof of the pre-pack WHS allocation-time snapshot.",
        }
    )

    plm = plm_dimensions.copy() if plm_dimensions is not None else pd.DataFrame()
    plm_source = "PLM keycode master (KEYCODE_CARTON_DSS_DIMENSIONS_VW)"
    plm_status = "Not available; no PLM dimension report is available or no assigned keycode matched it."
    plm_dimensions_text = "Not available"
    plm_weight_cube_text = "Not available"
    plm_allocation_text = "No matched PLM keycode(s)"
    if not plm.empty and "sku_id" in plm and "sku_id" in assigned:
        assigned_for_plm = assigned[["sku_id", "quantity_packed"]].copy()
        assigned_for_plm["sku_id"] = assigned_for_plm["sku_id"].astype("string").str.strip()
        assigned_for_plm["quantity_packed"] = pd.to_numeric(
            assigned_for_plm["quantity_packed"], errors="coerce"
        ).fillna(0.0)
        quantities = assigned_for_plm.groupby("sku_id", as_index=False)["quantity_packed"].sum()
        plm["sku_id"] = plm["sku_id"].astype("string").str.strip()
        plm = plm.drop_duplicates("sku_id", keep="first")
        plm = quantities.merge(plm, on="sku_id", how="left")
        dimension_columns = [
            "plm_shortest_cm",
            "plm_median_cm",
            "plm_longest_cm",
        ]
        measure_columns = [
            *dimension_columns,
            "plm_instore_gross_weight_kg",
            "plm_gross_cube_cm3",
            "plm_volumetric_weight_kg",
        ]
        for column in measure_columns:
            if column not in plm:
                plm[column] = pd.NA
            plm[column] = pd.to_numeric(plm[column], errors="coerce")
        usable = plm[plm[dimension_columns].notna().all(axis=1)].copy()
        if not usable.empty:
            dimension_values = usable[dimension_columns].drop_duplicates()
            dimension_strings = [
                " x ".join(f"{value:.1f}" for value in row) + " cm (shortest x median x longest)"
                for row in dimension_values.itertuples(index=False, name=None)
            ]
            plm_dimensions_text = "; ".join(dimension_strings)
            if len(dimension_strings) > 1:
                plm_dimensions_text += f" ({len(dimension_strings)} PLM dimension sets)"
            quantity = usable["quantity_packed"]
            plm_weight_total = (
                usable["plm_instore_gross_weight_kg"] * quantity
            ).sum(min_count=1)
            plm_cube_total = (usable["plm_gross_cube_cm3"] * quantity).sum(min_count=1)
            plm_volumetric_total = (
                usable["plm_volumetric_weight_kg"] * quantity
            ).sum(min_count=1)
            plm_unit_weight = _first_numeric(
                usable, ("plm_instore_gross_weight_kg",)
            )
            plm_unit_cube = _first_numeric(usable, ("plm_gross_cube_cm3",))
            plm_weight_cube_text = (
                f"{_format_measure(plm_unit_weight, 'kg')} per unit; "
                f"{_format_measure(None if pd.isna(plm_weight_total) else float(plm_weight_total), 'kg')} total / "
                f"{_format_measure(None if plm_unit_cube is None else plm_unit_cube / 1_000_000, 'm3', 6)} per unit; "
                f"{_format_measure(None if pd.isna(plm_cube_total) else float(plm_cube_total) / 1_000_000, 'm3', 6)} total"
            )
            if pd.notna(plm_volumetric_total):
                plm_weight_cube_text += f"; PLM volumetric {float(plm_volumetric_total):,.2f} kg total"
            plm_allocation_text = (
                f"PLM matched {len(usable):,} of {len(plm):,} assigned keycode(s); "
                f"{int(usable['quantity_packed'].sum()):,} packed unit(s) represented"
            )
            plm_status = (
                "Available as a product-master counterpoint. These PLM values are not substituted "
                "into the current Stage 1/2 estimate and are not proof of the dimensions used by WHS or Shippit."
            )
            if assigned_cube is not None and pd.notna(plm_cube_total) and plm_cube_total > 0:
                cube_gap_pct = (assigned_cube / float(plm_cube_total) - 1.0) * 100.0
                plm_status += f" CFC cube versus PLM cube: {cube_gap_pct:+.1f}%."
            if assigned_weight is not None and pd.notna(plm_weight_total) and plm_weight_total > 0:
                weight_gap_pct = (assigned_weight / float(plm_weight_total) - 1.0) * 100.0
                plm_status += f" CFC weight versus PLM weight: {weight_gap_pct:+.1f}%."

    rows.append(
        {
            "Evidence stage": "Product master counterpoint",
            "Source": plm_source,
            "Dimensions": plm_dimensions_text,
            "Weight / cube": plm_weight_cube_text,
            "Allocation / parcel evidence": plm_allocation_text,
            "Timestamp": "Latest available PLM record",
            "Status": plm_status,
        }
    )

    rows.append(
        {
            "Evidence stage": "Pre-pack carrier allocation",
            "Source": "WHS allocation-time snapshot",
            "Dimensions": "Not available",
            "Weight / cube": "Not available",
            "Allocation / parcel evidence": "Pre-pack input dimensions, weight, decision and selected carrier/service are not persisted",
            "Timestamp": "Not available",
            "Status": "Not available; the original WHS allocation decision cannot be inferred from the later WMS or Shippit records.",
        }
    )

    shippit_dead = _first_numeric(pd.DataFrame([invoice]), ("shippit_dead_weight_kg",))
    shippit_volumetric = _first_numeric(pd.DataFrame([invoice]), ("shippit_volumetric_weight_kg",))
    shippit_chargeable = _first_numeric(pd.DataFrame([invoice]), ("shippit_chargeable_weight_kg",))
    shippit_volumetric_cube = _first_numeric(pd.DataFrame([invoice]), ("shippit_volumetric_cube_cm3",))
    shippit_chargeable_cube = _first_numeric(pd.DataFrame([invoice]), ("shippit_chargeable_cube_cm3",))
    shippit_parcels = _first_numeric(pd.DataFrame([invoice]), ("shippit_parcel_count", "actual_parcel_count"))
    shippit_cube_text = "Not available"
    if shippit_volumetric_cube is not None or shippit_chargeable_cube is not None:
        parts = []
        if shippit_volumetric_cube is not None:
            parts.append(f"volumetric {_format_measure(shippit_volumetric_cube / 1_000_000, 'm3', 6)}")
        if shippit_chargeable_cube is not None:
            parts.append(f"chargeable-equivalent {_format_measure(shippit_chargeable_cube / 1_000_000, 'm3', 6)}")
        shippit_cube_text = "; ".join(parts)
    shippit_weight_parts = []
    if shippit_dead is not None:
        shippit_weight_parts.append(f"dead {_format_measure(shippit_dead, 'kg')}")
    if shippit_volumetric is not None:
        shippit_weight_parts.append(f"volumetric {_format_measure(shippit_volumetric, 'kg')}")
    if shippit_chargeable is not None:
        shippit_weight_parts.append(f"chargeable {_format_measure(shippit_chargeable, 'kg')}")
    rows.append(
        {
            "Evidence stage": "Shippit tracking / Catch article payload",
            "Source": "Shippit / Catch webhook",
            "Dimensions": "Article dimensions not persisted",
            "Weight / cube": f"{'; '.join(shippit_weight_parts) or 'Not available'} / {shippit_cube_text}",
            "Allocation / parcel evidence": f"{shippit_parcels:,.0f} Shippit parcel(s); timing relative to WHS allocation is not confirmed" if shippit_parcels is not None else "Parcel count not available",
            "Timestamp": str(invoice.get("despatch_date") or "Not available"),
            "Status": "Aggregate only; the Track Only design identifies article fields as coming from the Catch webhook, but raw article dimensions and the allocation timestamp are not persisted here.",
        }
    )

    estimated_weight = _first_numeric(pd.DataFrame([invoice]), ("est_packed_sku_weight_kg",))
    estimated_cube = _first_numeric(pd.DataFrame([invoice]), ("est_parcel_cube_total_m3",))
    estimated_chargeable = _first_numeric(pd.DataFrame([invoice]), ("est_chargeable_weight_kg",))
    estimated_dimensions = _dimension_text(
        physicals,
        ("estimated_height_cm", "estimated_width_cm", "estimated_length_cm"),
    )
    rows.append(
        {
            "Evidence stage": "Estimator output",
            "Source": "Estimator / Stage 2",
            "Dimensions": estimated_dimensions,
            "Weight / cube": f"packed {_format_measure(estimated_weight, 'kg')}; chargeable {_format_measure(estimated_chargeable, 'kg')} / {_format_measure(estimated_cube, 'm3', 6)}",
            "Allocation / parcel evidence": f"{len(physicals):,} Stage 2 parcel row(s)" if not physicals.empty else "Not available",
            "Timestamp": timestamp_text,
            "Status": "Available; calculated from the current Stage 1/2 outputs.",
        }
    )

    if reconciliation_rows.empty:
        rows.append(
            {
                "Evidence stage": "Invoice validation",
                "Source": "Invoiced actual",
                "Dimensions": "Not available",
                "Weight / cube": "Not available",
                "Allocation / parcel evidence": "No matched invoice record",
                "Timestamp": "Not available",
                "Status": "Not available; invoice coverage has not reached this consignment.",
            }
        )
    else:
        snapshot = _reconciliation_snapshot(reconciliation_rows)
        billed_cube = snapshot.get("cube_m3")
        rows.append(
            {
                "Evidence stage": "Invoice validation",
                "Source": "Invoiced actual",
                "Dimensions": "Carrier dimensions not available in current reconciliation output",
                "Weight / cube": f"chargeable {_format_measure(snapshot.get('chargeable_weight_kg'), 'kg')}; volumetric {_format_measure(snapshot.get('volumetric_weight_kg'), 'kg')} / {_format_measure(billed_cube, 'm3', 6)}",
                "Allocation / parcel evidence": f"{len(reconciliation_rows):,} matched invoice row(s)",
                "Timestamp": str(_first_value(reconciliation_rows.iloc[0], ("service_date", "billing_date")) or "Not available"),
                "Status": "Available; article-level carrier dimensions are not persisted in this reconciliation output.",
            }
        )

    return pd.DataFrame(rows)


def _refresh_consignment_evidence(
    site: str,
    invoice_number: str,
    despatch_date: object,
) -> dict[str, object]:
    """Run a targeted Snowflake refresh and return its small result files in memory."""
    dispatch_day = pd.to_datetime(despatch_date, errors="coerce")
    if pd.isna(dispatch_day):
        raise ValueError("The saved consignment does not have a usable despatch date.")

    with tempfile.TemporaryDirectory(prefix="lastmile_consignment_") as temporary_directory:
        command = [
            sys.executable,
            "-m",
            "src.consignment_refresh",
            "--invoice-number",
            invoice_number,
            "--site",
            site,
            "--despatch-date",
            dispatch_day.date().isoformat(),
            "--cache-directory",
            temporary_directory,
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "No error details were returned.").strip()
            raise RuntimeError(detail[-1600:])

        site_slug = site.lower()
        safe_invoice = re.sub(r"[^A-Za-z0-9_.-]+", "_", invoice_number).strip("._") or "consignment"
        output_directory = Path(temporary_directory) / f"{site_slug}_{safe_invoice}"

        def load_output(name: str) -> pd.DataFrame:
            path = output_directory / name
            if not path.exists():
                return pd.DataFrame()
            return pd.read_csv(
                path,
                low_memory=False,
                dtype={
                    "invoice_number": "string",
                    "shippit_invoice_number": "string",
                    "carrier_tracking_number": "string",
                },
            )

        stage3 = load_output(f"stage3_{site_slug}_cost_estimates_full.csv")
        if _stage3_invoice_matches(stage3, invoice_number).empty:
            raise RuntimeError(
                "Snowflake returned no Stage 3 estimate for this consignment. "
                "The saved history remains available for comparison."
            )
        return {
            "stage3": stage3,
            "physicals": load_output(f"stage2_{site_slug}_parcel_physicals_full.csv"),
            "assignment_lines": load_output(f"stage1_{site_slug}_parcel_assignment_lines.csv"),
        }


def _invoice_explorer(
    site: str,
    stage3: pd.DataFrame,
    physicals: pd.DataFrame,
    assignment_lines: pd.DataFrame,
    plm_dimensions: pd.DataFrame,
) -> None:
    st.subheader("Consignment explorer")
    search_key = f"consignment_search_{site}"
    active_key = f"active_consignment_search_{site}"
    with st.form(f"consignment_search_form_{site}"):
        st.text_input(
            "Shippit consignment number",
            key=search_key,
            help="Searches the durable shipment history. Excel-style values such as 5.801700011E+09 are accepted.",
        )
        submitted = st.form_submit_button("Find saved consignment")
    if submitted:
        st.session_state[active_key] = _normalise_consignment_number(
            st.session_state.get(search_key, "")
        )
    selected = _normalise_consignment_number(st.session_state.get(active_key, ""))
    if not selected:
        st.caption("Enter a Shippit consignment number to look it up in the saved history.")
        return

    history_path = ALL_CARRIER_HISTORY_STORE / "consignment_invoice_coverage.csv"
    history_matches = pd.DataFrame()
    if history_path.exists():
        history_stat = history_path.stat()
        history_matches = _saved_consignment_history(
            str(history_path),
            history_stat.st_mtime_ns,
            history_stat.st_size,
            site,
            selected,
        )
    history_source = "saved shipment history"
    invoice_matches = history_matches
    if invoice_matches.empty:
        current_path = SITE_FILES[site]["stage3"]
        if current_path.exists():
            current_stat = current_path.stat()
            invoice_matches = _current_stage3_consignment(
                str(current_path),
                current_stat.st_mtime_ns,
                current_stat.st_size,
                site,
                selected,
            )
            history_source = "current operational Stage 3 output"
    if invoice_matches.empty:
        st.warning(f"No consignment was found for `{selected}` in {site.title()} history or current Stage 3 output.")
        return
    stored_invoice = invoice_matches.iloc[0].copy()
    st.caption(f"Baseline source: {history_source}.")
    invoice = stored_invoice.copy()
    fresh_key = f"fresh_consignment_evidence_{site}"
    fresh_result = st.session_state.get(fresh_key)
    if not isinstance(fresh_result, dict) or fresh_result.get("invoice_number") != selected:
        fresh_result = None

    if st.button(
        "Fetch fresh consignment evidence from Snowflake",
        key=f"refresh_consignment_evidence_{site}",
        help="Re-runs the existing Stage 1 to 3 logic for this consignment and keeps the result in this browser session only.",
    ):
        st.session_state.pop(fresh_key, None)
        try:
            with st.spinner("Querying Snowflake and recalculating this consignment..."):
                fresh_outputs = _refresh_consignment_evidence(
                    site,
                    selected,
                    stored_invoice.get("despatch_date"),
                )
            fresh_result = {
                **fresh_outputs,
                "invoice_number": selected,
                "queried_at": pd.Timestamp.now(tz="Australia/Sydney").strftime(
                    "%d %b %Y %H:%M %Z"
                ),
            }
            st.session_state[fresh_key] = fresh_result
        except Exception as error:
            st.error(f"Fresh consignment query failed: {error}")

    if fresh_result is None:
        fresh_result = st.session_state.get(fresh_key)
        if not isinstance(fresh_result, dict) or fresh_result.get("invoice_number") != selected:
            fresh_result = None
    if fresh_result:
        fresh_rows = _stage3_invoice_matches(fresh_result["stage3"], selected)
        if not fresh_rows.empty:
            fresh_invoice = fresh_rows.iloc[0]
            for column, value in fresh_invoice.items():
                if pd.notna(value) and str(value).strip():
                    invoice[column] = value
            physicals = fresh_result["physicals"]
            assignment_lines = fresh_result["assignment_lines"]

    saved_estimate = pd.to_numeric(
        pd.Series([stored_invoice.get("actual_est_cost_ex_gst")]), errors="coerce"
    ).iloc[0]
    reconciliation_path = _reconciliation_path(_normalise_lookup(invoice.get("actual_carrier")))
    reconciliation_rows = (
        _find_reconciliation_rows(
            str(reconciliation_path),
            selected,
            _normalise_lookup(invoice.get("carrier_tracking_number")),
            reconciliation_path.stat().st_mtime_ns if reconciliation_path.exists() else None,
        )
        if reconciliation_path is not None
        else pd.DataFrame()
    )
    reconciliation = _reconciliation_snapshot(reconciliation_rows)

    st.markdown('<div class="panel">Start with what Shippit recorded for this consignment, compare it to the parcelizer estimate, then review the carrier cost decision and parcel contents. The pricing cube is normalised to Shippit for carrier comparison.</div>', unsafe_allow_html=True)
    context = st.columns(5)
    context[0].markdown(f"**From**  \n{str(invoice.get('ui_site') or site).title()} CFC")
    context[1].markdown(f"**To**  \n{invoice.get('dest_suburb') or '-'}, {invoice.get('dest_state') or '-'} {invoice.get('dest_postcode') or ''}")
    context[2].markdown(f"**Shipped on**  \n{invoice.get('actual_carrier') or '-'} | {invoice.get('actual_service') or '-'}")
    accounting_week = f"FY{invoice.get('kmart_accounting_year') or '-'} W{invoice.get('kmart_accounting_week_number_in_year') or '-'}"
    context[3].markdown(f"**Carrier tracking number**  \n{invoice.get('carrier_tracking_number') or '-'}")
    context[4].markdown(f"**Despatch and reporting week**  \n{invoice.get('despatch_date') or '-'} | {accounting_week}")
    if fresh_result:
        current_estimate = pd.to_numeric(
            pd.Series([invoice.get("actual_est_cost_ex_gst")]), errors="coerce"
        ).iloc[0]
        format_cost = lambda amount: "Unavailable" if pd.isna(amount) else f"${amount:,.2f}"
        comparison = st.columns(3)
        comparison[0].metric("Saved estimate baseline ex GST", format_cost(saved_estimate))
        comparison[1].metric("Fresh Snowflake estimate ex GST", format_cost(current_estimate))
        estimate_delta = (
            current_estimate - saved_estimate
            if pd.notna(current_estimate) and pd.notna(saved_estimate)
            else float("nan")
        )
        comparison[2].metric(
            "Fresh less saved estimate",
            format_cost(estimate_delta),
        )
        st.caption(
            "Fresh Stage 1–3 evidence queried from Snowflake at "
            f"{fresh_result.get('queried_at', 'unknown time')}. "
            "The saved estimate remains the historical baseline."
        )

    st.subheader("What shipped")
    physical_invoice_rows = pd.DataFrame()
    if not physicals.empty:
        physical_invoice_rows = _stage3_invoice_matches(physicals, selected)
    packed_sku_cube_cm3 = _number(physical_invoice_rows, "raw_packed_sku_cube_cm3").sum()
    if not packed_sku_cube_cm3:
        packed_sku_cube_cm3 = float(
            pd.to_numeric(pd.Series([invoice.get("est_parcel_cube_total_cm3")]), errors="coerce")
            .fillna(0.0)
            .iloc[0]
        )
    shippit_cube_cm3 = _shippit_cube_cm3(invoice)

    def value(column: str) -> float:
        return float(pd.to_numeric(pd.Series([invoice.get(column)]), errors="coerce").fillna(0.0).iloc[0])

    shipped = st.columns(4)
    shipped[0].metric("Shippit parcels", f"{_number(pd.DataFrame([invoice]), 'actual_parcel_count').iloc[0]:,.0f}")
    shipped[1].metric("Packed units", f"{_number(pd.DataFrame([invoice]), 'parcel_units_total').iloc[0]:,.0f}")
    shipped[2].metric("Packed SKU cube", f"{packed_sku_cube_cm3 / 1_000_000:.3f} m3")
    shipped[3].metric("Shippit chargeable cube", f"{shippit_cube_cm3 / 1_000_000:.3f} m3")

    rbu_matrix = _rbu_size_matrix(assignment_lines, selected)
    if not rbu_matrix.empty:
        st.caption("Packed units by RBU and size category")
        rbu_unit_columns = [column for column in rbu_matrix.columns if column != "RBU description"]
        st.dataframe(
            rbu_matrix.style.format({column: "{:.0f}" for column in rbu_unit_columns}).set_properties(
                subset=["Total"], **{"font-weight": "700"}
            ),
            width="stretch",
            hide_index=True,
        )
    keycode_detail = _keycode_detail(assignment_lines, selected)
    if not keycode_detail.empty:
        with st.expander("Keycode detail"):
            st.dataframe(
                keycode_detail.style.format(
                    {
                        "Quantity packed": "{:.0f}",
                        "Height cm": "{:.1f}",
                        "Width cm": "{:.1f}",
                        "Length cm": "{:.1f}",
                        "Weight kg": "{:.2f}",
                        "Packed cube m3": "{:.3f}",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

    st.subheader("Allocation and physical-basis evidence")
    st.caption("Available evidence is shown by source. Aggregate-only and unavailable fields are deliberately not inferred.")
    evidence = _allocation_physical_evidence(
        invoice,
        physical_invoice_rows,
        assignment_lines,
        reconciliation_rows,
        plm_dimensions,
    )
    st.dataframe(evidence, width="stretch", hide_index=True)

    st.subheader("Actual invoice evidence")
    if reconciliation_path is None:
        st.info(f"No reconciliation feed is configured for {invoice.get('actual_carrier') or 'this carrier'}.")
    elif not reconciliation_path.exists():
        st.info(f"The reconciliation output is not available yet: `{reconciliation_path.name}`.")
    elif not reconciliation:
        st.info("No matched carrier invoice record was found for this invoice. The Stage 3 estimate is still shown above.")
    else:
        st.caption(f"Matched {len(reconciliation_rows):,} reconciliation record(s) from `{reconciliation_path.name}`.")

        def money(value: object, decimals: int = 0) -> str:
            return "-" if value is None else f"${float(value):,.{decimals}f}"

        actual_cost_cards = st.columns(5)
        actual_cost_cards[0].metric("Actual invoice cost ex GST", money(reconciliation.get("total_cost_ex_gst")))
        actual_cost_cards[1].metric("Base freight ex GST", money(reconciliation.get("base_cost_ex_gst")))
        actual_cost_cards[2].metric("Fuel surcharge ex GST", money(reconciliation.get("fuel_cost_ex_gst")))
        actual_cost_cards[3].metric("Other surcharges ex GST", money(reconciliation.get("other_cost_ex_gst")))
        actual_cost = reconciliation.get("total_cost_ex_gst")
        modelled_cost = value("actual_est_cost_ex_gst")
        actual_cost_cards[4].metric("Actual less modelled", money(None if actual_cost is None else actual_cost - modelled_cost))
        if reconciliation.get("fuel_rate_pct") is not None:
            st.caption(f"Carrier fuel surcharge rate in the reconciliation: **{float(reconciliation['fuel_rate_pct']):.1f}%**.")
        if reconciliation.get("primary_cost_driver") or reconciliation.get("reconciliation_note"):
            st.markdown(
                f"**Reconciliation assessment:** {reconciliation.get('primary_cost_driver') or 'Not classified'}"
                f". {reconciliation.get('reconciliation_note') or ''}"
            )

        reconciliation_columns = [
            column
            for column in [
                "match_method",
                "actual_service_name",
                "actual_service_material",
                "actual_chargeable_weight_kg",
                "actual_rounded_chargeable_weight_kg",
                "actual_billed_cube_m3",
                "actual_total_volume_m3",
                "actual_cubic_volume_m3",
                "actual_base_cost_ex_gst",
                "actual_fuel_surcharge_ex_gst",
                "actual_other_surcharges_ex_gst",
                "actual_invoice_cost_ex_gst",
                "actual_fuel_rate_pct",
                "primary_cost_driver",
                "root_cause",
                "reconciliation_note",
            ]
            if column in reconciliation_rows
        ]
        if reconciliation_columns:
            with st.expander("Matched reconciliation detail"):
                st.dataframe(reconciliation_rows[reconciliation_columns], width="stretch", hide_index=True)

    estimator_vs_shippit = pd.DataFrame(
        [
            (
                "Dead / packed SKU weight kg",
                value("est_packed_sku_weight_kg"),
                value("shippit_dead_weight_kg"),
                reconciliation.get("declared_weight_kg"),
            ),
            (
                "Volumetric weight kg",
                value("est_volumetric_weight_kg"),
                value("shippit_volumetric_weight_kg"),
                reconciliation.get("volumetric_weight_kg"),
            ),
            (
                "Chargeable weight kg",
                value("est_chargeable_weight_kg"),
                value("shippit_chargeable_weight_kg"),
                reconciliation.get("chargeable_weight_kg"),
            ),
            (
                "Parcel cube m3",
                value("est_parcel_cube_total_cm3") / 1_000_000,
                shippit_cube_cm3 / 1_000_000,
                reconciliation.get("cube_m3"),
            ),
        ],
        columns=["Measure", "Parcelizer estimate", "Shippit recorded", "Carrier invoice"],
    )
    estimator_vs_shippit["Parcelizer less Shippit"] = estimator_vs_shippit["Parcelizer estimate"] - estimator_vs_shippit["Shippit recorded"]
    estimator_vs_shippit["Carrier less Shippit"] = estimator_vs_shippit["Carrier invoice"] - estimator_vs_shippit["Shippit recorded"]
    st.subheader("Weight and cube: parcelizer versus Shippit")
    numeric_weight_cube_columns = [
        "Parcelizer estimate",
        "Shippit recorded",
        "Carrier invoice",
        "Parcelizer less Shippit",
        "Carrier less Shippit",
    ]
    for column in numeric_weight_cube_columns:
        estimator_vs_shippit[column] = pd.to_numeric(estimator_vs_shippit[column], errors="coerce")
    weight_cube_style = estimator_vs_shippit.style.format(
        {column: "{:+.2f}" if "less" in column.lower() else "{:.2f}" for column in numeric_weight_cube_columns},
        na_rep="-",
    ).format(
        {column: "{:+.3f}" if "less" in column.lower() else "{:.3f}" for column in numeric_weight_cube_columns},
        subset=pd.IndexSlice[estimator_vs_shippit.index[estimator_vs_shippit["Measure"] == "Parcel cube m3"], numeric_weight_cube_columns],
    )
    st.dataframe(weight_cube_style, width="stretch", hide_index=True)
    chargeable_gap = abs(value("est_chargeable_weight_kg") - value("shippit_chargeable_weight_kg"))
    shippit_chargeable = value("shippit_chargeable_weight_kg")
    if chargeable_gap >= max(2.0, shippit_chargeable * 0.10):
        st.warning(f"Chargeable-weight variance requires review: parcelizer differs from Shippit by {chargeable_gap:.2f} kg.")
    else:
        st.success(f"Chargeable-weight variance is within the initial review tolerance: {chargeable_gap:.2f} kg.")

    st.subheader("Carrier cost decision")
    costs = st.columns(3)
    estimate_label = (
        "Current Snowflake estimate ex GST"
        if fresh_result
        else "Saved modelled shipped-carrier cost ex GST"
    )
    costs[0].metric(estimate_label, f"${value('actual_est_cost_ex_gst'):,.2f}")
    costs[0].markdown(
        f"<div style='color:#2c8674; font-weight:700; font-size:0.88rem; margin-top:-0.35rem'>{invoice.get('actual_carrier') or '-'} | {invoice.get('actual_service') or '-'}</div>",
        unsafe_allow_html=True,
    )
    alternative_cost = pd.to_numeric(
        pd.Series([invoice.get("cheapest_alt_cost_ex_gst")]), errors="coerce"
    ).iloc[0]
    estimated_saving = pd.to_numeric(
        pd.Series([invoice.get("est_saving_ex_gst")]), errors="coerce"
    ).iloc[0]
    costs[1].metric(
        "Cheapest valid alternative",
        "-" if pd.isna(alternative_cost) else f"${alternative_cost:,.2f}",
        f"{invoice.get('cheapest_alt_carrier') or '-'} | {invoice.get('cheapest_alt_service') or '-'}",
    )
    costs[2].metric(
        "Estimated saving ex GST",
        "-" if pd.isna(estimated_saving) else f"${estimated_saving:,.2f}",
    )
    if invoice.get("cp_pricing_basis_note"):
        st.caption(f"Costing basis: {invoice['cp_pricing_basis_note']}")

    linked_opportunities = read_opportunity_register(OPPORTUNITY_REGISTER_PATH)
    if not linked_opportunities.empty:
        linked_opportunities = linked_opportunities.loc[
            linked_opportunities["source_consignment"].map(_normalise_consignment_number).eq(selected)
        ]
    if not linked_opportunities.empty:
        st.subheader("Linked opportunity register item")
        linked_display = linked_opportunities[
            [
                "opportunity_id",
                "title",
                "status",
                "validation_status",
                "proposed_owner",
                "recommended_action",
            ]
        ].rename(
            columns={
                "opportunity_id": "Opportunity ID",
                "title": "Opportunity",
                "status": "Status",
                "validation_status": "Validation status",
                "proposed_owner": "Owner",
                "recommended_action": "Recommended action",
            }
        )
        st.dataframe(linked_display, width="stretch", hide_index=True)

    if not physicals.empty:
        rows = physical_invoice_rows
        columns = [column for column in [
            "parcel_id",
            "parcel_units",
            "raw_packed_sku_cube_cm3",
            "rectangularized_parcel_cube_cm3",
            "estimated_length_cm",
            "estimated_width_cm",
            "estimated_height_cm",
            "estimated_shape_source",
            "fit_band",
            "pricing_scaled_parcel_volumetric_weight_kg",
        ] if column in rows]
        st.subheader("Parcel and product allocation")
        allocation_source_rows = _stage3_invoice_matches(assignment_lines, selected)
        allocation_sources = sorted(
            {
                str(value).strip().upper()
                for value in allocation_source_rows.get(
                    "assignment_source", pd.Series(dtype="string")
                )
                if str(value).strip()
            }
        )
        if allocation_sources == ["ORD_FULFILLMENT_DETAIL"]:
            allocation_source_text = (
                "Stage 1 allocation source: `ORD_FULFILLMENT_DETAIL` "
                "(fulfilment detail passed the parcel-completeness check)."
            )
        elif allocation_sources == ["CATCH_CFC.PACKED"]:
            allocation_source_text = (
                "Stage 1 allocation source: `CATCH_CFC.PACKED` fallback "
                "(`ORD_FULFILLMENT_DETAIL` was unavailable or did not pass the parcel-completeness check)."
            )
        elif allocation_sources:
            allocation_source_text = (
                "Stage 1 allocation source: mixed or legacy sources - "
                + ", ".join(f"`{source}`" for source in allocation_sources)
                + "."
            )
        else:
            allocation_source_text = (
                "Stage 1 allocation source: not available in the persisted assignment output."
            )
        st.caption(allocation_source_text)
        allocation_display = rows[columns].copy()
        for column in ["raw_packed_sku_cube_cm3", "rectangularized_parcel_cube_cm3"]:
            if column in allocation_display:
                allocation_display[column] = pd.to_numeric(allocation_display[column], errors="coerce") / 1_000_000
        allocation_display = allocation_display.rename(
            columns={
                "raw_packed_sku_cube_cm3": "Packed SKU cube m3",
                "rectangularized_parcel_cube_cm3": "Estimated parcel cube m3",
            }
        )
        packed_cube_m3 = pd.to_numeric(
            allocation_display.get(
                "Packed SKU cube m3",
                pd.Series(float("nan"), index=allocation_display.index),
            ),
            errors="coerce",
        )
        estimated_cube_m3 = pd.to_numeric(
            allocation_display.get(
                "Estimated parcel cube m3",
                pd.Series(float("nan"), index=allocation_display.index),
            ),
            errors="coerce",
        )
        allocation_display["Estimated air ratio %"] = (
            1 - packed_cube_m3.div(estimated_cube_m3)
        ).mul(100).where(estimated_cube_m3.gt(0))
        allocation_display["Estimated parcel volumetric weight kg"] = (
            estimated_cube_m3.mul(250).where(estimated_cube_m3.gt(0))
        )
        display_column_order = [
            "parcel_id",
            "parcel_units",
            "Packed SKU cube m3",
            "Estimated parcel cube m3",
            "Estimated air ratio %",
            "Estimated parcel volumetric weight kg",
            "estimated_length_cm",
            "estimated_width_cm",
            "estimated_height_cm",
            "estimated_shape_source",
            "fit_band",
            "pricing_scaled_parcel_volumetric_weight_kg",
        ]
        allocation_display = allocation_display.reindex(
            columns=[column for column in display_column_order if column in allocation_display]
        )
        allocation_column_config = {
            "parcel_id": st.column_config.TextColumn("Parcel", width="small"),
            "parcel_units": st.column_config.NumberColumn("Units", format="%.0f", width="small"),
            "Packed SKU cube m3": st.column_config.NumberColumn(
                "Packed SKU cube\nm3", format="%.4f", width="small"
            ),
            "Estimated parcel cube m3": st.column_config.NumberColumn(
                "Estimated parcel cube\nm3", format="%.4f", width="small"
            ),
            "Estimated air ratio %": st.column_config.NumberColumn(
                "Estimated air ratio\n%", format="%.1f%%", width="small"
            ),
            "Estimated parcel volumetric weight kg": st.column_config.NumberColumn(
                "Estimated parcel\nvolumetric weight kg", format="%.2f", width=110
            ),
            "estimated_length_cm": st.column_config.NumberColumn(
                "Length\ncm", format="%.1f", width="small"
            ),
            "estimated_width_cm": st.column_config.NumberColumn(
                "Width\ncm", format="%.1f", width="small"
            ),
            "estimated_height_cm": st.column_config.NumberColumn(
                "Height\ncm", format="%.1f", width="small"
            ),
            "estimated_shape_source": st.column_config.TextColumn(
                "Estimated shape source", width="medium"
            ),
            "fit_band": st.column_config.TextColumn("Fit band", width="small"),
            "pricing_scaled_parcel_volumetric_weight_kg": st.column_config.NumberColumn(
                "Pricing scaled parcel\nvolumetric weight kg", format="%.2f", width=115
            ),
        }
        st.dataframe(
            allocation_display.style.format(
                {
                    "Packed SKU cube m3": "{:.4f}",
                    "Estimated parcel cube m3": "{:.4f}",
                    "Estimated air ratio %": "{:.1f}%",
                    "Estimated parcel volumetric weight kg": "{:.2f}",
                    "estimated_length_cm": "{:.1f}",
                    "estimated_width_cm": "{:.1f}",
                    "estimated_height_cm": "{:.1f}",
                    "pricing_scaled_parcel_volumetric_weight_kg": "{:.2f}",
                }
            ),
            column_config=allocation_column_config,
            width="stretch",
            hide_index=True,
        )
        composition_fields = {
            "Toteable": ("toteable_unit_count", "toteable_keycode_count"),
            "Non-Toteable": ("non_toteable_unit_count", "non_toteable_keycode_count"),
            "Bulky": ("bulky_unit_count", "bulky_keycode_count"),
            "No dims": ("no_dims_unit_count", "no_dims_keycode_count"),
        }
        if any(unit_column in rows for unit_column, _ in composition_fields.values()):
            composition_rows = []
            for _, parcel in rows.iterrows():
                composition = {"parcel_id": parcel.get("parcel_id")}
                for category, (unit_column, keycode_column) in composition_fields.items():
                    composition[f"{category} units"] = parcel.get(unit_column, 0)
                    composition[f"{category} distinct keycodes"] = parcel.get(keycode_column, 0)
                composition_rows.append(composition)
            st.caption("Parcel composition by size category")
            composition_display = pd.DataFrame(composition_rows)
            category_order = ["Toteable", "Non-Toteable", "Bulky", "No dims"]
            flat_columns = [
                "parcel_id",
                *[f"{category} units" for category in category_order],
                *[f"{category} distinct keycodes" for category in category_order],
            ]
            composition_display = composition_display.reindex(columns=flat_columns, fill_value=0)
            for column in flat_columns[1:]:
                composition_display[column] = pd.to_numeric(composition_display[column], errors="coerce").fillna(0).astype(int)
            total_row = {"parcel_id": "Total"}
            total_row.update({column: int(composition_display[column].sum()) for column in flat_columns[1:]})
            composition_display = pd.concat(
                [composition_display, pd.DataFrame([total_row])], ignore_index=True
            )
            composition_display["parcel_id"] = composition_display["parcel_id"].astype(str)
            composition_display.columns = pd.MultiIndex.from_tuples(
                [("", "parcel_id")]
                + [("Units", "No-dims" if category == "No dims" else category) for category in category_order]
                + [("Keycodes", "No-dims" if category == "No dims" else category) for category in category_order]
            )
            st.dataframe(composition_display, width="stretch", hide_index=True)
        product_columns = [column for column in ["parcel_id", "parcel_units", "parcel_sku_qty_pattern", "sku_ids", "raw_packed_sku_cube_cm3"] if column in rows]
        with st.expander("Parcel product allocation patterns"):
            st.caption("SKU IDs can be shortened in the Stage 2 export. Use the invoice drilldown workbook for the complete item-level review.")
            product_display = rows[product_columns].copy()
            if "raw_packed_sku_cube_cm3" in product_display:
                product_display["raw_packed_sku_cube_cm3"] = pd.to_numeric(
                    product_display["raw_packed_sku_cube_cm3"], errors="coerce"
                ) / 1_000_000
                product_display = product_display.rename(columns={"raw_packed_sku_cube_cm3": "Packed SKU cube m3"})
            st.dataframe(
                product_display.style.format({"Packed SKU cube m3": "{:.3f}"}),
                width="stretch",
                hide_index=True,
            )
    drilldown = OUTPUTS / "invoice_drilldowns" / f"{site.lower()}_{selected}_drilldown.xlsx"
    generate_label = "Regenerate consignment drilldown workbook" if drilldown.exists() else "Generate consignment drilldown workbook"
    if st.button(generate_label, key=f"generate_drilldown_{site}_{selected}"):
        with st.spinner("Generating the invoice drilldown workbook from the current pipeline outputs..."):
            generated, detail = _generate_invoice_drilldown(site, selected)
        if generated and drilldown.exists():
            st.success("Invoice drilldown workbook generated.")
        elif generated:
            st.error("The generator completed but the expected workbook was not found.")
        else:
            st.error(f"Unable to generate the invoice drilldown workbook: {detail}")
    _download("Download consignment drilldown workbook", drilldown, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with st.expander("Full Stage 3 record"):
        st.dataframe(pd.DataFrame([invoice]), width="stretch", hide_index=True)


def _quality(
    stage3: pd.DataFrame,
    stage2: pd.DataFrame,
    air_norms: pd.DataFrame,
    assignment_lines: pd.DataFrame,
    keycode_review: pd.DataFrame,
    site: str,
    reporting_period_label: str,
    parcel_scope: str,
    shipped_carrier: str,
) -> None:
    st.subheader("Model quality and assumptions")
    st.caption(
        f"Current review slice: {reporting_period_label} | {site.title() if site != 'ALL' else 'All sites'} | "
        f"{shipped_carrier} | {parcel_scope}. These checks describe model evidence; they do not replace invoice reconciliation."
    )

    readiness = _data_readiness(site)
    st.subheader("Data readiness")
    st.dataframe(
        readiness.style.format({"Size MB": "{:,.1f}"}),
        width="stretch",
        hide_index=True,
    )

    if stage2.empty and stage3.empty:
        st.warning("No Stage 2 or Stage 3 outputs are available for this review slice.")
        return

    stage2_match = (
        stage2.get("est_parcel_count_match", pd.Series(dtype="string"))
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("Y")
    )
    abs_weight_gap = _number(stage2, "est_weight_gap_vs_shippit_chargeable_kg").abs()
    imputed_units = _number(stage2, "cfc_cube_imputed_unit_count").sum()
    packed_units = _number(stage2, "est_parcel_units_total").sum()
    metric_columns = st.columns(5)
    metric_columns[0].metric("Stage 2 consignments", f"{len(stage2):,.0f}")
    metric_columns[1].metric("Parcel count match", f"{stage2_match.mean() * 100:,.1f}%" if len(stage2) else "-")
    metric_columns[2].metric("Median abs weight gap", f"{abs_weight_gap.median():,.2f} kg" if len(stage2) else "-")
    metric_columns[3].metric("P95 abs weight gap", f"{abs_weight_gap.quantile(0.95):,.2f} kg" if len(stage2) else "-")
    metric_columns[4].metric("Imputed units", f"{imputed_units:,.0f} / {packed_units:,.0f}")

    st.subheader("Where quality signals are concentrated")
    tab_cfc, tab_carrier, tab_process = st.tabs(["By CFC", "By shipped carrier", "By process path"])
    breakdown_format = {
        "Parcel match %": "{:,.1f}%",
        "Median abs chargeable gap kg": "{:,.2f}",
        "P95 abs chargeable gap kg": "{:,.2f}",
        "Median abs cube gap %": "{:,.1f}%",
        "Imputed units": "{:,.0f}",
        "Packed units": "{:,.0f}",
    }
    for tab, group_column in (
        (tab_cfc, "ui_site"),
        (tab_carrier, "shippit_carrier"),
        (tab_process, "est_process_path"),
    ):
        with tab:
            breakdown = _quality_breakdown(stage2, group_column)
            if breakdown.empty:
                st.info("No Stage 2 records are available for this slice.")
            else:
                st.dataframe(breakdown.style.format(breakdown_format), width="stretch", hide_index=True)

    st.subheader("Priority review list")
    review = stage2.copy()
    review["Absolute chargeable gap kg"] = abs_weight_gap
    review["Absolute cube gap %"] = (
        _number(review, "est_cube_gap_vs_shippit_cm3")
        .abs()
        .div(_number(review, "shippit_volumetric_cube_cm3").replace(0, pd.NA))
        .mul(100)
        .fillna(0)
    )
    review_columns = [
        "shippit_invoice_number",
        "ui_site",
        "shippit_carrier",
        "est_process_path",
        "est_parcel_contents_size_mix",
        "est_parcel_count_match",
        "Absolute chargeable gap kg",
        "Absolute cube gap %",
        "cfc_cube_imputed_unit_count",
        "est_flag_odd_allocation",
        "est_shape_source_pattern",
    ]
    review_columns = [column for column in review_columns if column in review]
    priority = review.sort_values("Absolute chargeable gap kg", ascending=False)[review_columns].head(100)
    st.caption("Top 100 consignments by absolute Stage 2 versus Shippit chargeable-weight gap.")
    st.dataframe(
        priority.style.format(
            {
                "Absolute chargeable gap kg": "{:,.2f}",
                "Absolute cube gap %": "{:,.1f}%",
                "cfc_cube_imputed_unit_count": "{:,.0f}",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    st.download_button(
        "Download full quality review CSV",
        review.to_csv(index=False).encode("utf-8"),
        "model_quality_review.csv",
        "text/csv",
        width="stretch",
    )

    if not assignment_lines.empty:
        st.subheader("Merchandise exposure")
        st.caption("Packed units and distinct keycodes by RBU and CFC size category for the selected consignments.")
        rbu = _rbu_mix(assignment_lines)
        if rbu.empty:
            st.info("RBU exposure is not available in the current Stage 1 output.")
        else:
            st.dataframe(
                rbu.style.format({column: "{:,.0f}" for column in rbu.columns if column != "RBU description"}),
                width="stretch",
                hide_index=True,
            )
            st.download_button(
                "Download RBU exposure CSV",
                rbu.to_csv(index=False).encode("utf-8"),
                "model_quality_rbu_exposure.csv",
                "text/csv",
                width="stretch",
            )

    if not keycode_review.empty:
        st.subheader("Flagged keycodes")
        st.caption("Pre-aggregated Stage 2 keycode review output. It is refreshed with the pipeline and is not re-aggregated by the selected reporting week.")
        keycode_columns = [
            "site",
            "sku_id",
            "flagged_invoice_count",
            "total_quantity_packed",
            "avg_weight_gap_vs_shippit_chargeable_kg",
            "avg_est_air_ratio_pct_vs_shippit_cube",
            "review_reasons",
            "review_commentary",
        ]
        keycode_columns = [column for column in keycode_columns if column in keycode_review]
        keycode_display = keycode_review.sort_values("total_quantity_packed", ascending=False)[keycode_columns].head(100)
        st.dataframe(
            keycode_display.style.format(
                {
                    "flagged_invoice_count": "{:,.0f}",
                    "total_quantity_packed": "{:,.0f}",
                    "avg_weight_gap_vs_shippit_chargeable_kg": "{:,.2f}",
                    "avg_est_air_ratio_pct_vs_shippit_cube": "{:,.1f}%",
                }
            ),
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "Download flagged keycode review CSV",
            keycode_review.to_csv(index=False).encode("utf-8"),
            "stage2_keycode_review.csv",
            "text/csv",
            width="stretch",
        )

    st.subheader("Stage 2 positive-air norms")
    st.caption(
        "These norms are median/p10/p90 air ratios from positive-air, single-parcel Shippit observations, "
        "segmented by pack-origin path, contents mix and unit band. They are estimator reference inputs, "
        "not invoice reconciliation results and not directly comparable with the observed weekly trend."
    )
    norm_columns = [
        "estimated_pack_origin_path",
        "parcel_contents_size_mix",
        "parcel_unit_band",
        "sample_count",
        "p10_air_ratio_pct",
        "p50_air_ratio_pct",
        "p90_air_ratio_pct",
        "norm_source",
    ]
    norm_columns = [column for column in norm_columns if column in air_norms]
    norm_display = air_norms[norm_columns].rename(
        columns={
            "estimated_pack_origin_path": "Pack-origin path",
            "parcel_contents_size_mix": "Contents size mix",
            "parcel_unit_band": "Unit band",
            "sample_count": "Positive-air sample count",
            "p10_air_ratio_pct": "Positive-air norm p10",
            "p50_air_ratio_pct": "Stage 2 positive-air norm p50",
            "p90_air_ratio_pct": "Positive-air norm p90",
            "norm_source": "Norm basis",
        }
    )
    st.dataframe(
        norm_display.style.format(
            {
                "Positive-air sample count": "{:,.0f}",
                "Positive-air norm p10": "{:,.1f}%",
                "Stage 2 positive-air norm p50": "{:,.1f}%",
                "Positive-air norm p90": "{:,.1f}%",
            }
        )
        if not norm_display.empty
        else pd.DataFrame(),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Active assumptions and reference inputs")
    st.dataframe(_quality_assumptions(), width="stretch", hide_index=True)
    reference_rows = []
    for path in [
        ROOT / "data" / "reference" / "cp_ratecard_moorebank_lane_rates_snowflake.csv",
        ROOT / "data" / "reference" / "goexpress_ratecard.csv",
        ROOT / "data" / "reference" / "allied_b2c_road_ratecard.csv",
        ROOT / "data" / "reference" / "carrier_fuel_surcharge_rates.csv",
    ]:
        reference_rows.append(
            {
                "Reference input": path.name,
                "Status": "Available" if path.exists() else "Missing",
                "Path": str(path.relative_to(ROOT)),
            }
        )
    st.dataframe(pd.DataFrame(reference_rows), width="stretch", hide_index=True)


def _downloads(site: str) -> None:
    st.subheader("Download centre")
    st.write("Downloads use the current pipeline outputs in this workspace; the UI does not recalculate them.")
    for site_key in (SITE_FILES if site == "ALL" else [site]):
        with st.expander(f"{site_key.title()} outputs", expanded=site != "ALL"):
            paths = SITE_FILES[site_key]
            for label, key in [("Stage 3 cost estimates", "stage3"), ("Stage 3 carrier validation", "carrier_validation"), ("Stage 2 parcel validation", "stage2"), ("Stage 2 parcel physicals", "physicals"), ("Stage 2 keycode review", "keycode_review"), ("Stage 2 air-ratio norms", "air_norms")]:
                _download(f"Download {label}", paths[key])


def _carrier_rules_workspace() -> None:
    """Explain the carrier rules used to interpret opportunity signals."""
    st.subheader("Carrier rules and eligibility")
    st.markdown(
        '<div class="panel">This is the transparent rulebook behind carrier-choice opportunity labels. '
        "Realizable today means the persisted Stage 3 article count, Shippit dead weight, and Shippit volumetric weight "
        "pass the measurable limits. Conditional alternatives remain visible when a published limit is breached, but their "
        "saving is not counted as actionable. Maximum-length, per-item, and combined length/girth checks are shown below "
        "but are not yet measurable in the lightweight dashboard output.</div>",
        unsafe_allow_html=True,
    )
    rules = carrier_rules_dataframe().rename(
        columns={
            "carrier_service": "Carrier / service",
            "chargeable_weight_basis": "Chargeable weight basis",
            "volumetric_conversion": "Volumetric conversion",
            "minimum_weight": "Minimum weight",
            "rounding": "Rounding",
            "dead_weight_limit": "Dead-weight limit",
            "volumetric_weight_limit": "Volumetric-weight limit",
            "max_articles": "Maximum articles",
            "max_length": "Maximum length",
            "surcharge_or_exception": "Surcharge / exception",
            "current_model_treatment": "Current model treatment",
            "source_status": "Source / status",
        }
    )
    st.dataframe(rules, width="stretch", hide_index=True)
    st.caption(
        "The volumetric conversion of 250 kg/m3 is equivalent to 4,000 cm3/kg. "
        "Unknown or unmeasured constraints are intentionally not treated as passed."
    )
    st.subheader("How to read opportunity status")
    status_help = pd.DataFrame(
        [
            {
                "Opportunity status": "Realizable today",
                "Meaning": "The persisted physical evidence passes the measurable limits for the candidate carrier.",
                "Treatment in saving totals": "Included in Realizable saving",
            },
            {
                "Opportunity status": "Conditional if rule changed",
                "Meaning": "The candidate is cheaper, but exceeds one or more published article or weight limits.",
                "Treatment in saving totals": "Excluded; shown as Conditional saving signal",
            },
            {
                "Opportunity status": "Needs validation",
                "Meaning": "The carrier rule or required physical evidence is not sufficiently configured or available.",
                "Treatment in saving totals": "Excluded until validated",
            },
        ]
    )
    st.dataframe(status_help, width="stretch", hide_index=True)


def _future_development_workspace() -> None:
    """Show proposed designs without presenting them as live functionality."""
    st.subheader("Future development")
    st.markdown(
        '<div class="future-banner"><strong>Design register:</strong> this workspace records proposed ideas and approved future designs. '
        "Nothing shown here changes the live estimator until it is moved to In build and implemented."
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Use this page to align on the problem, intended outcome, dependencies and next decision before implementation. "
        "Detailed technical designs remain version-controlled in the repository."
    )

    if not FUTURE_DEVELOPMENT_PATH.exists():
        st.info("No future development items have been registered yet.")
        return
    try:
        register = pd.read_csv(FUTURE_DEVELOPMENT_PATH, dtype="string").fillna("")
    except (OSError, pd.errors.ParserError, ValueError) as error:
        st.error(f"Unable to read the future development register: {error}")
        return
    if register.empty:
        st.info("No future development items have been registered yet.")
        return

    filter_columns = st.columns(3)
    status_values = sorted(value for value in register["status"].astype(str).unique() if value)
    priority_values = sorted(value for value in register["priority"].astype(str).unique() if value)
    category_values = sorted(value for value in register["category"].astype(str).unique() if value)
    selected_status = filter_columns[0].selectbox("Status", ["All statuses", *status_values])
    selected_priority = filter_columns[1].selectbox("Priority", ["All priorities", *priority_values])
    selected_category = filter_columns[2].selectbox("Category", ["All categories", *category_values])
    search = st.text_input("Search future development", placeholder="Feature, problem, owner, dependency...")

    filtered = register.copy()
    if selected_status != "All statuses":
        filtered = filtered.loc[filtered["status"].eq(selected_status)]
    if selected_priority != "All priorities":
        filtered = filtered.loc[filtered["priority"].eq(selected_priority)]
    if selected_category != "All categories":
        filtered = filtered.loc[filtered["category"].eq(selected_category)]
    if search.strip():
        search_value = search.strip().lower()
        searchable = filtered.astype(str).agg(" ".join, axis=1).str.lower()
        filtered = filtered.loc[searchable.str.contains(search_value, regex=False, na=False)]

    metrics = st.columns(4)
    status_lower = filtered["status"].astype(str).str.lower()
    metrics[0].metric("Items in view", f"{len(filtered):,}")
    metrics[1].metric("Proposed", f"{int(status_lower.eq('proposed').sum()):,}")
    metrics[2].metric("High priority", f"{int(filtered['priority'].astype(str).str.lower().eq('high').sum()):,}")
    metrics[3].metric("In build", f"{int(status_lower.eq('in build').sum()):,}")

    display_columns = [
        "item_id",
        "title",
        "status",
        "priority",
        "request_source",
        "category",
        "owner",
        "scope",
        "expected_benefit",
        "last_updated",
    ]
    display_columns = [column for column in display_columns if column in filtered]
    display = filtered[display_columns].rename(
        columns={
            "item_id": "ID",
            "title": "Future development item",
            "status": "Status",
            "priority": "Priority",
            "request_source": "Requested by",
            "category": "Category",
            "owner": "Owner",
            "scope": "Scope",
            "expected_benefit": "Expected benefit",
            "last_updated": "Updated",
        }
    )
    st.dataframe(display, width="stretch", hide_index=True)

    if filtered.empty:
        st.info("No future development items match the selected filters.")
        return
    option_ids = filtered["item_id"].astype(str).tolist()
    selected_item = st.selectbox(
        "Review design item",
        option_ids,
        format_func=lambda value: f"{value} - {filtered.loc[filtered['item_id'].eq(value), 'title'].iloc[0]}",
    )
    record = filtered.loc[filtered["item_id"].eq(selected_item)].iloc[0]
    st.markdown(
        f'<span class="future-status">{record["status"]}</span> '
        f'<strong>{record["priority"]} priority</strong>',
        unsafe_allow_html=True,
    )
    st.subheader(str(record["title"]))
    detail_rows = [
        ("Item ID", record["item_id"]),
        ("Requested by", record["request_source"]),
        ("Owner", record["owner"]),
        ("Category", record["category"]),
        ("Scope", record["scope"]),
        ("Problem", record["problem"]),
        ("Proposed solution", record["proposed_solution"]),
        ("Expected benefit", record["expected_benefit"]),
        ("Dependencies", record["dependencies"]),
        ("Next decision", record["next_decision"]),
        ("Last updated", record["last_updated"]),
    ]
    st.dataframe(pd.DataFrame(detail_rows, columns=["Field", "Description"]), width="stretch", hide_index=True)
    if record.get("design_document", ""):
        st.caption(f"Detailed design in repository: {record['design_document']}")
    st.download_button(
        "Download future development register CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        "future_development_register_filtered.csv",
        "text/csv",
        width="stretch",
    )


def _opportunity_register_workspace() -> None:
    """Show the persisted action register without recalculating reconciliation data."""
    register = read_opportunity_register(OPPORTUNITY_REGISTER_PATH)
    st.subheader("Opportunity register")
    st.markdown(
        '<div class="panel">This is the persistent action list for evidence-backed model improvements. '
        "Evidence can be refreshed later, while owner, action, status, and notes remain human-review fields. "
        "Potential savings are only booked after the candidate scenario is validated.</div>",
        unsafe_allow_html=True,
    )
    if register.empty:
        st.info("No opportunities have been registered yet.")
        return

    filter_columns = st.columns(4)
    cfc_options = ["All CFCs"] + sorted(value for value in register["cfc"].unique() if value)
    carrier_options = ["All carriers"] + sorted(value for value in register["carrier"].unique() if value)
    category_options = ["All categories"] + sorted(value for value in register["category"].unique() if value)
    status_options = ["All statuses"] + sorted(value for value in register["status"].unique() if value)
    selected_cfc = filter_columns[0].selectbox("CFC", cfc_options)
    selected_carrier = filter_columns[1].selectbox("Carrier", carrier_options)
    selected_category = filter_columns[2].selectbox("Category", category_options)
    selected_status = filter_columns[3].selectbox("Status", status_options)
    search = st.text_input("Search register", placeholder="Opportunity, consignment, keycode, root cause...")

    filtered = register.copy()
    if selected_cfc != "All CFCs":
        filtered = filtered.loc[filtered["cfc"].eq(selected_cfc)]
    if selected_carrier != "All carriers":
        filtered = filtered.loc[filtered["carrier"].eq(selected_carrier)]
    if selected_category != "All categories":
        filtered = filtered.loc[filtered["category"].eq(selected_category)]
    if selected_status != "All statuses":
        filtered = filtered.loc[filtered["status"].eq(selected_status)]
    if search.strip():
        search_value = search.strip().lower()
        searchable = filtered.astype(str).agg(" ".join, axis=1).str.lower()
        filtered = filtered.loc[searchable.str.contains(search_value, regex=False, na=False)]

    def money(value: object) -> str:
        numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        return "-" if pd.isna(numeric) else f"${float(numeric):,.2f}"

    metrics = st.columns(4)
    open_count = int((filtered["status"].str.lower() != "closed").sum())
    source_consignments = filtered["source_consignment"].astype(str).str.strip()
    identified_consignments = source_consignments.loc[source_consignments.ne("")]
    aggregate_rows = filtered.loc[source_consignments.eq("")]
    affected_consignments = identified_consignments.nunique() + pd.to_numeric(
        aggregate_rows["affected_consignment_count"], errors="coerce"
    ).sum()
    booked_saving = pd.to_numeric(filtered["potential_saving_ex_gst"], errors="coerce").sum(min_count=1)
    awaiting_validation = int(
        filtered["validation_status"].str.lower().str.contains("awaiting", na=False).sum()
    )
    metrics[0].metric("Open opportunities", f"{open_count:,}")
    metrics[1].metric("Affected consignments", f"{affected_consignments:,.0f}")
    metrics[2].metric("Booked saving", money(booked_saving))
    metrics[3].metric("Awaiting validation", f"{awaiting_validation:,}")

    display = filtered[
        [
            "opportunity_id",
            "title",
            "cfc",
            "carrier",
            "service",
            "category",
            "root_cause",
            "affected_consignment_count",
            "potential_saving_ex_gst",
            "proposed_owner",
            "status",
            "validation_status",
            "updated_date",
        ]
    ].rename(
        columns={
            "opportunity_id": "Opportunity ID",
            "title": "Opportunity",
            "cfc": "CFC",
            "carrier": "Carrier",
            "service": "Service",
            "category": "Category",
            "root_cause": "Root cause",
            "affected_consignment_count": "Affected consignments",
            "potential_saving_ex_gst": "Potential saving ex GST",
            "proposed_owner": "Owner",
            "status": "Status",
            "validation_status": "Validation status",
            "updated_date": "Updated",
        }
    )
    if "Potential saving ex GST" in display:
        display["Potential saving ex GST"] = display["Potential saving ex GST"].map(money)
    st.dataframe(display, width="stretch", hide_index=True)
    st.download_button(
        "Download filtered opportunity register CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        "opportunity_register_filtered.csv",
        mime="text/csv",
        width="stretch",
    )

    opportunity_options = ["Select an opportunity"] + filtered["opportunity_id"].tolist()
    selected_opportunity = st.selectbox("Review opportunity detail", opportunity_options)
    if selected_opportunity == "Select an opportunity":
        return
    record = filtered.loc[filtered["opportunity_id"].eq(selected_opportunity)].iloc[0]
    st.subheader(f"Opportunity detail: {record['title']}")
    detail_rows = [
        ("Opportunity ID", record["opportunity_id"]),
        ("Category", record["category"]),
        ("Root cause", record["root_cause"]),
        ("Source consignment", record["source_consignment"]),
        ("Affected keycode", record["affected_keycode"]),
        ("Carrier / service", f"{record['carrier']} / {record['service']}"),
        ("Accounting week", record["accounting_week"]),
        ("Evidence status", record["evidence_status"]),
        ("Current scenario", record["scenario_current"]),
        ("Candidate scenario", record["scenario_candidate"]),
        ("Modelled shipped-carrier cost ex GST", money(record["modelled_shipped_cost_ex_gst"])),
        ("Invoiced actual cost ex GST", money(record["invoiced_actual_cost_ex_gst"])),
        ("Potential saving ex GST", money(record["potential_saving_ex_gst"])),
        ("Alternative-carrier saving signal ex GST", money(record["alternative_carrier_saving_signal_ex_gst"])),
        ("Owner", record["proposed_owner"]),
        ("Recommended action", record["recommended_action"]),
        ("Status", record["status"]),
        ("Validation status", record["validation_status"]),
        ("Assumptions", record["assumptions"]),
        ("Notes", record["notes"]),
    ]
    st.dataframe(pd.DataFrame(detail_rows, columns=["Field", "Value"]), width="stretch", hide_index=True)
    st.caption(f"Evidence references: {record['source_reference']}")


def main() -> None:
    _inject_style()
    st.markdown('<div class="eyebrow">CFC transport analytics</div>', unsafe_allow_html=True)
    st.markdown('<div class="title">Kmart CFC Last Mile Cost Estimator</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">Parcel geometry, Shippit alignment, carrier cost decisions, and the evidence behind every estimate.</div>', unsafe_allow_html=True)
    with st.sidebar:
        st.header("Control panel")
        reporting_period = st.selectbox("Reporting period", REPORTING_PERIOD_OPTIONS)
        site = st.selectbox("CFC site", ["ALL", "MOOREBANK", "TRUGANINA"], format_func=lambda value: "All sites" if value == "ALL" else value.title())
        page = st.radio(
            "Workspace",
            [
                "Dashboard",
                "Historical trends",
                "Finance Report",
                "Consignment explorer",
                "Invoice reconciliation",
                "Refresh guide",
                "Carrier rules",
                "Opportunity register",
                "Future development",
                "FSC model inputs",
                "Model quality",
                "Downloads",
            ],
        )
        raw_stage3_for_controls = (
            pd.DataFrame()
            if page in {"FSC model inputs", "Opportunity register", "Carrier rules", "Future development", "Finance Report", "Refresh guide"}
            else _site_frame(
                site,
                "stage3",
                STAGE3_CONTROL_COLUMNS,
            )
        )
        carrier_column = "actual_carrier" if "actual_carrier" in raw_stage3_for_controls else "shippit_carrier"
        carrier_values = raw_stage3_for_controls.get(carrier_column, pd.Series(dtype="string")).fillna("").astype(str).str.strip()
        carrier_options = ["All shipped carriers"] + sorted(value for value in carrier_values.unique() if value)
        shipped_carrier = st.selectbox("Shipped carrier", carrier_options)
        parcel_scope = st.selectbox("Parcel scope", ["All consignments", "Single parcel", "Multi parcel"])
        st.divider()
        st.subheader("Pipeline refresh")
        st.caption(
            "Daily refresh updates recent shipment facts in the durable history store. Invoice refreshes add carrier actuals to that history when Finance uploads them."
        )
        if st.button(
            "Run daily refresh",
            key="run_daily_refresh",
            help="Run the normal Stage 1-3 refresh over the inclusive 14-day window ending today.",
            width="stretch",
        ):
            started, message = _start_refresh_job("src.daily_refresh")
            if started:
                st.success(message)
            else:
                st.warning(message)
        validation_dataset = "Live rolling 14-day outputs"
        if page == "Invoice reconciliation":
            validation_options = _available_validation_datasets()
            validation_dataset = st.selectbox(
                "Validation dataset",
                validation_options,
                help="Use the live dataset for recent operational checks, or an aligned historical snapshot when carrier invoices have arrived later.",
            )
        selected_week = None
        if reporting_period == "Single accounting week" and page != "Historical trends":
            if page == "Invoice reconciliation":
                _, weekly_scope = _load_persisted_reconciliation(validation_dataset)
                week_options = _accounting_week_options(weekly_scope)
                if not week_options:
                    week_options = _accounting_week_options(
                        _validation_stage3(
                            site,
                            validation_dataset,
                            STAGE3_CONTROL_COLUMNS,
                        )
                    )
            else:
                week_options = _accounting_week_options(raw_stage3_for_controls)
            if week_options:
                selected_week = st.selectbox("Accounting week", week_options)
            else:
                st.info("No accounting week is available for this scope.")
        if st.button("Refresh cached data", width="stretch"):
            st.cache_data.clear()
            st.rerun()
    if page in {"Historical trends", "Finance Report", "Downloads", "FSC model inputs", "Opportunity register", "Carrier rules", "Future development", "Refresh guide"}:
        stage3_source = pd.DataFrame()
    elif page == "Dashboard":
        stage3_source = _site_frame(site, "stage3", STAGE3_DASHBOARD_COLUMNS)
    elif page == "Model quality":
        stage3_source = _site_frame(site, "stage3", STAGE3_CONTROL_COLUMNS)
    elif page == "Consignment explorer":
        # Explorer searches the persisted store by consignment and only loads
        # one historical fact row; detailed evidence is queried on demand.
        stage3_source = pd.DataFrame()
    else:
        persisted_path = VALIDATION_DATASETS[validation_dataset].get(
            "persisted", {}
        ).get("coverage")
        if persisted_path is not None and persisted_path.exists():
            # Persisted coverage is the source for this workspace. Avoid
            # loading the much larger Stage 3 export until a temporary,
            # non-persisted reconciliation view is actually needed.
            stage3_source = pd.DataFrame()
        else:
            stage3_source = _validation_stage3(
                site,
                validation_dataset,
                STAGE3_RECONCILIATION_COLUMNS,
            )
    stage3 = stage3_source
    if page == "Consignment explorer":
        reporting_period_label = "All saved shipment history"
    elif page == "Historical trends":
        reporting_period_label = "All available saved historical data"
    elif validation_dataset == "Live rolling 14-day outputs":
        stage3, reporting_period_label = _filter_reporting_period(
            stage3, reporting_period, selected_week
        )
    else:
        if reporting_period == "Single accounting week":
            stage3, reporting_period_label = _filter_reporting_period(
                stage3, reporting_period, selected_week
            )
        else:
            reporting_period_label = f"Historical validation snapshot: {validation_dataset}"
    if page == "Dashboard":
        stage2 = _site_frame(site, "stage2", STAGE2_DASHBOARD_COLUMNS)
    elif page == "Model quality":
        stage2 = _site_frame(site, "stage2", STAGE2_QUALITY_COLUMNS)
    else:
        stage2 = pd.DataFrame()
    historical_coverage, historical_weekly = (
        _load_persisted_reconciliation("All carrier history")
        if page == "Historical trends"
        else (pd.DataFrame(), pd.DataFrame())
    )
    stage3 = _filter_carrier(stage3, shipped_carrier)
    stage3 = _filter_parcel_scope(stage3, parcel_scope)
    dashboard_comparison_stage3 = (
        _filter_parcel_scope(_filter_carrier(stage3_source, shipped_carrier), parcel_scope)
        if page == "Dashboard"
        else pd.DataFrame()
    )
    stage2_invoice_col = _invoice_column(stage2) if not stage2.empty else ""
    if not stage2.empty and not stage3.empty and stage2_invoice_col:
        stage2 = stage2[stage2[stage2_invoice_col].astype(str).isin(stage3[_invoice_column(stage3)].astype(str))]
    # Keep the dashboard and reconciliation workspaces light; product-level files are loaded only when needed.
    physicals = pd.DataFrame()
    plm_dimensions = (
        _site_frame(site, "plm_dimensions", PLM_DIMENSION_COLUMNS)
        if page == "Consignment explorer" and site != "ALL"
        else pd.DataFrame()
    )
    air_norms = _site_frame(site, "air_norms") if page == "Model quality" else pd.DataFrame()
    if page == "Model quality":
        assignment_lines = _site_frame(
            site,
            "assignment_lines",
            ASSIGNMENT_QUALITY_COLUMNS,
        )
    else:
        assignment_lines = pd.DataFrame()
    keycode_review = _site_frame(site, "keycode_review") if page == "Model quality" else pd.DataFrame()
    if page == "Model quality" and not assignment_lines.empty and not stage3.empty:
        assignment_invoice_col = _invoice_column(assignment_lines)
        selected_invoices = stage3[_invoice_column(stage3)].dropna().astype(str).unique()
        assignment_lines = assignment_lines[assignment_lines[assignment_invoice_col].astype(str).isin(selected_invoices)]
    if page == "Dashboard":
        _dashboard(
            stage3,
            stage2,
            parcel_scope,
            reporting_period_label,
            reporting_period,
            dashboard_comparison_stage3,
            site=site,
            shipped_carrier=shipped_carrier,
        )
    elif page == "Historical trends":
        _historical_trends_workspace(
            historical_coverage,
            historical_weekly,
            site,
            shipped_carrier,
            parcel_scope,
        )
    elif page == "Finance Report":
        _finance_report_workspace(site)
    elif page == "Refresh guide":
        _refresh_guide_workspace()
    elif page == "Consignment explorer":
        if site == "ALL":
            st.info("Select a CFC site before opening an invoice drilldown.")
        else:
            _invoice_explorer(site, stage3, physicals, assignment_lines, plm_dimensions)
    elif page == "Invoice reconciliation":
        reconciliation_files = VALIDATION_DATASETS[validation_dataset]["reconciliation"]
        _reconciliation_workspace(site, stage3, parcel_scope, reporting_period_label, shipped_carrier, validation_dataset, reconciliation_files)
    elif page == "Carrier rules":
        _carrier_rules_workspace()
    elif page == "Opportunity register":
        _opportunity_register_workspace()
    elif page == "Future development":
        _future_development_workspace()
    elif page == "FSC model inputs":
        _fsc_model_inputs()
    elif page == "Model quality":
        _quality(stage3, stage2, air_norms, assignment_lines, keycode_review, site, reporting_period_label, parcel_scope, shipped_carrier)
    else:
        _downloads(site)


if __name__ == "__main__":
    main()
