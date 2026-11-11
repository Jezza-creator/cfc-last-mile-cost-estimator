from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_PROXY_BOX_SUITE_PATH = Path("data/inputs/box_suite/proxy_box_suite_non_cfc.csv")
DEFAULT_CFC_BOX_SUITE_PATH = Path("data/inputs/box_suite/cfc_box_suite.csv")


@dataclass(frozen=True)
class BoxSuiteBand:
    suite_source: str
    carrier_service: str
    volumetric_weight_kg_band: float
    dead_weight_kg_band: float
    chargeable_weight_kg_band: float
    row_count: int
    order_count: int
    store_count: int
    avg_parcel_count: float
    suite_name: str = ""
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None


def load_box_suite_catalog(paths: Iterable[Path]) -> list[BoxSuiteBand]:
    catalog: list[BoxSuiteBand] = []
    for path in paths:
        catalog.extend(_load_box_suite_file(path))
    return catalog


def match_box_suite(
    carrier_service: str,
    volumetric_weight_kg: float | None,
    catalog: list[BoxSuiteBand],
) -> BoxSuiteBand | None:
    if volumetric_weight_kg is None:
        return None

    carrier_key = _normalize(carrier_service)
    candidates = [
        band
        for band in catalog
        if not _normalize(band.carrier_service)
        or _normalize(band.carrier_service) == carrier_key
    ]
    if not candidates:
        return None

    exact = [
        band
        for band in candidates
        if round(band.volumetric_weight_kg_band, 1) == round(volumetric_weight_kg, 1)
    ]
    if exact:
        return max(exact, key=_band_sort_key)

    nearest = min(
        candidates,
        key=lambda band: (
            abs(band.volumetric_weight_kg_band - volumetric_weight_kg),
            -band.row_count,
            -band.order_count,
            -band.store_count,
        ),
    )
    return nearest if abs(nearest.volumetric_weight_kg_band - volumetric_weight_kg) <= 0.15 else None


def summarize_box_suite_matches(matches: list[BoxSuiteBand]) -> tuple[str, str, str, int]:
    if not matches:
        return "", "", "", 0

    counts = Counter(
        (
            match.suite_source,
            match.carrier_service,
            match.suite_name or f"{match.volumetric_weight_kg_band:.1f}",
            f"{match.volumetric_weight_kg_band:.1f}",
        )
        for match in matches
    )
    (source, carrier_service, suite_name, band), match_count = max(
        counts.items(),
        key=lambda item: (
            item[1],
            item[0][0],
            item[0][1],
            item[0][3],
        ),
    )
    return source, f"{carrier_service}:{suite_name}", band, match_count


def _load_box_suite_file(path: Path) -> list[BoxSuiteBand]:
    if not path.exists():
        return []

    rows: list[BoxSuiteBand] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            carrier_service = str(row.get("carrier_service") or row.get("CARRIER_SERVICE") or "")
            volumetric_weight_kg_band = _to_float(
                row.get("volumetric_weight_kg_band") or row.get("VOLUMETRIC_WEIGHT_KG_BAND")
            )
            dead_weight_kg_band = _to_float(
                row.get("dead_weight_kg_band") or row.get("DEAD_WEIGHT_KG_BAND")
            )
            chargeable_weight_kg_band = _to_float(
                row.get("chargeable_weight_kg_band") or row.get("CHARGEABLE_WEIGHT_KG_BAND")
            )
            if volumetric_weight_kg_band is None:
                continue
            rows.append(
                BoxSuiteBand(
                    suite_source=str(row.get("suite_source") or row.get("SUITE_SOURCE") or "PROXY"),
                    carrier_service=carrier_service,
                    volumetric_weight_kg_band=volumetric_weight_kg_band,
                    dead_weight_kg_band=dead_weight_kg_band or 0.0,
                    chargeable_weight_kg_band=chargeable_weight_kg_band or 0.0,
                    row_count=int(row.get("row_count") or row.get("ROW_COUNT") or 0),
                    order_count=int(row.get("order_count") or row.get("ORDER_COUNT") or 0),
                    store_count=int(row.get("store_count") or row.get("STORE_COUNT") or 0),
                    avg_parcel_count=_to_float(row.get("avg_parcel_count") or row.get("AVG_PARCEL_COUNT")) or 0.0,
                    suite_name=str(row.get("suite_name") or row.get("SUITE_NAME") or ""),
                    length_cm=_to_float(row.get("length_cm") or row.get("LENGTH_CM")),
                    width_cm=_to_float(row.get("width_cm") or row.get("WIDTH_CM")),
                    height_cm=_to_float(row.get("height_cm") or row.get("HEIGHT_CM")),
                )
            )
    return rows


def _band_sort_key(band: BoxSuiteBand) -> tuple[int, int, int, float]:
    return band.row_count, band.order_count, band.store_count, band.avg_parcel_count


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
