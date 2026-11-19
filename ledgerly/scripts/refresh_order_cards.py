#!/usr/bin/env python3
"""Refresh open shipping cards after switching to automatic preparation.

Preview by default. Stop Ledgerly before --apply; the receiver will refresh
Repliq cards after restart, preserving its independent payment gate.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_db as db  # noqa: E402
from config import BOT_TOKEN, REPLIQ_BRIDGE_DB  # noqa: E402
from fulfillment_queue import ParcelQueue  # noqa: E402
from submission_service import prepare_submission  # noqa: E402
from tracking_notify import build_employee_text  # noqa: E402
from ui import awaiting_shipment_keyboard  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    queue = ParcelQueue(REPLIQ_BRIDGE_DB) if REPLIQ_BRIDGE_DB else None
    bot = None
    if args.apply:
        import telebot
        bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
    with db._transaction() as session:
        rows = session.all("SELECT id FROM order_submissions WHERE status IN ('PENDING','PROCESSING','CONFIRMED') AND telegram_message_id>0")
    count = 0
    for row in rows:
        saved = db.get_submission(row['id'])
        parsed = saved['parsed']
        if parsed.get('offline') or not saved['ttns']:
            continue
        if saved['source_user_name'] == 'Repliq' or queue and queue.by_submission(saved['id']):
            continue
        orders = [db.get_order_by_ttn(ttn) for ttn in saved['ttns']]
        if any(order and order['status'] != 'CONFIRMED' for order in orders):
            continue
        print('Open manual card:', saved['id'])
        count += 1
        if not args.apply:
            continue
        ready, warning = prepare_submission(bot, saved, saved['source_user_id'], saved['source_user_name'])
        text = build_employee_text(client_name=saved['source_user_name'], user_id=saved['source_user_id'],
            shipment_kind=parsed.get('shipment_kind') or 'Наложка', description=parsed.get('description') or '',
            cost_text=parsed.get('cost_text') or '', ttns=saved['ttns'], offline=False,
            quantity=parsed.get('quantity') or '1')
        text += '\n\n' + ('⚠️ Резерв складу потребує перевірки.' if warning else '📦 Товар зарезервовано. Натисни «Відправлено» після передачі посилки.')
        if not ready:
            text += '\nТрекінг потребує повторної перевірки.'
        try:
            bot.edit_message_text(text, chat_id=saved['telegram_chat_id'], message_id=saved['telegram_message_id'],
                                  reply_markup=awaiting_shipment_keyboard(saved['id']))
        except Exception as error:
            if 'message is not modified' not in str(error).casefold():
                print('Card needs manual review:', saved['id'], type(error).__name__)
    if queue:
        with queue.db() as session:
            query = "SELECT COUNT(*) FROM parcels WHERE ledgerly_status IN ('sent','confirmed') AND message_id IS NOT NULL"
            print('Open Repliq cards:', session.execute(query).fetchone()[0])
            if args.apply:
                session.execute("UPDATE parcels SET notified_revision=-1 WHERE ledgerly_status IN ('sent','confirmed') AND message_id IS NOT NULL")
    print(('Refreshed' if args.apply else 'Preview:'), count, 'manual card(s)')


if __name__ == '__main__':
    main()
