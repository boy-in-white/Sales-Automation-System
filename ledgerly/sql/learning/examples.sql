-- 1. All completed sales for one month.
SELECT
    sold_at,
    product_name,
    quantity,
    sale_price_cents / 100.0 AS revenue_uah,
    profit_cents / 100.0 AS profit_uah
FROM analytics.v_order_financials
WHERE sold_at >= TIMESTAMPTZ '2026-11-01 00:00:00+00'
  AND sold_at <  TIMESTAMPTZ '2026-12-01 00:00:00+00'
  AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')
ORDER BY sold_at;

-- 2. Monthly revenue, expenses and calculated profit.
SELECT
    month,
    sales_count,
    revenue_cents / 100.0 AS revenue_uah,
    expenses_cents / 100.0 AS expenses_uah,
    profit_cents / 100.0 AS profit_uah,
    cost_pending_count
FROM analytics.v_monthly_sales
ORDER BY month DESC;

-- 3. Best-selling products.
SELECT
    product_name,
    SUM(quantity) AS items_sold,
    SUM(sale_price_cents) / 100.0 AS revenue_uah,
    SUM(profit_cents) / 100.0 AS profit_uah
FROM analytics.v_order_financials
WHERE sold_at IS NOT NULL
  AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')
GROUP BY product_name
ORDER BY items_sold DESC, revenue_uah DESC;

-- 4. Refusals grouped by product.
SELECT
    product_name,
    COUNT(*) AS refusals
FROM analytics.v_refusals
GROUP BY product_name
ORDER BY refusals DESC, product_name;

-- 5. Orders delivered but not yet confirmed in 1C.
SELECT
    ttn,
    product_name,
    sold_at,
    sale_price_cents / 100.0 AS revenue_uah
FROM analytics.v_order_financials
WHERE sold_at IS NOT NULL
  AND onec_recorded_at IS NULL
ORDER BY sold_at;

-- 6. Product aliases: both word orders point to one product.
SELECT
    p.canonical_name,
    a.raw_alias,
    a.alias_key
FROM business.products p
JOIN business.product_aliases a ON a.product_id = p.id
ORDER BY p.canonical_name, a.raw_alias;

-- 7. Compare OLX, cash-on-delivery, FOP and offline sales.
SELECT
    sale_type,
    COUNT(*) AS sales,
    SUM(sale_price_cents) / 100.0 AS revenue_uah,
    SUM(profit_cents) / 100.0 AS profit_uah
FROM analytics.v_order_financials
WHERE sold_at IS NOT NULL
  AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')
GROUP BY sale_type
ORDER BY revenue_uah DESC;

-- 8. Current inventory by product, color and size.
SELECT
    product_name,
    color_name,
    size_code,
    on_hand,
    in_transit,
    available
FROM analytics.v_inventory_stock
ORDER BY product_name, color_code, size_code;

-- 9. Orders whose items are currently unavailable because they are in transit.
SELECT
    ttn,
    product_name,
    color_name,
    size_code,
    quantity,
    order_status,
    reserved_at
FROM analytics.v_inventory_in_transit
ORDER BY reserved_at DESC;

-- 10. Latest stock movements. order_id is filled for automatic 1C write-offs.
SELECT
    created_at,
    product_name,
    color_name,
    size_code,
    quantity_delta,
    balance_after,
    movement_type,
    order_id
FROM analytics.v_inventory_movements
ORDER BY created_at DESC
LIMIT 100;

-- 11. Remaining stock by priced receipt batch. Sea and air are still one product.
SELECT
    received_at,
    product_name,
    color_name,
    size_code,
    transport_name,
    unit_cost_cents / 100.0 AS unit_cost_uah,
    received_quantity,
    remaining_quantity
FROM analytics.v_inventory_batches
WHERE remaining_quantity > 0
ORDER BY received_at, product_name;

