"""Persistent, human-reviewable opportunity register for model feedback."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "outputs" / "opportunity_register.csv"

OPPORTUNITY_COLUMNS = (
    "opportunity_id",
    "title",
    "category",
    "root_cause",
    "cfc",
    "carrier",
    "service",
    "accounting_week",
    "source_consignment",
    "affected_keycode",
    "affected_consignment_count",
    "affected_parcel_count",
    "affected_unit_count",
    "modelled_shipped_cost_ex_gst",
    "invoiced_actual_cost_ex_gst",
    "observed_variance_ex_gst",
    "alternative_carrier_modelled_cost_ex_gst",
    "alternative_carrier_saving_signal_ex_gst",
    "scenario_current",
    "scenario_candidate",
    "potential_saving_ex_gst",
    "confidence",
    "assumptions",
    "proposed_owner",
    "recommended_action",
    "status",
    "validation_status",
    "evidence_status",
    "model_version",
    "source_reference",
    "created_date",
    "updated_date",
    "notes",
)

NUMERIC_COLUMNS = (
    "affected_consignment_count",
    "affected_parcel_count",
    "affected_unit_count",
    "modelled_shipped_cost_ex_gst",
    "invoiced_actual_cost_ex_gst",
    "observed_variance_ex_gst",
    "alternative_carrier_modelled_cost_ex_gst",
    "alternative_carrier_saving_signal_ex_gst",
    "potential_saving_ex_gst",
)

MANUAL_REVIEW_COLUMNS = (
    "proposed_owner",
    "recommended_action",
    "status",
    "validation_status",
    "notes",
)


def _empty_column(index: pd.Index) -> pd.Series:
    return pd.Series(pd.NA, index=index, dtype="string")


def _ensure_schema(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in OPPORTUNITY_COLUMNS:
        if column not in result:
            result[column] = _empty_column(result.index)

    for column in NUMERIC_COLUMNS:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    for column in OPPORTUNITY_COLUMNS:
        if column not in NUMERIC_COLUMNS:
            result[column] = result[column].astype("string").fillna("")
    return result.loc[:, list(OPPORTUNITY_COLUMNS)]


def read_opportunity_register(path: Path = DEFAULT_PATH) -> pd.DataFrame:
    """Read the register with a stable schema, including when it is empty."""
    if not path.exists():
        return _ensure_schema(pd.DataFrame())
    try:
        frame = pd.read_csv(path, low_memory=False)
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return _ensure_schema(pd.DataFrame())
    return _ensure_schema(frame)


def upsert_opportunities(
    existing: pd.DataFrame,
    updates: pd.DataFrame,
    *,
    preserve_manual_fields: bool = True,
) -> pd.DataFrame:
    """Upsert opportunities by ID without erasing human review fields."""
    current = _ensure_schema(existing)
    incoming = _ensure_schema(updates)
    if incoming.empty:
        return current

    current = current.loc[current["opportunity_id"].ne("")].copy()
    incoming = incoming.loc[incoming["opportunity_id"].ne("")].copy()
    if current.empty:
        return incoming.reset_index(drop=True)

    current = current.drop_duplicates("opportunity_id", keep="last").set_index("opportunity_id")
    incoming = incoming.drop_duplicates("opportunity_id", keep="last").set_index("opportunity_id")
    for opportunity_id, row in incoming.iterrows():
        if opportunity_id not in current.index:
            current.loc[opportunity_id, :] = row
            continue
        for column in OPPORTUNITY_COLUMNS:
            if column == "opportunity_id":
                continue
            if preserve_manual_fields and column in MANUAL_REVIEW_COLUMNS:
                continue
            value = row[column]
            if pd.notna(value) and value != "":
                current.loc[opportunity_id, column] = value

    result = current.reset_index()
    return _ensure_schema(result).reset_index(drop=True)


def write_opportunity_register(
    frame: pd.DataFrame,
    path: Path = DEFAULT_PATH,
) -> None:
    """Write the register atomically so the app never sees a partial CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    _ensure_schema(frame).to_csv(temporary, index=False)
    temporary.replace(path)


