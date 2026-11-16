# SQL learning path

Start with `examples.sql` in this order:

1. `SELECT`, `WHERE`, `ORDER BY`;
2. `COUNT`, `SUM`, and `GROUP BY`;
3. `JOIN` between `business.orders`, `business.products`, and aliases;
4. views such as `analytics.v_order_financials`;
5. indexes and `EXPLAIN ANALYZE` after the database grows.

Money is stored in **kopiykas** as integers. Divide by `100.0` only when showing
the result. This avoids rounding errors from floating-point numbers.

The important tables are:

- `business.products` — one canonical product name;
- `business.product_aliases` — alternative word order and spellings;
- `business.product_variants` — allowed color and size combinations;
- `business.inventory_balances` — current stock;
- `business.inventory_movements` — receipts and write-offs with a full history;
- `business.product_cost_profiles` — the current sea and air unit cost per product;
- `business.inventory_batches` — received quantities with the cost fixed at receipt time;
- `business.inventory_batch_allocations` — which batches formed each sale cost;
- `business.inventory_reservations` — stock currently sent to customers;
- `business.orders` — price, costs, status, shipment/sale/refusal times, 1C state;
- `business.order_events` — immutable history of important changes;
- `business.order_submissions` — cards waiting for the shipment button;
- `analytics.monthly_turnover` — result written by the separate analytics program and
  displayed by `/turnover` in the bot.
- `analytics.v_inventory_stock` — simple read-only stock for another project.
- `analytics.v_inventory_in_transit` — active shipments by product, color and size.
- `analytics.v_inventory_movements` — stock history, including 1C order write-offs.
- `analytics.v_inventory_batches` — remaining priced batches for FIFO profit calculation.

For a second project, create a PostgreSQL read-only user and query the views.
Do not give the second project the bot's write password.

An OLX availability check must use `available`, not `on_hand`:

```sql
SELECT available
FROM analytics.v_inventory_stock
WHERE product_name = 'Штани Suvene'
  AND color_code = 'BLACK'
  AND size_code = 'M';
```

Use `../postgresql/write_turnover.sql` as the write contract between the
analytics program and the Telegram bot. `sqlite_write_turnover.sql` is retained
only for inspecting a legacy SQLite copy.

