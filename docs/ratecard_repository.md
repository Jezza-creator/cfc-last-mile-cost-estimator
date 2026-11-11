# Ratecard Repository

This folder is the working library for carrier ratecard source files and validation artefacts used by the last-mile cost estimator.

## Purpose

- Store the original carrier inputs in one place, including PDFs, Excel workbooks, and any supporting notes.
- Provide a single validation pack that shows how each carrier charge is being interpreted.
- Make it easy for Ops and Transport teams to review the charging logic and spot missing data.

## What to upload

- Original carrier ratecards, commercial schedules, and master zone files.
- Any revised carrier ratecards when pricing changes.
- Validation workbooks generated from the cost estimator review pack.
- Supporting box suite files when available.

## Naming convention

Use a date-stamped, human-readable name:

- `Carrier name - document type - YYYYMMDD.ext`
- Examples:
  - `Couriers Please - Master Zone File - 20260824.xls`
  - `Allied Express - Ratecard - 20260824.xlsx`
  - `Validation - Ratecard Review Pack - 20260824.xlsx`

## Review pack

The current validation workbook is designed to make assumptions explicit. It highlights:

- where parcelisation is inferred from order flow and timing
- where box-suite dimensions are used as an overlay
- how Allied pricing is layered across base lane rates, remote area surcharges, and additional services
- where carrier zones or band matches are missing
- where carrier-specific caps or fallbacks are applied
- what source data is still needed to remove each workaround

This workbook is a starting point, not a finished answer.

- It is designed so Ops and Transport can review the assumptions, spot gaps, and suggest better ways to model the carrier logic.
- Repeated mismatches should be treated as improvement opportunities rather than failures.
- The aim is to reduce the number and size of the gaps over time as better source data and business feedback become available.

The two main validation views are:

- parcelizer validation: checks whether the order split looks sensible before we price it
- carrier validation: checks whether the carrier cost and cheapest alternative look sensible after the split
- invoice drilldown workbook: lets Ops enter one invoice number and review the parcel split, carrier cost, and SKU dimension checks together

A companion summary file is also produced for parcelizer review:

- parcelizer air-ratio summary: shows the normal fill and air ratio range by carrier and pack process, plus outliers that may point to bad SKU dimensions or poor PLM data

Simple reading guide:

- if `parcel_count_match = Y` and the parcel weights look balanced, the parcelizer is probably doing the right thing
- if `flag_odd_allocation = Y`, the split should be reviewed because one parcel may be carrying too much or too little weight
- the Shippit basis fields (`shippit_dead_weight_kg`, `shippit_volumetric_weight_kg`, `shippit_chargeable_weight_kg`) show what the parcelizer is being compared against
- the air-ratio summary helps spot the normal packing range so outliers can be investigated as possible SKU or PLM data issues
- the air-ratio summary flags groups that are unusually airy or noisy so Ops can review them first
- if `avg_weight_gap_kg` or `avg_chargeable_gap_kg` is large, the parcelization assumptions need review before we rely on the carrier economics
- if the carrier validation shows a higher actual cost than the cheapest valid alternative, the cost table, zone mapping, or carrier coverage should be checked
- the invoice drilldown workbook is the quickest way to sanity-check one order end to end when Ops wants to inspect the raw split, the Shippit comparison, and the SKU dimensions together

## Placeholders

- CFC box suite: this folder should eventually hold the Truganina and Moorebank CFC box suite inputs once they are confirmed.
- If a CFC box suite is not yet available, keep a clearly labelled placeholder file or note in the folder so the gap is visible.
- Allied validation sheets: keep the base Allied road ratecard, remote area surcharge matrix, additional services sheet, and zone guide together so the pricing hierarchy can be reviewed end to end.

## Recommended workflow

1. Drop the original source file into this folder.
2. Update the validation pack with the new file version.
3. Review the missing reason summary before publishing cost outputs.
4. Replace any assumptions or overlays with source data once Ops or Transport confirms it.
