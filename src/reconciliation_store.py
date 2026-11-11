"""Persist consignment invoice coverage and weekly reconciliation metrics."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from app.streamlit_app import (
    _coalesced_lookup,
    _normalise_consignment_number,
    _reconciliation_coverage,
    _reconciliation_detail_for_stage3,
    _reconciliation_weekly_summary,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
DEFAULT_RECONCILIATION_FILES = {
    "couriers_please": OUTPUTS / "carrier_invoices" / "couriers_please_reconciliation.csv",
    "australia_post": OUTPUTS / "carrier_invoices" / "australia_post_reconciliation.csv",
    "gopeople": OUTPUTS / "carrier_invoices" / "gopeople_reconciliation.csv",
    "allied": OUTPUTS / "carrier_invoices" / "allied_reconciliation.csv",
}
DEFAULT_STAGE3_FILES = {
    "MOOREBANK": OUTPUTS / "stage3_moorebank_cost_estimates_full.csv",
    "TRUGANINA": OUTPUTS / "stage3_truganina_cost_estimates_full.csv",
}
PERSISTENCE_DETAIL_COLUMNS = (
    "invoice_number",
    "stage3_invoice_number",
    "carrier_tracking_number",
    "stage3_tracking_number",
    "carrier_consignment_reference",
    "consignment_id",
    "actual_invoice_cost_ex_gst",
    "actual_base_cost_ex_gst",
    "actual_fuel_surcharge_ex_gst",
    "actual_other_surcharges_ex_gst",
    "actual_other_surcharge_ex_gst",
    "actual_delivery_surcharge_ex_gst",
    "actual_chargeable_weight_kg",
    "actual_rounded_chargeable_weight_kg",
    "actual_billed_weight_kg",
    "actual_cubic_weight_kg",
    "actual_weight_kg",
    "actual_declared_weight_kg",
    "actual_billed_cube_cm3",
    "actual_chargeable_cube_cm3",
    "actual_billed_cube_m3",
    "actual_total_volume_m3",
    "actual_declared_cube_cm3",
    "actual_declared_cube_m3",
    "physical_article_row_count",
    "physical_distinct_article_count",
    "physical_article_duplicate_row_count",
    "physical_anonymous_row_count",
    "physical_aggregation_method",
    "physical_article_cube_sum_cm3",
    "physical_article_cube_max_cm3",
    "physical_article_billed_weight_sum_kg",
    "physical_article_billed_weight_max_kg",
    "physical_article_cubic_weight_sum_kg",
    "physical_article_cubic_weight_max_kg",
    "primary_cost_driver",
    "root_cause",
)

INCREMENTAL_CARRIER_ALIASES = {
    "australia_post": {"AUSPOST", "AUSTRALIAPOST", "EPARCEL", "EPARCELMETRO"},
    "couriers_please": {"COURIERSP", "COURIERSPLEASE"},
    "gopeople": {"GOPEOPLE", "GOEXPRESS", "GOBUNDLE"},
    "allied": {"ALLIED", "ALLIEDEXPRESS"},
}


def _dataset_slug(dataset_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", dataset_name.lower()).strip("_") or "dataset"


def _dataset_directory(output_directory: Path, dataset_name: str, flat_output: bool) -> Path:
    """Choose a dataset folder, with flat mode for sync-placeholder directories."""
    return output_directory if flat_output else output_directory / _dataset_slug(dataset_name)


def _load_stage3(stage3_files: dict[str, Path]) -> pd.DataFrame:
    frames = []
    for site, path in stage3_files.items():
        if not path.exists():
            print(f"Skipping missing Stage 3 file: {path}", flush=True)
            continue
        available_columns = set(pd.read_csv(path, nrows=0).columns)
        requested_columns = [
            "invoice_number",
            "shippit_invoice_number",
            "actual_carrier",
            "shippit_carrier",
            "actual_service",
            "shippit_service",
            "actual_est_service",
            "carrier_tracking_number",
            "shippit_tracking_number",
            "despatch_date",
            "kmart_accounting_year",
            "kmart_accounting_week_number_in_year",
            "kmart_accounting_period_number",
            "actual_est_cost_ex_gst",
            "modelled_total_cost_ex_gst",
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
            "actual_est_base_cost_ex_gst",
            "actual_est_fuel_surcharge_ex_gst",
            "actual_est_fuel_surcharge_pct",
            "actual_est_fuel_surcharge_source",
            "allied_zone",
            "allied_total_cost_ex_gst",
            "ap_zone",
            "cp_zone",
            "goexpress_zone",
        ]
        frame = pd.read_csv(
            path,
            low_memory=False,
            usecols=[column for column in requested_columns if column in available_columns],
            dtype={"invoice_number": "string", "carrier_tracking_number": "string"},
        )
        if not frame.empty:
            frame["ui_site"] = site
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _coverage_key(frame: pd.DataFrame) -> pd.Series:
    parts = []
    for column in ("CFC", "Shippit invoice number", "Shippit tracking number"):
        if column in frame:
            values = frame[column].fillna("").astype(str).str.strip()
            if column != "CFC":
                values = values.map(_normalise_consignment_number)
        else:
            values = pd.Series("", index=frame.index, dtype="string")
        parts.append(values)
    return parts[0] + "|" + parts[1] + "|" + parts[2]


def _upsert_coverage(existing: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """Replace refreshed consignment rows while retaining older rolling-window rows."""
    if existing.empty:
        return current.copy()
    if current.empty:
        return existing.copy()

    current_rows = current.copy()
    current_rows["_coverage_key"] = _coverage_key(current_rows)
    current_rows = current_rows.drop_duplicates("_coverage_key", keep="last")
    existing_rows = existing.copy()
    existing_rows["_coverage_key"] = _coverage_key(existing_rows)

    # Keep previously captured evidence when a refresh has no value for that
    # field, while allowing refreshed non-null values to replace stale values.
    existing_by_key = existing_rows.drop_duplicates("_coverage_key", keep="last").set_index(
        "_coverage_key"
    )
    existing_columns = [column for column in existing.columns if column != "_coverage_key"]
    for column in existing_columns:
        if column not in current_rows:
            current_rows[column] = current_rows["_coverage_key"].map(existing_by_key[column])
    for column in current_rows.columns:
        if column == "_coverage_key" or column not in existing_by_key:
            continue
        current_rows[column] = current_rows[column].combine_first(
            current_rows["_coverage_key"].map(existing_by_key[column])
        )

    existing_rows = existing_rows.loc[
        ~existing_rows["_coverage_key"].isin(current_rows["_coverage_key"])
    ]
    combined = pd.concat([existing_rows, current_rows], ignore_index=True, sort=False)
    return combined.drop(columns="_coverage_key")


def _stage3_key(frame: pd.DataFrame) -> pd.Series:
    cfc = _coalesced_lookup(frame, ("ui_site", "site"))
    invoice = _coalesced_lookup(frame, ("invoice_number", "shippit_invoice_number"))
    tracking = _coalesced_lookup(frame, ("carrier_tracking_number", "shippit_tracking_number"))
    key = cfc + "|" + invoice + "|" + tracking
    return key.where(key.ne("||"), "row|" + frame.index.astype(str))


def _coverage_history_as_stage3(coverage: pd.DataFrame) -> pd.DataFrame:
    if coverage.empty:
        return pd.DataFrame()

    def column(name: str) -> pd.Series:
        return coverage[name] if name in coverage else pd.Series("", index=coverage.index, dtype="string")

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
    history["dest_suburb"] = column("Destination suburb")
    history["dest_state"] = column("Destination state")
    history["dest_postcode"] = column("Destination postcode")
    history["actual_est_cost_ex_gst"] = column("Modelled cost ex GST")
    history["modelled_total_cost_ex_gst"] = column("Modelled cost ex GST")
    history["actual_est_base_cost_ex_gst"] = column("Modelled base cost ex GST")
    history["actual_est_fuel_surcharge_ex_gst"] = column("Modelled fuel surcharge ex GST")
    history["actual_est_fuel_surcharge_pct"] = column("Modelled fuel surcharge %")
    history["actual_est_fuel_surcharge_source"] = column("Modelled fuel surcharge source")
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


def _merge_stage3_history(history: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([history, current], ignore_index=True, sort=False)
    if combined.empty:
        return combined
    combined["_stage3_key"] = _stage3_key(combined)
    combined = combined.drop_duplicates("_stage3_key", keep="last")
    return combined.drop(columns="_stage3_key").reset_index(drop=True)


def _normalise_carrier(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _incremental_carrier_mask(frame: pd.DataFrame, carrier: str) -> pd.Series:
    aliases = INCREMENTAL_CARRIER_ALIASES.get(carrier, {_normalise_carrier(carrier)})
    values = _coalesced_lookup(frame, ("actual_carrier", "shippit_carrier", "Shipped carrier"))
    return values.map(_normalise_carrier).isin(aliases)


def _incremental_date_mask(
    frame: pd.DataFrame,
    start_date: str | None,
    end_date: str | None,
) -> pd.Series | None:
    if not start_date and not end_date:
        return None
    dates = _coalesced_lookup(frame, ("despatch_date", "Despatch date", "service_date", "consignment_date"))
    parsed = pd.to_datetime(dates, errors="coerce").dt.normalize()
    mask = pd.Series(True, index=frame.index)
    if start_date:
        mask &= parsed.ge(pd.Timestamp(start_date))
    if end_date:
        mask &= parsed.le(pd.Timestamp(end_date))
    return mask.fillna(False)


def _reconciliation_keys_for_sources(
    reconciliation_files: dict[str, Path],
    carrier: str,
    source_files: tuple[str, ...],
    start_date: str | None = None,
    end_date: str | None = None,
) -> set[str]:
    """Return invoice/tracking keys from the changed source slice.

    Excel reconciliation exports retain source workbook names, so a changed
    workbook can be narrowed without rematching unrelated historical rows.
    The same helper can restrict AP rows by service date when required.
    """
    paths = []
    path = reconciliation_files.get(carrier)
    if path is not None:
        paths.append(path)
    if not paths:
        return set()

    keys: set[str] = set()
    source_filter = {str(value).strip() for value in source_files if str(value).strip()}
    key_columns = (
        "invoice_number",
        "stage3_invoice_number",
        "carrier_tracking_number",
        "stage3_tracking_number",
        "carrier_consignment_reference",
        "consignment_id",
    )
    for path in paths:
        if not path.exists():
            continue
        try:
            available = set(pd.read_csv(path, nrows=0).columns)
            columns = [column for column in ("source_file", "service_date", "consignment_date", *key_columns) if column in available]
            if not any(column in available for column in key_columns):
                continue
            for chunk in pd.read_csv(path, usecols=columns, chunksize=50_000, low_memory=False, dtype="string"):
                mask = pd.Series(True, index=chunk.index)
                if source_filter and "source_file" in chunk:
                    mask &= chunk["source_file"].fillna("").astype(str).str.strip().isin(source_filter)
                date_values = _incremental_date_mask(chunk, start_date, end_date)
                if date_values is not None:
                    mask &= date_values
                if not mask.any():
                    continue
                for column in key_columns:
                    if column in chunk:
                        values = chunk.loc[mask, column].fillna("").astype(str).map(_normalise_consignment_number)
                        keys.update(value for value in values if value)
        except (OSError, pd.errors.ParserError):
            continue
    return keys


def _incremental_stage3_slice(
    frame: pd.DataFrame,
    carrier: str,
    start_date: str | None,
    end_date: str | None,
    reconciliation_keys: set[str],
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    carrier_mask = _incremental_carrier_mask(frame, carrier)
    criteria: list[pd.Series] = []
    date_mask = _incremental_date_mask(frame, start_date, end_date)
    if date_mask is not None:
        criteria.append(date_mask)
    if reconciliation_keys:
        invoice_values = _coalesced_lookup(
            frame,
            ("invoice_number", "shippit_invoice_number", "Shippit invoice number"),
        ).map(_normalise_consignment_number)
        tracking_values = _coalesced_lookup(
            frame,
            (
                "carrier_tracking_number",
                "shippit_tracking_number",
                "Shippit tracking number",
            ),
        ).map(_normalise_consignment_number)
        criteria.append(invoice_values.isin(reconciliation_keys) | tracking_values.isin(reconciliation_keys))
    if not criteria:
        return frame.loc[carrier_mask].copy()
    candidate_mask = criteria[0]
    for criterion in criteria[1:]:
        candidate_mask |= criterion
    return frame.loc[carrier_mask & candidate_mask].copy()


def _incremental_coverage_slice(
    coverage: pd.DataFrame,
    carrier: str,
    start_date: str | None,
    end_date: str | None,
    reconciliation_keys: set[str],
) -> pd.DataFrame:
    if coverage.empty:
        return coverage.copy()
    carrier_mask = _incremental_carrier_mask(coverage, carrier)
    criteria: list[pd.Series] = []
    date_mask = _incremental_date_mask(coverage, start_date, end_date)
    if date_mask is not None:
        criteria.append(date_mask)
    if reconciliation_keys:
        invoice_values = _coalesced_lookup(
            coverage,
            ("Shippit invoice number", "invoice_number"),
        ).map(_normalise_consignment_number)
        tracking_values = _coalesced_lookup(
            coverage,
            ("Shippit tracking number", "carrier_tracking_number"),
        ).map(_normalise_consignment_number)
        criteria.append(invoice_values.isin(reconciliation_keys) | tracking_values.isin(reconciliation_keys))
    if not criteria:
        return coverage.loc[carrier_mask].copy()
    candidate_mask = criteria[0]
    for criterion in criteria[1:]:
        candidate_mask |= criterion
    return coverage.loc[carrier_mask & candidate_mask].copy()


def _public_coverage(coverage: pd.DataFrame) -> pd.DataFrame:
    return coverage.drop(columns=["_accuracy_matched", "_week_sort"], errors="ignore")


def _combine_coverage_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine saved coverage stores, preferring matched and newest records."""
    non_empty = [frame.copy() for frame in frames if not frame.empty]
    if not non_empty:
        return pd.DataFrame()

    combined = pd.concat(non_empty, ignore_index=True, sort=False)
    combined["_coverage_key"] = _coverage_key(combined)
    invalid_key = combined["_coverage_key"].eq("||")
    combined.loc[invalid_key, "_coverage_key"] = (
        "row|" + combined.index[invalid_key].astype(str)
    )
    status_rank = {
        "Not yet matched": 0,
        "Matched": 1,
        "Matched by tracking number": 1,
    }
    combined["_match_rank"] = (
        combined.get("Invoice status", pd.Series("Not yet matched", index=combined.index))
        .astype(str)
        .map(status_rank)
        .fillna(0)
    )
    combined["_refresh_rank"] = pd.to_datetime(
        combined.get(
            "coverage_refresh_timestamp_utc",
            pd.Series(index=combined.index, dtype="string"),
        ),
        errors="coerce",
        utc=True,
    )
    sort_columns = ["_coverage_key", "_match_rank", "_refresh_rank"]
    combined = combined.sort_values(sort_columns, na_position="last")
    winners = combined.drop_duplicates("_coverage_key", keep="last").copy()

    # Keep the preferred row's values, but fill newly added evidence columns from
    # another source when that source has the physical data and the winner does not.
    public_columns = [
        column
        for column in combined.columns
        if column not in {"_coverage_key", "_match_rank", "_refresh_rank"}
    ]
    preferred_first = combined.sort_values(
        sort_columns,
        ascending=[True, False, False],
        na_position="last",
    )
    preferred_values = preferred_first.groupby(
        "_coverage_key", sort=False, dropna=False
    )[public_columns].first()
    winners = winners.drop(columns=["_match_rank", "_refresh_rank"])
    for column in public_columns:
        winners[column] = winners["_coverage_key"].map(preferred_values[column])
    return winners.drop(columns="_coverage_key").reset_index(drop=True)


