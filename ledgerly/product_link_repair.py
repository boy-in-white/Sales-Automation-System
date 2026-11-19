"""Repair legacy product labels from actual stock evidence, never deduct stock."""

import business_db as db


def repair_product_links(*, order_ids=(), apply=False):
    repaired = []
    with db._LOCK, db._transaction() as session:
        if apply and session.backend == 'sqlite':
            session.execute('BEGIN IMMEDIATE')
        clause, params = '', ()
        if order_ids:
            clause = ' AND o.id IN (' + ','.join('?' for _ in order_ids) + ')'
            params = tuple(order_ids)
        orders = session.all('''SELECT o.id,o.ttn,o.product_id,o.raw_product_name
            FROM orders o WHERE NOT EXISTS (
                SELECT 1 FROM product_variants v WHERE v.product_id=o.product_id
            )''' + clause + (' FOR UPDATE OF o' if apply and session.backend == 'postgresql' else ''), params)
        for order in orders:
            evidence = session.all('''SELECT DISTINCT product_id FROM (
                SELECT v.product_id FROM inventory_movements m
                JOIN product_variants v ON v.id=m.variant_id
                WHERE m.order_id=? AND m.movement_type='SALE_1C' AND m.quantity_delta<0
                UNION
                SELECT v.product_id FROM inventory_reservations r
                JOIN product_variants v ON v.id=r.variant_id
                WHERE r.order_id=? AND r.status IN ('ACTIVE','CONSUMED')
                UNION
                SELECT product_id FROM repliq_parcel_items WHERE ttn=?
            ) sources''', (order['id'], order['id'], order['ttn']))
            if len(evidence) != 1:
                continue
            target_id = evidence[0]['product_id']
            base_key, size, color = db._inventory_variant_hints(order['raw_product_name'])
            _, matches = db._catalog_product_matches(session, base_key)
            if matches != [target_id] or 'AMBIGUOUS' in (size, color):
                continue
            target = session.one('SELECT id,canonical_name FROM products WHERE id=? AND active=1', (target_id,))
            if not target or target_id == order['product_id']:
                continue
            change = {'order_id': order['id'], 'old_product_id': order['product_id'],
                      'product_id': target_id, 'product_name': target['canonical_name'],
                      'raw_product_name': order['raw_product_name']}
            if apply:
                session.execute('UPDATE orders SET product_id=?,updated_at=? WHERE id=?',
                                (target_id, db._now(), order['id']))
                db._event(session, order['id'], 'PRODUCT_LINK_REPAIRED', 0, 'Ledgerly repair', change)
                if order['product_id']:
                    # Preserve the old row and aliases for audit/history, but
                    # hide an empty legacy label from the stock menu.
                    session.execute('''UPDATE products SET active=0,updated_at=? WHERE id=?
                        AND NOT EXISTS (SELECT 1 FROM orders WHERE product_id=?)
                        AND NOT EXISTS (SELECT 1 FROM product_variants WHERE product_id=?)
                        AND NOT EXISTS (SELECT 1 FROM repliq_parcel_items WHERE product_id=?)
                        AND NOT EXISTS (SELECT 1 FROM product_cost_profiles WHERE product_id=?)''',
                        (db._now(), order['product_id'], order['product_id'], order['product_id'], order['product_id'], order['product_id']))
            repaired.append(change)
    return repaired
