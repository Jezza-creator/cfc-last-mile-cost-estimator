from __future__ import annotations

import math

from src.db import get_session


STORE_NAME = "1194 Moorebank CFC NSW AU"
DAYS_BACK = 6
BUCKET_SECONDS = 120

# These are deliberately broad starting points. We will use the results to
# decide which parcel-capacity assumptions deserve to become the next heuristic.
WEIGHT_LIMITS_KG = [5, 7.5, 10, 12.5, 15, 17.5, 20, 22, 25]
CUBE_LIMITS = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]


def main() -> None:
    session = get_session()
    try:
        rows = session.sql(
            f"""
            WITH shippit AS (
                SELECT
                    ORDER_ATTRIBUTE__ORDER_INVOICE_NUMBER AS invoice_number,
                    ORDER_PARCEL__PARCEL_COUNT AS actual_parcel_count
                FROM KSFPA.FREIGHT.SHIPPIT_ORDERS
                WHERE STORE_ATTRIBUTE__STORE_NAME = '{STORE_NAME}'
                  AND ORDER_TRACKING_EVENT_2_DESPATCH_IN_PROGRESS__TIMESTAMP::DATE
                        BETWEEN DATEADD(day, -{DAYS_BACK}, CURRENT_DATE()) AND CURRENT_DATE()
                  AND ORDER_PARCEL__PARCEL_COUNT IS NOT NULL
            ),
            packed AS (
                SELECT
                    REGEXP_REPLACE(p.ORDER_ID, '^KMT', '') AS invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP
                FROM KSFDA.CATCH_CFC.PACKED p
                JOIN shippit s
                  ON s.invoice_number = REGEXP_REPLACE(p.ORDER_ID, '^KMT', '')
            ),
            sku_latest AS (
                SELECT *
                FROM (
                    SELECT
                        sa.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY sa.SKU_ID
                            ORDER BY sa.EXTRACT_TIMESTAMP DESC
                        ) AS rn
                    FROM KSFDA.CATCH_CFC.SKU_ATTRIBUTE sa
                )
                WHERE rn = 1
            ),
            packed_physical AS (
                SELECT
                    p.invoice_number,
                    p.SKU_ID,
                    p.QUANTITY_PACKED,
                    p.PACK_TIMESTAMP,
                    DATEADD(
                        second,
                        FLOOR(DATEDIFF(second, '1970-01-01'::timestamp_ntz, p.PACK_TIMESTAMP) / {BUCKET_SECONDS}) * {BUCKET_SECONDS},
                        '1970-01-01'::timestamp_ntz
                    ) AS pack_bucket_ts,
                    s.WEIGHT AS sku_weight_kg,
                    s.CUBAGE AS sku_cubage,
                    s.IS_OVERSIZE_SYDM01
                FROM packed p
                LEFT JOIN sku_latest s
                  ON s.SKU_ID = p.SKU_ID
            ),
            bucketed AS (
                SELECT
                    invoice_number,
                    pack_bucket_ts,
                    COUNT(*) AS packed_rows,
                    SUM(QUANTITY_PACKED) AS qty_packed,
                    SUM(COALESCE(sku_weight_kg, 0) * QUANTITY_PACKED) AS bucket_weight_kg,
                    SUM(COALESCE(sku_cubage, 0) * QUANTITY_PACKED) AS bucket_cubage,
                    COUNT_IF(IS_OVERSIZE_SYDM01 = TRUE) AS oversize_rows
                FROM packed_physical
                GROUP BY 1, 2
            ),
            order_features AS (
                SELECT
                    invoice_number,
                    COUNT(*) AS time_bucket_count,
                    SUM(bucket_weight_kg) AS total_weight_kg,
                    SUM(bucket_cubage) AS total_cubage,
                    SUM(oversize_rows) AS oversize_rows
                FROM bucketed
                GROUP BY 1
            )
            SELECT
                s.invoice_number,
                s.actual_parcel_count,
                f.time_bucket_count,
                f.total_weight_kg,
                f.total_cubage,
                f.oversize_rows
            FROM shippit s
            LEFT JOIN order_features f
              ON f.invoice_number = s.invoice_number
            """
        ).collect()

        orders = []
        for row in rows:
            orders.append(
                {
                    "invoice_number": row["INVOICE_NUMBER"],
                    "actual_parcel_count": int(row["ACTUAL_PARCEL_COUNT"]),
                    "time_bucket_count": int(row["TIME_BUCKET_COUNT"] or 0),
                    "total_weight_kg": float(row["TOTAL_WEIGHT_KG"] or 0),
                    "total_cubage": float(row["TOTAL_CUBAGE"] or 0),
                    "oversize_rows": int(row["OVERSIZE_ROWS"] or 0),
                }
            )

        print("ORDER_COUNT")
        print(len(orders))

        print("\nBASELINE_TIME_BUCKET_ONLY")
        _print_metrics(
            orders,
            lambda order: order["time_bucket_count"],
        )

        print("\nWEIGHT_ONLY_GRID")
        for weight_limit in WEIGHT_LIMITS_KG:
            print(f"\nWEIGHT_LIMIT_KG={weight_limit}")
            _print_metrics(
                orders,
                lambda order, limit=weight_limit: max(
                    order["time_bucket_count"],
                    _ceil_div(order["total_weight_kg"], limit),
                    order["oversize_rows"],
                ),
            )

        print("\nHYBRID_WEIGHT_PLUS_CUBE_GRID")
        hybrid_results = []
        for weight_limit in WEIGHT_LIMITS_KG:
            for cube_limit in CUBE_LIMITS:
                estimate_label = f"WEIGHT_LIMIT_KG={weight_limit}, CUBE_LIMIT={cube_limit}"
                metrics = _score_orders(
                    orders,
                    lambda order, w_limit=weight_limit, c_limit=cube_limit: max(
                        order["time_bucket_count"],
                        _ceil_div(order["total_weight_kg"], w_limit),
                        _ceil_div(order["total_cubage"], c_limit),
                        order["oversize_rows"],
                    ),
                )
                hybrid_results.append(
                    {
                        "label": estimate_label,
                        **metrics,
                    }
                )

        for result in sorted(
            hybrid_results,
            key=lambda item: (item["mean_abs_error"], -item["exact_match_pct"], item["undercount_pct"]),
        )[:10]:
            print(
                f"{result['label']} -> "
                f"MAE={result['mean_abs_error']:.2f}, "
                f"EXACT={result['exact_match_pct']:.2f}, "
                f"UNDER={result['undercount_pct']:.2f}, "
                f"OVER={result['overcount_pct']:.2f}, "
                f"AVG={result['avg_inferred_parcel_count']:.2f}"
            )
    finally:
        session.close()


