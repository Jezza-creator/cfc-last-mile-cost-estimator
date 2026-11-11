# Stage 3 Cost Estimator Logic Map

This document is the working end-to-end logic map for the Moorebank CFC last-mile cost estimator.
It describes the intended processing sequence, the rules that drive the estimate, and the transparency
signals we want to preserve for review and refinement.

This is a working starting point.

- The purpose is to make the logic transparent enough for Ops to review and improve.
- The current rules are intentionally practical and explainable, but they are not the final word.
- If the outputs show persistent gaps, the expectation is that the business will help refine the logic and the inputs.
- Fallback means the model used the next-best explainable rule because the cleanest signal was missing or ambiguous.

## Purpose

Estimate order-level last-mile cost from Shippit and CFC data by:

1. Using the actual Shippit chargeable weight for single-parcel orders.
2. Parcelising multi-parcel orders into plausible parcels before costing.
3. Adding a box-suite proxy where overboxing and carton air matter.
4. Producing transparent adjustment reasons so we can see where the estimate changed.
5. Highlighting PLM vs CFC dimensional and weight variances that may warrant Ops review.

The estimator is meant to be reviewed, challenged, and improved in partnership with Ops and Transport teams.

## Inputs

### Primary order source

- `KSFPA.FREIGHT.SHIPPIT_ORDERS`
- Provides:
  - `ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER`
  - carrier and service
  - destination data
  - parcel count
  - dead weight
  - volumetric weight
  - chargeable weight

### CFC parcelisation source

- `KSFDA.CATCH_CFC.PACKED`
- Used to infer how items were packed into parcels.
- Supports:
  - pack timestamp clustering
  - parcel-level item grouping
  - manual vs automated flow inference

### Product dimension sources

#### Preferred CFC source

- `KSFDA.CATCH_CFC.SKU_ATTRIBUTE`
- Use first where available because it reflects CFC cubiscan and WMS processing.

#### Backup PLM source

- `KSFPA.MERCHANDISE_PVT.KEYCODE_CARTON_DSS_DIMENSIONS_VW`
- Use when CFC attributes are missing or need validation.

### Box suite source

- `data/inputs/box_suite/proxy_box_suite_non_cfc.csv`
- Placeholder for the future CFC-specific suite:
  - `data/inputs/box_suite/cfc_box_suite.csv`

The box suite is used as a proxy for carton overboxing and air ratio so volumetric weight can be represented
even where the packed contents alone would understate parcel cube.

### Rate and zone sources

- Shippit rate tables and zone mappings used by the stage 3 cost estimator.
- These remain carrier-service specific and feed the final cost lookup.

### Allied pricing sources

- `Allied ratecard - Online Rate Schedule 26ECOMRXC5VF-KMA (Proposed)26.xlsx`
- `Allied Additional Services VFF`
- `Allied Remote Area Surcharge`
- `Allied - ZoneGuideCondensed.pdf`

These together define the Allied pricing hierarchy:

1. base lane price
2. chargeable weight basis
3. remote area surcharge
4. special services surcharges such as overweight, oversized, manual measurement and pickup handling
5. reverse logistics uplift

The estimator now exposes these Allied charge elements separately in the stage 3 output so Ops can see
whether a cost is being driven by the lane, the remote matrix, or a surcharge overlay.

## Core Rules

### 1. Single parcel orders

If `ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER` has one parcel:

- use the Shippit chargeable weight as the basis for cost estimation
- do not force parcelization logic
- still preserve parcel-level transparency in the output

### 1b. Couriers Please billing exception

Couriers Please is treated as an invoice-level billing carrier in the estimator:

- the billing weight is the order chargeable weight
- the billing weight is rounded up to the next whole kilogram
- the drilldown shows that rounded invoice weight explicitly
- a `10 parcel` review cap flag is surfaced when the order exceeds the carrier review limit

### 2. Multi-parcel orders

If an order has more than one parcel:

- apply parcelization logic first
- infer which items likely belong in which parcel
- calculate parcel-level cube and weight
- estimate parcel chargeable weight as the greater of:
  - dead weight
  - volumetric weight
- sum parcel costs to order level

### 3. Weight and cube basis

Parcel cube should be derived from the parcel dimensions:

- `shortest`
- `median`
- `longest`

This replaces a more technical H/W/L label in reporting and makes it easier to discuss the physical shape of the parcel.

Volumetric weight is calculated from parcel cube using the working divisor:

