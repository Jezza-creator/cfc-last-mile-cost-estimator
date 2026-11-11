"""Build and reconcile a deterministic, targeted Australia Post validation sample."""

from __future__ import annotations

import argparse
import csv
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from src.carrier_invoice_australia_post_ingest import (
    _aggregate_consignments,
    _charge_line_headers,
    _consignment_headers,
    _normalise_row,
    _write_control_summary,
    _write_csv,
)
from src.db import get_session


SOURCE_TABLE = "KSFPA.ONLINE_UGAM_PVT.OF_INV_AUSPOST"
SHIPPIT_TABLE = "KSFPA.FREIGHT.SHIPPIT_ORDERS"
DEFAULT_SAMPLE_SIZE = 1_000
DEFAULT_OUTPUT_ROOT = Path("outputs/carrier_invoice_snapshots")
SITE_STORE_NAMES = (
    "1194 Moorebank CFC NSW AU",
    "1190 Truganina CFC VIC AU",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=_iso_date, help="Inclusive AP service date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", required=True, type=_iso_date, help="Inclusive AP service date in YYYY-MM-DD format.")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE, help="Target number of AP consignments; defaults to 1000.")
    parser.add_argument("--output-directory", type=Path, default=None, help="Dedicated output directory for this sample run.")
    parser.add_argument("--prepare-only", action="store_true", help="Select and ingest the sample without running Stages 1-3 or reconciliation.")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise ValueError("--start-date must be on or before --end-date.")
    if args.sample_size <= 0:
        raise ValueError("--sample-size must be positive.")

    output_directory = args.output_directory or (
        DEFAULT_OUTPUT_ROOT / f"australia_post_sample_{args.start_date}_{args.end_date}_{args.sample_size}"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    stage_directory = output_directory / "stage_outputs"

    print("Selecting AP consignments with historical Shippit tracking matches...", flush=True)
    candidates = _select_candidates(args.start_date, args.end_date, args.sample_size)
    if not candidates:
        raise RuntimeError("No Australia Post consignments matched Shippit in the requested date range.")

    _write_sample_manifest(output_directory / "australia_post_sample_manifest.csv", candidates)
    raw_rows = _fetch_selected_source_rows(args.start_date, args.end_date, candidates)
    charge_lines = [_normalise_row(row, index) for index, row in enumerate(raw_rows, start=1)]
    charge_lines = [row for row in charge_lines if row["invoice_no"] or row["consignment_id"] or row["material"]]
    consignments = _aggregate_consignments(charge_lines)
    _write_csv(output_directory / "australia_post_charge_lines.csv", _charge_line_headers(), charge_lines)
    _write_csv(output_directory / "australia_post_consignments.csv", _consignment_headers(), consignments)
    _write_control_summary(
        output_directory / "australia_post_invoice_control_summary.csv",
        charge_lines,
        consignments,
        args.start_date,
        args.end_date,
    )
    _write_invoice_filters(output_directory, candidates)
    print(f"Selected {len(candidates)} AP consignments.", flush=True)
    print(f"Read {len(charge_lines)} AP charge lines for the selected consignments.", flush=True)
    print(f"Prepared sample outputs in {output_directory}.", flush=True)

    if args.prepare_only:
        return

    _run_targeted_pipeline(output_directory, stage_directory, args.start_date, candidates)
    _run_reconciliation(output_directory, stage_directory)
    print(f"Completed AP sample reconciliation in {output_directory}.", flush=True)


def _select_candidates(start_date: str, end_date: str, sample_size: int) -> list[dict[str, str]]:
    session = get_session()
    try:
        count_rows = [dict(row.as_dict()) for row in session.sql(_candidate_counts_query(start_date, end_date)).collect()]
        quotas = _allocate_quotas(count_rows, sample_size)
        if not quotas:
            return []
        rows = [
            dict(row.as_dict())
            for row in session.sql(_candidate_rows_query(start_date, end_date, quotas)).collect()
        ]
    finally:
        session.close()

    selected = []
    for row in rows:
        selected.append(
            {
                "carrier_tracking_number": _text(row.get("CARRIER_TRACKING_NUMBER")),
                "ap_invoice_no": _text(row.get("AP_INVOICE_NO")),
                "assignment_no": _text(row.get("ASSIGNMENT_NO")),
                "service_date": _text(row.get("SERVICE_DATE")),
                "site": _text(row.get("SITE")),
                "invoice_number": _text(row.get("INVOICE_NUMBER")),
                "shippit_service": _text(row.get("SHIPPIT_SERVICE")),
                "service_material": _text(row.get("SERVICE_MATERIAL")),
                "candidate_chargeable_weight_kg": _text(row.get("CANDIDATE_CHARGEABLE_WEIGHT_KG")),
                "weight_band": _text(row.get("WEIGHT_BAND")),
                "service_week": _text(row.get("SERVICE_WEEK")),
                "destination_zone": _text(row.get("DESTINATION_ZONE")),
                "destination_postcode": _text(row.get("DESTINATION_POSTCODE")),
                "sample_quota": _text(row.get("SAMPLE_QUOTA")),
            }
        )
    return sorted(selected, key=lambda row: (row["site"], row["service_week"], row["carrier_tracking_number"]))


def _candidate_cte(start_date: str, end_date: str) -> str:
    stores_sql = ", ".join(_sql_literal(store) for store in SITE_STORE_NAMES)
    return f"""
    WITH ap_candidates AS (
        SELECT
            NULLIF(TRIM(CONSIGNMENT_ID), '') AS carrier_tracking_number,
            MIN(NULLIF(TRIM(INVOICE_NO), '')) AS ap_invoice_no,
            MIN(NULLIF(TRIM(ASSIGNMENT_NO), '')) AS assignment_no,
            MIN(TRY_TO_DATE(SERVICE_DATE, 'YYYYMMDD')) AS service_date,
            COALESCE(
                MIN(NULLIF(TRIM(FROM_POSTAL_CODE), '')),
                MIN(NULLIF(TRIM(POST_CODE), ''))
            ) AS origin_postcode,
            MIN(NULLIF(TRIM(FROM_CITY), '')) AS origin_city,
            MIN(NULLIF(TRIM(FROM_STATE), '')) AS origin_state,
            MIN(NULLIF(TRIM(TO_POSTAL_CODE), '')) AS destination_postcode,
            MIN(NULLIF(TRIM(TO_CITY), '')) AS destination_city,
            MIN(NULLIF(TRIM(TO_STATE), '')) AS destination_state,
            COALESCE(
                MAX(IFF(UPPER(TRIM(MATERIAL)) IN ('B48', 'B5R', 'B5S'), UPPER(TRIM(MATERIAL)), NULL)),
                'UNKNOWN'
            ) AS service_material,
            COALESCE(
                NULLIF(MAX(BILLED_WEIGHT), 0),
                NULLIF(MAX(CUBIC_WEIGHT), 0),
                NULLIF(MAX(ACTUAL_WEIGHT), 0),
                NULLIF(MAX(DECLARED_WEIGHT), 0),
                0
            ) AS candidate_chargeable_weight_kg,
            MIN(NULLIF(TRIM(DESTINATION_ZONE), '')) AS destination_zone
        FROM {SOURCE_TABLE}
        WHERE TRY_TO_DATE(SERVICE_DATE, 'YYYYMMDD')
              BETWEEN TO_DATE('{start_date}') AND TO_DATE('{end_date}')
          AND NULLIF(TRIM(CONSIGNMENT_ID), '') IS NOT NULL
        GROUP BY 1
    ),
    shippit_candidates AS (
        SELECT
            TO_VARCHAR(s.ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER) AS invoice_number,
            TO_VARCHAR(s.ORDER_CARRIER__CARRIER_TRACKING_NUMBER) AS carrier_tracking_number,
            s.STORE_ATTRIBUTE__STORE_NAME AS store_name,
            s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE AS despatch_date,
            s.CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME AS shippit_service
        FROM {SHIPPIT_TABLE} s
        WHERE s.STORE_ATTRIBUTE__STORE_NAME IN ({stores_sql})
          AND s.ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
              BETWEEN TO_DATE('{start_date}') AND TO_DATE('{end_date}')
          AND NULLIF(TRIM(TO_VARCHAR(s.ORDER_CARRIER__CARRIER_TRACKING_NUMBER)), '') IS NOT NULL
          AND (
                UPPER(COALESCE(s.CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME, '')) LIKE '%EPARCEL%'
                OR UPPER(COALESCE(s.CARRIER_ATTRIBUTE__CARRIER_SERVICE_NAME, '')) LIKE '%AUSTRALIA POST%'
          )
    ),
    matched AS (
        SELECT
            a.*,
            s.invoice_number,
            s.store_name,
            s.despatch_date,
            s.shippit_service
        FROM ap_candidates a
        JOIN shippit_candidates s
          ON UPPER(TRIM(a.carrier_tracking_number)) = UPPER(TRIM(s.carrier_tracking_number))
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY a.carrier_tracking_number
            ORDER BY IFF(a.service_date = s.despatch_date, 0, 1), s.invoice_number
        ) = 1
    ),
    classified AS (
        SELECT
            *,
            CASE
                WHEN store_name LIKE '%Moorebank%' THEN 'MOOREBANK'
                WHEN store_name LIKE '%Truganina%' THEN 'TRUGANINA'
                ELSE 'UNKNOWN'
            END AS site,
            CASE
                WHEN candidate_chargeable_weight_kg < 1 THEN '0-1 kg'
                WHEN candidate_chargeable_weight_kg < 2 THEN '1-2 kg'
                WHEN candidate_chargeable_weight_kg < 5 THEN '2-5 kg'
                WHEN candidate_chargeable_weight_kg < 10 THEN '5-10 kg'
                WHEN candidate_chargeable_weight_kg < 15 THEN '10-15 kg'
                WHEN candidate_chargeable_weight_kg < 22 THEN '15-22 kg'
                WHEN candidate_chargeable_weight_kg < 32 THEN '22-32 kg'
                WHEN candidate_chargeable_weight_kg < 45 THEN '32-45 kg'
                ELSE '45+ kg'
            END AS weight_band,
            TO_VARCHAR(DATE_TRUNC('WEEK', service_date)) AS service_week
        FROM matched
    )
    """


def _candidate_counts_query(start_date: str, end_date: str) -> str:
    return (
        _candidate_cte(start_date, end_date)
        + """
        SELECT
            site,
            service_material,
            weight_band,
            service_week,
            COUNT(*) AS population_count
        FROM classified
        GROUP BY site, service_material, weight_band, service_week
        ORDER BY site, service_material, weight_band, service_week
        """
    )


def _candidate_rows_query(start_date: str, end_date: str, quotas: dict[tuple[str, str, str, str], int]) -> str:
    quota_values = ", ".join(
        "(" + ", ".join(
            [
                _sql_literal(site),
                _sql_literal(material),
                _sql_literal(weight_band),
                _sql_literal(service_week),
                str(quota),
            ]
        ) + ")"
        for (site, material, weight_band, service_week), quota in sorted(quotas.items())
    )
    return (
        _candidate_cte(start_date, end_date)
        + f"""
        , ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY site, service_material, weight_band, service_week
                    ORDER BY HASH(carrier_tracking_number)
                ) AS sample_rank
            FROM classified
        ),
        quotas AS (
            SELECT
                column1 AS site,
                column2 AS service_material,
                column3 AS weight_band,
                column4 AS service_week,
                column5::NUMBER AS sample_quota
            FROM VALUES {quota_values}
        )
        SELECT
            r.carrier_tracking_number,
            r.ap_invoice_no,
            r.assignment_no,
            TO_VARCHAR(r.service_date) AS service_date,
            r.site,
            r.invoice_number,
            r.shippit_service,
            r.service_material,
            TO_VARCHAR(r.candidate_chargeable_weight_kg) AS candidate_chargeable_weight_kg,
            r.weight_band,
            r.service_week,
            r.destination_zone,
            r.destination_postcode,
            q.sample_quota
        FROM ranked r
        JOIN quotas q
          ON q.site = r.site
         AND q.service_material = r.service_material
         AND q.weight_band = r.weight_band
         AND q.service_week = r.service_week
        WHERE r.sample_rank <= q.sample_quota
        ORDER BY r.site, r.service_week, r.service_material, r.weight_band, r.sample_rank
        """
    )


def _allocate_quotas(count_rows: list[dict[str, object]], sample_size: int) -> dict[tuple[str, str, str, str], int]:
    populations = {
        (
            _text(row.get("SITE")),
            _text(row.get("SERVICE_MATERIAL")),
            _text(row.get("WEIGHT_BAND")),
            _text(row.get("SERVICE_WEEK")),
        ): int(float(row.get("POPULATION_COUNT") or 0))
        for row in count_rows
    }
    populations = {key: count for key, count in populations.items() if count > 0}
    total = sum(populations.values())
    target = min(sample_size, total)
    if target <= 0:
        return {}
    if target == total:
        return populations

    keys = sorted(populations)
    quotas = {key: 0 for key in keys}
    if target >= len(keys):
        for key in keys:
            quotas[key] = 1
        remaining = target - len(keys)
        capacities = {key: populations[key] - 1 for key in keys}
    else:
        remaining = target
        capacities = populations

    capacity_total = sum(capacities.values())
    if remaining and capacity_total:
        fractional: list[tuple[float, tuple[str, str, str, str]]] = []
        allocated = 0
        for key in keys:
            raw = remaining * capacities[key] / capacity_total
            whole = min(capacities[key], math.floor(raw))
            quotas[key] += whole
            allocated += whole
            fractional.append((raw - whole, key))
        for _, key in sorted(fractional, key=lambda item: (-item[0], item[1]))[: remaining - allocated]:
            if quotas[key] < populations[key]:
                quotas[key] += 1
    elif remaining:
        for key in keys[:remaining]:
            quotas[key] += 1
    return {key: quota for key, quota in quotas.items() if quota > 0}


def _selected_source_rows_query(start_date: str, end_date: str, candidates: list[dict[str, str]]) -> str:
    values = ", ".join(
        "(" + ", ".join(
            [
                _sql_literal(row["carrier_tracking_number"]),
                _sql_literal(row["ap_invoice_no"]),
                _sql_literal(row["assignment_no"]),
            ]
        ) + ")"
        for row in candidates
    )
    return f"""
        WITH selected AS (
            SELECT
                column1 AS carrier_tracking_number,
                column2 AS ap_invoice_no,
                column3 AS assignment_no
            FROM VALUES {values}
        ), selected_tracking AS (
            SELECT DISTINCT carrier_tracking_number
            FROM selected
            WHERE NULLIF(TRIM(carrier_tracking_number), '') IS NOT NULL
        ), selected_assignments AS (
            SELECT DISTINCT ap_invoice_no, assignment_no
            FROM selected
            WHERE NULLIF(TRIM(ap_invoice_no), '') IS NOT NULL
              AND NULLIF(TRIM(assignment_no), '') IS NOT NULL
        ), source_rows AS (
            SELECT a.*
            FROM {SOURCE_TABLE} a
            JOIN selected_tracking s
              ON NULLIF(TRIM(a.CONSIGNMENT_ID), '') = s.carrier_tracking_number
            WHERE TRY_TO_DATE(a.SERVICE_DATE, 'YYYYMMDD')
                  BETWEEN TO_DATE('{start_date}') AND TO_DATE('{end_date}')
            UNION ALL
            SELECT a.*
            FROM {SOURCE_TABLE} a
            JOIN selected_assignments s
              ON NULLIF(TRIM(a.INVOICE_NO), '') = s.ap_invoice_no
             AND NULLIF(TRIM(a.ASSIGNMENT_NO), '') = s.assignment_no
            WHERE TRY_TO_DATE(a.SERVICE_DATE, 'YYYYMMDD')
                  BETWEEN TO_DATE('{start_date}') AND TO_DATE('{end_date}')
              AND NULLIF(TRIM(a.CONSIGNMENT_ID), '') IS NULL
        )
        SELECT *
        FROM source_rows
        ORDER BY SERVICE_DATE, INVOICE_NO, ASSIGNMENT_NO, CONSIGNMENT_ID, MATERIAL, ITEM
    """


def _fetch_selected_source_rows(start_date: str, end_date: str, candidates: list[dict[str, str]]) -> list[dict[str, object]]:
    query = _selected_source_rows_query(start_date, end_date, candidates)
    session = get_session()
    try:
        return [dict(row.as_dict()) for row in session.sql(query).collect()]
    finally:
        session.close()


def _write_sample_manifest(path: Path, candidates: list[dict[str, str]]) -> None:
    headers = list(candidates[0]) if candidates else []
    _write_csv(path, headers, candidates)


def _write_invoice_filters(output_directory: Path, candidates: list[dict[str, str]]) -> None:
    for site in ("MOOREBANK", "TRUGANINA"):
        path = output_directory / f"{site.lower()}_invoice_filter.csv"
        rows = sorted({row["invoice_number"] for row in candidates if row["site"] == site and row["invoice_number"]})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["invoice_number"])
            writer.writerows([[value] for value in rows])


