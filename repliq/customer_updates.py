"""Durable shipping and receipt-review messages bound to the exact buyer/invoice."""

import hashlib
import json
import re
import time

from order_summary import snapshot, invoice_key, confirmation_key
from carrier_payment import direct_delivery


def shipped_waybill(parcel):
    ttn = str(parcel.get('ttn') or '')
    if parcel.get('status') == 'created' and parcel.get('ledgerly_status') == 'shipped' and re.fullmatch(r'\d{14}', ttn):
        return ttn
    return ''


def can_watch_shipment(context, raw):
    order = str(context.get('invoice_id') or '')
    references = set(re.findall(r'\bRQ-[0-9A-F]{12}\b', raw.upper()))
    numbers = set(re.findall(r'(?<!\d)\d{14}(?!\d)', raw))
    parcel = context.get('parcel', {})
    return bool(order and context.get('invoice_issued') and parcel.get('status') != 'cancelled' and
                (not references or references == {order.upper()}) and (not numbers or numbers == {str(parcel.get('ttn'))}))


class CustomerUpdates:
    def __init__(self, store):
        self.store = store
        self.cursor, self.thread_cursor = 0, ""
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS customer_updates (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                    buyer TEXT NOT NULL, advert TEXT NOT NULL, invoice_id TEXT NOT NULL,
                    invoice_key TEXT NOT NULL, kind TEXT NOT NULL, ttn TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS customer_update_queue ON customer_updates(account,status,created);
                CREATE INDEX IF NOT EXISTS order_update_feed ON order_cards(account,verified,updated,thread);
            """)
            if 'revision' not in {row['name'] for row in db.execute('PRAGMA table_info(customer_updates)')}:
                db.execute('ALTER TABLE customer_updates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')

    def recover(self):
        with self.store.db() as db:
            db.execute("UPDATE customer_updates SET status='uncertain',note='send_result_unknown' WHERE status='sending'")

    def cleanup(self, days):
        with self.store.db() as db:
            db.execute("UPDATE customer_updates SET status='stale',note='notification_expired',updated=? "
                       "WHERE status='pending' AND created<?", (time.time(), time.time() - 86400))
            # Keep unsolved/uncertain deliveries for the owner; purge only settled history.
            db.execute("DELETE FROM customer_updates WHERE status IN ('sent','stale') AND updated<?", (time.time() - days * 86400,))

    def schedule(self, account, thread, buyer, advert, context, catalog):
        order = snapshot(catalog, context)
        key = invoice_key(order)
        if (not order["issued"] or not order["id"] or order["method"] not in ("cod", "full") or
                order["confirmation"] != confirmation_key(order)):
            return
        parcel = order.get("parcel", {})
        if parcel.get("status") in ("needs_review", "invalid", "uncertain", "cancelled"):
            return
        verified = context.get('payment_verified') and context.get('verified_invoice_key') == key
        review = int(parcel.get('payment_review_revision') or 0) if not verified and not direct_delivery(order) else 0
        kinds = []
        if review:
            kinds.append(('payment_review', ''))
        elif direct_delivery(order) or verified:
            if verified:
                if direct_delivery(order) and parcel.get('ledgerly_status') != 'shipped':
                    kinds.append(('delivery_paid', ''))
                elif not direct_delivery(order):
                    kinds.append(('dispatch', ''))
            if parcel.get('status') == 'created' and re.fullmatch(r'\d{14}', str(parcel.get('ttn') or '')):
                kinds.insert(0, ('waybill', parcel['ttn']))
        ttn = shipped_waybill(parcel)
        if ttn and context.get('shipment_notice_invoice') == order['id']:
            kinds = [(kind, value) for kind, value in kinds if kind != 'waybill']
            kinds.insert(0, ('shipped', ttn))
        for kind, ttn in kinds:
            values = [account, thread, key, kind, ttn]
            if kind == 'payment_review':
                values.append(review)
            identity = hashlib.sha256(json.dumps(values).encode()).hexdigest()
            announced = kind == "dispatch" and (context.get("dispatch_announced_key") == key or parcel.get("ledgerly_status") == "shipped")
            if kind == 'shipped':
                announced = context.get('shipment_notice_announced') == ttn
            with self.store.db() as db:
                db.execute("INSERT OR IGNORE INTO customer_updates(id,account,thread,buyer,advert,invoice_id,invoice_key,kind,ttn,status,created,updated,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (identity, account, thread, buyer, advert, order["id"], key, kind, ttn, "sent" if announced else "pending", time.time(), time.time(), review if kind == 'payment_review' else 0))
                if verified:
                    db.execute("UPDATE customer_updates SET status='pending',note='',updated=? WHERE id=? AND status='stale' AND note='payment_wait'", (time.time(), identity))
                if announced:
                    db.execute("UPDATE customer_updates SET status='sent',note='announced_in_reply',updated=? WHERE id=? AND status='pending'", (time.time(), identity))

    def sync_cards(self, account, store, catalog):
        with store.db() as db:
            rows = db.execute("SELECT c.*,h.buyer,h.advert FROM order_cards c JOIN handoffs h ON h.account=c.account AND h.thread=c.thread JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert WHERE c.account=? AND (c.verified=1 OR json_extract(c.snapshot,'$.delivery_prepay')=1) AND c.updated>? AND (c.updated>? OR (c.updated=? AND c.thread>?)) ORDER BY c.updated,c.thread LIMIT 50",
                              (account, time.time() - 86400, self.cursor, self.cursor, self.thread_cursor)).fetchall()
        for row in rows:
            context = store.context(account, row["thread"], row["buyer"], row["advert"], catalog.data["limits"]["memory_days"])
            self.schedule(account, row["thread"], row["buyer"], row["advert"], context, catalog)
            self.cursor, self.thread_cursor = row["updated"], row["thread"]

    def next(self, account):
        with self.store.db() as db:
            db.execute("UPDATE customer_updates SET status='stale',note='expired' WHERE account=? AND status='pending' AND created<?", (account, time.time() - 86400))
            row = db.execute("SELECT n.* FROM customer_updates n JOIN handoffs h ON h.account=n.account AND h.thread=n.thread AND h.buyer=n.buyer AND h.advert=n.advert JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert WHERE n.account=? AND n.status='pending' AND h.mode IN ('bot','human') ORDER BY n.created LIMIT 1", (account,)).fetchone()
        return dict(row) if row else None

    def claim(self, identity):
        with self.store.db() as db:
            return db.execute("UPDATE customer_updates SET status='sending',updated=? WHERE id=? AND status='pending'", (time.time(), identity)).rowcount == 1

    def finish(self, identity, status, note=""):
        with self.store.db() as db:
            db.execute("UPDATE customer_updates SET status=?,note=?,updated=? WHERE id=?", (status, note, time.time(), identity))