def _metadata_columns(frame: pd.DataFrame, dataset_name: str, refreshed_at: str) -> pd.DataFrame:
    result = frame.copy()
    if "validation_dataset" not in result:
        result.insert(0, "validation_dataset", dataset_name)
    else:
        result["validation_dataset"] = dataset_name
    if "coverage_refresh_timestamp_utc" not in result:
        result.insert(1, "coverage_refresh_timestamp_utc", refreshed_at)
    else:
        result["coverage_refresh_timestamp_utc"] = refreshed_at
    return result


def _write_manifest(
    path: Path,
    dataset_name: str,
    stage3_files: dict[str, Path],
    reconciliation_files: dict[str, Path],
    coverage: pd.DataFrame,
    refreshed_at: str,
) -> None:
    matched = coverage.get("Invoice status", pd.Series(dtype="string")).astype(str).ne("Not yet matched")
    row = {
        "validation_dataset": dataset_name,
        "refreshed_at_utc": refreshed_at,
        "coverage_rows": len(coverage),
        "matched_rows": int(matched.sum()),
        "unmatched_rows": int((~matched).sum()),
        "coverage_weeks": coverage["Week"].nunique() if "Week" in coverage else 0,
        "stage3_files": "|".join(f"{site}={file}" for site, file in stage3_files.items()),
        "reconciliation_files": "|".join(f"{carrier}={file}" for carrier, file in reconciliation_files.items()),
    }
    manifest = pd.DataFrame([row])
    if path.exists():
        previous = pd.read_csv(path, low_memory=False)
        manifest = pd.concat([previous, manifest], ignore_index=True, sort=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(path, index=False)


def build_store(
    dataset_name: str,
    stage3_files: dict[str, Path],
    reconciliation_files: dict[str, Path],
    output_directory: Path,
    flat_output: bool = False,
    rematch_existing: bool = False,
    incremental_carrier: str | None = None,
    invoice_start_date: str | None = None,
    invoice_end_date: str | None = None,
    invoice_source_files: tuple[str, ...] = (),
) -> tuple[Path, Path, Path]:
    """Persist coverage, matching only refreshed Stage 3 rows by default.

    The normal daily path updates the rolling window and upserts it into the
    existing store. Invoice-arrival runs can opt into the slower full rematch
    so invoices received for older weeks update their historical rows too.
    """
    stage3 = _load_stage3(stage3_files)
    dataset_directory = _dataset_directory(output_directory, dataset_name, flat_output)
    coverage_path = dataset_directory / "consignment_invoice_coverage.csv"
    weekly_path = dataset_directory / "weekly_invoice_summary.csv"
    manifest_path = dataset_directory / "manifest.csv"
    existing = (
        pd.read_csv(coverage_path, low_memory=False)
        if coverage_path.exists()
        else pd.DataFrame()
    )
    if stage3.empty and existing.empty:
        raise ValueError("No Stage 3 records or persisted coverage history were found for the requested store build.")
    if rematch_existing and incremental_carrier:
        reconciliation_keys = _reconciliation_keys_for_sources(
            reconciliation_files,
            incremental_carrier,
            invoice_source_files,
            invoice_start_date,
            invoice_end_date,
        )
        existing_slice = _incremental_coverage_slice(
            existing,
            incremental_carrier,
            invoice_start_date,
            invoice_end_date,
            reconciliation_keys,
        )
        stage3_slice = _incremental_stage3_slice(
            stage3,
            incremental_carrier,
            invoice_start_date,
            invoice_end_date,
            reconciliation_keys,
        )
        stage3_for_refresh = _merge_stage3_history(
            _coverage_history_as_stage3(existing_slice),
            stage3_slice,
        )
        scope = f"incremental {incremental_carrier} invoice scope"
    else:
        stage3_for_refresh = (
            _merge_stage3_history(_coverage_history_as_stage3(existing), stage3)
            if rematch_existing
            else stage3
        )
        scope = "persisted history" if rematch_existing else "the refreshed rolling window"

    print(
        f"Matching {len(stage3_for_refresh):,} Stage 3 consignments from {scope} "
        "to carrier reconciliation records...",
        flush=True,
    )
    detail = _reconciliation_detail_for_stage3(
        stage3_for_refresh,
        "ALL",
        reconciliation_files,
        detail_columns=PERSISTENCE_DETAIL_COLUMNS,
    )
    coverage, _ = _reconciliation_coverage(stage3_for_refresh, detail)
    refreshed_at = pd.Timestamp.now(tz="UTC").isoformat()
    coverage = _public_coverage(coverage)
    combined = _upsert_coverage(existing, coverage)
    combined = combined.sort_values(
        [column for column in ["CFC", "Week", "Shippit invoice number"] if column in combined],
        na_position="last",
    ).reset_index(drop=True)
    combined_with_metadata = _metadata_columns(combined, dataset_name, refreshed_at)
    weekly = _reconciliation_weekly_summary(combined)
    weekly = _metadata_columns(weekly, dataset_name, refreshed_at)

    dataset_directory.mkdir(parents=True, exist_ok=True)
    combined_with_metadata.to_csv(coverage_path, index=False)
    weekly.to_csv(weekly_path, index=False)
    _write_manifest(
        manifest_path,
        dataset_name,
        stage3_files,
        reconciliation_files,
        combined,
        refreshed_at,
    )
    print(f"Wrote {coverage_path}", flush=True)
    print(f"Wrote {weekly_path}", flush=True)
    print(f"Wrote {manifest_path}", flush=True)
    return coverage_path, weekly_path, manifest_path


def _stage3_physical_frame(stage3: pd.DataFrame) -> pd.DataFrame:
    """Build physical evidence keyed like persisted coverage rows."""
    if stage3.empty:
        return pd.DataFrame()

    physical = pd.DataFrame(index=stage3.index)
    physical["CFC"] = _coalesced_lookup(stage3, ("ui_site", "site"))
    physical["Shippit invoice number"] = _coalesced_lookup(
        stage3, ("invoice_number", "shippit_invoice_number")
    )
    physical["Shippit tracking number"] = _coalesced_lookup(
        stage3, ("carrier_tracking_number", "shippit_tracking_number")
    )

    def numeric(columns: tuple[str, ...]) -> pd.Series:
        result = pd.Series(float("nan"), index=stage3.index, dtype="float64")
        for column in columns:
            if column in stage3:
                result = result.fillna(pd.to_numeric(stage3[column], errors="coerce"))
        return result

    physical["Estimator chargeable weight kg"] = numeric(("est_chargeable_weight_kg",))
    physical["Estimator packed SKU weight kg"] = numeric(("est_packed_sku_weight_kg",))
    packed_sku_cube_m3 = numeric(("est_packed_sku_cube_total_m3",))
    physical["Estimator packed SKU cube m3"] = packed_sku_cube_m3.fillna(
        numeric(("est_packed_sku_cube_total_cm3",)) / 1_000_000
    )
    physical["Estimator packed SKU cube imputed"] = stage3.get(
        "est_packed_sku_cube_imputed_flag",
        pd.Series("", index=stage3.index, dtype="string"),
    ).fillna("").astype("string")
    physical["Estimator volumetric weight kg"] = numeric(("est_volumetric_weight_kg",))
    estimator_cube_m3 = numeric(("est_parcel_cube_total_m3",))
    physical["Estimator cube m3"] = estimator_cube_m3.fillna(
        numeric(("est_parcel_cube_total_cm3",)) / 1_000_000
    )
    physical["Shippit chargeable weight kg"] = numeric(("shippit_chargeable_weight_kg",))
    physical["Shippit dead weight kg"] = numeric(("shippit_dead_weight_kg",))
    physical["Shippit volumetric weight kg"] = numeric(("shippit_volumetric_weight_kg",))
    physical["Shippit chargeable cube m3"] = numeric(
        ("shippit_chargeable_cube_cm3", "shippit_volumetric_cube_cm3")
    ) / 1_000_000
    physical["_coverage_key"] = _coverage_key(physical)
    return physical.drop_duplicates("_coverage_key", keep="last")


def enrich_store_physical(
    dataset_name: str,
    stage3_files: dict[str, Path],
    output_directory: Path,
    flat_output: bool = False,
) -> tuple[Path, Path, Path]:
    """Fill persisted coverage physical fields without re-matching invoices."""
    dataset_directory = _dataset_directory(output_directory, dataset_name, flat_output)
    coverage_path = dataset_directory / "consignment_invoice_coverage.csv"
    weekly_path = dataset_directory / "weekly_invoice_summary.csv"
    manifest_path = dataset_directory / "manifest.csv"
    if not coverage_path.exists():
        raise ValueError(f"No persisted coverage file was found: {coverage_path}")

    coverage = pd.read_csv(coverage_path, low_memory=False)
    physical = _stage3_physical_frame(_load_stage3(stage3_files))
    if physical.empty:
        raise ValueError("No Stage 3 records were found for physical enrichment.")

    coverage["_coverage_key"] = _coverage_key(coverage)
    physical_by_key = physical.set_index("_coverage_key")
    physical_columns = [
        column
        for column in physical.columns
        if column not in {"CFC", "Shippit invoice number", "Shippit tracking number", "_coverage_key"}
    ]
    filled = 0
    for column in physical_columns:
        if pd.api.types.is_numeric_dtype(physical[column]):
            if column not in coverage:
                coverage[column] = float("nan")
            existing_values = pd.to_numeric(coverage[column], errors="coerce")
            mapped_values = pd.to_numeric(
                coverage["_coverage_key"].map(physical_by_key[column]), errors="coerce"
            )
            missing_before = existing_values.isna()
            coverage[column] = existing_values.fillna(mapped_values)
        else:
            existing_values = (
                coverage[column].astype("string")
                if column in coverage
                else pd.Series("", index=coverage.index, dtype="string")
            ).fillna("")
            mapped_values = coverage["_coverage_key"].map(physical_by_key[column]).astype("string")
            missing_before = existing_values.str.strip().eq("")
            coverage[column] = existing_values.mask(missing_before, mapped_values)
        filled += int((missing_before & coverage[column].notna()).sum())

    coverage = coverage.drop(columns="_coverage_key")
    weekly = _reconciliation_weekly_summary(coverage)
    refreshed_at = pd.Timestamp.now(tz="UTC").isoformat()
    coverage = _metadata_columns(coverage, dataset_name, refreshed_at)
    weekly = _metadata_columns(weekly, dataset_name, refreshed_at)
    dataset_directory.mkdir(parents=True, exist_ok=True)
    coverage.to_csv(coverage_path, index=False)
    weekly.to_csv(weekly_path, index=False)
    _write_manifest(manifest_path, dataset_name, stage3_files, {}, coverage, refreshed_at)
    print(f"Filled {filled:,} missing physical evidence values.", flush=True)
    print(f"Wrote {coverage_path}", flush=True)
    print(f"Wrote {weekly_path}", flush=True)
    print(f"Wrote {manifest_path}", flush=True)
    return coverage_path, weekly_path, manifest_path


def enrich_store_dimensions(
    coverage_path: Path,
    weekly_path: Path,
    stage3_files: dict[str, Path],
) -> tuple[Path, Path]:
    """Backfill lane and weight-band fields without re-matching invoices."""
    coverage = pd.read_csv(coverage_path, low_memory=False)
    if coverage.empty:
        raise ValueError(f"No persisted coverage rows were found at {coverage_path}.")

    stage3 = _load_stage3(stage3_files)
    required = {"ap_pricing_zone", "reporting_weight_band"}
    if stage3.empty or not required.issubset(stage3.columns):
        raise ValueError("The supplied Stage 3 files do not contain the required matrix dimensions.")

    lookup = pd.DataFrame(
        {
            "CFC": _coalesced_lookup(stage3, ("ui_site", "site")),
            "Shippit invoice number": _coalesced_lookup(
                stage3, ("invoice_number", "shippit_invoice_number")
            ),
            "Shippit tracking number": _coalesced_lookup(
                stage3, ("carrier_tracking_number", "shippit_tracking_number")
            ),
            "AP pricing zone": stage3["ap_pricing_zone"].astype("string"),
            "Reporting weight band": stage3["reporting_weight_band"].astype("string"),
        }
    )
    lookup["_coverage_key"] = _coverage_key(lookup)
    lookup = lookup.loc[lookup["_coverage_key"].ne("||")].drop_duplicates(
        "_coverage_key", keep="last"
    )
    coverage_keys = _coverage_key(coverage)
    filled = 0
    for column in ("AP pricing zone", "Reporting weight band"):
        mapped = coverage_keys.map(lookup.set_index("_coverage_key")[column])
        existing = (
            coverage[column].astype("string")
            if column in coverage
            else pd.Series("", index=coverage.index, dtype="string")
        ).fillna("")
        missing = existing.str.strip().eq("")
        coverage[column] = existing.mask(missing, mapped)
        filled += int((missing & mapped.notna()).sum())

    coverage.to_csv(coverage_path, index=False)
    refresh_weekly_summary(coverage_path, weekly_path)
    print(f"Backfilled {filled:,} lane/weight dimension values.", flush=True)
    return coverage_path, weekly_path


def combine_persisted_stores(
    dataset_name: str,
    input_paths: list[Path],
    output_directory: Path,
    flat_output: bool = False,
) -> tuple[Path, Path, Path]:
    """Create one coverage history from previously persisted dataset stores."""
    frames = [pd.read_csv(path, low_memory=False) for path in input_paths if path.exists()]
    combined = _combine_coverage_frames(frames)
    if combined.empty:
        raise ValueError("No persisted coverage files were found for the combined store.")

    dataset_directory = _dataset_directory(output_directory, dataset_name, flat_output)
    coverage_path = dataset_directory / "consignment_invoice_coverage.csv"
    weekly_path = dataset_directory / "weekly_invoice_summary.csv"
    manifest_path = dataset_directory / "manifest.csv"
    refreshed_at = pd.Timestamp.now(tz="UTC").isoformat()
    combined = _public_coverage(combined)
    combined_with_metadata = _metadata_columns(combined, dataset_name, refreshed_at)
    weekly = _metadata_columns(
        _reconciliation_weekly_summary(combined), dataset_name, refreshed_at
    )

    dataset_directory.mkdir(parents=True, exist_ok=True)
    combined_with_metadata.to_csv(coverage_path, index=False)
    weekly.to_csv(weekly_path, index=False)
    _write_manifest(
        manifest_path,
        dataset_name,
        {},
        {},
        combined,
        refreshed_at,
    )
    print(f"Wrote {coverage_path}", flush=True)
    print(f"Wrote {weekly_path}", flush=True)
    print(f"Wrote {manifest_path}", flush=True)
    return coverage_path, weekly_path, manifest_path


def discover_persisted_coverage_paths(output_directory: Path) -> list[Path]:
    """Find live, combined, and archived coverage stores for history rebuilds."""
    candidates: set[Path] = set()
    if output_directory.name.lower() == "reconciliation_store":
        store_root = output_directory
        history_root = output_directory.parent
    else:
        store_root = output_directory / "reconciliation_store"
        history_root = output_directory
    if store_root.exists():
        candidates.update(store_root.glob("*/consignment_invoice_coverage.csv"))

    snapshots_root = history_root / "carrier_invoice_snapshots"
    if snapshots_root.exists():
        candidates.update(snapshots_root.rglob("consignment_invoice_coverage.csv"))

    return sorted(candidates, key=lambda path: str(path).lower())


def combine_discovered_stores(
    dataset_name: str,
    output_directory: Path,
    flat_output: bool = False,
) -> tuple[Path, Path, Path]:
    """Rebuild a combined history from all locally persisted coverage stores."""
    input_paths = discover_persisted_coverage_paths(output_directory)
    if not input_paths:
        raise ValueError("No persisted coverage files were found for the combined store.")
    print(
        f"Combining {len(input_paths)} persisted coverage store(s) into {dataset_name}...",
        flush=True,
    )
    return combine_persisted_stores(
        dataset_name,
        input_paths,
        output_directory,
        flat_output,
    )


def refresh_weekly_summary(coverage_path: Path, weekly_path: Path) -> Path:
    coverage = pd.read_csv(coverage_path, low_memory=False)
    weekly = _reconciliation_weekly_summary(coverage)
    if "validation_dataset" in coverage:
        dataset_name = str(coverage["validation_dataset"].dropna().iloc[0]) if coverage["validation_dataset"].notna().any() else ""
        weekly.insert(0, "validation_dataset", dataset_name)
    if "coverage_refresh_timestamp_utc" in coverage:
        timestamps = coverage["coverage_refresh_timestamp_utc"].dropna().astype(str)
        if not timestamps.empty:
            weekly.insert(1, "coverage_refresh_timestamp_utc", timestamps.iloc[0])
    weekly_path.parent.mkdir(parents=True, exist_ok=True)
    weekly.to_csv(weekly_path, index=False)
    print(f"Wrote {weekly_path}", flush=True)
    return weekly_path


def export_stage3_history(
    dataset_name: str = "live",
    output_directory: Path = OUTPUTS / "reconciliation_store",
    coverage_path: Path | None = None,
    stage3_files: dict[str, Path] | None = None,
) -> tuple[Path, Path]:
    """Export compact historical shipment facts for carrier invoice matching."""
    dataset_directory = output_directory / _dataset_slug(dataset_name)
    persisted_path = coverage_path or dataset_directory / "consignment_invoice_coverage.csv"
    if not persisted_path.exists():
        raise FileNotFoundError(f"No persisted coverage file was found: {persisted_path}")
    coverage = pd.read_csv(persisted_path, low_memory=False)
    history = _coverage_history_as_stage3(coverage)
    current = _load_stage3(stage3_files or DEFAULT_STAGE3_FILES)
    stage3 = _merge_stage3_history(history, current)
    if stage3.empty:
        raise ValueError("No historical or current shipment facts were available for invoice matching.")

    dataset_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "MOOREBANK": dataset_directory / "stage3_moorebank_invoice_history.csv",
        "TRUGANINA": dataset_directory / "stage3_truganina_invoice_history.csv",
    }
    site_column = stage3.get("ui_site", pd.Series("", index=stage3.index)).fillna("").astype(str).str.upper()
    for site, path in paths.items():
        site_rows = stage3.loc[site_column.eq(site)].copy()
        site_rows.to_csv(path, index=False)
        print(f"Wrote {len(site_rows):,} {site} historical shipment facts to {path}", flush=True)
    return paths["MOOREBANK"], paths["TRUGANINA"]


