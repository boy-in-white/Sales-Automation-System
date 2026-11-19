"""Receipt permalink persistence and payment UI; Telegram is a local fake."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import repliq_inbox as inbox
import test_repliq_inbox as legacy

class ReceiptArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f=legacy.InboxTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.queue,self.receiver,self.bot=self.f.queue,self.f.receiver,self.f.bot
        data=copy.deepcopy(self.f.data);data['receipt_required']=True
        self.row=self.f.created(data)
        self.bot.send_photo=Mock(return_value=SimpleNamespace(message_id=701))
        self.bot.send_document=Mock(return_value=SimpleNamespace(message_id=702))

    def receipt(self):
        self.queue.attach_receipts(self.row['id'],['https://images.example.test/receipt.jpg'])
        self.assertTrue(self.receiver.deliver())
        self.receiver.next_send=0
        return self.receiver.decorate(self.queue.get(self.row['id']))

    def test_forwarded_receipt_has_permanent_private_link_on_the_order_card(self):
        row=self.receipt()
        self.assertEqual(self.bot.send_photo.call_count,1)
        self.assertEqual(inbox.receipt_links(row),['https://t.me/c/1/701'])
        self.assertIn('✅ Квитанцію отримано',inbox.card_text(row))
        self.assertIn('https://t.me/c/1/701',inbox.card_text(row))
        self.assertNotIn('https://images.example.test',inbox.card_text(row))
        self.assertFalse(row['paid'])
        self.assertTrue(self.receiver.deliver())
        self.assertEqual(self.bot.send_photo.call_count,1)

    def test_bank_only_is_not_shown_as_both_conditions_met(self):
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET payment_source='monobank_pending_receipt',bank_payment=? WHERE id=?",
                       (json.dumps({'actual_cents':10000,'credited_cents':10000}),self.row['id']))
        row=self.receiver.decorate(self.queue.get(self.row['id']))
        text=inbox.card_text(row)
        self.assertIn('⏳ Очікуємо квитанцію',text)
        self.assertIn('✅ mono: отримано 100 грн',text)
        self.assertFalse(row['paid'])

    def test_both_conditions_hide_manual_payment_confirmation_button(self):
        self.receipt()
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET paid=1,payment_source='monobank',bank_payment=? WHERE id=?",
                       (json.dumps({'actual_cents':10000,'credited_cents':10000}),self.row['id']))
        row=self.receiver.decorate(self.queue.get(self.row['id']))
        row['submission_id']='fake-submission'
        texts=[b.text for line in inbox.keyboard(row).keyboard for b in line]
        self.assertNotIn('✅ Оплату перевірено',texts)
        self.assertNotIn('❌ Оплата не підтвердилась',texts)
        self.assertIn('📦 Відправлено',texts)
        self.assertNotIn('✅ Підтвердити замовлення',texts)
        self.assertIn('🧾 Квитанція',texts)
        text=inbox.card_text(row)
        self.assertIn('✅ mono',text);self.assertIn('✅ Квитанцію',text)

    def test_known_rejected_photo_can_use_document_fallback(self):
        error=RuntimeError('Fake rejection');error.error_code=400
        self.bot.send_photo.side_effect=error
        row=self.receipt()
        self.assertEqual(row['archive_urls'],['https://t.me/c/1/702'])
        self.assertEqual(self.bot.send_document.call_count,1)

    def test_network_timeout_does_not_duplicate_upload_or_approve(self):
        self.bot.send_photo.side_effect=TimeoutError('Fake timeout')
        row=self.receipt()
        self.assertFalse(row['archive_urls']);self.assertTrue(row['archive_failed'])
        self.assertFalse(row['paid'])
        self.receiver.deliver()
        self.assertEqual(self.bot.send_photo.call_count,1)
        self.assertFalse(self.bot.send_document.called)

    def test_wrong_or_replaced_invoice_does_not_inherit_a_receipt(self):
        self.receipt()
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET invoice_key='different' WHERE id=?",(self.row['id'],))
        self.assertEqual(self.queue.receipts(self.row['id']),[])