- `volumetric weight = cube / divisor`

### 4. Parcelisation signals

Parcelisation should use the following signals in priority order:

1. Actual CFC pack sequence and timestamps
2. Likely pick path:
   - Automated / AMR
   - Manual / PFR
3. Item-level dimensions and weights
4. Box suite matching
5. PLM fallback dimensions and weights

### 5. Adjustment handling

The estimator must record when it changes an order’s physical profile.

Examples:

- item cube summed smaller than expected carton cube
- overboxing adds cube and therefore volumetric weight
- summed product dims larger than the Shippit parcel expectation
- summed product dims smaller than the Shippit parcel expectation

Every adjustment should have a visible reason code so we can trace what happened.

## Parcelisation Logic

### A. Packing path inference

Use pack timestamps to cluster items into likely parcels.

The intent is to:

- separate manual PFR style packs from automated packs where needed
- keep together items that were likely packed together
- avoid forcing the same keycode into the same parcel when the operational pattern suggests otherwise

### B. SKU-level physical selection

When dimensions and weights are needed:

1. Use CFC SKU attributes first
2. Fall back to PLM catalogue values if CFC data is missing
3. Cross-check the result against the inferred parcel weight and cube

### C. Box suite overlay

When the sum of content dimensions under-represents the parcel footprint:

- use a likely box-suite match
- add carton air / overbox allowance
- let the box suite drive the parcel’s volumetric impact where appropriate

This is especially important when the contents are light but the carton is large enough to trigger volumetric pricing.

## Cost Estimation Flow

1. Read Shippit order metadata.
2. Determine whether the order is single parcel or multi parcel.
3. If single parcel:
   - cost from Shippit chargeable weight basis
4. If multi parcel:
   - parcelise the order
   - estimate each parcel’s chargeable weight
   - sum parcel costs to order cost
5. Apply carrier-ratecard lookup.
6. Compare the actual carrier cost against the cheapest valid alternative.
7. Record missing-rate reasons when no cost can be assigned.

## Transparency Outputs

The estimator should always expose:

- actual parcel count
- estimated parcel count
- parcel-level units
- parcel-level weight
- parcel-level cube
- parcel-level chargeable weight
- adjustment or fallback rule name
- cost missing reason
- cheapest alternative carrier/service
- estimated saving

## Variance and Validation Outputs

The validation output should show:

- CFC vs PLM dimension deltas
- CFC vs PLM weight deltas
- gross cube variance percentage
- volumetric weight variance percentage
- variance bucket
- carrier importance where volumetric weight changes the outcome
- major group / department / RBU rollups for Ops review

How to interpret the main gap fields:

- `avg_weight_gap_kg` compares the modelled parcel weight with the shipped parcel weight
- `avg_chargeable_gap_kg` compares the modelled chargeable weight with the shipped chargeable weight
- a small gap usually means the parcel split is close enough for pricing review
- a large positive or negative gap means the parcelizer is probably over- or under-allocating weight and cube
- when the gap is large, review the parcelizer output first before drawing conclusions about carrier pricing

How to use the two validation tables together:

- start with parcelizer validation to check whether the split looks operationally sensible
- then use carrier validation to see whether the correct carrier and service would likely win on cost
- if the parcelizer split is odd, the carrier comparison may be misleading because the cost basis is already distorted

Recommended variance buckets:

- `0-9%`
- `10-24%`
- `25-49%`
- `50%+`

## Ops Review Priorities

Flag the items most likely to affect shipping cost:

- large gross cube variance
- large volumetric variance
- missing PLM dimensions
- missing PLM weight
- category mismatch between CFC and PLM
- carriers where volumetric weighting is a frequent driver

## Future CFC Box Suite Upload Process

The proxy suite is temporary.

When the CFC suite is ready:

1. Export the CFC box suite to CSV.
2. Save it as `data/inputs/box_suite/cfc_box_suite.csv`.
3. Keep the same schema as the proxy suite.
4. Re-run the stage 3 estimator so it uses the new suite automatically.
5. Validate the change against a known order sample before relying on it.

## Working Assumptions

- Shippit is the authoritative source for actual parcel count and chargeable weight.
- CFC data is the preferred source for parcelisation support.
- PLM is the fallback source when CFC values are missing or need comparison.
- Box suite data is a proxy until the real CFC suite is available.
- All rule-based adjustments must be explainable in the output.