def _ceil_div(value: float, limit: float) -> int:
    if limit <= 0:
        return 0
    if value <= 0:
        return 0
    return int(math.ceil(value / limit))


def _score_orders(orders: list[dict[str, object]], estimate_fn) -> dict[str, float]:
    actual_counts = [int(order["actual_parcel_count"]) for order in orders]
    inferred_counts = [int(estimate_fn(order)) for order in orders]

    order_count = len(orders)
    mean_abs_error = sum(abs(i - a) for i, a in zip(inferred_counts, actual_counts)) / order_count
    exact_match_pct = sum(1 for i, a in zip(inferred_counts, actual_counts) if i == a) * 100 / order_count
    undercount_pct = sum(1 for i, a in zip(inferred_counts, actual_counts) if i < a) * 100 / order_count
    overcount_pct = sum(1 for i, a in zip(inferred_counts, actual_counts) if i > a) * 100 / order_count
    avg_inferred = sum(inferred_counts) / order_count

    return {
        "mean_abs_error": mean_abs_error,
        "exact_match_pct": exact_match_pct,
        "undercount_pct": undercount_pct,
        "overcount_pct": overcount_pct,
        "avg_inferred_parcel_count": avg_inferred,
    }


def _print_metrics(orders: list[dict[str, object]], estimate_fn) -> None:
    metrics = _score_orders(orders, estimate_fn)
    avg_actual = sum(int(order["actual_parcel_count"]) for order in orders) / len(orders)
    print(
        "Row("
        f"ORDER_COUNT={len(orders)}, "
        f"AVG_ACTUAL_PARCEL_COUNT={avg_actual:.2f}, "
        f"AVG_INFERRED_PARCEL_COUNT={metrics['avg_inferred_parcel_count']:.2f}, "
        f"MEAN_ABS_ERROR={metrics['mean_abs_error']:.2f}, "
        f"EXACT_MATCH_PCT={metrics['exact_match_pct']:.2f}, "
        f"UNDERCOUNT_PCT={metrics['undercount_pct']:.2f}, "
        f"OVERCOUNT_PCT={metrics['overcount_pct']:.2f}"
        ")"
    )


if __name__ == "__main__":
    main()
