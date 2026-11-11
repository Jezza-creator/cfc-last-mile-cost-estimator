# Ratecard File Inventory

This register is the working inventory for the `LM rate cards` SharePoint folder.

## Notes

- Files marked `Source` are the primary carrier inputs used to build or validate the cost tables.
- Files marked `Supporting` help explain logic, zones, or parcelisation but are not themselves ratecards.
- Files marked `Placeholder` are intentionally present as future inputs or temporary review artefacts.

## Inventory

| File name | Carrier / topic | File type | Role | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| `AP ratecard - Kmart Australia Limited 05082025.pdf` | Australia Post | PDF | Source | Confirmed | Base AP ratecard source. |
| `AP Metro to Metro - Kmart MELB 01AUG25.pdf` | Australia Post Metro | PDF | Source | Confirmed | Metro-specific AP reference. |
| `CP - Master_Zone_File_November_2023_Final_2_2a874cf3a0.xls` | Couriers Please | Excel | Source | Confirmed | Zone mapping source. |
| `CP ratecard - Moorebank CFC 2026.xlsx` | Couriers Please | Excel | Source | Confirmed | Moorebank pricing source. |
| `Go People Commercials (Updated June 2026).docx` | GoPeople | Word | Source | Confirmed | Commercials / pricing reference. |
| `Allied ratecard - Online Rate Schedule 26ECOMRXC5VF-KMA (Proposed)26.xlsx` | Allied Express | Excel | Source | Confirmed | Allied rate schedule source. |
| `Allied - ZoneGuideCondensed.pdf` | Allied Express | PDF | Source | Confirmed | Allied zone guide used for fallback lookups. |
| `Allied Additional Services VFF` | Allied Express | Text / Excel | Source | Confirmed | Allied surcharge and handling rules. |
| `Allied Remote Area Surcharge` | Allied Express | Text / Excel | Source | Confirmed | Allied remote area surcharge matrix. |
| `data/reference/allied_b2c_road_ratecard.csv` | Allied Express | CSV | Derived | Confirmed | Local lane table extracted from the Allied road card. |
| `data/reference/allied_additional_services.csv` | Allied Express | CSV | Derived | Confirmed | Local surcharge rule table used by the estimator. |
| `Proxy_box_suite_data_Non-CFC.24082026.xlsx` | Non-CFC proxy box suite | Excel | Supporting | Confirmed | Proxy box suite used until CFC suite is available. |
| `PLM_CFC_Dimension_Variance_Report_24082026.xlsx` | PLM vs CFC variance | Excel | Supporting | Confirmed | Highlights dimension/weight variance for ops review. |
| `Parcel_Estimator_output_test_24082026.xlsx` | Parcel estimator output | Excel | Supporting | Confirmed | Sample estimator output for validation. |
| `Test_can delete later.xlsx` | Temporary test file | Excel | Placeholder | Temporary | Safe to remove once no longer needed. |
| `cfc_box_suite.csv` | CFC box suite | CSV | Placeholder | Not yet provided | Add when the CFC box suite is confirmed. |

## Validation workbook

The main review pack is:

- `ratecard_review_pack.xlsx`

It summarizes:

- carrier-level missing reasons
- assumptions and overlays
- data still required
- a source register for the folder

## Next additions

- Add the CFC box suite when it is ready.
- Replace proxy box-suite assumptions with the confirmed CFC suite.
- Add any new carrier files as they are received from Ops / Transport.