def enrich_store_units(
    coverage_path: Path,
    weekly_path: Path,
    stage3_files: dict[str, Path],
) -> tuple[Path, Path]:
    """Backfill packed units into an existing coverage store without re-matching invoices."""
    coverage = pd.read_csv(coverage_path, low_memory=False)
    if coverage.empty:
        raise ValueError(f"No persisted coverage rows were found at {coverage_path}.")

    stage3 = _load_stage3(stage3_files)
    if "parcel_units_total" not in stage3:
        raise ValueError("The supplied Stage 3 files do not contain parcel_units_total.")

    stage3_lookup = pd.DataFrame(
        {
            "CFC": _coalesced_lookup(stage3, ("ui_site", "site")),
            "Shippit invoice number": _coalesced_lookup(
                stage3, ("invoice_number", "shippit_invoice_number")
            ),
            "Shippit tracking number": _coalesced_lookup(
                stage3, ("carrier_tracking_number", "shippit_tracking_number")
            ),
            "Shippit units": pd.to_numeric(stage3["parcel_units_total"], errors="coerce"),
        }
    )
    stage3_lookup["_coverage_key"] = _coverage_key(stage3_lookup)
    stage3_lookup = stage3_lookup.loc[
        stage3_lookup["Shippit units"].notna()
        & stage3_lookup["_coverage_key"].ne("||")
    ].drop_duplicates("_coverage_key", keep="last")
    units_by_key = stage3_lookup.set_index("_coverage_key")["Shippit units"]

    coverage_keys = _coverage_key(coverage)
    mapped_units = coverage_keys.map(units_by_key)
    existing_units = (
        pd.to_numeric(coverage["Shippit units"], errors="coerce")
        if "Shippit units" in coverage
        else pd.Series(float("nan"), index=coverage.index, dtype="float64")
    )
    units = existing_units.fillna(mapped_units).round().astype("Int64")
    if "Shippit units" in coverage:
        coverage["Shippit units"] = units
    else:
        insert_at = (
            coverage.columns.get_loc("Shippit parcel count") + 1
            if "Shippit parcel count" in coverage
            else len(coverage.columns)
        )
        coverage.insert(insert_at, "Shippit units", units)

    coverage.to_csv(coverage_path, index=False)
    refresh_weekly_summary(coverage_path, weekly_path)
    print(
        f"Backfilled Shippit units for {int(mapped_units.notna().sum()):,} coverage rows.",
        flush=True,
    )
    return coverage_path, weekly_path


