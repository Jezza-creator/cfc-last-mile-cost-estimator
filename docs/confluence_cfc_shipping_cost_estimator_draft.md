# CFC Last Mile Shipping Cost Estimator and Carrier Opportunity Comparator

**Status:** Draft for review  
**Proposed action:** Replace the existing *CFC Shipping Cost Estimator and Alternative Comparator* page  
**Product owner:** Advanced Analytics / Online Transport  
**Primary users:** Online Transport, CFC Operations, Finance, and carrier-management teams

## 1. Purpose

The CFC Last Mile Shipping Cost Estimator provides an explainable view of:

- The expected cost of each CFC consignment by carrier and service.
- The carrier that was selected and the next-best valid alternative.
- The difference between modelled cost and invoiced actual cost once an invoice is available.
- The physical and data-quality drivers behind cost differences, including dimensions, weight, parcelisation, fuel, and surcharges.
- Prioritised opportunities that Operations or other teams can investigate and action.

The product is a decision-support and validation tool. It does not change WHS carrier allocation, create carrier bookings, or replace the carrier invoice process.

## 2. Product Overview

The application has separate workspaces for operational monitoring, model quality, historical trends, invoice validation, and consignment-level investigation.

### Dashboard

The Dashboard is the operational view for the current rolling window. It provides:

- Consignments, parcels, or units by day and carrier.
- Carrier mix as counts or percentage of the daily total.
- Modelled cost by shipped carrier.
- Lane and weight matrix views.
- Carrier-choice opportunities where a valid alternative is cheaper on the same modelled physical basis.
- Air and packaging signals for current operations.

The normal daily process reruns the current 14-day overlap. The overlap is deliberate: it allows missed runs, late Shippit corrections, and recent operational changes to be captured before they fall outside the live window.

### Historical Trends

Historical Trends uses the combined persisted history and is intended to show consistency, trends, and lumpy weeks. It includes:

- Weekly volume and cost trends.
- Invoice verification percentage.
- Cost per consignment, parcel, and unit.
- Historical lane and weight matrices.
- Historical air and packaging diagnostics.

Users can select accounting weeks and choose between estimator and invoice-based measures where invoice data exists. For historical cost measures, invoice-verified values are preferred; the estimator is used as the fallback when no invoice is available and the estimate is valid.

### Model Quality

Model Quality separates input and logic quality from invoice validation. It shows:

- Stage 1 parcel-count matching.
- Shippit versus estimator parcel, cube, dead-weight, and chargeable-weight signals.
- CFC versus PLM SKU dimension and weight differences.
- Parcelisation paths and unusual allocation patterns.
- Air-ratio norms and keycodes that need review.
- Missing or fallback data sources.

This workspace can be used with the daily Stage 1-3 outputs even when carrier invoices are not yet available.

### Invoice Reconciliation

Invoice Reconciliation compares the original model estimate with invoiced actuals after carrier invoice data becomes available. It shows:

- Invoice coverage by accounting week, CFC, shipped carrier, and service.
- Matched and unmatched consignments.
- Modelled cost, actual invoiced cost, and signed variance.
- Variance percentage and cost-per-unit measures where the units denominator is complete.
- A fast dim/weight variance signal for high-level triage.
- Detailed variance bridges for selected carriers, CFCs, and periods.
- Australia Post fuel-surcharge validation against the model input.

The sign convention is:

- Positive variance: invoiced actual cost is above the modelled cost.
- Negative variance: invoiced actual cost is below the modelled cost.

The total observed variance is preserved as the complete invoice-versus-model result. Dim/weight variance and residual model/rate variance are analytical explanations of that result, not additional amounts to add to it.

### Consignment Explorer

Consignment Explorer provides the evidence trail for one order or invoice. It brings together:

- Order and invoice identifiers.
- Product keycodes and quantities.
- Parcel allocation and source method.
- Estimated parcel dimensions, cube, air ratio, weight, and volumetric weight.
- Shippit parcel and chargeable-weight measures.
- Carrier cost decision and valid alternatives.
- Invoice actuals and variance where available.
- CFC versus PLM physical evidence.

It is intended for deep dives into high-priority consignments identified by Model Quality, Invoice Reconciliation, or the opportunity lists.

