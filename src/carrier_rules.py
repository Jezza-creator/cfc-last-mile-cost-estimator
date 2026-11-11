"""Transparent carrier eligibility rules used by the opportunity views."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class CarrierRule:
    """A user-facing summary of a carrier or service rule."""

    carrier_service: str
    chargeable_weight_basis: str
    volumetric_conversion: str
    minimum_weight: str
    rounding: str
    dead_weight_limit: str
    volumetric_weight_limit: str
    max_articles: str
    max_length: str
    surcharge_or_exception: str
    current_model_treatment: str
    source_status: str


# These are the transport-manager-aligned rules currently available to the
# model. Keep unknown values explicit so they cannot be mistaken for zero.
CARRIER_RULES: tuple[CarrierRule, ...] = (
    CarrierRule(
        carrier_service="Couriers Please / Couriers Please",
        chargeable_weight_basis="Higher of dead or volumetric weight",
        volumetric_conversion="250 kg/m3",
        minimum_weight="Local 2 kg; Metro 4-7 kg",
        rounding="Round up to nearest kg",
        dead_weight_limit="25 kg",
        volumetric_weight_limit="40 kg / 0.160 m3",
        max_articles="10",
        max_length="180 cm",
        surcharge_or_exception="Over 120-180 cm surcharge; multi-parcel consignments use I99 consignment pricing",
        current_model_treatment="Article, dead-weight and volumetric checks used for opportunity status; length and per-item checks not yet measured",
        source_status="Transport Manager aligned summary; limits confirmed for planning use",
    ),
    CarrierRule(
        carrier_service="GoPeople / GoEXPRESS",
        chargeable_weight_basis="Higher of dead or volumetric weight",
        volumetric_conversion="250 kg/m3",
        minimum_weight="Local 3 kg; Interstate Metro 0.5 kg",
        rounding="Round up to nearest kg",
        dead_weight_limit="20 kg",
        volumetric_weight_limit="16 kg / 0.064 m3",
        max_articles="10",
        max_length="39 cm",
        surcharge_or_exception="GoEXPRESS weight bands; validate GoPeople-specific article and dimension rules",
        current_model_treatment="Article, dead-weight and volumetric checks used for opportunity status; length and per-item checks not yet measured",
        source_status="Transport Manager aligned summary; GoPeople service rules require confirmation",
    ),
    CarrierRule(
        carrier_service="GoPeople / GoBUNDLE",
        chargeable_weight_basis="Higher of dead or volumetric weight",
        volumetric_conversion="250 kg/m3",
        minimum_weight="Service-specific",
        rounding="Round up to nearest kg",
        dead_weight_limit="20 kg",
        volumetric_weight_limit="16 kg / 0.064 m3",
        max_articles="10",
        max_length="39 cm",
        surcharge_or_exception="GoBUNDLE remains a proxy and is excluded from carrier-choice opportunities",
        current_model_treatment="Proxy pricing only; not treated as a realizable opportunity",
        source_status="Transport Manager aligned summary; GoBUNDLE rule basis not yet validated",
    ),
    CarrierRule(
        carrier_service="Australia Post / eParcel and eParcel Metro",
        chargeable_weight_basis="Higher of dead or volumetric weight",
        volumetric_conversion="250 kg/m3",
        minimum_weight="Service-specific ratecard bands",
        rounding="Below 5 kg by band; above 5 kg round up to nearest kg",
        dead_weight_limit="22 kg",
        volumetric_weight_limit="62.5 kg / 0.250 m3",
        max_articles="99 (eParcel); 20 (Metro)",
        max_length="105 cm; combined length and girth 200 cm",
        surcharge_or_exception="Subsequent parcel discounts vary by service",
        current_model_treatment="Ratecard and AP physical checks are applied; combined length/girth is not currently measured here",
        source_status="Transport Manager aligned summary; AP ratecard remains the pricing source",
    ),
    CarrierRule(
        carrier_service="Allied Express",
        chargeable_weight_basis="Higher of dead or volumetric weight",
        volumetric_conversion="250 kg/m3",
        minimum_weight="10 kg",
        rounding="Round up to nearest kg",
        dead_weight_limit="Not confirmed",
        volumetric_weight_limit="Not confirmed",
        max_articles="Not applicable",
        max_length="600 cm",
        surcharge_or_exception="Overweight 56-90 kg and over 90 kg; oversize over 220 cm",
        current_model_treatment="Ratecard and known surcharge components are modelled; upper limits require carrier/WHS confirmation",
        source_status="Transport Manager aligned summary; Allied limits not yet confirmed",
    ),
)


# Numeric limits used by the fast opportunity screen. Dimensions that are not
# persisted at consignment level are deliberately not represented here.
CARRIER_ELIGIBILITY_LIMITS: dict[str, dict[str, float | int]] = {
    "Couriers Please": {
        "max_articles": 10,
        "max_dead_weight_kg": 25.0,
        "max_volumetric_weight_kg": 40.0,
    },
    "GoPeople": {
        "max_articles": 10,
        "max_dead_weight_kg": 20.0,
        "max_volumetric_weight_kg": 16.0,
    },
    "Australia Post": {
        "max_articles": 99,
        "max_dead_weight_kg": 22.0,
        "max_volumetric_weight_kg": 62.5,
    },
}


def carrier_rules_dataframe() -> pd.DataFrame:
    """Return the catalogue in a stable order for the Streamlit rules page."""

    return pd.DataFrame([asdict(rule) for rule in CARRIER_RULES])