def _parse_carrier_files(values: list[str], reconciliation_directory: Path) -> dict[str, Path]:
    files = {
        key: reconciliation_directory / path.name
        for key, path in DEFAULT_RECONCILIATION_FILES.items()
    }
    for value in values:
        if "=" not in value:
            raise ValueError(f"Carrier file must use carrier_key=path: {value}")
        key, path = value.split("=", 1)
        files[key.strip()] = Path(path.strip())
    return files


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=(
            "build",
            "refresh-invoices",
            "weekly",
            "combine",
            "combine-discovered",
            "enrich-units",
            "enrich-physical",
            "enrich-dimensions",
            "export-stage3-history",
        ),
        default="build",
    )
    parser.add_argument("--dataset-name", default="live")
    parser.add_argument("--output-directory", type=Path, default=OUTPUTS / "reconciliation_store")
    parser.add_argument(
        "--flat-output",
        action="store_true",
        help="Write directly to --output-directory instead of a dataset subdirectory.",
    )
    parser.add_argument("--reconciliation-directory", type=Path, default=OUTPUTS / "carrier_invoices")
    parser.add_argument("--carrier-file", action="append", default=[], help="Override a carrier input as carrier_key=path.")
    parser.add_argument("--stage3-moorebank", type=Path, default=DEFAULT_STAGE3_FILES["MOOREBANK"])
    parser.add_argument("--stage3-truganina", type=Path, default=DEFAULT_STAGE3_FILES["TRUGANINA"])
    parser.add_argument("--coverage-path", type=Path)
    parser.add_argument("--weekly-path", type=Path)
    parser.add_argument(
        "--incremental-carrier",
        choices=tuple(INCREMENTAL_CARRIER_ALIASES),
        help="When refreshing invoices, rematch only this carrier's affected scope.",
    )
    parser.add_argument(
        "--invoice-start-date",
        help="Inclusive invoice/service date for an incremental refresh in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--invoice-end-date",
        help="Inclusive invoice/service date for an incremental refresh in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--invoice-source-file",
        action="append",
        default=[],
        help="Changed invoice workbook name to include in an incremental refresh; repeat as needed.",
    )
    parser.add_argument(
        "--input-store",
        action="append",
        default=[],
        help="Persisted coverage CSV to include when --mode combine is selected.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.mode == "export-stage3-history":
        export_stage3_history(
            args.dataset_name,
            args.output_directory,
            args.coverage_path,
            {
                "MOOREBANK": args.stage3_moorebank,
                "TRUGANINA": args.stage3_truganina,
            },
        )
        return
    if args.mode == "combine-discovered":
        combine_discovered_stores(
            args.dataset_name,
            args.output_directory,
            args.flat_output,
        )
        return
    if args.mode == "refresh-invoices":
        build_store(
            args.dataset_name,
            {
                "MOOREBANK": args.stage3_moorebank,
                "TRUGANINA": args.stage3_truganina,
            },
            _parse_carrier_files(args.carrier_file, args.reconciliation_directory),
            args.output_directory,
            args.flat_output,
            rematch_existing=True,
            incremental_carrier=args.incremental_carrier,
            invoice_start_date=args.invoice_start_date,
            invoice_end_date=args.invoice_end_date,
            invoice_source_files=tuple(args.invoice_source_file),
        )
        return
    if args.mode == "combine":
        if not args.input_store:
            raise ValueError("At least one --input-store is required when --mode combine is selected.")
        combine_persisted_stores(
            args.dataset_name,
            [Path(path) for path in args.input_store],
            args.output_directory,
            args.flat_output,
        )
        return
    if args.mode == "weekly":
        dataset_directory = _dataset_directory(args.output_directory, args.dataset_name, args.flat_output)
        coverage_path = args.coverage_path or dataset_directory / "consignment_invoice_coverage.csv"
        weekly_path = args.weekly_path or dataset_directory / "weekly_invoice_summary.csv"
        refresh_weekly_summary(coverage_path, weekly_path)
        return

    if args.mode == "enrich-units":
        dataset_directory = _dataset_directory(args.output_directory, args.dataset_name, args.flat_output)
        coverage_path = args.coverage_path or dataset_directory / "consignment_invoice_coverage.csv"
        weekly_path = args.weekly_path or dataset_directory / "weekly_invoice_summary.csv"
        enrich_store_units(
            coverage_path,
            weekly_path,
            {
                "MOOREBANK": args.stage3_moorebank,
                "TRUGANINA": args.stage3_truganina,
            },
        )
        return
    if args.mode == "enrich-physical":
        enrich_store_physical(
            args.dataset_name,
            {"MOOREBANK": args.stage3_moorebank, "TRUGANINA": args.stage3_truganina},
            args.output_directory,
            args.flat_output,
        )
        return
    if args.mode == "enrich-dimensions":
        dataset_directory = _dataset_directory(args.output_directory, args.dataset_name, args.flat_output)
        coverage_path = args.coverage_path or dataset_directory / "consignment_invoice_coverage.csv"
        weekly_path = args.weekly_path or dataset_directory / "weekly_invoice_summary.csv"
        enrich_store_dimensions(
            coverage_path,
            weekly_path,
            {
                "MOOREBANK": args.stage3_moorebank,
                "TRUGANINA": args.stage3_truganina,
            },
        )
        return

    stage3_files = {
        "MOOREBANK": args.stage3_moorebank,
        "TRUGANINA": args.stage3_truganina,
    }
    reconciliation_files = _parse_carrier_files(args.carrier_file, args.reconciliation_directory)
    build_store(args.dataset_name, stage3_files, reconciliation_files, args.output_directory, args.flat_output)


if __name__ == "__main__":
    main()
