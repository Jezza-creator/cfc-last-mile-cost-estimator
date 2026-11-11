from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable

from src.box_suite import (
    DEFAULT_CFC_BOX_SUITE_PATH,
    DEFAULT_PROXY_BOX_SUITE_PATH,
    BoxSuiteBand,
    load_box_suite_catalog,
    match_box_suite,
    summarize_box_suite_matches,
)
from src.db import get_session
from src.extraction_window import EXTRACTION_END_DATE, EXTRACTION_START_DATE
from src.site_config import SITE_CONFIGS, get_site_config, get_site_config_for_store_name


OUTPUT_DIR = Path(os.environ.get("LM_OUTPUT_DIR", "outputs"))
INPUT_PATH = Path(
    os.environ.get("LM_STAGE2_INPUT_PATH", OUTPUT_DIR / "stage2_moorebank_parcel_physicals_full.csv")
)
OUTPUT_PATH = Path(
    os.environ.get("LM_STAGE3_OUTPUT_PATH", OUTPUT_DIR / "stage3_moorebank_cost_estimates_full.csv")
)
STORE_NAME = SITE_CONFIGS["MOOREBANK"].store_name
ALLIED_ZONE_GUIDE_LOOKUP_PATH = Path("data/reference/allied_zone_guide_condensed.csv")
ALLIED_ECOM_CFC_RATECARD_PATH = Path("data/reference/allied_ecom_cfc_ratecard.csv")
ALLIED_ROAD_RATECARD_PATH = Path("data/reference/allied_b2c_road_ratecard.csv")
ALLIED_REMOTE_AREA_SURCHARGE_PATH = Path("data/reference/allied_remote_area_surcharge.csv")
ALLIED_ADDITIONAL_SERVICES_PATH = Path("data/reference/allied_additional_services.csv")
FUEL_SURCHARGE_RATES_PATH = Path("data/reference/carrier_fuel_surcharge_rates.csv")
ALLIED_ORIGIN_ZONE = SITE_CONFIGS["MOOREBANK"].allied_origin_zone
ALLIED_ZONE_RATECARD_OVERRIDES = {
    "N17": "N16",
    "N18": "N16",
}
CP_RATECARD_IDS = {
    "CP_I99_20260504",
    "CP_PA0_20260504",
    "CP_PA1_20260504",
    "CP_PA3_20260504",
    "CP_PA5_20260504",
}
CP_SINGLE_PARCEL_RATECARD_BANDS = (
    (0.0, 0.5, "CP_PA0_20260504"),
    (0.5, 1.0, "CP_PA1_20260504"),
    (1.0, 3.0, "CP_PA3_20260504"),
    (3.0, 5.0, "CP_PA5_20260504"),
)
GOBUNDLE_PLUG_PATH = Path("data/reference/gobundle_plug.csv")
GOEXPRESS_WAREHOUSE_ZONES_PATH = Path("data/reference/goexpress_warehouse_zones.csv")
GOEXPRESS_RATECARD_PATH = Path("data/reference/goexpress_ratecard.csv")
GOEXPRESS_WAREHOUSE_DESTINATION_PATH = Path("data/reference/goexpress_warehouse_destination.csv")
CP_RATECARD_LOCAL_PATHS = (
    Path("data/reference/cp_ratecard_moorebank_lane_rates_snowflake.csv"),
    Path("data/reference/cp_ratecard_moorebank_lane_rates.csv"),
)

# Volumetric divisor used consistently across the pipeline.
VOLUMETRIC_DIVISOR_CM3_PER_KG = 4000.0
AP_MAX_PARCEL_KG = 21.99
REPORTING_WEIGHT_BANDS: list[tuple[float, float | None, str]] = [
    (0.0, 1.0, "0-1 kg"),
    (1.0, 2.0, "1-2 kg"),
    (2.0, 5.0, "2-5 kg"),
    (5.0, 10.0, "5-10 kg"),
    (10.0, 15.0, "10-15 kg"),
    (15.0, 22.0, "15-22 kg"),
    (22.0, 32.0, "22-32 kg"),
    (32.0, 45.0, "32-45 kg"),
    (45.0, None, "45+ kg"),
]


SUPPORTED_SERVICES = {
    "Allied Express": "Allied Express",
    "Couriers Please": "Couriers Please",
    "GoEXPRESS": "GoEXPRESS",
    "GoBUNDLE": "GoBUNDLE",
    "eParcel": "eParcel",
    "eParcel Metro": "eParcel",
}


@dataclass(frozen=True)
class ParcelPhysicalInput:
    invoice_number: str
    actual_carrier: str
    actual_parcel_count: int
    estimated_parcel_count: int
    rule_name: str
    process_path: str
    estimated_pack_origin_path: str
    parcel_contents_size_mix: str
    parcel_id: int
    placeholder_parcel: str
    parcel_units: int
    parcel_weight_kg: float
    parcel_cube_cm3: float
    packed_sku_cube_cm3: float
    raw_packed_sku_cube_cm3: float
    estimated_parcel_cube_cm3: float
    cube_uplift_cm3: float
    pricing_scaled_parcel_cube_cm3: float | None
    estimated_length_cm: float | None
    estimated_width_cm: float | None
    estimated_height_cm: float | None
    fit_band: str
    sku_ids: str
    cfc_cube_imputed: str


@dataclass
class OrderParcels:
    actual_carrier: str
    actual_parcel_count: int
    estimated_parcel_count: int
    rule_name: str
    parcels: list[ParcelPhysicalInput]


@dataclass(frozen=True)
class OrderMeta:
    invoice_number: str
    despatch_date: str
    kmart_accounting_week_number: str
    kmart_accounting_week_number_in_year: str
    kmart_accounting_month_number: str
    kmart_accounting_period_number: str
    kmart_accounting_period_description: str
    kmart_accounting_quarter_description: str
    kmart_accounting_year: str
    dest_suburb: str
    dest_state: str
    dest_postcode: str
    quoted_cost_inc_gst: float | None
    actual_order_chargeable_kg: float | None
    actual_dead_weight_kg: float | None
    actual_volumetric_weight_kg: float | None
    actual_carrier: str
    carrier_tracking_number: str


@dataclass(frozen=True)
class ServiceCost:
    carrier: str
    service: str
    cost_ex_gst: float | None
    base_cost_ex_gst: float | None = None
    fuel_surcharge_ex_gst: float = 0.0
    fuel_surcharge_pct: float | None = None
    fuel_surcharge_source: str = ""


@dataclass(frozen=True)
class FuelSurchargeRule:
    carrier: str
    service: str
    start_date: date
    end_date: date
    pct: float
    source_note: str


@dataclass(frozen=True)
class AlliedChargeBreakdown:
    base_ex_gst: float | None
    remote_area_surcharge_ex_gst: float
    overweight_surcharge_ex_gst: float
    oversized_item_surcharge_ex_gst: float
    manual_measurement_fee_ex_gst: float
    reverse_logistics_surcharge_ex_gst: float

    @property
    def additional_services_ex_gst(self) -> float:
        return (
            self.overweight_surcharge_ex_gst
            + self.oversized_item_surcharge_ex_gst
            + self.manual_measurement_fee_ex_gst
            + self.reverse_logistics_surcharge_ex_gst
        )

    @property
    def total_ex_gst(self) -> float | None:
        if self.base_ex_gst is None:
            return None
        return self.base_ex_gst + self.remote_area_surcharge_ex_gst + self.additional_services_ex_gst


