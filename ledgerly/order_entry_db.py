"""Atomic transitions for the persistent manual order form."""

import json

import business_db as db


def advance(user_id, chat_id, token, revision, step, payload, submission=None):
    with db._LOCK, db._transaction() as session:
        if session.backend == 'sqlite':
            session.execute('BEGIN IMMEDIATE')
        suffix = ' FOR UPDATE' if session.backend == 'postgresql' else ''
        row = session.one('SELECT * FROM inventory_wizards WHERE user_id=?' + suffix, (user_id,))
        if not row or row['chat_id'] != chat_id or not row['step'].startswith('ORDER_'):
            return False
        current = json.loads(row['payload_json'])
        if current.get('token') != token or current.get('revision') != revision:
            return False
        if submission:
            ttns = submission['ttns']
            if ttns:
                ttn = ttns[0]
                if session.backend == 'postgresql':
                    session.execute('SELECT pg_advisory_xact_lock(hashtext(?))', ('manual-order:' + ttn,))
                if session.one('SELECT id FROM orders WHERE ttn=?', (ttn,)):
                    raise ValueError('Ця ТТН уже є в обліку. Нове замовлення не створено.')
                rows = session.all("SELECT id,ttns_json FROM order_submissions WHERE status NOT IN ('CANCELLED','NEEDS_EDIT') AND ttns_json LIKE ?", ('%' + ttn + '%',))
                if any(ttn in json.loads(item['ttns_json']) for item in rows):
                    raise ValueError('Цю ТТН уже додано. Перевір картку замовлення.')
            session.execute('''INSERT INTO order_submissions(
                id,raw_text,parsed_json,ttns_json,source_user_id,source_user_name,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?)''',
                (token, submission['raw_text'], json.dumps(submission['parsed'], ensure_ascii=False),
                 json.dumps(ttns), user_id, submission['source_user_name'], db._now(), db._now()))
        updated = dict(payload, token=token, revision=revision + 1)
        session.execute('UPDATE inventory_wizards SET step=?,payload_json=?,updated_at=? WHERE user_id=?',
                        (step, json.dumps(updated, ensure_ascii=False), db._now(), user_id))
        return True


def claim_publication(submission_id, chat_id):
    # A send with an unknown outcome must not be retried automatically: Telegram
    # does not offer an idempotency key. Keep the SQL order, avoid a second card.
    with db._LOCK, db._transaction() as session:
        result = session.execute('''UPDATE order_submissions SET telegram_chat_id=?,telegram_message_id=-1
            WHERE id=? AND telegram_message_id=0''', (chat_id, submission_id))
        return result.rowcount == 1
