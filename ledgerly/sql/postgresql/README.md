# PostgreSQL layout

The bot uses one database, `olx_business`, with three schemas:

- `business`: products, variants, stock balances, stock movements, orders and events;
- `bot`: shipments, processed Telegram messages and Ukrposhta polling state;
- `analytics`: financial and inventory views, `monthly_turnover`, monthly resets.

The application creates and upgrades its own tables at startup. The SQL files
in this directory document the reporting contract and can be used for learning.

## Roles

`olx_bot` owns and writes application data. `olx_reader` can read only the
`business` and `analytics` schemas. It deliberately cannot read the technical
`bot` schema or modify sales.

The Mac setup script creates both roles and these default privileges:

```sql
GRANT USAGE ON SCHEMA business, analytics TO olx_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA business, analytics TO olx_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE olx_bot IN SCHEMA business
GRANT SELECT ON TABLES TO olx_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE olx_bot IN SCHEMA analytics
GRANT SELECT ON TABLES TO olx_reader;
```

Use `olx_reader` for the future analytics project. Never copy the bot's write
password into a read-only project.

## Oracle Cloud

PostgreSQL and the bot can initially run on the same Oracle Cloud VM. Bind
PostgreSQL to localhost/private networking and do not create a public ingress
rule for port `5432`.

Use daily custom-format `pg_dump` backups and copy them outside the VM. The
project's `scripts/backup_once.py` creates the same backup as Telegram `/backup`.

## External analytics contract

The separate analytics program writes one finished row per month to
`analytics.monthly_turnover`. See `write_turnover.sql`. The Telegram command
`/turnover YYYY-MM` only reads that row; it does not calculate the result.

Inventory is available to other read-only projects through:

- `analytics.v_inventory_stock` — current quantity by product, color and size;
- `analytics.v_inventory_in_transit` — items currently sent to customers;
- `analytics.v_inventory_movements` — every receipt, manual adjustment and 1C write-off;
- `analytics.v_inventory_batches` — sea/air receipt batches, their cost and remainder;
- `order_id` in the movements view — the order that caused an automatic write-off.

For OLX availability, always read `available` from `analytics.v_inventory_stock`.
`on_hand` is the total quantity still owned by the business. `reserved` includes
both confirmed items waiting to be sent and items already in transit, so
`available = on_hand - reserved` is what can actually be offered to a new
customer. Use `awaiting_shipment` and `in_transit` to show that split in another
program.

After a deployment or manual database change, run
`python scripts/audit_database.py`. It checks stock, reservations, order dates
and stale tracking claims without changing business data.

Sea and air never create separate products. They are cost attributes of receipt
batches. Sales consume the oldest available batches first (FIFO), so a changed
price affects only new receipts and never rewrites historical profit.

`/clear_month` records a reset point for the current month. It does not delete
orders or inventory history, and it cannot reset an older month.