### Opportunity Register

The opportunity workflow is intended to convert an analytical signal into an owned action. An opportunity record should retain:

- Opportunity ID and consignment or cohort scope.
- Evidence and source fields.
- Suspected root cause.
- Recommended action.
- Owner and target team.
- Estimated or validated saving.
- Status, validation result, and supporting notes.

The register is separate from the automated candidate list so that an item is not treated as an approved saving until it has been investigated and validated.

## 3. Processing Flow

The product processes data in three stages.

### Stage 1: Parcel and Product Allocation

Stage 1 determines which products and quantities belong to each parcel.

Source priority:

1. `KSFPA.OMFP.ORD_FULFILLMENT_DETAIL` where positive-quantity rows provide sufficient coverage of the Shippit parcel count.
2. `KSFDA.CATCH_CFC.PACKED` as the fallback when fulfillment detail is missing or incomplete.

The Stage 1 output records the source used, including source shipment/package identifiers and `assignment_source`, so the allocation can be audited.

Important distinction:

- Shippit order data represents the shipped order and its parcel-level measures.
- Fulfillment detail represents article/package records created during fulfillment.
- A consignment may therefore have a different number of product or package-detail rows from the number of Shippit tracking records.

### Stage 2: Physical Parcel Estimation

Stage 2 estimates the physical profile of each allocated parcel from the keycodes and quantities assigned in Stage 1.

The estimator retains separately:

- Packed SKU cube and weight.
- Estimated parcel cube and weight.
- Estimated parcel volumetric weight.
- Estimated parcel chargeable weight.
- Shippit cube, dead weight, volumetric weight, and chargeable weight.
- Estimated air ratio.

Physical source priority is:

1. CFC SKU attributes, reflecting the available CFC cubiscan/WMS record.
2. PLM dimensions and weight as a fallback or comparison source.
3. Box-suite or proxy-box information where carton overboxing and air need to be represented.

The model records when fallback or adjustment logic is used. A large difference between CFC, PLM, Shippit, and estimated physical measures should be investigated before drawing conclusions about carrier pricing.

### Stage 3: Carrier Cost Estimation

Stage 3 applies carrier-specific ratecards, zones, physical limits, parcel rules, fuel, and surcharge assumptions to produce the modelled carrier costs.

For each valid carrier/service option it records:

- Ratecard and pricing-zone result.
- Weight basis and rounding rule.
- Parcel or consignment pricing basis.
- Fuel surcharge treatment.
- Other applicable surcharge components.
- Missing-cost reason where no valid estimate can be produced.

The current carrier result is compared with the cheapest valid alternative. The comparison must always be read with the physical-basis and carrier-limit flags because a cost difference can be caused by an incorrect parcel or weight estimate rather than a real carrier-choice opportunity.

## 4. Carrier Rules and Transparency

The application should expose the active carrier assumptions so users can identify stale or questionable parameters quickly. The rules are working assumptions and must be validated against current commercial agreements before operational changes are made.

| Carrier/service | Key rules represented in the model | Main review point |
|---|---|---|
| Australia Post eParcel | Higher of dead and volumetric weight; 250 kg/m3 working divisor; article limit and zone/ratecard bands; effective-dated fuel surcharge | Check article limits, manifest correction/unmanifested fees, fuel allocation, and service mapping |
| Couriers Please | Higher of dead and volumetric weight; 250 kg/m3 working divisor; 10-parcel review limit; length and multi-parcel surcharge assumptions | Confirm the WHS parcel/weight basis and CP treatment of over-length or misdeclared freight |
| GoExpress / GoBundle | Higher of dead and volumetric weight; carrier-specific weight, dimension, and article limits; fuel treatment where configured | GoBUNDLE remains a proxy/working assumption and should not be treated as an approved commercial result |
| Allied Express | Consignment-level road pricing, lane and zone tables, remote-area and additional-service surcharge overlays, and effective-dated fuel assumptions | Confirm ratecard effective date, WHS configuration, and which surcharge categories are actually avoidable |

The application distinguishes between:

- A model parameter used for estimation.
- A carrier invoice observation used for validation.
- A business rule that has been confirmed and approved for operational change.

## 5. Core Measures

### Invoice Coverage

