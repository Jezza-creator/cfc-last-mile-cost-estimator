"""Load editable FSC rules and calculate invoice-based validation rates.

The model rules are intentionally kept separate from invoice evidence.  A
published or user-entered rule drives Stage 3; invoice evidence only provides
a weighted rate for validation on the FSC inputs page.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


FSC_RULE_COLUMNS = (
    "carrier",
    "service",
    "start_date",
    "end_date",
    "fuel_surcharge_pct",
    "source_note",
)

_AP_BASE_SERVICES = {
    "B48": "eParcel Metro",
    "B5R": "eParcel Return",
    "B5S": "eParcel",
}


def load_model_rules(path: Path) -> list[dict[str, str]]:
    """Read the persisted model FSC schedule in its Stage 3 column order."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {column: str(row.get(column) or "").strip() for column in FSC_RULE_COLUMNS}
            for row in csv.DictReader(handle)
        ]


def validate_model_rules(rows: Iterable[dict[str, object]]) -> list[str]:
    """Return user-facing validation errors for an editable rule schedule."""
    errors: list[str] = []
    parsed: list[tuple[int, str, str, date, date]] = []
    for row_number, row in enumerate(rows, start=1):
        carrier = _text(row.get("carrier"))
        service = _text(row.get("service"))
        start_text = _text(row.get("start_date"))
        end_text = _text(row.get("end_date"))
        rate_text = _text(row.get("fuel_surcharge_pct"))
        rate = _number(rate_text)
        if not carrier or not service:
            errors.append(f"Row {row_number}: carrier and service are required.")
        try:
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
            if end < start:
                errors.append(f"Row {row_number}: end date must not be before start date.")
        except ValueError:
            errors.append(f"Row {row_number}: start and end dates must use YYYY-MM-DD.")
            continue
        if not rate_text:
            errors.append(f"Row {row_number}: FSC rate is required.")
        elif not _is_number(rate_text) or rate < 0 or rate > 100:
            errors.append(f"Row {row_number}: FSC rate must be between 0 and 100 percent.")
        parsed.append((row_number, carrier.upper(), service.upper(), start, end))

    for index, left in enumerate(parsed):
        for right in parsed[index + 1 :]:
            if left[1:3] != right[1:3]:
                continue
            if left[3] <= right[4] and right[3] <= left[4]:
                errors.append(
                    f"Rows {left[0]} and {right[0]} overlap for {left[1]} / {left[2]}."
                )
    return errors


def invoice_validated_rates(
    rules: Iterable[dict[str, object]],
    invoice_directory: Path,
) -> list[dict[str, object]]:
    """Calculate weighted invoice FSC rates for each configured model rule.

    AP's base-line fuel field and D05/DS1 are two representations of the same
    charge. The base-line field is used for the rate calculation because it is
    available at consignment level; the separate D05/DS1 lines are not added.
    """
    model_rules = [
        {
            "carrier": _text(row.get("carrier")),
            "service": _text(row.get("service")),
            "start_date": _date(row.get("start_date")),
            "end_date": _date(row.get("end_date")),
            "fuel_surcharge_pct": _number(row.get("fuel_surcharge_pct")),
            "source_note": _text(row.get("source_note")),
        }
        for row in rules
    ]
    evidence = _collect_invoice_evidence(model_rules, invoice_directory)
    output: list[dict[str, object]] = []
    for rule in model_rules:
        start = _parse_date(rule["start_date"])
        end = _parse_date(rule["end_date"])
        matching = [
            item
            for item in evidence
            if _matches(rule["carrier"], item["carrier"])
            and _matches(rule["service"], item["service"])
            and item["effective_date"] is not None
            and (start is None or item["effective_date"] >= start)
            and (end is None or item["effective_date"] <= end)
        ]
        base = sum(float(item["base"]) for item in matching)
        fuel = sum(float(item["fuel"]) for item in matching)
        output.append(
            {
                **rule,
                "invoice_validated_fsc_pct": fuel / base * 100.0 if base > 0 else None,
                "invoice_base_cost_ex_gst": base if base > 0 else None,
                "invoice_fuel_cost_ex_gst": fuel if fuel > 0 else None,
                "invoice_evidence_count": sum(int(item["count"]) for item in matching),
                "invoice_validation_source": _source_label(matching),
            }
        )
    return output


def _collect_invoice_evidence(
    rules: list[dict[str, str]],
    invoice_directory: Path,
) -> list[dict[str, object]]:
    carriers = {row["carrier"].upper() for row in rules}
    evidence: list[dict[str, object]] = []
    if "AUSTRALIA POST" in carriers:
        evidence.extend(_read_australia_post(invoice_directory / "australia_post_charge_lines.csv"))
    if "GOPEOPLE" in carriers:
        evidence.extend(_read_gopeople(invoice_directory / "gopeople_charge_lines.csv"))
    if "COURIERS PLEASE" in carriers:
        evidence.extend(_read_couriers_please(invoice_directory / "couriers_please_charge_lines.csv"))
    if "ALLIED EXPRESS" in carriers:
        evidence.extend(_read_allied(invoice_directory / "allied_jobs.csv"))
    return evidence