def main() -> None:
    print("Loading stage 2 parcel output...", flush=True)
    orders = _load_stage2_orders(INPUT_PATH)
    invoices = sorted(orders.keys())
    box_suite_catalog = load_box_suite_catalog(
        [DEFAULT_CFC_BOX_SUITE_PATH, DEFAULT_PROXY_BOX_SUITE_PATH]
    )

    session = get_session()
    try:
        print("Loading order metadata from Snowflake...", flush=True)
        meta_by_invoice = _load_order_meta(session, invoices)
        print("Loading zone mappings from Snowflake...", flush=True)
        zone_maps = _load_zone_maps(session, invoices)
        print("Loading rate tables from Snowflake...", flush=True)
        site_key = get_site_config_for_store_name(STORE_NAME).site_key
        rate_tables = _load_rate_tables(session, site_key)
    finally:
        session.close()

    allied_zone_guide = _load_allied_zone_guide_lookup(ALLIED_ZONE_GUIDE_LOOKUP_PATH)
    allied_zone_proxy = _build_allied_zone_proxy_map(meta_by_invoice, zone_maps)

    results: list[dict[str, object]] = []
    for invoice_number in invoices:
        order = orders[invoice_number]
        meta = meta_by_invoice.get(invoice_number)
        if meta is None:
            continue

        reconstructed_chargeable_kg = _estimate_order_chargeable_kg(order.parcels)
        sorted_parcels = sorted(order.parcels, key=lambda parcel: parcel.parcel_id)
        actual_service = SUPPORTED_SERVICES.get(meta.actual_carrier)
        raw_parcel_weight_total_kg = sum(max(parcel.parcel_weight_kg, 0.0) for parcel in sorted_parcels)
        parcel_cube_total_cm3 = sum(max(parcel.parcel_cube_cm3, 0.0) for parcel in sorted_parcels)
        packed_sku_cube_total_cm3 = sum(max(parcel.packed_sku_cube_cm3, 0.0) for parcel in sorted_parcels)
        packed_sku_cube_imputed_flag = (
            "Y"
            if any(str(parcel.cfc_cube_imputed or "").strip().upper() == "Y" for parcel in sorted_parcels)
            else ""
        )
        parcel_cube_total_m3 = parcel_cube_total_cm3 / 1_000_000.0
        parcel_volumetric_equivalent_kg = parcel_cube_total_cm3 / VOLUMETRIC_DIVISOR_CM3_PER_KG
        shippit_chargeable_cube_cm3 = (
            (meta.actual_order_chargeable_kg or 0.0) * VOLUMETRIC_DIVISOR_CM3_PER_KG
            if meta.actual_order_chargeable_kg is not None
            else None
        )
        pricing_scale_factor = (
            (shippit_chargeable_cube_cm3 / parcel_cube_total_cm3)
            if shippit_chargeable_cube_cm3 is not None and parcel_cube_total_cm3 > 0
            else None
        )
        pricing_scaled_parcel_cube_total_cm3 = shippit_chargeable_cube_cm3 if shippit_chargeable_cube_cm3 is not None else parcel_cube_total_cm3
        pricing_scaled_parcel_cube_total_m3 = pricing_scaled_parcel_cube_total_cm3 / 1_000_000.0 if pricing_scaled_parcel_cube_total_cm3 is not None else None
        pricing_scaled_parcel_volumetric_weight_kg = (
            pricing_scaled_parcel_cube_total_cm3 / VOLUMETRIC_DIVISOR_CM3_PER_KG
            if pricing_scaled_parcel_cube_total_cm3 is not None
            else None
        )
        pricing_scaled_parcel_cube_pattern_cm3 = (
            "|".join(f"{max(parcel.parcel_cube_cm3, 0.0) * (pricing_scale_factor or 1.0):.2f}" for parcel in sorted_parcels)
            if sorted_parcels
            else ""
        )
        box_dunnage_cube_cm3 = parcel_cube_total_cm3 - packed_sku_cube_total_cm3
        box_dunnage_weight_kg = max(parcel_volumetric_equivalent_kg - raw_parcel_weight_total_kg, 0.0)
        shippit_dead_weight_kg = meta.actual_dead_weight_kg
        shippit_volumetric_weight_kg = meta.actual_volumetric_weight_kg
        shippit_volumetric_cube_cm3 = (
            shippit_volumetric_weight_kg * VOLUMETRIC_DIVISOR_CM3_PER_KG
            if shippit_volumetric_weight_kg is not None
            else None
        )
        shippit_vs_estimated_weight_gap_kg = max((shippit_dead_weight_kg or 0.0) - raw_parcel_weight_total_kg, 0.0)
        # Signed residual: negative means the rectangularized estimate is
        # larger than Shippit's reported volumetric cube.
        shippit_vs_estimated_cube_gap_cm3 = (
            shippit_volumetric_cube_cm3 - parcel_cube_total_cm3
            if shippit_volumetric_cube_cm3 is not None
            else None
        )
        shippit_vs_estimated_cube_gap_m3 = (
            shippit_vs_estimated_cube_gap_cm3 / 1_000_000.0
            if shippit_vs_estimated_cube_gap_cm3 is not None
            else None
        )
        box_suite_matches = _match_box_suites_for_order(
            sorted_parcels,
            actual_service or meta.actual_carrier,
            box_suite_catalog,
        )
        box_suite_source, box_suite_family, box_suite_band, box_suite_match_count = summarize_box_suite_matches(
            box_suite_matches
        )
        parcel_units_total = sum(parcel.parcel_units for parcel in sorted_parcels)
        parcel_units_per_package = "|".join(str(parcel.parcel_units) for parcel in sorted_parcels)
        pack_flow_summary = _pack_flow_summary(order.rule_name, sorted_parcels)
        assignment_scope = "Single package" if order.actual_parcel_count == 1 else "Multi parcel"
        assignment_required = "N" if order.actual_parcel_count == 1 else "Y"
        costing_grain = "SINGLE_PARCEL" if order.actual_parcel_count == 1 else "PARCEL_LEVEL"
        cost_weight_basis_kg = meta.actual_order_chargeable_kg if meta.actual_order_chargeable_kg is not None else reconstructed_chargeable_kg
        cost_parcel_count_basis = order.actual_parcel_count if order.actual_parcel_count > 0 else order.estimated_parcel_count
        normalized_parcel_weights = _normalize_parcel_weights(sorted_parcels, meta.actual_order_chargeable_kg)
        normalized_parcel_weight_total_kg = sum(normalized_parcel_weights)
        normalized_parcel_weight_per_package = "|".join(f"{value:.2f}" for value in normalized_parcel_weights)
        allocated_dead_weights = _normalize_parcel_weights(sorted_parcels, meta.actual_dead_weight_kg)
        pricing_scaled_volumetric_weights = [
            (
                parcel.pricing_scaled_parcel_cube_cm3 / VOLUMETRIC_DIVISOR_CM3_PER_KG
                if parcel.pricing_scaled_parcel_cube_cm3 is not None
                else max(parcel.parcel_cube_cm3, 0.0) * (pricing_scale_factor or 1.0) / VOLUMETRIC_DIVISOR_CM3_PER_KG
            )
            for parcel in sorted_parcels
        ]
        # Parcel-level alternatives charge each parcel separately. Use the
        # greater allocated dead or Shippit-scaled volumetric weight per parcel.
        pricing_parcel_weights = [
            max(dead_weight, volumetric_weight)
            for dead_weight, volumetric_weight in zip(allocated_dead_weights, pricing_scaled_volumetric_weights)
        ]
        pricing_parcel_weight_total_kg = sum(pricing_parcel_weights)
        pricing_parcel_weight_per_package = "|".join(f"{value:.2f}" for value in pricing_parcel_weights)
        pricing_scaled_parcel_volumetric_weight_per_package = "|".join(
            f"{value:.2f}" for value in pricing_scaled_volumetric_weights
        )
        allocated_dead_weight_per_package = "|".join(f"{value:.2f}" for value in allocated_dead_weights)
        ap_adjusted_parcel_weights, ap_overflow_kg = _cap_and_redistribute_parcel_weights(pricing_parcel_weights, AP_MAX_PARCEL_KG)
        ap_adjusted_parcel_weight_total_kg = sum(ap_adjusted_parcel_weights)
        ap_adjusted_parcel_weight_per_package = "|".join(f"{value:.2f}" for value in ap_adjusted_parcel_weights)
        placeholder_parcel = "Y" if any(parcel.placeholder_parcel == "Y" for parcel in sorted_parcels) else ""
        reporting_weight_band = _reporting_weight_band(cost_weight_basis_kg)
        zones = zone_maps.get(invoice_number, {})
        allied_zone_exact = zones.get("ALLIED", "")
        allied_zone_source = "snowflake_lookup" if allied_zone_exact else ""
        allied_zone = allied_zone_exact
        cp_zone = zones.get("CP", "")
        ap_zone = zones.get("AP", "")
        goexpress_zone, goexpress_zone_source = _lookup_goexpress_zone(
            meta,
            rate_tables["goexpress_zone_lookup"],
            rate_tables["goexpress_coverage"],
            rate_tables["goexpress_warehouse"],
        )
        (
            goexpress_weight_cap_applied,
            goexpress_published_max_weight_kg,
            goexpress_weight_cap_note,
        ) = _goexpress_weight_cap_details(
            str(rate_tables["goexpress_warehouse"]),
            goexpress_zone,
            pricing_parcel_weights,
            rate_tables["goexpress_bands"],
        )
        if not allied_zone:
            allied_zone = _lookup_allied_zone_from_pdf(meta, allied_zone_guide)
            if allied_zone:
                allied_zone_source = "allied_zone_guide_pdf"
        if not allied_zone:
            allied_zone = allied_zone_proxy.get((ap_zone, meta.dest_state), "")
            if allied_zone:
                allied_zone_source = "ap_zone_proxy"
        zones_effective = dict(zones)
        zones_effective["ALLIED"] = allied_zone
        zones_effective["GOEXPRESS"] = goexpress_zone
        ap_pricing_zone = _ap_pricing_zone(
            ap_zone,
            meta.dest_state,
            rate_tables["ap_area_to_zone"],
        )
        (
            service_costs,
            allied_breakdown,
            cp_pricing_chargeable_weight_kg,
            cp_pricing_cap_applied,
            cp_pricing_ratecard_id,
            cp_pricing_basis_note,
        ) = _estimate_service_costs(
            meta,
            zones_effective,
            rate_tables,
            pricing_parcel_weights,
            sorted_parcels,
            ap_adjusted_parcel_weights,
        )
        actual_cost = _cost_for_service(service_costs, actual_service) if actual_service else None
        actual_cost_missing_reason = _actual_cost_missing_reason(
            meta.actual_carrier,
            actual_service,
            order,
            zones_effective,
            rate_tables,
            ap_pricing_zone,
            actual_cost,
            ap_overflow_kg,
            ap_adjusted_parcel_weights,
        )
        actual_cost_missing_detail = _actual_cost_missing_detail(
            actual_cost_missing_reason,
            meta,
            zones_effective,
            ap_pricing_zone,
            allied_zone_source,
        )
        alt_costs = [cost for cost in service_costs if cost.service != actual_service and cost.cost_ex_gst is not None]
        cheapest_alt = min(alt_costs, key=lambda item: item.cost_ex_gst) if alt_costs else None
        est_saving = None
        if actual_cost is not None and actual_cost.cost_ex_gst is not None and cheapest_alt is not None and cheapest_alt.cost_ex_gst is not None:
            est_saving = actual_cost.cost_ex_gst - cheapest_alt.cost_ex_gst

        results.append(
            {
                "invoice_number": invoice_number,
                "actual_carrier": meta.actual_carrier,
                "actual_service": actual_service or "",
                "carrier_tracking_number": meta.carrier_tracking_number,
                "despatch_date": meta.despatch_date,
                "kmart_accounting_week_number": meta.kmart_accounting_week_number,
                "kmart_accounting_week_number_in_year": meta.kmart_accounting_week_number_in_year,
                "kmart_accounting_month_number": meta.kmart_accounting_month_number,
                "kmart_accounting_period_number": meta.kmart_accounting_period_number,
                "kmart_accounting_period_description": meta.kmart_accounting_period_description,
                "kmart_accounting_quarter_description": meta.kmart_accounting_quarter_description,
                "kmart_accounting_year": meta.kmart_accounting_year,
                "dest_suburb": meta.dest_suburb,
                "dest_state": meta.dest_state,
                "dest_postcode": meta.dest_postcode,
                "quoted_cost_inc_gst": meta.quoted_cost_inc_gst,
                "shippit_chargeable_weight_kg": meta.actual_order_chargeable_kg,
                "shippit_chargeable_cube_cm3": shippit_chargeable_cube_cm3,
                "est_chargeable_weight_kg": reconstructed_chargeable_kg,
                "est_packed_sku_weight_kg": raw_parcel_weight_total_kg,
                "est_packed_sku_cube_total_cm3": packed_sku_cube_total_cm3,
                "est_packed_sku_cube_total_m3": packed_sku_cube_total_cm3 / 1_000_000.0,
                "est_packed_sku_cube_imputed_flag": packed_sku_cube_imputed_flag,
                "est_parcel_cube_total_cm3": parcel_cube_total_cm3,
                "est_parcel_cube_total_m3": parcel_cube_total_m3,
                "est_volumetric_weight_kg": parcel_volumetric_equivalent_kg,
                "pricing_scale_factor": pricing_scale_factor,
                "pricing_scaled_parcel_cube_total_cm3": pricing_scaled_parcel_cube_total_cm3,
                "pricing_scaled_parcel_cube_total_m3": pricing_scaled_parcel_cube_total_m3,
                "pricing_scaled_parcel_volumetric_weight_kg": pricing_scaled_parcel_volumetric_weight_kg,
                "pricing_scaled_parcel_cube_pattern_cm3": pricing_scaled_parcel_cube_pattern_cm3,
                "est_box_dunnage_cube_cm3": box_dunnage_cube_cm3,
                "est_box_dunnage_weight_kg": box_dunnage_weight_kg,
                "shippit_dead_weight_kg": shippit_dead_weight_kg,
                "shippit_volumetric_weight_kg": shippit_volumetric_weight_kg,
                "shippit_volumetric_cube_cm3": shippit_volumetric_cube_cm3,
                # Backward-compatible aliases for downstream readers that still expect the older labels.
                "actual_order_chargeable_kg": meta.actual_order_chargeable_kg,
                "shippit_chargeable_cube_cm3": shippit_chargeable_cube_cm3,
                "estimated_order_chargeable_kg": reconstructed_chargeable_kg,
                "raw_parcel_weight_total_kg": raw_parcel_weight_total_kg,
                "packed_sku_weight_kg": raw_parcel_weight_total_kg,
                "packed_sku_cube_total_cm3": packed_sku_cube_total_cm3,
                "packed_sku_cube_total_m3": packed_sku_cube_total_cm3 / 1_000_000.0,
                "parcel_cube_total_cm3": parcel_cube_total_cm3,
                "parcel_cube_total_m3": parcel_cube_total_m3,
                "parcel_volumetric_equivalent_kg": parcel_volumetric_equivalent_kg,
                "pricing_scale_factor": pricing_scale_factor,
                "pricing_scaled_parcel_cube_total_cm3": pricing_scaled_parcel_cube_total_cm3,
                "pricing_scaled_parcel_cube_total_m3": pricing_scaled_parcel_cube_total_m3,
                "pricing_scaled_parcel_volumetric_weight_kg": pricing_scaled_parcel_volumetric_weight_kg,
                "pricing_scaled_parcel_cube_pattern_cm3": pricing_scaled_parcel_cube_pattern_cm3,
                "box_dunnage_cube_cm3": box_dunnage_cube_cm3,
                "box_dunnage_weight_kg": box_dunnage_weight_kg,
                "shippit_vs_estimated_weight_gap_kg": shippit_vs_estimated_weight_gap_kg,
                "shippit_vs_estimated_cube_gap_cm3": shippit_vs_estimated_cube_gap_cm3,
                "shippit_vs_estimated_cube_gap_m3": shippit_vs_estimated_cube_gap_m3,
                "weight_gap_kg": raw_parcel_weight_total_kg - (shippit_dead_weight_kg or 0.0),
                "cube_gap_equivalent_kg": parcel_volumetric_equivalent_kg - (shippit_volumetric_weight_kg or 0.0),
                "chargeable_gap_kg": reconstructed_chargeable_kg - (meta.actual_order_chargeable_kg or 0.0),
                "box_suite_source": box_suite_source,
                "box_suite_family": box_suite_family,
                "box_suite_band": box_suite_band,
                "box_suite_match_count": box_suite_match_count,
                "parcel_units_total": parcel_units_total,
                "parcel_units_per_package": parcel_units_per_package,
                "actual_parcel_count": order.actual_parcel_count,
                "estimated_parcel_count": order.estimated_parcel_count,
                "normalized_parcel_weight_total_kg": normalized_parcel_weight_total_kg,
                "normalized_parcel_weight_per_package": normalized_parcel_weight_per_package,
                "allocated_dead_weight_per_package_kg": allocated_dead_weight_per_package,
                "pricing_scaled_parcel_volumetric_weight_per_package_kg": pricing_scaled_parcel_volumetric_weight_per_package,
                "pricing_parcel_chargeable_weight_total_kg": pricing_parcel_weight_total_kg,
                "pricing_parcel_chargeable_weight_per_package_kg": pricing_parcel_weight_per_package,
                "ap_adjusted_parcel_weight_total_kg": ap_adjusted_parcel_weight_total_kg,
                "ap_adjusted_parcel_weight_per_package": ap_adjusted_parcel_weight_per_package,
                "ap_overflow_kg": ap_overflow_kg,
                "cost_weight_basis_kg": cost_weight_basis_kg,
                "reporting_weight_band": reporting_weight_band,
                "cost_parcel_count_basis": cost_parcel_count_basis,
                "rule_name": order.rule_name,
                "pack_flow_summary": pack_flow_summary,
                "assignment_scope": assignment_scope,
                "assignment_required": assignment_required,
                "costing_grain": costing_grain,
                "allied_zone": allied_zone,
                "allied_zone_exact": allied_zone_exact,
                "allied_zone_source": allied_zone_source,
                "cp_zone": cp_zone,
                "ap_zone": ap_zone,
                "ap_pricing_zone": ap_pricing_zone,
                "ap_origin_area": rate_tables["ap_origin_area"],
                "ap_ratecard_origin_area": rate_tables["ap_ratecard_origin_area"],
                "goexpress_zone": goexpress_zone,
                "goexpress_zone_source": goexpress_zone_source,
                "goexpress_weight_cap_applied": goexpress_weight_cap_applied,
                "goexpress_published_max_weight_kg": goexpress_published_max_weight_kg,
                "goexpress_weight_cap_note": goexpress_weight_cap_note,
                "gobundle_plug_site": rate_tables["gobundle_plug"]["site"],
                "gobundle_plug_cost_per_parcel_ex_gst": rate_tables["gobundle_plug"]["cost_per_parcel_ex_gst"],
                "gobundle_plug_source": rate_tables["gobundle_plug"]["source"],
                "cp_pricing_chargeable_weight_kg": cp_pricing_chargeable_weight_kg,
                "cp_pricing_cap_applied": cp_pricing_cap_applied,
                "cp_pricing_ratecard_id": cp_pricing_ratecard_id,
                "cp_pricing_basis_note": cp_pricing_basis_note,
                "placeholder_parcel": placeholder_parcel,
                "allied_base_cost_ex_gst": allied_breakdown.base_ex_gst,
                "allied_remote_area_surcharge_ex_gst": allied_breakdown.remote_area_surcharge_ex_gst,
                "allied_overweight_surcharge_ex_gst": allied_breakdown.overweight_surcharge_ex_gst,
                "allied_oversized_item_surcharge_ex_gst": allied_breakdown.oversized_item_surcharge_ex_gst,
                "allied_manual_measurement_fee_ex_gst": allied_breakdown.manual_measurement_fee_ex_gst,
                "allied_reverse_logistics_surcharge_ex_gst": allied_breakdown.reverse_logistics_surcharge_ex_gst,
                "allied_additional_services_ex_gst": allied_breakdown.additional_services_ex_gst,
                "allied_total_cost_ex_gst": allied_breakdown.total_ex_gst,
                "actual_est_base_cost_ex_gst": _service_base_cost(actual_cost),
                "actual_est_fuel_surcharge_ex_gst": actual_cost.fuel_surcharge_ex_gst if actual_cost else None,
                "actual_est_fuel_surcharge_pct": actual_cost.fuel_surcharge_pct if actual_cost else None,
                "actual_est_fuel_surcharge_source": actual_cost.fuel_surcharge_source if actual_cost else "",
                "actual_est_fsc_charge_ex_gst": actual_cost.fuel_surcharge_ex_gst if actual_cost else None,
                "actual_est_fsc_rate_pct": actual_cost.fuel_surcharge_pct if actual_cost else None,
                "actual_est_fsc_source": actual_cost.fuel_surcharge_source if actual_cost else "",
                "actual_est_cost_ex_gst": actual_cost.cost_ex_gst if actual_cost else None,
                "actual_est_cost_missing_reason": actual_cost_missing_reason,
                "actual_est_cost_missing_detail": actual_cost_missing_detail,
                "actual_est_service": actual_service or "",
                "cheapest_alt_carrier": cheapest_alt.carrier if cheapest_alt else "",
                "cheapest_alt_service": cheapest_alt.service if cheapest_alt else "",
                "cheapest_alt_base_cost_ex_gst": _service_base_cost(cheapest_alt),
                "cheapest_alt_fuel_surcharge_ex_gst": cheapest_alt.fuel_surcharge_ex_gst if cheapest_alt else None,
                "cheapest_alt_cost_ex_gst": cheapest_alt.cost_ex_gst if cheapest_alt else None,
                "est_saving_ex_gst": est_saving,
                "cheaper_option_exists": "Y" if est_saving is not None and est_saving > 0 else "",
            }
        )

    print("Writing stage 3 output...", flush=True)
    _write_output(OUTPUT_PATH, results)
    _write_carrier_validation_output(_carrier_validation_output_path(OUTPUT_PATH), results)
    _print_summary(results)


