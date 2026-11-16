"""Exact, multiple-item inventory accounting for parcels imported from Repliq."""

from collections import Counter
import uuid


def schema_statement():
    return """CREATE TABLE IF NOT EXISTS repliq_parcel_items (
        ttn TEXT NOT NULL, line INTEGER NOT NULL, product_id TEXT NOT NULL, variant_id TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity>0), unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents>0),
        reserved_quantity INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'NEW',
        PRIMARY KEY(ttn,line)
    )"""


def rows(session, order):
    return session.all("SELECT i.*,v.size_code,v.color_code,p.canonical_name AS product_name FROM repliq_parcel_items i "
                       "JOIN product_variants v ON v.id=i.variant_id JOIN products p ON p.id=i.product_id "
                       "WHERE i.ttn=? ORDER BY i.line", (order.get("ttn", ""),))


def prepare(ttn, items):
    import business_db as business
    with business._LOCK, business._transaction() as session:
        expected = []
        for index, item in enumerate(items):
            found = session.all("SELECT v.id FROM product_variants v JOIN products p ON p.id=v.product_id "
                                "WHERE v.product_id=? AND v.size_code=? AND v.color_code=? AND v.active=1 AND p.active=1",
                                (item["product_id"], item["size"], item["color"]))
            if len(found) != 1 or not 1 <= item["quantity"] <= 20 or not 0 < item["unit_price_cents"] <= 100000000:
                raise ValueError("Unmatched imported item")
            expected.append((ttn, index, item["product_id"], found[0]["id"], item["quantity"], item["unit_price_cents"]))
        existing = session.all("SELECT ttn,line,product_id,variant_id,quantity,unit_price_cents FROM repliq_parcel_items WHERE ttn=? ORDER BY line", (ttn,))
        if existing:
            if [tuple(row[key] for key in ("ttn", "line", "product_id", "variant_id", "quantity", "unit_price_cents")) for row in existing] != expected:
                raise ValueError("Waybill manifest changed")
            return
        if session.one("SELECT id FROM orders WHERE ttn=?", (ttn,)):
            raise ValueError("Waybill already exists outside this integration")
        for values in expected:
            session.execute("INSERT INTO repliq_parcel_items(ttn,line,product_id,variant_id,quantity,unit_price_cents) VALUES(?,?,?,?,?,?)", values)


def reserve(session, order, actor, actor_name):
    import business_db as business
    if session.backend == 'postgresql':
        session.one('SELECT id FROM orders WHERE id=? FOR UPDATE', (order['id'],))
    items = rows(session, order)
    if not items:
        return None
    if order.get("onec_recorded_at") or any(item["state"] == "CONSUMED" for item in items):
        return {"reservation_status": "already_finished", "reserved_quantity": 0}
    if all(item["state"] == "RESERVED" for item in items):
        return {"reservation_status": "already_reserved", "reserved_quantity": sum(item["reserved_quantity"] for item in items)}
    demands = Counter()
    for item in items:
        demands[item["variant_id"]] += item["quantity"]
    for variant, quantity in demands.items():
        balance = session.one("SELECT on_hand,reserved FROM inventory_balances WHERE variant_id=?", (variant,))
        if not balance or int(balance["on_hand"]) - int(balance["reserved"]) < quantity:
            return {"reservation_status": "shortage", "reserved_quantity": 0, "warning": "Для всіх позицій посилки недостатньо вільного товару. Перевір склад."}
    now = business._now()
    for variant, quantity in sorted(demands.items()):
        changed = session.execute("UPDATE inventory_balances SET reserved=reserved+?,updated_at=? WHERE variant_id=? AND on_hand-reserved>=?",
                                  (quantity, now, variant, quantity))
        if changed.rowcount != 1:
            raise RuntimeError("Залишок змінився під час резервування посилки")
    session.execute("UPDATE repliq_parcel_items SET state='RESERVED',reserved_quantity=quantity WHERE ttn=?", (order["ttn"],))
    result = {"reservation_status": "reserved", "reserved_quantity": sum(demands.values()), "items": items}
    business._event(session, order["id"], "INVENTORY_RESERVED", actor, actor_name, result)
    return result


