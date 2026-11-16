"""Private operator order cards. Payment marks are owner attestations, not bank checks."""

import json
import time

from order_summary import snapshot, invoice_key, summary, phase, confirmation_key


class OrderBoard:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS order_cards (
                    account TEXT NOT NULL, thread TEXT NOT NULL, order_id TEXT NOT NULL,
                    invoice_key TEXT NOT NULL, snapshot TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(account,thread)
                );
                CREATE TABLE IF NOT EXISTS payment_decisions (
                    event_key TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                    order_id TEXT NOT NULL, invoice_key TEXT NOT NULL, verified INTEGER NOT NULL,
                    owner INTEGER NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS order_card_retention ON order_cards(updated);
                CREATE INDEX IF NOT EXISTS payment_decision_retention ON payment_decisions(created);
            """)

    def get(self, account, thread):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM order_cards WHERE account=? AND thread=?", (account, thread)).fetchone()
        return dict(row, data=json.loads(row["snapshot"])) if row else None

    def sync(self, account, thread, context, catalog):
        order = snapshot(catalog, context)
        if not order["items"] or not any(item.get("size") or item.get("color") for item in order["items"]) and not order["id"]:
            old = self.get(account, thread)
            if old:
                self.clear(account, thread)
                return old["revision"] + 1
            return None
        encoded = json.dumps(order, sort_keys=True, ensure_ascii=False)
        key = invoice_key(order)
        verified = int(bool(context.get("payment_verified") and context.get("verified_invoice_key") == key))
        with self.store.db() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM order_cards WHERE account=? AND thread=?", (account, thread)).fetchone()
            if old and old["snapshot"] == encoded and old["verified"] == verified:
                return None
            revision = old["revision"] + 1 if old else 1
            db.execute("INSERT OR REPLACE INTO order_cards VALUES(?,?,?,?,?,?,?,?)",
                       (account, thread, order["id"], key, encoded, verified, revision, time.time()))
        return revision

    def verify(self, account, thread, order_id, key_prefix, owner, verified, event_key):
        with self.store.db() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            card = self.get(account, thread)
            route = db.execute("SELECT buyer,advert,mode FROM handoffs WHERE account=? AND thread=?", (account, thread)).fetchone()
            if (not card or not route or route["mode"] not in ("bot", "human", "paused") or card["order_id"] != order_id or
                    card["invoice_key"][:16] != key_prefix or not card["data"]["issued"] or
                    card["data"]["method"] not in ("cod", "full")):
                return None
            context = self.store.context(account, thread, route["buyer"], route["advert"], 90)
            if context.get("invoice_id") != order_id:
                return None
            if bool(card["verified"]) == verified:
                return card["revision"]
            inserted = db.execute("INSERT OR IGNORE INTO payment_decisions VALUES(?,?,?,?,?,?,?,?)",
                                  (event_key, account, thread, order_id, card["invoice_key"], int(verified), owner, time.time())).rowcount
            if not inserted:
                return card["revision"]
            revision = card["revision"] + 1
            db.execute("UPDATE order_cards SET verified=?,revision=?,updated=? WHERE account=? AND thread=?",
                       (int(verified), revision, time.time(), account, thread))
            context["payment_verified"] = verified
            context["payment_verified_by"] = owner
            context["verified_invoice_key"] = card["invoice_key"] if verified else None
            self.store.save_context(account, thread, route["buyer"], route["advert"], context)
        from fulfillment import payment_to_queue
        payment_to_queue(self.store.path.parent, account, thread, order_id, card["invoice_key"], owner, verified)
        return revision

    def clear(self, account, thread):
        with self.store.db() as db:
            db.execute("DELETE FROM order_cards WHERE account=? AND thread=?", (account, thread))

    def cleanup(self, days):
        with self.store.db() as db:
            cutoff = time.time() - days * 86400
            db.execute("DELETE FROM order_cards WHERE updated<?", (cutoff,))
            db.execute("DELETE FROM payment_decisions WHERE created<?", (cutoff,))

    def text(self, account, thread, header):
        card = self.get(account, thread)
        if not card:
            return header
        order = card["data"]
        payment = "\nВкладення для перевірки оплати: отримане" if order["receipt"] else "\nВкладення для твого перегляду" if order.get("attachment_pending") else ""
        if order.get("previous_amounts"):
            payment += "\nСума до останньої зміни: %s грн." % order["previous_amounts"][-1]
        change = order.get("quantity_change")
        if change:
            payment += "\nЗапит клієнта: %s%s шт.%s" % (
                "+" if change.get("mode") == "add" else "загалом ", change["amount"],
                " для позиції %s" % (change["line"] + 1) if "line" in change else " — позиція ще уточнюється")
        parcel = order.get("parcel", {})
        if parcel.get("ttn"):
            payment += "\n📦 ТТН підготовлена: " + parcel["ttn"]
            state = parcel.get("ledgerly_status")
            payment += ("\n✅ Відправлено в Ledgerly" if state == "shipped" else "\nПередано в Ledgerly" if state == "sent" else
                        "\nПередачу скасовано" if state == "cancelled" else "\nОчікує передачі в Ledgerly")
        if parcel.get("status") in ("invalid", "uncertain", "needs_review") or parcel.get("ledgerly_status") == "uncertain":
            payment += "\n⚠️ Накладна потребує перевірки. Автоповтор створення вимкнений."
            if str(parcel.get("error", "")).startswith("active_profile_not_configured:"):
                payment += "\nАктивний за розкладом акаунт Нової пошти ще не налаштований."
        address = order.get("shipping", {})
        place = "\n📍 " + " · ".join(address.get(k, "") for k in ("place", "branch") if address.get(k)) if address.get("place") or address.get("branch") else ""
        return header + "\n\n" + phase(order, bool(card["verified"])) + "\n\n" + summary(order, shipping=False) + place + payment + "\n\nОплата й відправлення — у Ledgerly."

    def details(self, account, thread):
        card = self.get(account, thread)
        if not card:
            return "📦 Замовлення ще формується. Вибір клієнта з'явиться тут після уточнення товару."
        order = card["data"]
        text = "📦 Повні дані замовлення\n\n" + phase(order, bool(card["verified"])) + "\n\n" + summary(order)
        parcel = order.get("parcel", {})
        if parcel.get("ttn"):
            text += "\n\nТТН: " + parcel["ttn"] + "\nАкаунт НП: " + str(parcel.get("profile", ""))
        return text