def _load_stage2_orders(path: Path) -> dict[str, OrderParcels]:
    if not path.exists():
        raise FileNotFoundError(f"Missing stage 2 output: {path}")

    orders: dict[str, OrderParcels] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            parcel = ParcelPhysicalInput(
                invoice_number=str(row["invoice_number"]),
                actual_carrier=str(row["actual_carrier"]),
                actual_parcel_count=int(row["actual_parcel_count"] or 0),
                estimated_parcel_count=int(row["estimated_parcel_count"] or 0),
                rule_name=str(row["rule_name"]),
                process_path=str(row.get("process_path") or ""),
                estimated_pack_origin_path=str(row.get("estimated_pack_origin_path") or ""),
                parcel_contents_size_mix=str(row.get("parcel_contents_size_mix") or ""),
                parcel_id=int(row["parcel_id"] or 0),
                placeholder_parcel=str(row.get("placeholder_parcel") or ""),
                parcel_units=int(row["parcel_units"] or 0),
                parcel_weight_kg=float(row["parcel_weight_kg"] or 0.0),
                parcel_cube_cm3=float(row.get("estimated_parcel_cube_cm3") or row["parcel_cube_cm3"] or 0.0),
                packed_sku_cube_cm3=float(
                    row.get("raw_packed_sku_cube_cm3")
                    or row.get("packed_sku_cube_cm3")
                    or row.get("packed_sku_cube_total_cm3")
                    or 0.0
                ),
                raw_packed_sku_cube_cm3=float(
                    row.get("raw_packed_sku_cube_cm3")
                    or row.get("packed_sku_cube_cm3")
                    or row.get("packed_sku_cube_total_cm3")
                    or 0.0
                ),
                estimated_parcel_cube_cm3=float(row.get("estimated_parcel_cube_cm3") or row.get("parcel_cube_cm3") or 0.0),
                cube_uplift_cm3=float(row.get("cube_uplift_cm3") or 0.0),
                pricing_scaled_parcel_cube_cm3=_to_float(row.get("pricing_scaled_parcel_cube_cm3")),
                estimated_length_cm=_to_float(row["estimated_length_cm"]),
                estimated_width_cm=_to_float(row["estimated_width_cm"]),
                estimated_height_cm=_to_float(row["estimated_height_cm"]),
                fit_band=str(row["fit_band"] or ""),
                sku_ids=str(row["sku_ids"] or ""),
                cfc_cube_imputed=str(row.get("cfc_cube_imputed") or ""),
            )
            order = orders.get(parcel.invoice_number)
            if order is None:
                orders[parcel.invoice_number] = OrderParcels(
                    actual_carrier=parcel.actual_carrier,
                    actual_parcel_count=parcel.actual_parcel_count,
                    estimated_parcel_count=parcel.estimated_parcel_count,
                    rule_name=parcel.rule_name,
                    parcels=[parcel],
                )
            else:
                order.parcels.append(parcel)
    return orders


def _load_order_meta(session, invoices: list[str]) -> dict[str, OrderMeta]:
    if not invoices:
        return {}

    target_invoices = set(invoices)
    rows = []
    for start in range(0, len(invoices), 5_000):
        invoice_batch = invoices[start : start + 5_000]
        values_sql = ", ".join(
            "('" + invoice.replace("'", "''") + "')" for invoice in invoice_batch
        )
        rows.extend(
            session.sql(
                f"""
                WITH target_invoices AS (
                    SELECT column1 AS invoice_number
                    FROM VALUES {values_sql}
                )
                SELECT
                    TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
                    TO_VARCHAR(s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE) AS despatch_date,
                    TO_VARCHAR(d.ACCOUNTING_WEEK_NUMBER) AS kmart_accounting_week_number,
                    TO_VARCHAR(d.ACCOUNTING_WEEK_NUMBER_IN_YEAR) AS kmart_accounting_week_number_in_year,
                    TO_VARCHAR(d.ACCOUNTING_MONTH_NUMBER) AS kmart_accounting_month_number,
                    TO_VARCHAR(d.ACCOUNTING_PERIOD_NUMBER) AS kmart_accounting_period_number,
                    d.ACCOUNTING_PERIOD_DESCRIPTION AS kmart_accounting_period_description,
                    d.ACCOUNTING_QUARTER_DESCRIPTION AS kmart_accounting_quarter_description,
                    TO_VARCHAR(d.ACCOUNTING_YEAR) AS kmart_accounting_year,
                    s.ORDER_LOCATION__DELIVERY_SUBURB AS dest_suburb,
                    s.ORDER_LOCATION__DELIVERY_STATE AS dest_state,
                    TO_VARCHAR(s.ORDER_LOCATION__DELIVERY_POSTCODE) AS dest_postcode,
                    s.ORDER_CARRIER__QUOTED_SHIPPING_COST_INCL_GST AS quoted_cost_inc_gst,
                    GREATEST(s.ORDER_WEIGHT__DEAD_WEIGHT, s.ORDER_WEIGHT__VOLUMETRIC_WEIGHT) AS actual_order_chargeable_kg,
                    s.ORDER_WEIGHT__DEAD_WEIGHT AS actual_dead_weight_kg,
                    s.ORDER_WEIGHT__VOLUMETRIC_WEIGHT AS actual_volumetric_weight_kg,
                    s.CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS actual_carrier,
                    TO_VARCHAR(s.ORDER_CARRIER__CARRIER_TRACKING_NUMBER) AS carrier_tracking_number
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS s
                JOIN target_invoices t
                  ON t.invoice_number = TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)
                LEFT JOIN KSF_SOPHIA_DATA_INTELLIGENCE_HUB_PROD.COMMON_DIMENSIONS.DIM_DATE d
                  ON d.DATE = s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                WHERE s.STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
                  AND s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
                """
            ).collect()
        )

    meta_by_invoice: dict[str, OrderMeta] = {}
    for row in rows:
        invoice_number = str(row["INVOICE_NUMBER"])
        if invoice_number not in target_invoices:
            continue
        meta_by_invoice[invoice_number] = OrderMeta(
            invoice_number=invoice_number,
            despatch_date=str(row["DESPATCH_DATE"]),
            kmart_accounting_week_number=str(row["KMART_ACCOUNTING_WEEK_NUMBER"] or ""),
            kmart_accounting_week_number_in_year=str(row["KMART_ACCOUNTING_WEEK_NUMBER_IN_YEAR"] or ""),
            kmart_accounting_month_number=str(row["KMART_ACCOUNTING_MONTH_NUMBER"] or ""),
            kmart_accounting_period_number=str(row["KMART_ACCOUNTING_PERIOD_NUMBER"] or ""),
            kmart_accounting_period_description=str(row["KMART_ACCOUNTING_PERIOD_DESCRIPTION"] or ""),
            kmart_accounting_quarter_description=str(row["KMART_ACCOUNTING_QUARTER_DESCRIPTION"] or ""),
            kmart_accounting_year=str(row["KMART_ACCOUNTING_YEAR"] or ""),
            dest_suburb=str(row["DEST_SUBURB"] or ""),
            dest_state=str(row["DEST_STATE"] or ""),
            dest_postcode=str(row["DEST_POSTCODE"] or ""),
            quoted_cost_inc_gst=_to_float(row["QUOTED_COST_INC_GST"]),
            actual_order_chargeable_kg=_to_float(row["ACTUAL_ORDER_CHARGEABLE_KG"]),
            actual_dead_weight_kg=_to_float(row["ACTUAL_DEAD_WEIGHT_KG"]),
            actual_volumetric_weight_kg=_to_float(row["ACTUAL_VOLUMETRIC_WEIGHT_KG"]),
            actual_carrier=str(row["ACTUAL_CARRIER"] or ""),
            carrier_tracking_number=str(row["CARRIER_TRACKING_NUMBER"] or ""),
        )
    return meta_by_invoice


def _load_zone_maps(session, invoices: list[str]) -> dict[str, dict[str, str]]:
    if not invoices:
        return {}

    def _fetch(carrier: str, mapping_version: str) -> dict[str, str]:
        rows = []
        for start in range(0, len(invoices), 5_000):
            invoice_batch = invoices[start : start + 5_000]
            values_sql = ", ".join(
                "('" + invoice.replace("'", "''") + "')" for invoice in invoice_batch
            )
            rows.extend(
                session.sql(
                    f"""
                    WITH target_invoices AS (
                        SELECT column1 AS invoice_number
                        FROM VALUES {values_sql}
                    ), orders AS (
                        SELECT
                            TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
                            s.ORDER_LOCATION__DELIVERY_SUBURB AS dest_suburb,
                            s.ORDER_LOCATION__DELIVERY_POSTCODE::NUMBER AS dest_postcode
                        FROM KSFPA.FREIGHT.SHIPPIT_ORDERS s
                        JOIN target_invoices t
                          ON t.invoice_number = TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER)
                        WHERE s.STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
                          AND s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                                BETWEEN TO_DATE('{EXTRACTION_START_DATE}') AND TO_DATE('{EXTRACTION_END_DATE}')
                    )
                    SELECT
                        o.invoice_number,
                        pz.zone_code AS zone_code
                    FROM orders o
                    JOIN KSFPA.ONLINE_PVT.LM_POSTCODE_ZONE pz
                      ON pz.postcode = o.dest_postcode
                     AND pz.carrier = '{carrier}'
                     AND pz.mapping_version = '{mapping_version}'
                     AND pz.mapping_status = 'ACTIVE'
                     AND pz.destination_eligible = TRUE
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY o.invoice_number
                        ORDER BY
                            IFF(UPPER(TRIM(pz.suburb)) = UPPER(TRIM(o.dest_suburb)), 0, 1),
                            IFF(pz.zone_code = 'REM', 1, 0)
                    ) = 1
                    """
                ).collect()
            )
        return {str(row["INVOICE_NUMBER"]): str(row["ZONE_CODE"] or "") for row in rows}

    ap = _fetch("Australia Post", "AP_9_ZONE_20250805")
    allied = _fetch("Allied Express", "ALLIED_ZONE_GUIDE_CONDENSED")
    cp = _fetch("Couriers Please", "NOVEMBER_2023_FINAL")

    zone_maps: dict[str, dict[str, str]] = {}
    for invoice in invoices:
        zone_maps[invoice] = {
            "AP": ap.get(invoice, ""),
            "ALLIED": allied.get(invoice, ""),
            "CP": cp.get(invoice, ""),
        }
    return zone_maps