def _read_australia_post(path: Path) -> list[dict[str, object]]:
    grouped: dict[tuple[str, date], dict[str, object]] = defaultdict(
        lambda: {"base": 0.0, "fuel": 0.0, "count": 0}
    )
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            service = _AP_BASE_SERVICES.get(_text(row.get("material")).upper())
            effective_date = _parse_date(row.get("service_date"))
            if not service or effective_date is None:
                continue
            base = _number(row.get("amount_excl_tax"))
            if base <= 0:
                continue
            group = grouped[(service, effective_date)]
            group["base"] = float(group["base"]) + base
            group["fuel"] = float(group["fuel"]) + max(_number(row.get("fuel_surcharge_field_value")), 0.0)
            group["count"] = int(group["count"]) + 1
    return [
        {
            "carrier": "Australia Post",
            "service": service,
            "effective_date": effective_date,
            **values,
            "source": "AP base service fuel field; D05/DS1 control lines not added",
        }
        for (service, effective_date), values in sorted(grouped.items())
    ]


def _read_gopeople(path: Path) -> list[dict[str, object]]:
    grouped: dict[tuple[str, date], dict[str, object]] = defaultdict(
        lambda: {"base": 0.0, "fuel": 0.0, "count": 0}
    )
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            effective_date = _parse_date(row.get("consignment_date"))
            if effective_date is None:
                continue
            raw_fuel = row.get("invoice_notes_or_fuel")
            if raw_fuel in (None, ""):
                # Blank GoPeople fuel fields are not FSC evidence. Excluding
                # them avoids diluting the observed rate with non-FSC rows.
                continue
            base = _number(row.get("actual_base_cost_ex_gst"))
            fuel = _number(row.get("actual_fuel_surcharge_ex_gst"))
            if base <= 0 and fuel == 0:
                charge = _number(row.get("charge_ex_gst"))
                if charge > 0:
                    # GoPeople's invoice field is a dollar fuel amount, not a rate.
                    fuel = min(max(_number(raw_fuel), 0.0), charge)
                    base = charge - fuel
            if base <= 0 or fuel <= 0:
                continue
            group = grouped[("GoEXPRESS", effective_date)]
            group["base"] = float(group["base"]) + base
            group["fuel"] = float(group["fuel"]) + fuel
            group["count"] = int(group["count"]) + 1
    return [
        {
            "carrier": "GoPeople",
            "service": service,
            "effective_date": effective_date,
            **values,
            "source": "GoPeople parsed invoice fuel field",
        }
        for (service, effective_date), values in sorted(grouped.items())
    ]


def _read_couriers_please(path: Path) -> list[dict[str, object]]:
    grouped: dict[tuple[str, date], dict[str, object]] = defaultdict(
        lambda: {"base": 0.0, "fuel": 0.0, "count": 0}
    )
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            service = _text(row.get("service_code")) or _text(row.get("service_name"))
            effective_date = _parse_date(row.get("consignment_date"))
            base = _number(row.get("base_cost_ex_gst"))
            fuel = _number(row.get("fuel_surcharge_ex_gst"))
            if not service or effective_date is None or base <= 0 or fuel < 0:
                continue
            group = grouped[(service, effective_date)]
            group["base"] = float(group["base"]) + base
            group["fuel"] = float(group["fuel"]) + fuel
            group["count"] = int(group["count"]) + 1
    return [
        {
            "carrier": "Couriers Please",
            "service": service,
            "effective_date": effective_date,
            **values,
            "source": "Couriers Please charge-line fuel component",
        }
        for (service, effective_date), values in sorted(grouped.items())
    ]


def _read_allied(path: Path) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = defaultdict(
        lambda: {"base": 0.0, "fuel": 0.0, "count": 0}
    )
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            service = _text(row.get("service_code"))
            base = _number(row.get("actual_base_cost_ex_gst"))
            fuel = _number(row.get("actual_fuel_surcharge_ex_gst"))
            if not service or base <= 0 or fuel < 0:
                continue
            group = grouped[service]
            group["base"] = float(group["base"]) + base
            group["fuel"] = float(group["fuel"]) + fuel
            group["count"] = int(group["count"]) + 1
    # Allied's consolidated jobs extract has a week rather than a date. It is
    # still useful as validation evidence, but it cannot be matched to a date
    # bounded rule without a source-sheet date mapping.
    return [
        {
            "carrier": "Allied Express",
            "service": service,
            "effective_date": None,
            **values,
            "source": "Allied consolidated invoice fuel component; date unavailable",
        }
        for service, values in sorted(grouped.items())
    ]


def _source_label(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No invoice fuel evidence in configured period"
    return "; ".join(sorted({str(row["source"]) for row in rows}))


def _matches(rule_value: str, actual_value: str) -> bool:
    return not rule_value or rule_value == "*" or rule_value.upper() == actual_value.upper()


def _date(value: object) -> str:
    return _parse_date(value).isoformat() if _parse_date(value) else _text(value)


def _parse_date(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    for parser in (date.fromisoformat, lambda item: datetime.strptime(item, "%d/%m/%Y").date()):
        try:
            return parser(text)
        except ValueError:
            continue
    return None


def _number(value: object) -> float:
    try:
        result = float(str(value or "").replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _is_number(value: object) -> bool:
    try:
        return math.isfinite(float(str(value).replace(",", "").replace("$", "").replace("%", "").strip()))
    except (TypeError, ValueError):
        return False


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
