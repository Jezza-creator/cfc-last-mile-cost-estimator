-- Reporting weight band definition for Moorebank stage 3 outputs.
-- Use this against order-level chargeable weight unless you explicitly need parcel-level bands.

CASE
    WHEN weight_kg IS NULL THEN NULL
    WHEN weight_kg < 1 THEN '0-1 kg'
    WHEN weight_kg < 2 THEN '1-2 kg'
    WHEN weight_kg < 5 THEN '2-5 kg'
    WHEN weight_kg < 10 THEN '5-10 kg'
    WHEN weight_kg < 15 THEN '10-15 kg'
    WHEN weight_kg < 22 THEN '15-22 kg'
    WHEN weight_kg < 32 THEN '22-32 kg'
    WHEN weight_kg < 45 THEN '32-45 kg'
    ELSE '45+ kg'
END AS reporting_weight_band
