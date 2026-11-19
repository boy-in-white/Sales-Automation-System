"""Prepare a saved order once; shipping remains a separate physical action."""

import logging

from business_db import claim_notification, claim_submission, finish_notification, finish_submission, get_submission, reserve_order_by_ttn
from config import TG_ALERTS_THREAD_ID, TG_GROUP_ID

LOGGER = logging.getLogger(__name__)


def prepare_submission(bot, submission, actor_id, actor_name=''):
    from incoming_group_listener import process_submission
    from repliq_inbox import process_imported, submission_confirmed

    saved = get_submission(submission['id'])
    if not saved or saved['status'] not in ('PENDING', 'PROCESSING', 'CONFIRMED'):
        return False, True
    success = saved['status'] == 'CONFIRMED'
    if not success:
        if not claim_submission(saved['id'], actor_id):
            return False, True
        try:
            outcome = process_imported(bot, saved)
            if outcome is None:
                outcome = process_submission(bot, saved)
            success = outcome in ('processed', 'repeat')
        except Exception:
            LOGGER.exception('Order preparation needs retry: %s', saved['id'])
        finally:
            finish_submission(saved['id'], success)
    warning = False
    if not saved['parsed'].get('offline'):
        for ttn in saved['ttns']:
            result = reserve_order_by_ttn(ttn, actor_id, actor_name)
            warning |= not result or bool(result.get('warning')) or result.get('reservation_status') in ('shortage', 'unmatched', 'partial', 'not_allowed')
            if result and result.get('warning') and claim_notification(result['id'], 'RESERVATION_WARNING'):
                try:
                    sent = bot.send_message(TG_GROUP_ID, '⚠️ Перевір резерв складу\nТТН: %s\n%s\n%s' % (
                        ttn, result.get('raw_product_name') or '', result['warning']), message_thread_id=TG_ALERTS_THREAD_ID)
                    finish_notification(result['id'], 'RESERVATION_WARNING', True, sent.message_id)
                except Exception:
                    finish_notification(result['id'], 'RESERVATION_WARNING', False)
                    LOGGER.warning('Could not publish stock reservation warning: %s', saved['id'])
    if success:
        submission_confirmed(saved['id'])
    return success, bool(warning)
