"""Bounded SQL aggregates for private Telegram charts; no customer data."""

from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

import business_db as db
from config import TIMEZONE


def bounds(period):
    period = (period or 'month').strip().lower()
    if period == 'all':
        return period, None, None
    if period == 'month':
        period = datetime.now(ZoneInfo(TIMEZONE)).strftime('%Y-%m')
    if not re.fullmatch(r'[1-9]\d{3}-(?:0[1-9]|1[0-2])', period):
        raise ValueError('Use month, all or YYYY-MM')
    year, month = map(int, period.split('-'))
    start = datetime(year, month, 1, tzinfo=ZoneInfo(TIMEZONE))
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=ZoneInfo(TIMEZONE))
    return period, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def dashboard_data(period='month'):
    label, start, end = bounds(period)
    stats = db.get_sales_stats(label)
    postgres = db.database_backend(db.BUSINESS_DATABASE_URL) == 'postgresql'

    def date_key(field, monthly=False):
        if postgres:
            return "TO_CHAR(%s AT TIME ZONE 'Europe/Kyiv', '%s')" % (field, 'YYYY-MM' if monthly else 'YYYY-MM-DD')
        return 'substr(%s,1,%s)' % (field, 7 if monthly else 10)

    month_key = date_key('o.sold_at', True)
    sales = "o.sold_at IS NOT NULL AND o.status NOT IN ('REFUSED','RETURNED','CANCELLED')"
    sales += (' AND NOT EXISTS (SELECT 1 FROM monthly_analytics_resets r '
              'WHERE r.month = %s AND o.sold_at < r.cleared_at)' % month_key)
    refusals = 'refused_at IS NOT NULL'
    params = ()
    if start:
        sales += ' AND o.sold_at >= ? AND o.sold_at < ?'
        refusals += ' AND refused_at >= ? AND refused_at < ?'
        params = (start, end)
    day = date_key('o.sold_at', label == 'all')
    profit = "CASE WHEN o.cost_source IN ('FIFO_BATCH','MANUAL') AND o.sale_price_cents > 0 THEN o.sale_price_cents-o.purchase_cost_cents-o.delivery_cost_cents-o.platform_fee_cents-o.other_cost_cents ELSE 0 END"
    with db._LOCK, db._transaction() as session:
        timeline = session.all(
            f'''SELECT {day} AS bucket, COUNT(*) AS sales_count,
                SUM(o.sale_price_cents) AS turnover_cents, SUM({profit}) AS profit_cents
                FROM orders o WHERE {sales} GROUP BY {day}
                ORDER BY bucket DESC LIMIT 24''' if label == 'all' else
            f'''SELECT {day} AS bucket, COUNT(*) AS sales_count,
                SUM(o.sale_price_cents) AS turnover_cents, SUM({profit}) AS profit_cents
                FROM orders o WHERE {sales} GROUP BY {day} ORDER BY bucket DESC LIMIT 31''', params)
        top = session.all(
            f'''SELECT name, SUM(revenue) AS turnover_cents FROM (
                SELECT COALESCE(p.canonical_name,o.raw_product_name) AS name,
                       o.sale_price_cents AS revenue
                FROM orders o LEFT JOIN products p ON p.id=o.product_id
                WHERE {sales} AND NOT EXISTS (SELECT 1 FROM repliq_parcel_items i WHERE i.ttn=o.ttn)
                UNION ALL
                SELECT COALESCE(p.canonical_name,'Товар') AS name,
                       i.quantity*i.unit_price_cents AS revenue
                FROM orders o JOIN repliq_parcel_items i ON i.ttn=o.ttn
                LEFT JOIN products p ON p.id=i.product_id
                WHERE {sales}
                ) line_sales GROUP BY name
                ORDER BY turnover_cents DESC LIMIT 6''', params + params)
        channels = session.all(
            f'''SELECT o.sale_type AS name, COUNT(*) AS amount FROM orders o
                WHERE {sales} GROUP BY o.sale_type ORDER BY amount DESC''', params)
        refusal = session.one(f'SELECT COUNT(*) AS amount FROM orders WHERE {refusals}', params)
        stock = session.one('''SELECT COALESCE(SUM(on_hand),0) AS on_hand,
            COALESCE(SUM(available),0) AS available,
            COALESCE(SUM(awaiting_shipment),0) AS awaiting_shipment,
            COALESCE(SUM(in_transit),0) AS in_transit FROM v_inventory_stock''')
    return {'period': label, 'stats': stats, 'timeline': list(reversed(timeline)),
            'top_products': top, 'channels': channels, 'refusals': int(refusal['amount']),
            'stock': {key: int(value) for key, value in stock.items()}}