Invoice coverage is the proportion of the selected Shippit consignments that can be matched to an invoice record. A missing invoice is not the same as zero variance.

### Observed Variance

Observed variance is:

`actual invoiced cost - modelled cost`

It is calculated only where both actual and modelled cost are present. Modelled cost and actual invoiced cost remain separate measures in the persisted history.

### Dim/Weight Variance

Dim/weight variance identifies the signed observed gap associated with consignments where carrier-invoiced physical measures differ materially from Shippit physical measures. It is directional evidence for triage and can exceed 100% of the total gap when another driver offsets it.

### Residual Model and Rate Variance

Residual variance is the observed gap for the comparable-basis cohort where Shippit physical measures are not materially different from invoice physical measures. This is the cleaner cohort for judging estimator, rate, fuel, and surcharge performance.

### Air and Packaging Diagnostics

The air ratio is based on the difference between packed SKU cube and the selected parcel cube. Depending on data availability, the selected parcel cube can be the invoiced actual cube, Shippit cube, or estimated parcel cube.

The diagnostic also compares dead weight with packed SKU weight. The difference is a directional signal for packaging, dunnage, parcelisation, or other added weight.

The current air-saving signal estimates the potential value of reducing volume-led chargeable weight toward a cohort norm. It is not a committed saving and requires operational validation of the packaging change.

## 6. Carrier-Choice Opportunities

The carrier-choice opportunity view identifies consignments where a valid alternative carrier/service is cheaper using the same Shippit physical basis. It reports:

- Current carrier and next-best carrier.
- AP pricing zone where applicable.
- Eligible and opportunity consignment counts.
- Percentage of eligible consignments presenting an opportunity.
- Current average cost per consignment.
- Next-best average cost per consignment.
- Realisable saving under the current rules.
- Conditional saving where a carrier rule or limit would need to change.

Two categories are shown:

1. **Realisable today:** the next-best carrier passes the currently modelled article, dead-weight, volumetric-weight, and dimension limits.
2. **Conditional if rule changed:** the alternative is cheaper but is currently excluded by a modelled carrier constraint or rule.

The result is a prioritisation signal, not a recommendation to change carrier allocation automatically. Before action, confirm:

- WHS parcel-count and chargeable-weight logic at each CFC.
- How carrier ratecards and carrier-choice rules are configured in WHS.
- Whether Shippit's chargeable weight reflects the same basis.
- The actual carrier limits and surcharge consequences.

The cost opportunity is most reliable when the parcel count and chargeable weight are physically credible. A poor parcelizer can make a carrier appear ineligible or can overstate a saving.

## 7. Refresh and Data Lifecycle

### Daily Operational Refresh

The recommended daily process:

1. Reruns Stage 1-3 over the current 14-day overlap.
2. Updates the live rolling outputs.
3. Refreshes the small accounting-week to Finance-period map from the Snowflake date dimension.
4. Incrementally upserts shipment facts and modelled cost into the durable `live` coverage store and refreshes its weekly summary.
5. Writes refresh status and a log.

The Dashboard refresh control reloads files already written by the pipeline. It does not run Snowflake extraction or Stage 1-3 by itself.

### Carrier Invoice Refresh

Invoice sources are carrier-specific:

- Australia Post is ingested from Snowflake.
- Couriers Please, GoPeople, and Allied are ingested from their configured Excel input folders.

The Invoice Reconciliation workspace can check for new invoice data and run a selected carrier refresh. The selected refresh ingests the carrier data, builds the carrier reconciliation against compact Stage 3 facts exported from durable shipment history, and updates the affected invoice coverage in place. It does not rerun Stage 1-3 across the historical period.

`All carrier history`, Finance reporting, and weekly trends read the same durable coverage store. A successful carrier invoice refresh updates that store and its weekly summary, so newly matched invoice coverage is available after the application reloads its cached data. Finance period numbers come from saved coverage or the refreshed Snowflake date-dimension map, so older weeks can be grouped without rebuilding Stage 1-3 history.

### Consignment Explorer

The Explorer searches the durable store for one consignment and keeps its saved model estimate as the baseline. When requested, it runs Stage 1-3 for that consignment and its CFC using the saved despatch date, reads the detailed results into the current application session, and removes the temporary query files when the request completes. It does not load or retain full historical parcel and product detail in the app.