def _load_allied_zone_guide_lookup(path: Path) -> dict[tuple[str, str, str], str]:
    if not path.exists():
        return {}

    lookup: dict[tuple[str, str, str], str] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            state = str(row.get("state") or "").strip().upper()
            postcode = str(row.get("postcode") or "").strip().zfill(4)
            suburb = " ".join(str(row.get("suburb") or "").split()).strip().upper()
            zone = str(row.get("zone") or "").strip().upper()
            if not state or not postcode or not suburb or not zone:
                continue
            lookup[(state, postcode, suburb)] = zone
    return lookup


def _normalize_suburb_key(value: object) -> str:
    return " ".join(str(value or "").split()).strip().upper()


def _goexpress_warehouse_code() -> str:
    return get_site_config_for_store_name(STORE_NAME).goexpress_warehouse_code


def _couriers_please_origin_zone() -> str:
    return get_site_config_for_store_name(STORE_NAME).cp_origin_zone


def _load_goexpress_warehouse_destinations(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()

    coverage: set[tuple[str, str, str]] = set()
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            warehouse = str(row.get("warehouse") or "").strip().upper()
            postcode = str(row.get("postcode") or "").strip()
            suburbs = str(row.get("suburbs") or "").strip()
            if not warehouse or not postcode or not suburbs:
                continue
            for suburb in suburbs.split(","):
                suburb_key = _normalize_suburb_key(suburb)
                if suburb_key:
                    coverage.add((warehouse, postcode, suburb_key))
    return coverage


def _load_goexpress_warehouse_zones(path: Path) -> dict[tuple[str, str, str], str]:
    if not path.exists():
        return {}

    lookup: dict[tuple[str, str, str], str] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            warehouse = str(row.get("warehouse") or "").strip().upper()
            postcode = str(row.get("postcode") or "").strip()
            zone = str(row.get("zone") or "").strip()
            suburbs = str(row.get("suburbs") or "").strip()
            if not warehouse or not postcode or not zone or not suburbs:
                continue
            for suburb in suburbs.split(","):
                suburb_key = _normalize_suburb_key(suburb)
                if suburb_key:
                    lookup[(warehouse, postcode, suburb_key)] = zone
    return lookup


def _load_goexpress_ratecard(path: Path) -> dict[tuple[str, str], list[tuple[float, float, float]]]:
    if not path.exists():
        return {}

    bands: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            warehouse = str(row.get("warehouse") or "").strip().upper()
            zone = str(row.get("zone") or "").strip()
            if not warehouse or not zone:
                continue
            bands.setdefault((warehouse, zone), []).append(
                (
                    _to_float(row.get("minKg")) or 0.0,
                    _to_float(row.get("maxKg")) or 0.0,
                    _to_float(row.get("charge")) or 0.0,
                )
            )

    for key in bands:
        bands[key].sort(key=lambda band: (band[0], band[1], band[2]))
    return bands


def _lookup_goexpress_zone(
    meta: OrderMeta,
    zone_lookup: dict[tuple[str, str, str], str],
    coverage: set[tuple[str, str, str]],
    warehouse_code: str,
) -> tuple[str, str]:
    postcode = str(meta.dest_postcode or "").strip()
    suburb = _normalize_suburb_key(meta.dest_suburb)
    if not warehouse_code or not postcode or not suburb:
        return "", ""

    key = (warehouse_code, postcode, suburb)
    if key not in coverage:
        return "", ""
    zone = zone_lookup.get(key, "")
    if zone:
        return zone, "goexpress_csv"
    return "", ""


def _build_allied_zone_proxy_map(
    meta_by_invoice: dict[str, OrderMeta],
    zone_maps: dict[str, dict[str, str]],
) -> dict[tuple[str, str], str]:
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for invoice_number, zones in zone_maps.items():
        ap_zone = str(zones.get("AP") or "").strip()
        allied_zone = str(zones.get("ALLIED") or "").strip()
        if not ap_zone or not allied_zone:
            continue
        meta = meta_by_invoice.get(invoice_number)
        if meta is None:
            continue
        state = str(meta.dest_state or "").strip().upper()
        if not state:
            continue
        key = (ap_zone, state)
        counts.setdefault(key, {})
        counts[key][allied_zone] = counts[key].get(allied_zone, 0) + 1

    proxy_map: dict[tuple[str, str], str] = {}
    for key, zone_counts in counts.items():
        proxy_map[key] = max(zone_counts.items(), key=lambda item: (item[1], item[0]))[0]
    return proxy_map


def _lookup_allied_zone_from_pdf(
    meta: OrderMeta,
    lookup: dict[tuple[str, str, str], str],
) -> str:
    state = str(meta.dest_state or "").strip().upper()
    postcode = str(meta.dest_postcode or "").strip().zfill(4)
    suburb = " ".join(str(meta.dest_suburb or "").split()).strip().upper()
    if not state or not postcode or not suburb:
        return ""
    return lookup.get((state, postcode, suburb), "")


def _ap_origin_area(site_key: str | None = None) -> str:
    config = get_site_config(site_key) if site_key else get_site_config_for_store_name(STORE_NAME)
    return config.ap_origin_area


def _load_ap_rate_tables(
    session,
    origin_area: str,
) -> tuple[
    dict[str, str],
    dict[str, list[tuple[float, float, float]]],
    dict[str, dict[str, float]],
]:
    area_to_zone: dict[str, str] = {}
    weight_bands: dict[str, list[tuple[float, float, float]]] = {}
    lane_rates: dict[str, dict[str, float]] = {}

    for row in session.sql(
        f"""
        SELECT DISTINCT
            SC.DESTINATION_AREA,
            SC.DESTINATION_ZONE
        FROM KSFPA.ONLINE_PVT.LM_SERVICE_COVERAGE SC
        WHERE SC.RATECARD_ID = 'AP_EPARCEL_20250805'
          AND SC.ORIGIN_AREA = '{origin_area}'
          AND SC.COVERAGE_ACTION = 'INCLUDE'
        """
    ).collect():
        area_to_zone[str(row["DESTINATION_AREA"])] = str(row["DESTINATION_ZONE"])

    for row in session.sql(
        f"""
        SELECT
            SC.DESTINATION_ZONE,
            WB.MIN_WEIGHT_KG,
            WB.MAX_WEIGHT_KG,
            WB.BAND_RATE_EX_GST
        FROM KSFPA.ONLINE_PVT.LM_SERVICE_COVERAGE SC
        JOIN KSFPA.ONLINE_PVT.LM_WEIGHT_BAND WB
          ON WB.RATECARD_ID = SC.RATECARD_ID
         AND WB.ORIGIN_ZONE = '{origin_area}'
         AND WB.DESTINATION_ZONE = SC.DESTINATION_ZONE
        WHERE SC.RATECARD_ID = 'AP_EPARCEL_20250805'
          AND SC.ORIGIN_AREA = '{origin_area}'
          AND SC.COVERAGE_ACTION = 'INCLUDE'
        """
    ).collect():
        zone = str(row["DESTINATION_ZONE"])
        weight_bands.setdefault(zone, []).append(
            (
                _to_float(row["MIN_WEIGHT_KG"]) or 0.0,
                _to_float(row["MAX_WEIGHT_KG"]) or 0.0,
                _to_float(row["BAND_RATE_EX_GST"]) or 0.0,
            )
        )

    lane_rows = session.sql(
        """
        SELECT
            TRIM(LR.RATECARD_ID) AS RATECARD_ID,
            TRIM(LR.DESTINATION_ZONE) AS PRICING_ZONE,
            LR.FIRST_PARCEL_RATE_EX_GST,
            LR.SUBSEQUENT_PARCEL_RATE_EX_GST,
            LR.PER_KG_RATE_EX_GST,
            LR.MINIMUM_CHARGE_EX_GST
        FROM KSFPA.ONLINE_PVT.LM_LANE_RATE LR
        WHERE UPPER(TRIM(LR.RATECARD_ID)) = 'AP_EPARCEL_20250805'
          AND UPPER(TRIM(LR.RATE_LINE_ID)) LIKE 'AP_EPARCEL_OVER5_%'
        """
    ).collect()
    if not lane_rows:
        # Some Snowflake environments expose the current AP over-5 kg rows
        # under a different effective-dated ratecard ID. Prefer the configured
        # ID above, but keep the estimator usable when only the AP row family
        # is available.
        lane_rows = session.sql(
            """
            SELECT
                TRIM(LR.RATECARD_ID) AS RATECARD_ID,
                TRIM(LR.DESTINATION_ZONE) AS PRICING_ZONE,
                LR.FIRST_PARCEL_RATE_EX_GST,
                LR.SUBSEQUENT_PARCEL_RATE_EX_GST,
                LR.PER_KG_RATE_EX_GST,
                LR.MINIMUM_CHARGE_EX_GST
            FROM KSFPA.ONLINE_PVT.LM_LANE_RATE LR
            WHERE REGEXP_LIKE(
                    UPPER(TRIM(LR.RATE_LINE_ID)),
                    '^AP_EPARCEL_OVER5_[0-9]+$'
                  )
            """
        ).collect()
        fallback_ratecard_ids = {
            str(row["RATECARD_ID"] or "").strip()
            for row in lane_rows
            if str(row["RATECARD_ID"] or "").strip()
        }
        if fallback_ratecard_ids:
            # Ratecard IDs are effective-dated in this repository. If the
            # configured ID is absent, use the latest available AP row family
            # rather than mixing rates from multiple versions.
            fallback_ratecard_id = max(fallback_ratecard_ids)
            lane_rows = [
                row
                for row in lane_rows
                if str(row["RATECARD_ID"] or "").strip() == fallback_ratecard_id
            ]

    for row in lane_rows:
        # AP over-5 kg rows store the pricing-zone label in DESTINATION_ZONE;
        # ORIGIN_ZONE is null for this rate-line family.
        pricing_zone = str(row["PRICING_ZONE"] or "").strip()
        if not pricing_zone:
            continue
        lane_rates[pricing_zone] = {
            "first": _to_float(row["FIRST_PARCEL_RATE_EX_GST"]) or 0.0,
            "subsequent": _to_float(row["SUBSEQUENT_PARCEL_RATE_EX_GST"]) or 0.0,
            "per_kg": _to_float(row["PER_KG_RATE_EX_GST"]) or 0.0,
            "minimum": _to_float(row["MINIMUM_CHARGE_EX_GST"]) or 0.0,
        }

    return area_to_zone, weight_bands, lane_rates


def _load_rate_tables(session, site_key: str | None = None) -> dict[str, object]:
    site_config = get_site_config(site_key) if site_key else get_site_config_for_store_name(STORE_NAME)
    allied_origin_zone = site_config.allied_origin_zone
    allied_rates = {}
    allied_remote_area_surcharge = {}
    allied_additional_services = {}
    gobundle_plug = _load_gobundle_plug(GOBUNDLE_PLUG_PATH, _gobundle_site_key())
    cp_rates: dict[str, dict[str, dict[str, float | str]]] = {}
    goexpress_zone_lookup = _load_goexpress_warehouse_zones(GOEXPRESS_WAREHOUSE_ZONES_PATH)
    goexpress_coverage = _load_goexpress_warehouse_destinations(GOEXPRESS_WAREHOUSE_DESTINATION_PATH)
    goexpress_bands = _load_goexpress_ratecard(GOEXPRESS_RATECARD_PATH)
    goexpress_warehouse = _goexpress_warehouse_code()

    for row in session.sql(
        f"""
        SELECT
            DESTINATION_ZONE,
            MINIMUM_CHARGE_EX_GST,
            BASIC_CHARGE_EX_GST,
            PER_KG_RATE_EX_GST
        FROM KSFPA.ONLINE_PVT.LM_LANE_RATE
        WHERE RATECARD_ID = 'ALLIED_ECOM_20260706'
          AND ORIGIN_ZONE = '{allied_origin_zone}'
        """
    ).collect():
        allied_rates[str(row["DESTINATION_ZONE"])] = {
            "minimum": _to_float(row["MINIMUM_CHARGE_EX_GST"]) or 0.0,
            "basic": _to_float(row["BASIC_CHARGE_EX_GST"]) or 0.0,
            "per_kg": _to_float(row["PER_KG_RATE_EX_GST"]) or 0.0,
        }

    local_allied_rates = _load_allied_ecom_cfc_rates(ALLIED_ECOM_CFC_RATECARD_PATH, site_config.site_key)
    if local_allied_rates:
        allied_rates.update(local_allied_rates)
    else:
        legacy_allied_rates = _load_allied_road_rates(ALLIED_ROAD_RATECARD_PATH, allied_origin_zone)
        if legacy_allied_rates:
            allied_rates.update(legacy_allied_rates)

    cp_ratecard_filter = ", ".join(f"'{ratecard_id}'" for ratecard_id in sorted(CP_RATECARD_IDS))
    cp_origin_zone = _couriers_please_origin_zone()
    for row in session.sql(
        f"""
        SELECT
            RATECARD_ID,
            DESTINATION_ZONE,
            MINIMUM_CHARGE_EX_GST,
            BASIC_CHARGE_EX_GST,
            PER_KG_RATE_EX_GST,
            PER_ITEM_RATE_EX_GST,
            FIRST_PARCEL_RATE_EX_GST,
            SUBSEQUENT_PARCEL_RATE_EX_GST,
            INCLUDED_WEIGHT_KG,
            MAX_WEIGHT_KG,
            WEIGHT_ROUNDING_METHOD,
            RATE_BASIS,
            SOURCE_REFERENCE,
            NOTES
        FROM KSFPA.ONLINE_PVT.LM_LANE_RATE
        WHERE RATECARD_ID IN ({cp_ratecard_filter})
          AND ORIGIN_ZONE = '{cp_origin_zone}'
        """
    ).collect():
        ratecard_id = str(row["RATECARD_ID"])
        cp_rates.setdefault(ratecard_id, {})[str(row["DESTINATION_ZONE"])] = {
            "minimum": _to_float(row["MINIMUM_CHARGE_EX_GST"]) or 0.0,
            "basic": _to_float(row["BASIC_CHARGE_EX_GST"]) or 0.0,
            "per_kg": _to_float(row["PER_KG_RATE_EX_GST"]) or 0.0,
            "per_item": _to_float(row["PER_ITEM_RATE_EX_GST"]) or 0.0,
            "first_parcel": _to_float(row["FIRST_PARCEL_RATE_EX_GST"]) or 0.0,
            "subsequent_parcel": _to_float(row["SUBSEQUENT_PARCEL_RATE_EX_GST"]) or 0.0,
            "included_weight_kg": _to_float(row["INCLUDED_WEIGHT_KG"]) or 0.0,
            "max_weight_kg": _to_float(row["MAX_WEIGHT_KG"]) or 0.0,
            "weight_rounding_method": str(row["WEIGHT_ROUNDING_METHOD"] or "").strip(),
            "rate_basis": str(row["RATE_BASIS"] or "").strip(),
            "source_reference": str(row["SOURCE_REFERENCE"] or "").strip(),
            "notes": str(row["NOTES"] or "").strip(),
        }

    if not cp_rates:
        for cp_path in CP_RATECARD_LOCAL_PATHS:
            cp_rates = _load_cp_ratecard_from_csv(cp_path)
            if cp_rates:
                break

    ap_origin_area = _ap_origin_area(site_key)
    ap_area_to_zone, ap_weight_bands, ap_lane_rates = _load_ap_rate_tables(session, ap_origin_area)
    ap_ratecard_origin_area = ap_origin_area
    if (
        (not ap_area_to_zone or not ap_weight_bands or not ap_lane_rates)
        and ap_origin_area != "Sydney"
    ):
        # Keep the run usable if the site-specific AP coverage has not yet
        # been loaded; the pricing-zone fallback still swaps VIC/NSW lanes.
        ap_area_to_zone, ap_weight_bands, ap_lane_rates = _load_ap_rate_tables(session, "Sydney")
        ap_ratecard_origin_area = "Sydney (fallback)"

    if not goexpress_bands:
        for row in session.sql(
            """
            SELECT
                DESTINATION_ZONE,
                MIN_WEIGHT_KG,
                MAX_WEIGHT_KG,
                BAND_RATE_EX_GST
            FROM KSFPA.ONLINE_PVT.LM_WEIGHT_BAND
            WHERE RATECARD_ID = 'GOPEOPLE_GOEXPRESS_20260615'
              AND ORIGIN_ZONE = 'Sydney'
            """
        ).collect():
            goexpress_bands.setdefault((goexpress_warehouse, str(row["DESTINATION_ZONE"])), []).append(
                (
                    _to_float(row["MIN_WEIGHT_KG"]) or 0.0,
                    _to_float(row["MAX_WEIGHT_KG"]) or 0.0,
                    _to_float(row["BAND_RATE_EX_GST"]) or 0.0,
                )
            )

    allied_remote_area_surcharge = _load_allied_remote_area_surcharges(ALLIED_REMOTE_AREA_SURCHARGE_PATH)
    allied_additional_services = _load_allied_additional_services(ALLIED_ADDITIONAL_SERVICES_PATH)
    fuel_surcharge_rules = _load_fuel_surcharge_rules(FUEL_SURCHARGE_RATES_PATH)

    return {
        "allied_rates": allied_rates,
        "allied_remote_area_surcharge": allied_remote_area_surcharge,
        "allied_additional_services": allied_additional_services,
        "gobundle_plug": gobundle_plug,
        "cp_rates": cp_rates,
        "ap_area_to_zone": ap_area_to_zone,
        "ap_weight_bands": ap_weight_bands,
        "ap_lane_rates": ap_lane_rates,
        "ap_origin_area": ap_origin_area,
        "ap_ratecard_origin_area": ap_ratecard_origin_area,
        "goexpress_bands": goexpress_bands,
        "goexpress_zone_lookup": goexpress_zone_lookup,
        "goexpress_coverage": goexpress_coverage,
        "goexpress_warehouse": goexpress_warehouse,
        "fuel_surcharge_rules": fuel_surcharge_rules,
    }


def _load_fuel_surcharge_rules(path: Path) -> list[FuelSurchargeRule]:
    if not path.exists():
        return []
    rules: list[FuelSurchargeRule] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                rules.append(
                    FuelSurchargeRule(
                        carrier=str(row.get("carrier") or "").strip(),
                        service=str(row.get("service") or "").strip(),
                        start_date=date.fromisoformat(str(row.get("start_date") or "").strip()),
                        end_date=date.fromisoformat(str(row.get("end_date") or "").strip()),
                        pct=_to_float(row.get("fuel_surcharge_pct")) or 0.0,
                        source_note=str(row.get("source_note") or "").strip(),
                    )
                )
            except ValueError as error:
                raise ValueError(f"Invalid fuel surcharge rule in {path}: {row}") from error
    return rules


def _load_cp_ratecard_from_csv(path: Path) -> dict[str, dict[str, dict[str, float | str]]]:
    if not path.exists():
        return {}
    cp_rates: dict[str, dict[str, dict[str, float | str]]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ratecard_id = str(row.get("RATECARD_ID") or "").strip()
            zone = str(row.get("DESTINATION_ZONE") or "").strip()
            if not ratecard_id or not zone:
                continue
            per_item = row.get("PER_ITEM_RATE_EX_GST")
            if per_item is None:
                per_item = row.get("PER_ITEM_EX_GST")
            cp_rates.setdefault(ratecard_id, {})[zone] = {
                "minimum": _to_float(row.get("MINIMUM_CHARGE_EX_GST")) or 0.0,
                "basic": _to_float(row.get("BASIC_CHARGE_EX_GST")) or 0.0,
                "per_kg": _to_float(row.get("PER_KG_RATE_EX_GST")) or 0.0,
                "per_item": _to_float(per_item) or 0.0,
                "first_parcel": _to_float(row.get("FIRST_PARCEL_RATE_EX_GST")) or 0.0,
                "subsequent_parcel": _to_float(row.get("SUBSEQUENT_PARCEL_RATE_EX_GST")) or 0.0,
                "included_weight_kg": _to_float(row.get("INCLUDED_WEIGHT_KG")) or 0.0,
                "max_weight_kg": _to_float(row.get("MAX_WEIGHT_KG")) or 0.0,
                "weight_rounding_method": str(row.get("WEIGHT_ROUNDING_METHOD") or "").strip(),
                "rate_basis": str(row.get("RATE_BASIS") or "").strip(),
                "source_reference": str(row.get("SOURCE_REFERENCE") or "").strip(),
                "notes": str(row.get("NOTES") or "").strip(),
            }
    return cp_rates


def _estimate_order_chargeable_kg(parcels: Iterable[ParcelPhysicalInput]) -> float:
    total_weight = 0.0
    total_cube = 0.0
    for parcel in parcels:
        total_weight += max(parcel.parcel_weight_kg or 0.0, 0.0)
        total_cube += max(parcel.parcel_cube_cm3 or 0.0, 0.0)
    return max(total_weight, total_cube / VOLUMETRIC_DIVISOR_CM3_PER_KG)


def _estimate_service_costs(
    meta: OrderMeta,
    zones: dict[str, str],
    rate_tables: dict[str, object],
    parcel_weights: list[float],
    parcels: list[ParcelPhysicalInput],
    ap_parcel_weights: list[float] | None = None,
) -> tuple[list[ServiceCost], AlliedChargeBreakdown, float | None, str]:
    estimates: list[ServiceCost] = []
    parcel_weights = [max(weight, 0.0) for weight in parcel_weights]
    ap_weights = [max(weight, 0.0) for weight in (ap_parcel_weights or parcel_weights)]

    allied_zone = zones.get("ALLIED", "")
    cp_zone = zones.get("CP", "")
    ap_zone = zones.get("AP", "")

    allied_breakdown = _allied_charge_breakdown(
        meta,
        allied_zone,
        parcel_weights,
        parcels,
        rate_tables["allied_rates"],
        rate_tables["allied_remote_area_surcharge"],
        rate_tables["allied_additional_services"],
    )
    allied = allied_breakdown.total_ex_gst
    estimates.append(ServiceCost("Allied Express", "Allied Express", allied))

    cp, cp_pricing_chargeable_weight_kg, cp_pricing_cap_applied, cp_pricing_ratecard_id, cp_pricing_basis_note = _couriers_please_invoice_cost(
        cp_zone,
        meta.actual_order_chargeable_kg if meta.actual_order_chargeable_kg is not None else _sum_costs(parcel_weights),
        len(parcels),
        rate_tables["cp_rates"],
    )
    estimates.append(ServiceCost("Couriers Please", "Couriers Please", cp))

    if meta.actual_carrier == "GoBUNDLE":
        gobundle_cost_per_parcel = float(rate_tables["gobundle_plug"].get("cost_per_parcel_ex_gst") or 0.0)
        gobundle = gobundle_cost_per_parcel * len(parcels)
        estimates.append(ServiceCost("GoBUNDLE", "GoBUNDLE", gobundle))

    goexpress_zone = zones.get("GOEXPRESS", "")
    goexpress = _sum_costs(
        _goexpress_band_cost(
            str(rate_tables["goexpress_warehouse"]),
            goexpress_zone,
            weight,
            rate_tables["goexpress_bands"],
        )
        for weight in parcel_weights
    )
    estimates.append(ServiceCost("GoPeople", "GoEXPRESS", goexpress))

    ap_pricing_zone = _ap_pricing_zone(
        ap_zone,
        meta.dest_state,
        rate_tables["ap_area_to_zone"],
    )
    ap = _ap_cost(
        ap_zone,
        rate_tables["ap_area_to_zone"],
        rate_tables["ap_weight_bands"],
        rate_tables["ap_lane_rates"],
        ap_weights,
        ap_pricing_zone,
    )
    estimates.append(ServiceCost("Australia Post", "eParcel", ap))
    estimates = _apply_fuel_surcharges(estimates, meta.despatch_date, rate_tables["fuel_surcharge_rules"])

    return (
        estimates,
        allied_breakdown,
        cp_pricing_chargeable_weight_kg,
        cp_pricing_cap_applied,
        cp_pricing_ratecard_id,
        cp_pricing_basis_note,
    )


def _apply_fuel_surcharges(
    estimates: list[ServiceCost],
    despatch_date: str,
    rules: list[FuelSurchargeRule],
) -> list[ServiceCost]:
    try:
        effective_date = date.fromisoformat(str(despatch_date))
    except ValueError:
        return estimates
    adjusted: list[ServiceCost] = []
    for estimate in estimates:
        rule = next(
            (
                candidate
                for candidate in rules
                if candidate.start_date <= effective_date <= candidate.end_date
                and _fuel_rule_matches(candidate.carrier, estimate.carrier)
                and _fuel_rule_matches(candidate.service, estimate.service)
            ),
            None,
        )
        if rule is None or estimate.cost_ex_gst is None:
            adjusted.append(estimate)
            continue
        fuel = estimate.cost_ex_gst * rule.pct / 100.0
        adjusted.append(
            replace(
                estimate,
                cost_ex_gst=estimate.cost_ex_gst + fuel,
                base_cost_ex_gst=estimate.cost_ex_gst,
                fuel_surcharge_ex_gst=fuel,
                fuel_surcharge_pct=rule.pct,
                fuel_surcharge_source=rule.source_note,
            )
        )
    return adjusted


def _fuel_rule_matches(rule_value: str, actual_value: str) -> bool:
    return not rule_value or rule_value == "*" or rule_value.strip().upper() == actual_value.strip().upper()


def _service_base_cost(service_cost: ServiceCost | None) -> float | None:
    if service_cost is None:
        return None
    return service_cost.base_cost_ex_gst if service_cost.base_cost_ex_gst is not None else service_cost.cost_ex_gst


def _couriers_please_invoice_cost(
    zone: str,
    invoice_chargeable_kg: float,
    parcel_count: int,
    rates: dict[str, dict[str, dict[str, float | str]]],
) -> tuple[float | None, float | None, str, str, str]:
    ratecard_id = _couriers_please_ratecard_id(invoice_chargeable_kg, parcel_count)
    ratebook = rates.get(ratecard_id, {})
    rate = ratebook.get(str(zone or "").strip())
    if rate is None:
        return None, None, "", ratecard_id, ""
    pricing_chargeable_kg = float(math.ceil(max(invoice_chargeable_kg or 0.0, 0.0)))
    if ratecard_id == "CP_I99_20260504":
        cost = max(float(rate["minimum"]), float(rate["basic"]) + pricing_chargeable_kg * float(rate["per_kg"]))
        basis_note = "Consignment-level charging; invoice chargeable weight is rounded up to the next whole kg."
    else:
        per_item = float(rate.get("per_item") or 0.0)
        cost = per_item if per_item > 0 else max(float(rate["minimum"]), float(rate["basic"]) + pricing_chargeable_kg * float(rate["per_kg"]))
        basis_note = "Single-parcel tier pricing; cost is taken from the parcel band ratecard."
    cap_applied = "Y" if parcel_count > 10 else ""
    return cost, pricing_chargeable_kg, cap_applied, ratecard_id, basis_note


def _couriers_please_ratecard_id(invoice_chargeable_kg: float, parcel_count: int) -> str:
    if parcel_count > 1:
        return "CP_I99_20260504"
    weight = max(invoice_chargeable_kg or 0.0, 0.0)
    for lower, upper, ratecard_id in CP_SINGLE_PARCEL_RATECARD_BANDS:
        if lower <= weight < upper:
            return ratecard_id
    return "CP_I99_20260504"


def _allied_cost(zone: str, chargeable_kg: float, rates: dict[str, dict[str, float]]) -> float | None:
    effective_zone = _allied_effective_zone(zone)
    rate = rates.get(effective_zone)
    if rate is None:
        return None
    pricing_chargeable_kg = math.ceil(max(chargeable_kg, 0.0))
    return max(rate["minimum"], rate["basic"] + pricing_chargeable_kg * rate["per_kg"])


def _allied_effective_zone(zone: str) -> str:
    zone = str(zone or "").strip()
    return ALLIED_ZONE_RATECARD_OVERRIDES.get(zone, zone)


def _goexpress_band_cost(
    warehouse: str,
    zone: str,
    chargeable_kg: float,
    bands: dict[tuple[str, str], list[tuple[float, float, float]]],
) -> float | None:
    chargeable_kg = round(max(chargeable_kg, 0.0), 2)
    zone_bands = bands.get((warehouse, zone), [])
    for min_w, max_w, cost in zone_bands:
        if chargeable_kg >= min_w and chargeable_kg <= max_w + 1e-9:
            return cost
    if zone_bands and chargeable_kg > max(max_w for _, max_w, _ in zone_bands):
        # The source ratecard only provides bands up to the highest published ceiling.
        # When parcels exceed that ceiling, price them at the top band rather than
        # leaving the order unresolved.
        return max(zone_bands, key=lambda band: band[1])[2]
    return None


def _goexpress_weight_cap_details(
    warehouse: str,
    zone: str,
    parcel_weights: list[float],
    bands: dict[tuple[str, str], list[tuple[float, float, float]]],
) -> tuple[str, float | None, str]:
    """Describe when the top published GoEXPRESS band is used above its ceiling."""
    zone_bands = bands.get((warehouse, zone), [])
    if not zone_bands:
        return "", None, ""
    maximum_weight = max(max_weight for _, max_weight, _ in zone_bands)
    if any(weight > maximum_weight + 1e-9 for weight in parcel_weights):
        return (
            "Y",
            maximum_weight,
            f"Parcel above published GoEXPRESS {maximum_weight:.2f} kg ceiling; top published band applied.",
        )
    return "", maximum_weight, ""


def _allied_charge_breakdown(
    meta: OrderMeta,
    allied_zone: str,
    parcel_weights: list[float],
    parcels: list[ParcelPhysicalInput],
    rates: dict[str, dict[str, float]],
    remote_area_surcharge: dict[tuple[str, str, str], tuple[float, float]],
    additional_services: dict[str, float],
) -> AlliedChargeBreakdown:
    total_chargeable_kg = sum(max(weight, 0.0) for weight in parcel_weights)
    # Allied ROAD is invoiced at the job/consignment grain, not once per estimated parcel.
    base = _allied_cost(allied_zone, total_chargeable_kg, rates) or 0.0

    remote = 0.0
    remote_rate = remote_area_surcharge.get(_remote_area_key(meta))
    if remote_rate is not None:
        remote_basic, remote_per_kg = remote_rate
        remote = remote_basic + (remote_per_kg * total_chargeable_kg)

    overweight = 0.0
    if total_chargeable_kg >= 90.0:
        overweight = additional_services.get("OWS_90_PLUS", 0.0)
    elif total_chargeable_kg >= 56.0:
        overweight = additional_services.get("OWS_56_TO_90", 0.0)

    oversized = 0.0
    for parcel in parcels:
        longest_side_m = max(
            max(parcel.estimated_height_cm or 0.0, 0.0),
            max(parcel.estimated_width_cm or 0.0, 0.0),
            max(parcel.estimated_length_cm or 0.0, 0.0),
        ) / 100.0
        if 2.21 <= longest_side_m <= 3.60:
            oversized += additional_services.get("OIS_2_21_TO_3_60", 0.0)
        elif 3.60 < longest_side_m <= 6.00:
            oversized += additional_services.get("OIS_3_60_TO_6_00", 0.0)

    manual_measurement = 0.0
    reverse_logistics = 0.0

    return AlliedChargeBreakdown(
        base_ex_gst=base,
        remote_area_surcharge_ex_gst=remote,
        overweight_surcharge_ex_gst=overweight,
        oversized_item_surcharge_ex_gst=oversized,
        manual_measurement_fee_ex_gst=manual_measurement,
        reverse_logistics_surcharge_ex_gst=reverse_logistics,
    )


def _remote_area_key(meta: OrderMeta) -> tuple[str, str, str]:
    return (
        str(meta.dest_state or "").strip().upper(),
        str(meta.dest_postcode or "").strip(),
        " ".join(str(meta.dest_suburb or "").split()).strip().upper(),
    )


def _load_allied_road_rates(path: Path, origin_zone: str) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}

    rates: dict[str, dict[str, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("service", "")).strip().upper() != "ROAD":
                continue
            if str(row.get("from_zone", "")).strip().upper() != origin_zone.upper():
                continue
            to_zone = str(row.get("to_zone", "")).strip().upper()
            if not to_zone:
                continue
            rates[to_zone] = {
                "minimum": _to_float(row.get("minimum_charge_ex_gst")) or 0.0,
                "basic": _to_float(row.get("basic_charge_ex_gst")) or 0.0,
                "per_kg": _to_float(row.get("per_kg_rate_ex_gst")) or 0.0,
            }
    return rates


def _load_allied_ecom_cfc_rates(path: Path, site_key: str) -> dict[str, dict[str, float]]:
    """Load the site-specific origin column from the proposed Allied ECOM ratecard."""
    if not path.exists():
        return {}

    rates: dict[str, dict[str, float]] = {}
    requested_site = str(site_key or "").strip().upper()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("origin_site", "")).strip().upper() != requested_site:
                continue
            destination_zone = str(row.get("destination_zone", "")).strip().upper()
            if not destination_zone:
                continue
            rates[destination_zone] = {
                "minimum": _to_float(row.get("minimum_charge_ex_gst")) or 0.0,
                "basic": _to_float(row.get("basic_charge_ex_gst")) or 0.0,
                "per_kg": _to_float(row.get("per_kg_rate_ex_gst")) or 0.0,
            }
    return rates


def _load_allied_remote_area_surcharges(path: Path) -> dict[tuple[str, str, str], tuple[float, float]]:
    if not path.exists():
        return {}

    lookup: dict[tuple[str, str, str], tuple[float, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            state = str(row.get("state", "")).strip().upper()
            postcode = str(row.get("postcode", "")).strip()
            suburb = " ".join(str(row.get("suburb", "")).split()).strip().upper()
            if not state or not postcode or not suburb:
                continue
            lookup[(state, postcode, suburb)] = (
                _to_float(row.get("basic")) or 0.0,
                _to_float(row.get("per_kg")) or 0.0,
            )
    return lookup


def _load_allied_additional_services(path: Path) -> dict[str, float]:
    defaults = {
        "OWS_56_TO_90": 20.60,
        "OWS_90_PLUS": 51.50,
        "OIS_2_21_TO_3_60": 51.50,
        "OIS_3_60_TO_6_00": 360.50,
        "MM": 11.11,
        "TL": 49.45,
        "HU": 52.74,
        "WT": 11.29,
        "FT": 18.55,
        "MPFEE": 35.07,
        "DGC": 29.72,
    }
    if not path.exists():
        return defaults

    services = dict(defaults)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            code = str(row.get("code", "")).strip().upper()
            amount = _to_float(row.get("amount_ex_gst"))
            if code and amount is not None:
                services[code] = amount
    return services


def _lane_cost(zone: str, chargeable_kg: float, rates: dict[str, dict[str, float]]) -> float | None:
    rate = rates.get(zone)
    if rate is None:
        return None
    return max(rate["minimum"], rate["basic"] + chargeable_kg * rate["per_kg"])


_AP_ZONE_ORIGIN_OVERRIDES = {
    "Sydney": {
        "Sydney": "Same State Local",
        "NSW Metro": "Same State Metro",
        "NSW Country": "Same State Remote",
        "Melbourne": "Near State Capital",
        "VIC Metro": "Near State Metro",
        "VIC Country": "Near State Remote",
    },
    "Melbourne": {
        "Melbourne": "Same State Local",
        "VIC Metro": "Same State Metro",
        "VIC Country": "Same State Remote",
        "Sydney": "Near State Capital",
        "NSW Metro": "Near State Metro",
        "NSW Country": "Near State Remote",
    },
}


def _ap_pricing_zone(
    ap_zone: object,
    destination_state: object,
    area_to_zone: dict[str, str],
    site_key: str | None = None,
) -> str:
    zone = str(ap_zone or "").strip()
    destination = str(destination_state or "").strip().upper()
    config = get_site_config(site_key) if site_key else get_site_config_for_store_name(STORE_NAME)
    override = _AP_ZONE_ORIGIN_OVERRIDES.get(config.ap_origin_area, {}).get(zone)
    zone_state = "NSW" if zone in {"Sydney", "NSW Metro", "NSW Country"} else "VIC" if zone in {"Melbourne", "VIC Metro", "VIC Country"} else ""
    if override and (not destination or destination == zone_state):
        return override
    return area_to_zone.get(zone, "")


def _weight_band_cost(zone: str, chargeable_kg: float, bands: dict[str, list[tuple[float, float, float]]]) -> float | None:
    chargeable_kg = round(max(chargeable_kg, 0.0), 1)
    for min_w, max_w, cost in bands.get(zone, []):
        if chargeable_kg >= min_w and chargeable_kg <= max_w + 1e-9:
            return cost
    return None


def _ap_cost(
    zone: str,
    area_to_zone: dict[str, str],
    bands: dict[str, list[tuple[float, float, float]]],
    lane_rates: dict[str, dict[str, float]],
    parcel_weights: list[float] | None = None,
    pricing_zone: str | None = None,
) -> float | None:
    pricing_zone = (pricing_zone or area_to_zone.get(zone, "")).strip()
    if not pricing_zone:
        return None

    weights = parcel_weights or []
    if not weights:
        return None
    adjusted_weights, overflow_kg = _cap_and_redistribute_parcel_weights(weights, AP_MAX_PARCEL_KG)
    if overflow_kg > 0:
        return None

    total = 0.0
    rate = lane_rates.get(pricing_zone)
    for index, weight in enumerate(adjusted_weights):
        if weight <= 5.0:
            parcel_cost = _weight_band_cost(pricing_zone, weight, bands)
        else:
            if rate is None:
                return None
            parcel_cost = rate["first"] + rate["per_kg"] * weight if index == 0 else rate["subsequent"] + rate["per_kg"] * weight
            parcel_cost = max(rate["minimum"], parcel_cost)
        if parcel_cost is None:
            return None
        total += parcel_cost
    return total


def _cost_for_service(service_costs: list[ServiceCost], service: str | None) -> ServiceCost | None:
    if service is None:
        return None
    for item in service_costs:
        if item.service == service:
            return item
    return None


def _pack_flow_summary(rule_name: str, parcels: list[ParcelPhysicalInput]) -> str:
    if rule_name == "ALL_TOTEABLE_EQUAL_SPLIT":
        return "All Auto"
    if rule_name == "MIXED_MANUAL_TOTEABLE":
        return "Mixed"
    if rule_name == "CONTAINS_BULKY":
        return "All Manual"

    fit_bands = {parcel.fit_band for parcel in parcels}
    if fit_bands <= {"TOTE_LIKE"}:
        return "All Auto"
    if fit_bands <= {"MANUAL_LIKE", "BULKY_LIKE"}:
        return "All Manual"
    return "Mixed"


def _normalize_parcel_weights(parcels: list[ParcelPhysicalInput], total_order_weight_kg: float | None) -> list[float]:
    if not parcels:
        return []

    if total_order_weight_kg is None or total_order_weight_kg <= 0:
        return [max(parcel.parcel_weight_kg, 0.0) for parcel in parcels]

    raw_weights = [max(parcel.parcel_weight_kg, 0.0) for parcel in parcels]
    raw_total = sum(raw_weights)
    if raw_total <= 0:
        equal_share = total_order_weight_kg / len(parcels)
        normalized = [equal_share for _ in parcels]
    else:
        normalized = [total_order_weight_kg * (weight / raw_total) for weight in raw_weights]

    if normalized:
        normalized[-1] += total_order_weight_kg - sum(normalized)
    return normalized


def _sum_costs(values: Iterable[float | None]) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        if value is None:
            return None
        total += value
        seen = True
    return total if seen else None


def _cap_and_redistribute_parcel_weights(weights: list[float], cap_kg: float) -> tuple[list[float], float]:
    if not weights:
        return [], 0.0

    adjusted = [max(weight, 0.0) for weight in weights]
    overflow_kg = 0.0

    for index, weight in enumerate(adjusted):
        if weight > cap_kg:
            overflow_kg += weight - cap_kg
            adjusted[index] = cap_kg

    if overflow_kg <= 0:
        return adjusted, 0.0

    for index in sorted(range(len(adjusted)), key=lambda item: (adjusted[item], item)):
        if overflow_kg <= 0:
            break
        available = cap_kg - adjusted[index]
        if available <= 0:
            continue
        transfer = min(available, overflow_kg)
        adjusted[index] += transfer
        overflow_kg -= transfer

    return adjusted, overflow_kg


def _actual_cost_missing_reason(
    actual_carrier: str,
    actual_service: str | None,
    order: OrderParcels,
    zones: dict[str, str],
    rate_tables: dict[str, object],
    ap_pricing_zone: str,
    actual_cost: ServiceCost | None,
    ap_overflow_kg: float,
    ap_parcel_weights: list[float] | None,
) -> str:
    if actual_cost is not None and actual_cost.cost_ex_gst is not None:
        return ""

    if not actual_service:
        return "unsupported_carrier"

    allied_zone = zones.get("ALLIED", "")
    cp_zone = zones.get("CP", "")
    ap_zone = zones.get("AP", "")
    goexpress_zone = zones.get("GOEXPRESS", "")

    if actual_service == "Allied Express":
        if not allied_zone:
            return "missing_allied_zone"
        return "no_allied_rate_match"

    if actual_service == "Couriers Please":
        if not cp_zone:
            return "missing_cp_zone"
        return "no_cp_rate_match"

    if actual_service == "GoBUNDLE":
        return ""

    if actual_service == "GoEXPRESS":
        if not goexpress_zone:
            return "missing_goexpress_zone"
        warehouse = str(rate_tables["goexpress_warehouse"])
        for index, weight in enumerate(parcel_weights or [], start=1):
            if _goexpress_band_cost(warehouse, goexpress_zone, weight, rate_tables["goexpress_bands"]) is None:
                return f"no_goexpress_band_match_parcel_{index}"
        return "no_goexpress_rate_match"

    if actual_service == "eParcel":
        if not ap_zone:
            return "missing_ap_zone"
        if not ap_pricing_zone:
            return "missing_ap_coverage"
        if ap_overflow_kg > 0:
            return "ap_over_22kg_not_supported"
        # Use the same capped/redistributed weights that _ap_cost receives so
        # the explanation describes the failed pricing path, not the raw
        # normalized allocation used earlier in the pipeline.
        weights = ap_parcel_weights or []
        if any(weight <= 5.0 and _weight_band_cost(ap_pricing_zone, weight, rate_tables["ap_weight_bands"]) is None for weight in weights):
            return "no_ap_weight_band_match_under_5kg"
        if any(weight > 5.0 for weight in weights) and rate_tables["ap_lane_rates"].get(ap_pricing_zone) is None:
            return "no_ap_lane_rate_match_over_5kg"
        return ""

    return "unsupported_carrier"


def _gobundle_site_key() -> str:
    return get_site_config_for_store_name(STORE_NAME).gobundle_site_key


def _load_gobundle_plug(path: Path, site_key: str) -> dict[str, object]:
    default_plug = {
        "site": site_key,
        "carrier": "GoBUNDLE",
        "cost_per_parcel_ex_gst": 0.0,
        "notes": "",
        "source": "default_zero",
    }
    if not path.exists():
        return default_plug

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_site = str(row.get("site") or "").strip().upper() or "DEFAULT"
            if row_site not in {site_key, "DEFAULT"}:
                continue
            carrier = str(row.get("carrier") or "").strip()
            if carrier and carrier.upper() != "GOBUNDLE":
                continue
            plug = _to_float(row.get("cost_per_parcel_ex_gst"))
            return {
                "site": row_site,
                "carrier": carrier or "GoBUNDLE",
                "cost_per_parcel_ex_gst": plug or 0.0,
                "notes": str(row.get("notes") or "").strip(),
                "source": str(path),
            }
    return default_plug


def _actual_cost_missing_detail(
    actual_cost_missing_reason: str,
    meta: OrderMeta,
    zones: dict[str, str],
    ap_pricing_zone: str,
    allied_zone_source: str,
) -> str:
    postcode = meta.dest_postcode or "unknown postcode"
    suburb = meta.dest_suburb or "unknown suburb"
    state = meta.dest_state or "unknown state"
    allied_zone = zones.get("ALLIED", "")
    cp_zone = zones.get("CP", "")
    ap_zone = zones.get("AP", "")
    goexpress_zone = zones.get("GOEXPRESS", "")

    if actual_cost_missing_reason == "missing_allied_zone":
        return f"Allied zone missing for {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "missing_cp_zone":
        return f"Couriers Please zone missing for {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "missing_goexpress_zone":
        return f"GoEXPRESS zone missing for {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "missing_ap_zone":
        return f"Australia Post zone missing for {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "missing_ap_coverage":
        return f"Australia Post pricing coverage missing for AP zone {ap_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "ap_over_22kg_not_supported":
        return f"AP parcel overflow above 21.99 kg at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason.startswith("no_goexpress_band_match_parcel_"):
        parcel_no = actual_cost_missing_reason.rsplit("_", 1)[-1]
        return f"GoEXPRESS band missing for parcel {parcel_no} in zone {goexpress_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "no_goexpress_rate_match":
        return f"GoEXPRESS lane match missing for zone {goexpress_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "no_ap_weight_band_match_under_5kg":
        return f"AP under-5kg band missing for pricing zone {ap_pricing_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "no_ap_lane_rate_match_over_5kg":
        return f"AP over-5kg lane rate missing for pricing zone {ap_pricing_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "no_allied_rate_match":
        source_text = f" via {allied_zone_source}" if allied_zone_source else ""
        return f"Allied rate missing for Allied zone {allied_zone or 'unknown'}{source_text} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "no_cp_rate_match":
        return f"Couriers Please rate missing for zone {cp_zone or 'unknown'} at {suburb}, {state} {postcode}"
    if actual_cost_missing_reason == "unsupported_carrier":
        return f"Unsupported carrier for {suburb}, {state} {postcode}"
    return f"Missing cost detail for {suburb}, {state} {postcode}"


def _write_output(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "invoice_number",
                "actual_carrier",
                "actual_service",
                "carrier_tracking_number",
                "despatch_date",
                "kmart_accounting_week_number",
                "kmart_accounting_week_number_in_year",
                "kmart_accounting_month_number",
                "kmart_accounting_period_number",
                "kmart_accounting_period_description",
                "kmart_accounting_quarter_description",
                "kmart_accounting_year",
                "dest_suburb",
                "dest_state",
                "dest_postcode",
                "quoted_cost_inc_gst",
                "shippit_chargeable_weight_kg",
                "shippit_chargeable_cube_cm3",
                "est_chargeable_weight_kg",
                "est_packed_sku_weight_kg",
                "est_packed_sku_cube_total_cm3",
                "est_packed_sku_cube_total_m3",
                "est_packed_sku_cube_imputed_flag",
                "est_parcel_cube_total_cm3",
                "est_parcel_cube_total_m3",
                "est_volumetric_weight_kg",
                "pricing_scale_factor",
                "pricing_scaled_parcel_cube_total_cm3",
                "pricing_scaled_parcel_cube_total_m3",
                "pricing_scaled_parcel_volumetric_weight_kg",
                "pricing_scaled_parcel_cube_pattern_cm3",
                "shippit_dead_weight_kg",
                "shippit_volumetric_weight_kg",
                "shippit_volumetric_cube_cm3",
                "shippit_vs_estimated_weight_gap_kg",
                "shippit_vs_estimated_cube_gap_cm3",
                "shippit_vs_estimated_cube_gap_m3",
                "weight_gap_kg",
                "cube_gap_equivalent_kg",
                "chargeable_gap_kg",
                "box_suite_source",
                "box_suite_family",
                "box_suite_band",
                "box_suite_match_count",
                "parcel_units_total",
                "parcel_units_per_package",
                "actual_parcel_count",
                "estimated_parcel_count",
                "normalized_parcel_weight_total_kg",
                "normalized_parcel_weight_per_package",
                "allocated_dead_weight_per_package_kg",
                "pricing_scaled_parcel_volumetric_weight_per_package_kg",
                "pricing_parcel_chargeable_weight_total_kg",
                "pricing_parcel_chargeable_weight_per_package_kg",
                "ap_adjusted_parcel_weight_total_kg",
                "ap_adjusted_parcel_weight_per_package",
                "ap_overflow_kg",
                "cost_weight_basis_kg",
                "reporting_weight_band",
                "cost_parcel_count_basis",
                "rule_name",
                "pack_flow_summary",
                "assignment_scope",
                "assignment_required",
                "costing_grain",
                "allied_zone",
                "allied_zone_exact",
                "allied_zone_source",
                "cp_zone",
                "ap_zone",
                "ap_pricing_zone",
                "ap_origin_area",
                "ap_ratecard_origin_area",
                "goexpress_zone",
                "goexpress_zone_source",
                "goexpress_weight_cap_applied",
                "goexpress_published_max_weight_kg",
                "goexpress_weight_cap_note",
                "gobundle_plug_site",
                "gobundle_plug_cost_per_parcel_ex_gst",
                "gobundle_plug_source",
                "cp_pricing_chargeable_weight_kg",
                "cp_pricing_cap_applied",
                "cp_pricing_ratecard_id",
                "cp_pricing_basis_note",
                "placeholder_parcel",
                "allied_base_cost_ex_gst",
                "allied_remote_area_surcharge_ex_gst",
                "allied_overweight_surcharge_ex_gst",
                "allied_oversized_item_surcharge_ex_gst",
                "allied_manual_measurement_fee_ex_gst",
                "allied_reverse_logistics_surcharge_ex_gst",
                "allied_additional_services_ex_gst",
                "allied_total_cost_ex_gst",
                "actual_est_base_cost_ex_gst",
                "actual_est_fuel_surcharge_ex_gst",
                "actual_est_fuel_surcharge_pct",
                "actual_est_fuel_surcharge_source",
                "actual_est_fsc_charge_ex_gst",
                "actual_est_fsc_rate_pct",
                "actual_est_fsc_source",
                "actual_est_cost_ex_gst",
                "actual_est_cost_missing_reason",
                "actual_est_cost_missing_detail",
                "actual_est_service",
                "cheapest_alt_carrier",
                "cheapest_alt_service",
                "cheapest_alt_base_cost_ex_gst",
                "cheapest_alt_fuel_surcharge_ex_gst",
                "cheapest_alt_cost_ex_gst",
                "est_saving_ex_gst",
                "cheaper_option_exists",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["invoice_number"],
                    row["actual_carrier"],
                    row["actual_service"],
                    row["carrier_tracking_number"],
                    row["despatch_date"],
                    row["kmart_accounting_week_number"],
                    row["kmart_accounting_week_number_in_year"],
                    row["kmart_accounting_month_number"],
                    row["kmart_accounting_period_number"],
                    row["kmart_accounting_period_description"],
                    row["kmart_accounting_quarter_description"],
                    row["kmart_accounting_year"],
                    row["dest_suburb"],
                    row["dest_state"],
                    row["dest_postcode"],
                    _fmt(row["quoted_cost_inc_gst"]),
                    _fmt(row["shippit_chargeable_weight_kg"]),
                    _fmt(row["shippit_chargeable_cube_cm3"]),
                    _fmt(row["est_chargeable_weight_kg"]),
                    _fmt(row["est_packed_sku_weight_kg"]),
                    _fmt(row["est_packed_sku_cube_total_cm3"]),
                    _fmt(row["est_packed_sku_cube_total_m3"]),
                    row["est_packed_sku_cube_imputed_flag"],
                    _fmt(row["est_parcel_cube_total_cm3"]),
                    _fmt(row["est_parcel_cube_total_m3"]),
                    _fmt(row["est_volumetric_weight_kg"]),
                    _fmt_ratio(row["pricing_scale_factor"]),
                    _fmt(row["pricing_scaled_parcel_cube_total_cm3"]),
                    _fmt(row["pricing_scaled_parcel_cube_total_m3"]),
                    _fmt(row["pricing_scaled_parcel_volumetric_weight_kg"]),
                    row["pricing_scaled_parcel_cube_pattern_cm3"],
                    _fmt(row["shippit_dead_weight_kg"]),
                    _fmt(row["shippit_volumetric_weight_kg"]),
                    _fmt(row["shippit_volumetric_cube_cm3"]),
                    _fmt(row["shippit_vs_estimated_weight_gap_kg"]),
                    _fmt(row["shippit_vs_estimated_cube_gap_cm3"]),
                    _fmt(row["shippit_vs_estimated_cube_gap_m3"]),
                    _fmt(row["weight_gap_kg"]),
                    _fmt(row["cube_gap_equivalent_kg"]),
                    _fmt(row["chargeable_gap_kg"]),
                    row["box_suite_source"],
                    row["box_suite_family"],
                    row["box_suite_band"],
                    row["box_suite_match_count"],
                    row["parcel_units_total"],
                    row["parcel_units_per_package"],
                    row["actual_parcel_count"],
                    row["estimated_parcel_count"],
                    _fmt(row["normalized_parcel_weight_total_kg"]),
                    row["normalized_parcel_weight_per_package"],
                    row["allocated_dead_weight_per_package_kg"],
                    row["pricing_scaled_parcel_volumetric_weight_per_package_kg"],
                    _fmt(row["pricing_parcel_chargeable_weight_total_kg"]),
                    row["pricing_parcel_chargeable_weight_per_package_kg"],
                    _fmt(row["ap_adjusted_parcel_weight_total_kg"]),
                    row["ap_adjusted_parcel_weight_per_package"],
                    _fmt(row["ap_overflow_kg"]),
                    _fmt(row["cost_weight_basis_kg"]),
                    row["reporting_weight_band"],
                    row["cost_parcel_count_basis"],
                    row["rule_name"],
                    row["pack_flow_summary"],
                    row["assignment_scope"],
                    row["assignment_required"],
                    row["costing_grain"],
                    row["allied_zone"],
                    row["allied_zone_exact"],
                    row["allied_zone_source"],
                    row["cp_zone"],
                    row["ap_zone"],
                    row["ap_pricing_zone"],
                    row["ap_origin_area"],
                    row["ap_ratecard_origin_area"],
                    row["goexpress_zone"],
                    row["goexpress_zone_source"],
                    row["goexpress_weight_cap_applied"],
                    _fmt(row["goexpress_published_max_weight_kg"]),
                    row["goexpress_weight_cap_note"],
                    row["gobundle_plug_site"],
                    _fmt(row["gobundle_plug_cost_per_parcel_ex_gst"]),
                    row["gobundle_plug_source"],
                    _fmt(row["cp_pricing_chargeable_weight_kg"]),
                    row["cp_pricing_cap_applied"],
                    row["cp_pricing_ratecard_id"],
                    row["cp_pricing_basis_note"],
                    row["placeholder_parcel"],
                    _fmt(row["allied_base_cost_ex_gst"]),
                    _fmt(row["allied_remote_area_surcharge_ex_gst"]),
                    _fmt(row["allied_overweight_surcharge_ex_gst"]),
                    _fmt(row["allied_oversized_item_surcharge_ex_gst"]),
                    _fmt(row["allied_manual_measurement_fee_ex_gst"]),
                    _fmt(row["allied_reverse_logistics_surcharge_ex_gst"]),
                    _fmt(row["allied_additional_services_ex_gst"]),
                    _fmt(row["allied_total_cost_ex_gst"]),
                    _fmt(row["actual_est_base_cost_ex_gst"]),
                    _fmt(row["actual_est_fuel_surcharge_ex_gst"]),
                    _fmt(row["actual_est_fuel_surcharge_pct"]),
                    row["actual_est_fuel_surcharge_source"],
                    _fmt(row["actual_est_fsc_charge_ex_gst"]),
                    _fmt(row["actual_est_fsc_rate_pct"]),
                    row["actual_est_fsc_source"],
                    _fmt(row["actual_est_cost_ex_gst"]),
                    row["actual_est_cost_missing_reason"],
                    row["actual_est_cost_missing_detail"],
                    row["actual_est_service"],
                    row["cheapest_alt_carrier"],
                    row["cheapest_alt_service"],
                    _fmt(row["cheapest_alt_base_cost_ex_gst"]),
                    _fmt(row["cheapest_alt_fuel_surcharge_ex_gst"]),
                    _fmt(row["cheapest_alt_cost_ex_gst"]),
                    _fmt(row["est_saving_ex_gst"]),
                    row["cheaper_option_exists"],
                ]
            )


def _print_summary(rows: list[dict[str, object]]) -> None:
    order_count = len(rows)
    actual_costs = [row["actual_est_cost_ex_gst"] for row in rows if row["actual_est_cost_ex_gst"] is not None]
    alt_costs = [row["cheapest_alt_cost_ex_gst"] for row in rows if row["cheapest_alt_cost_ex_gst"] is not None]
    savings = [row["est_saving_ex_gst"] for row in rows if row["est_saving_ex_gst"] is not None]
    weight_gaps = [row["weight_gap_kg"] for row in rows if row["weight_gap_kg"] is not None]
    chargeable_gaps = [row["chargeable_gap_kg"] for row in rows if row["chargeable_gap_kg"] is not None]
    cheaper_count = sum(1 for row in rows if row["cheaper_option_exists"] == "Y")
    missing_reasons: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("actual_est_cost_missing_reason") or "")
        if reason:
            missing_reasons[reason] = missing_reasons.get(reason, 0) + 1

    print("STAGE 3 SUMMARY")
    print(
        "Row("
        f"ORDER_COUNT={order_count}, "
        f"AVG_ACTUAL_EST_COST_EX_GST={_avg(actual_costs):.2f}, "
        f"AVG_CHEAPEST_ALT_COST_EX_GST={_avg(alt_costs):.2f}, "
        f"AVG_EST_SAVING_EX_GST={_avg(savings):.2f}, "
        f"AVG_WEIGHT_GAP_KG={_avg(weight_gaps):.2f}, "
        f"AVG_CHARGEABLE_GAP_KG={_avg(chargeable_gaps):.2f}, "
        f"CHEAPER_OPTION_PCT={(cheaper_count * 100 / order_count) if order_count else 0.0:.2f}"
        ")"
    )
    if missing_reasons:
        print("MISSING COST REASONS")
        for reason, count in sorted(missing_reasons.items(), key=lambda item: item[1], reverse=True):
            print(f"Row(REASON='{reason}', ROW_COUNT={count})")
    print(f"CSV_EXPORTED={OUTPUT_PATH}")


def _carrier_validation_output_path(output_path: Path) -> Path:
    name = output_path.name
    if name.endswith("_cost_estimates_full.csv"):
        name = name.replace("_cost_estimates_full.csv", "_carrier_validation.csv")
    else:
        name = f"{output_path.stem}_carrier_validation.csv"
    return output_path.with_name(name)


def _write_carrier_validation_output(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (str(row.get("actual_carrier") or ""), str(row.get("actual_service") or ""))
        grouped.setdefault(key, []).append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "site",
                "shippit_carrier",
                "shippit_service",
                "shippit_order_count",
                "est_cost_available_count",
                "est_avg_actual_cost_ex_gst",
                "est_avg_cheapest_alt_cost_ex_gst",
                "est_avg_saving_ex_gst",
                "est_avg_weight_gap_kg",
                "est_avg_chargeable_gap_kg",
                "est_cheaper_option_pct",
                "est_unsupported_count",
                "est_top_cheapest_alt_carrier",
                "est_top_cheapest_alt_service",
                "est_top_cheapest_alt_count",
                "est_actual_cost_basis_note",
                "est_alternative_cost_basis_note",
            ]
        )
        for (carrier, service), carrier_rows in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
            order_count = len(carrier_rows)
            actual_costs = [row["actual_est_cost_ex_gst"] for row in carrier_rows if row["actual_est_cost_ex_gst"] is not None]
            alt_costs = [row["cheapest_alt_cost_ex_gst"] for row in carrier_rows if row["cheapest_alt_cost_ex_gst"] is not None]
            savings = [row["est_saving_ex_gst"] for row in carrier_rows if row["est_saving_ex_gst"] is not None]
            weight_gaps = [row["weight_gap_kg"] for row in carrier_rows if row["weight_gap_kg"] is not None]
            chargeable_gaps = [row["chargeable_gap_kg"] for row in carrier_rows if row["chargeable_gap_kg"] is not None]
            cheaper_count = sum(1 for row in carrier_rows if row["cheaper_option_exists"] == "Y")
            unsupported_count = sum(1 for row in carrier_rows if row["actual_est_cost_missing_reason"] == "unsupported_carrier")
            alt_counts: dict[tuple[str, str], int] = {}
            for row in carrier_rows:
                alt_key = (str(row.get("cheapest_alt_carrier") or ""), str(row.get("cheapest_alt_service") or ""))
                if alt_key[0] or alt_key[1]:
                    alt_counts[alt_key] = alt_counts.get(alt_key, 0) + 1
            top_alt_carrier = ""
            top_alt_service = ""
            top_alt_count = 0
            if alt_counts:
                (top_alt_carrier, top_alt_service), top_alt_count = max(alt_counts.items(), key=lambda item: item[1])
            actual_cost_basis_note = _carrier_cost_basis_note(carrier, "actual", "")
            alternative_cost_basis_note = _carrier_cost_basis_note(top_alt_carrier or top_alt_service, "alternative", top_alt_service)
            writer.writerow(
                [
                    STORE_NAME,
                    carrier,
                    service,
                    order_count,
                    len(actual_costs),
                    _fmt(_avg(actual_costs)),
                    _fmt(_avg(alt_costs)),
                    _fmt(_avg(savings)),
                    _fmt(_avg(weight_gaps)),
                    _fmt(_avg(chargeable_gaps)),
                    _fmt((cheaper_count * 100 / order_count) if order_count else 0.0),
                    unsupported_count,
                    top_alt_carrier,
                    top_alt_service,
                    top_alt_count,
                    actual_cost_basis_note,
                    alternative_cost_basis_note,
                ]
            )