def release(session, order, actor, actor_name):
    import business_db as business
    if session.backend == 'postgresql':
        session.one('SELECT id FROM orders WHERE id=? FOR UPDATE', (order['id'],))
    items = rows(session, order)
    if not items:
        return None
    quantities = Counter()
    for item in items:
        if item["state"] == "RESERVED":
            quantities[item["variant_id"]] += item["reserved_quantity"]
    now, released = business._now(), 0
    for variant, quantity in quantities.items():
        balance = session.one("SELECT reserved FROM inventory_balances WHERE variant_id=?", (variant,))
        amount = min(quantity, int((balance or {}).get("reserved", 0)))
        session.execute("UPDATE inventory_balances SET reserved=reserved-?,updated_at=? WHERE variant_id=?", (amount, now, variant))
        released += amount
    session.execute("UPDATE repliq_parcel_items SET state='RELEASED',reserved_quantity=0 WHERE ttn=? AND state='RESERVED'", (order["ttn"],))
    result = {"reservation_status": "released", "released_quantity": released}
    business._event(session, order["id"], "INVENTORY_RESERVATION_RELEASED", actor, actor_name, result)
    return result


def record_in_1c(session, order, actor, actor_name):
    import business_db as business
    items = rows(session, order)
    if not items:
        return None
    now = business._now()
    changed = session.execute("UPDATE orders SET onec_recorded_at=?,onec_recorded_by=?,updated_at=? WHERE id=? AND onec_recorded_at IS NULL",
                              (now, actor, now, order["id"]))
    if changed.rowcount != 1:
        return {"ok": True, "already_recorded": True, "inventory_status": "already_recorded", "ttn": order["ttn"]}
    cost, allocated, requested, deducted_total, results, modes = 0, 0, 0, 0, [], []
    for item in items:
        balance = session.one("SELECT on_hand,reserved FROM inventory_balances WHERE variant_id=?", (item["variant_id"],)) or {}
        on_hand, reserved = int(balance.get("on_hand", 0)), int(balance.get("reserved", 0))
        own = min(reserved, item["reserved_quantity"] if item["state"] == "RESERVED" else 0)
        deducted = min(max(0, on_hand - reserved + own), item["quantity"])
        left = on_hand - deducted
        session.execute("UPDATE inventory_balances SET on_hand=?,reserved=?,updated_at=? WHERE variant_id=?", (left, reserved-own, now, item["variant_id"]))
        allocation = business._consume_fifo_batches(session, item["variant_id"], order["id"], deducted, now)
        allocated += allocation["allocated_quantity"]
        requested += item["quantity"]
        deducted_total += deducted
        cost += allocation["total_cost_cents"]
        modes.extend(mode for mode in allocation["transport_modes"] if mode not in modes)
        session.execute("UPDATE repliq_parcel_items SET state='CONSUMED',reserved_quantity=0 WHERE ttn=? AND line=?", (order["ttn"], item["line"]))
        session.execute("INSERT INTO inventory_movements(id,variant_id,order_id,quantity_delta,balance_after,movement_type,note,actor_user_id,actor_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:20], item["variant_id"], order["id"], -deducted, left, "SALE_1C", "Repliq; ТТН: " + order["ttn"], actor, actor_name, now))
        results.append(dict(item, deducted_quantity=deducted, balance_after=left))
    source = "FIFO_BATCH" if allocated == requested else "PARTIAL_BATCH" if allocated else "MISSING"
    if order.get("cost_source") == "MANUAL" and int(order.get("purchase_cost_cents") or 0) > 0:
        cost, source = int(order["purchase_cost_cents"]), "MANUAL"
    session.execute("UPDATE orders SET purchase_cost_cents=?,cost_source=?,cost_calculated_at=?,updated_at=? WHERE id=?", (cost, source, now, now, order["id"]))
    financial = business._profit_fields(order, cost, source)
    result = {"ok": True, "already_recorded": False, "inventory_status": "written_off" if deducted_total == requested else "shortage",
              "ttn": order["ttn"], "items": results, "product_name": "Кілька речей" if len(items)>1 else items[0]["product_name"],
              "quantity": requested, "deducted_quantity": deducted_total, "transport_modes": modes, **financial}
    warnings = []
    if deducted_total != requested:
        warnings.append("Не вистачає товару для всіх позицій посилки")
    if not financial["cost_ready"]:
        warnings.append("Не вистачає партій із собівартістю — прибуток ще не пораховано")
    if warnings:
        result["warning"] = ". ".join(warnings)
    business._event(session, order["id"], "RECORDED_IN_1C", actor, actor_name, result)
    return result
