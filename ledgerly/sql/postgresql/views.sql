-- Financial result for every order. Monetary values are integer kopiykas.
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE OR REPLACE VIEW analytics.v_order_financials AS
SELECT
    o.id,
    o.ttn,
    o.olx_order_id,
    o.sale_type,
    o.carrier,
    COALESCE(p.canonical_name, o.raw_product_name) AS product_name,
    o.quantity,
    o.sale_price_cents,
    o.purchase_cost_cents
        + o.delivery_cost_cents
        + o.platform_fee_cents
        + o.other_cost_cents AS total_cost_cents,
    CASE
        WHEN o.cost_source IN ('FIFO_BATCH', 'MANUAL')
        THEN o.sale_price_cents
            - o.purchase_cost_cents
            - o.delivery_cost_cents
            - o.platform_fee_cents
            - o.other_cost_cents
        ELSE NULL
    END AS profit_cents,
    o.status,
    o.sold_at,
    o.refused_at,
    o.onec_recorded_at,
    o.purchase_cost_cents,
    o.delivery_cost_cents,
    o.platform_fee_cents,
    o.other_cost_cents,
    o.cost_source
FROM business.orders o
LEFT JOIN business.products p ON p.id = o.product_id;

CREATE OR REPLACE VIEW analytics.v_monthly_sales AS
SELECT
    TO_CHAR(sold_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM') AS month,
    COUNT(*) AS sales_count,
    SUM(quantity) AS items_count,
    SUM(sale_price_cents) AS revenue_cents,
    SUM(CASE
        WHEN cost_source IN ('FIFO_BATCH', 'MANUAL') AND sale_price_cents > 0
        THEN purchase_cost_cents + delivery_cost_cents
            + platform_fee_cents + other_cost_cents
        ELSE 0
    END) AS expenses_cents,
    SUM(CASE
        WHEN cost_source IN ('FIFO_BATCH', 'MANUAL') AND sale_price_cents > 0
        THEN sale_price_cents - purchase_cost_cents
            - delivery_cost_cents - platform_fee_cents - other_cost_cents
        ELSE 0
    END) AS profit_cents,
    SUM(CASE
        WHEN cost_source NOT IN ('FIFO_BATCH', 'MANUAL') OR sale_price_cents <= 0
        THEN 1 ELSE 0
    END) AS cost_pending_count
FROM business.orders
WHERE sold_at IS NOT NULL
  AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')
  AND NOT EXISTS (
      SELECT 1
      FROM analytics.monthly_analytics_resets r
      WHERE r.month = TO_CHAR(sold_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM')
        AND sold_at < r.cleared_at
  )
GROUP BY TO_CHAR(sold_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM');

CREATE OR REPLACE VIEW analytics.v_refusals AS
SELECT
    o.id,
    o.ttn,
    COALESCE(p.canonical_name, o.raw_product_name) AS product_name,
    o.sale_type,
    o.refused_at,
    o.returned_at,
    o.return_reason
FROM business.orders o
LEFT JOIN business.products p ON p.id = o.product_id
WHERE o.refused_at IS NOT NULL;

CREATE OR REPLACE VIEW analytics.v_inventory_stock AS
SELECT
    p.id AS product_id,
    p.canonical_name AS product_name,
    v.id AS variant_id,
    v.color_code,
    CASE v.color_code
        WHEN 'BLACK' THEN 'Чорний'
        WHEN 'WHITE' THEN 'Білий'
        WHEN 'GRAY' THEN 'Сірий'
    END AS color_name,
    v.size_code,
    COALESCE(b.on_hand, 0) AS on_hand,
    COALESCE(b.reserved, 0) AS reserved,
    COALESCE(b.on_hand, 0) - COALESCE(b.reserved, 0) AS available,
    b.updated_at,
    COALESCE(b.reserved, 0) AS in_transit
FROM business.products p
JOIN business.product_variants v ON v.product_id = p.id AND v.active = 1
LEFT JOIN business.inventory_balances b ON b.variant_id = v.id
WHERE p.active = 1;

CREATE OR REPLACE VIEW analytics.v_inventory_in_transit AS
SELECT
    r.order_id,
    o.ttn,
    p.id AS product_id,
    p.canonical_name AS product_name,
    v.id AS variant_id,
    v.color_code,
    CASE v.color_code
        WHEN 'BLACK' THEN 'Чорний'
        WHEN 'WHITE' THEN 'Білий'
        WHEN 'GRAY' THEN 'Сірий'
    END AS color_name,
    v.size_code,
    r.quantity,
    o.status AS order_status,
    r.reserved_at,
    r.updated_at
FROM business.inventory_reservations r
JOIN business.orders o ON o.id = r.order_id
JOIN business.product_variants v ON v.id = r.variant_id
JOIN business.products p ON p.id = v.product_id
WHERE r.status = 'ACTIVE';

CREATE OR REPLACE VIEW analytics.v_inventory_movements AS
SELECT
    m.id,
    m.order_id,
    p.id AS product_id,
    p.canonical_name AS product_name,
    v.id AS variant_id,
    v.color_code,
    CASE v.color_code
        WHEN 'BLACK' THEN 'Чорний'
        WHEN 'WHITE' THEN 'Білий'
        WHEN 'GRAY' THEN 'Сірий'
    END AS color_name,
    v.size_code,
    m.quantity_delta,
    m.balance_after,
    m.movement_type,
    m.note,
    m.actor_user_id,
    m.actor_name,
    m.created_at,
    m.batch_id
FROM business.inventory_movements m
JOIN business.product_variants v ON v.id = m.variant_id
JOIN business.products p ON p.id = v.product_id;

CREATE OR REPLACE VIEW analytics.v_inventory_batches AS
SELECT
    b.id AS batch_id,
    p.id AS product_id,
    p.canonical_name AS product_name,
    v.id AS variant_id,
    v.color_code,
    CASE v.color_code
        WHEN 'BLACK' THEN 'Чорний'
        WHEN 'WHITE' THEN 'Білий'
        WHEN 'GRAY' THEN 'Сірий'
    END AS color_name,
    v.size_code,
    b.transport_mode,
    CASE b.transport_mode WHEN 'SEA' THEN 'Море' WHEN 'AIR' THEN 'Літак' END
        AS transport_name,
    b.unit_cost_cents,
    b.received_quantity,
    b.remaining_quantity,
    b.received_at
FROM business.inventory_batches b
JOIN business.product_variants v ON v.id = b.variant_id
JOIN business.products p ON p.id = v.product_id;