def seed_pilot_opportunity(path: Path = DEFAULT_PATH) -> pd.DataFrame:
    """Add the first reviewed pilot without overwriting an existing register."""
    existing = read_opportunity_register(path)
    if not existing.empty:
        return existing

    pilot = pd.DataFrame(
        [
            {
                "opportunity_id": "OPP-TRUG-EPARCEL-42909484-WHS-ALLOCATION",
                "title": "Review WHS carrier allocation for consignment 6559050711",
                "category": "Carrier allocation / cost configuration",
                "root_cause": (
                    "WHS allocated Australia Post despite Shippit chargeable weight of 29.04 kg "
                    "and a lower-cost CP alternative in the current decision output."
                ),
                "cfc": "TRUGANINA",
                "carrier": "Australia Post",
                "service": "eParcel",
                "accounting_week": "FY2027 W09",
                "source_consignment": "6559050711",
                "affected_keycode": "42909484",
                "affected_consignment_count": 1,
                "affected_parcel_count": 6,
                "affected_unit_count": 32,
                "modelled_shipped_cost_ex_gst": 20.42,
                "alternative_carrier_modelled_cost_ex_gst": 5.30,
                "alternative_carrier_saving_signal_ex_gst": 15.12,
                "scenario_current": "WHS allocation: Australia Post / eParcel",
                "scenario_candidate": "Validated WHS threshold and valid-carrier rule basis",
                "confidence": "To validate",
                "assumptions": (
                    "The $15.12 difference is a modelled carrier-choice signal, not a realised saving. "
                    "Allocation occurs before the six post-pack articles are created, so this record "
                    "does not assume WHS used the later Shippit physical values."
                ),
                "proposed_owner": "WHS / Carrier Allocation",
                "recommended_action": (
                    "Review the WHS carrier-allocation configuration for this order type: source "
                    "dimension and weight snapshot, thresholds, valid-carrier rules, and service "
                    "mapping. Re-run the decision using the six packed articles as evidence."
                ),
                "status": "New",
                "validation_status": "Awaiting WHS allocation-rule validation",
                "evidence_status": "Stage 1-3, Shippit and carrier-rate comparison",
                "model_version": "Current Stage 3 output",
                "source_reference": (
                    "outputs/stage3_truganina_cost_estimates_full.csv; "
                    "outputs/cfc_plm_variance_report.csv"
                ),
                "created_date": "2026-09-03",
                "updated_date": "2026-09-03",
                "notes": (
                    "Keep this allocation opportunity separate from the physical-estimation opportunity "
                    "below so a future saving is not double counted."
                ),
            },
            {
                "opportunity_id": "OPP-TRUG-EPARCEL-42909484-WHS-PHYSICAL-ESTIMATE",
                "title": "Review WHS parcel and cube estimation for keycode 42909484",
                "category": "WHS parcel / cube estimation",
                "root_cause": (
                    "The Shippit chargeable basis of 29.04 kg appears inconsistent with the six packed "
                    "articles and the PLM/packed-dimension comparator."
                ),
                "cfc": "TRUGANINA",
                "carrier": "Australia Post",
                "service": "eParcel",
                "accounting_week": "FY2027 W09",
                "source_consignment": "6559050711",
                "affected_keycode": "42909484",
                "affected_consignment_count": 1,
                "affected_parcel_count": 6,
                "affected_unit_count": 32,
                "modelled_shipped_cost_ex_gst": 20.42,
                "scenario_current": "Shippit chargeable weight: 29.04 kg",
                "scenario_candidate": "Validated six-article PLM / rolled packed scenario",
                "confidence": "To validate",
                "assumptions": (
                    "The CFC dimensions are 63.1 x 45.5 x 70.0 cm at 0.87 kg per unit. The PLM/packed "
                    "scenario discussed is approximately 140 kg volumetric, but the divisor, carton "
                    "split, and final packing basis must be confirmed before using that figure."
                ),
                "proposed_owner": "WHS / CFC Ops / Master Data",
                "recommended_action": (
                    "Trace parcel and cube estimation from order creation through the six packed articles. "
                    "Confirm which WMS dimensions are sent to Shippit, why 29.04 kg is retained, and how "
                    "the CFC, PLM, and rolled packed scenarios should be represented."
                ),
                "status": "New",
                "validation_status": "Awaiting WHS physical-estimation validation",
                "evidence_status": "Six packed articles, Shippit, PLM and WMS evidence",
                "model_version": "Current Stage 3 output",
                "source_reference": (
                    "outputs/stage2_truganina_parcel_physicals_full.csv; "
                    "outputs/cfc_plm_variance_report.csv"
                ),
                "created_date": "2026-09-03",
                "updated_date": "2026-09-03",
                "notes": (
                    "No dimensional saving is booked. This is a physical-basis investigation and must "
                    "not be combined with the carrier-allocation saving signal."
                ),
            },
        ]
    )
    result = upsert_opportunities(existing, pilot)
    write_opportunity_register(result, path)
    return result


if __name__ == "__main__":
    seed_pilot_opportunity()
    print(f"Opportunity register ready: {DEFAULT_PATH}")