def _avg(values: list[float | None]) -> float:
    numeric = [float(value) for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else 0.0


def _carrier_cost_basis_note(carrier_or_service: str, role: str, alt_service: str) -> str:
    carrier_or_service = str(carrier_or_service or "")
    role = "alternative" if role == "alternative" else "actual"
    if carrier_or_service == "Couriers Please":
        if role == "actual":
            return "Consignment-level charging; invoice chargeable weight is rounded to the next whole kg."
        return "Alternative comparison is also consignment-level and should be averaged across parcels, not treated as a parcel tariff."
    if carrier_or_service in {"Australia Post", "eParcel"}:
        return "Parcel-level tariff with first/subsequent parcel logic where relevant."
    if carrier_or_service == "Allied Express":
        return "Lane plus surcharge pricing."
    if carrier_or_service in {"GoEXPRESS", "GoPeople"}:
        return "Zone and weight band pricing."
    if carrier_or_service == "GoBUNDLE":
        return "Configured per-parcel plug pricing until a live ratecard is available."
    if role == "alternative" and alt_service:
        return f"Alternative basis inferred from {alt_service}."
    return "Cost basis shown using the best available rule."


def _reporting_weight_band(weight_kg: float | None) -> str:
    if weight_kg is None:
        return ""
    for lower, upper, label in REPORTING_WEIGHT_BANDS:
        if upper is None:
            if weight_kg >= lower:
                return label
        elif lower <= weight_kg < upper:
            return label
    return "45+ kg"


def _match_box_suites_for_order(
    parcels: list[ParcelPhysicalInput],
    carrier_service: str,
    catalog: list[BoxSuiteBand],
) -> list[BoxSuiteBand]:
    matches: list[BoxSuiteBand] = []
    for parcel in parcels:
        volumetric_band = round(parcel.parcel_cube_cm3 / VOLUMETRIC_DIVISOR_CM3_PER_KG, 1) if parcel.parcel_cube_cm3 > 0 else None
        match = match_box_suite(carrier_service, volumetric_band, catalog)
        if match is not None:
            matches.append(match)
    return matches


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _fmt_ratio(value: object) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