def _run_targeted_pipeline(output_directory: Path, stage_directory: Path, start_date: str, candidates: list[dict[str, str]]) -> None:
    stage_directory.mkdir(parents=True, exist_ok=True)
    for site, stage1_module, stage2_module, stage3_module in (
        ("MOOREBANK", "src.stage1_moorebank_parcel_assigner", "src.stage2_moorebank_parcel_physical_estimator", "src.stage3_moorebank_cost_estimator"),
        ("TRUGANINA", "src.stage1_truganina_parcel_assigner", "src.stage2_truganina_parcel_physical_estimator", "src.stage3_truganina_cost_estimator"),
    ):
        lower = site.lower()
        env = os.environ.copy()
        env["LM_EXTRACTION_START_DATE"] = start_date
        env["LM_OUTPUT_DIR"] = str(stage_directory.resolve())
        env["LM_INVOICE_FILTER_PATH"] = str((output_directory / f"{lower}_invoice_filter.csv").resolve())
        env["LM_STAGE1_INPUT_PATH"] = str((stage_directory / f"stage1_{lower}_parcel_assignments_full.csv").resolve())
        env["LM_STAGE1_PARCEL_CONTENTS_PATH"] = str((stage_directory / f"stage1_{lower}_parcel_assignment_lines.csv").resolve())
        env["LM_STAGE2_INPUT_PATH"] = str((stage_directory / f"stage2_{lower}_parcel_physicals_full.csv").resolve())
        env["LM_STAGE2_OUTPUT_PATH"] = str((stage_directory / f"stage2_{lower}_parcel_physicals_full.csv").resolve())
        env["LM_STAGE3_OUTPUT_PATH"] = str((stage_directory / f"stage3_{lower}_cost_estimates_full.csv").resolve())
        _run_module(stage1_module, env)
        _run_module(stage2_module, env)
        _run_module(stage3_module, env)


def _run_reconciliation(output_directory: Path, stage_directory: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "src.carrier_invoice_australia_post_reconciliation",
        "--consignments",
        str(output_directory / "australia_post_consignments.csv"),
        "--output-directory",
        str(output_directory),
        "--moorebank-stage3",
        str(stage_directory / "stage3_moorebank_cost_estimates_full.csv"),
        "--truganina-stage3",
        str(stage_directory / "stage3_truganina_cost_estimates_full.csv"),
    ]
    subprocess.run(command, check=True)


def _run_module(module: str, env: dict[str, str]) -> None:
    print(f"Running {module} for the AP sample...", flush=True)
    subprocess.run([sys.executable, "-m", module], check=True, env=env)


def _sql_literal(value: object) -> str:
    return "'" + _text(value).replace("'", "''") + "'"


def _iso_date(value: str) -> str:
    return date.fromisoformat(str(value).strip()).isoformat()


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


if __name__ == "__main__":
    main()