The fresh result shows the recalculated estimate beside the saved estimate. Matched invoice records continue to come from the carrier reconciliation output, which remains separate from the Snowflake refresh.

The history stores the original estimator output and later invoice evidence side by side. Invoice actuals must not overwrite the original model estimate.

## 8. Data Lineage

| Data area | Primary source | Use in the product |
|---|---|---|
| Shippit orders | `KSFPA.FREIGHT.SHIPPIT_ORDERS` | Order, carrier/service, parcel, weight, cube, destination, and invoice identifiers |
| Fulfillment detail | `KSFPA.OMFP.ORD_FULFILLMENT_DETAIL` | Preferred article, keycode, quantity, shipment, and package-detail allocation |
| Packed fallback | `KSFDA.CATCH_CFC.PACKED` | Fallback parcel and item grouping when fulfillment detail is incomplete |
| CFC SKU attributes | `KSFDA.CATCH_CFC.SKU_ATTRIBUTE` | Preferred CFC dimensions and weight for Stage 2 |
| PLM dimensions | `KSFPA.MERCHANDISE_PVT.KEYCODE_CARTON_DSS_DIMENSIONS_VW` | Fallback and comparison physical evidence |
| Carrier invoices | AP Snowflake source and carrier Excel feeds | Invoice actuals, charge lines, matching, surcharge, and coverage validation |
| Ratecards and zones | Versioned files in `data/reference/` | Carrier pricing, zones, limits, fuel, and surcharge assumptions |

## 9. Known Limitations and Controls

- WHS carrier allocation is upstream of this product. The application can identify possible gaps but does not change WHS rules.
- CFC dimensions may represent the latest cubiscan/WMS value retained by WHS and may differ between the two CFC systems.
- Shippit dimensions and weights come from the Catch-to-Shippit integration and may not represent the final packed article configuration.
- An invoice may be article-level, parcel-level, or consignment-level depending on the carrier. Aggregation must therefore be carrier-specific.
- The estimator may use proxy box-suite data until a validated CFC box suite is available.
- A valid ratecard lookup is required for a cost estimate. Missing rate matches are shown explicitly rather than treated as zero cost.
- Invoice coverage and invoice verification are separate from estimator completeness.
- Candidate savings require physical, commercial, operational, and service-level validation before being treated as realisable.
- Historical comparisons are only meaningful when the selected weeks, invoice population, model version, and ratecard assumptions are understood.

## 10. Recommended Operating Process

1. Review the Dashboard for current volume, cost, carrier mix, and top opportunity signals.
2. Check Model Quality for missing data, unusual parcelisation, CFC/PLM differences, and air-ratio outliers.
3. Review Carrier-Choice Opportunities, starting with the highest-value realisable candidates.
4. Use Consignment Explorer to test the evidence for representative consignments.
5. Check Invoice Reconciliation as carrier invoices become available.
6. Separate dim/weight issues from residual estimator or rate issues.
7. Create an owned opportunity record only after the evidence and proposed action are clear.
8. Validate the operational change against later invoice actuals and update the opportunity status.

## 11. Success Measures

The product should be assessed by whether it improves the quality and speed of decisions, not only by the number of dashboard features. Suggested measures are:

- Higher invoice coverage and faster invoice validation.
- Fewer missing-cost Stage 3 records.
- Better parcel-count and chargeable-weight agreement with eventual invoices.
- Reduced unexplained residual variance.
- A validated reduction in avoidable surcharge leakage.
- A validated reduction in transport cost from carrier-choice changes.
- Reduced air and packaging cost where operational changes are implemented.
- Clear ownership and closure of high-value opportunity records.

## 12. Change Governance

Changes to carrier rules, ratecards, fuel assumptions, parcelisation logic, or source priority should be:

1. Documented with the reason and effective-date assumption.
2. Tested against a known consignment sample.
3. Compared with invoice evidence where available.
4. Rerun through the affected Stage 1-3 process.
5. Reflected in the active assumptions and versioned reference files.
6. Communicated to users where the interpretation of a metric changes.

The opportunity register and invoice reconciliation history should retain enough context to explain which model and ratecard assumptions produced each result.
