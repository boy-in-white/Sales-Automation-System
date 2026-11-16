"""Buttons follow the last message and never change another buyer's mode."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from chat_store import Store
from handoff_store import Handoffs
from telegram_bridge import Bridge
from telegram_controls import TopicControls
from telegram_errors import TelegramError


class FakeTelegram:
    def __init__(self):
        self.calls, self.keyboards, self.next_id = [], {}, 200
        self.fail_edit = False

    def call(self, method, fields, document=None):
        self.calls.append((method, fields))
        if method == 'sendMessage':
            self.next_id += 1
            self.keyboards[self.next_id] = fields.get('reply_markup', {})
            return {'message_id': self.next_id}
        if method == 'editMessageReplyMarkup':
            if self.fail_edit:
                raise TelegramError(method=method)
            self.keyboards[fields['message_id']] = fields['reply_markup']
        elif method == 'editMessageText':
            self.keyboards[fields['message_id']] = fields['reply_markup']
        return True


class MovingControls(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state.sqlite3'); self.addCleanup(self.store.close_current)
        self.routes = Handoffs(self.store)
        self.api = FakeTelegram()
        self.bridge = Bridge(self.routes, {'bot_token':'test','owner_ids':[7],'group_id':-100123},
                             'account', threading.Event(), api=self.api)
        with self.store.db() as db:
            db.execute("INSERT INTO effective_ads VALUES('account','101','product')")
        for thread, topic in [('a',10), ('b',20)]:
            self.routes.waiting('account', thread, 'buyer-'+thread, '101', 1, thread)
            with self.store.db() as db:
                db.execute("UPDATE handoffs SET group_id=-100123,topic_id=?,topic_state='ready',mode='human' WHERE thread=?", (topic,thread))
                db.execute("UPDATE bridge_outbox SET status='cancelled' WHERE thread=?", (thread,))

    def deliver(self, thread, kind='customer'):
        with self.store.db() as db:
            self.routes.enqueue(db, 'event:'+str(len(self.api.calls)), 'account', thread, kind, 'Клієнт: запитання')
        self.bridge.next_send = 0
        self.bridge.topic_controls.next_cleanup = 0
        self.bridge.deliver()
        return self.routes.get('account',thread)['keyboard_message_id']

    def test_only_last_of_customer_seller_and_history_keeps_buttons(self):
        first = self.deliver('a')
        second = self.deliver('a', 'seller')
        self.assertNotEqual(first, second)
        self.assertEqual(self.api.keyboards[first]['inline_keyboard'], [])
        self.assertEqual(len(self.api.keyboards[second]['inline_keyboard'][0]), 2)

    def test_control_status_is_moved_down_after_a_new_message(self):
        self.routes.control('account','a','status','first-control')
        self.bridge.deliver()
        first = self.routes.get('account','a')['control_message_id']
        self.deliver('a')
        self.routes.control('account','a','status','second-control')
        self.bridge.next_send=0; self.bridge.topic_controls.next_cleanup=0; self.bridge.deliver()
        current=self.routes.get('account','a')
        self.assertNotEqual(current['control_message_id'],first)
        self.assertEqual(current['control_message_id'],current['topic_tail_id'])

    def test_owner_message_gets_a_new_control_below_it(self):
        first = self.deliver('a')
        self.bridge.topic_controls.owner_message(self.routes.get('account','a'),1000)
        self.assertEqual(self.routes.get('account','a')['topic_tail_id'],1000)
        self.routes.control('account','a','status','owner-replied')
        self.bridge.next_send=0; self.bridge.deliver()
        self.assertNotEqual(self.routes.get('account','a')['keyboard_message_id'],first)

    def test_edit_failure_retries_without_resending_message_or_losing_mode(self):
        first = self.deliver('a')
        self.api.fail_edit=True
        second = self.deliver('a')
        self.api.fail_edit=False
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bridge_markup_cleanup').fetchone()[0],1)
            db.execute('UPDATE bridge_markup_cleanup SET retry_at=0')
        before=len([call for call in self.api.calls if call[0]=='sendMessage'])
        self.bridge.topic_controls.next_cleanup=0; self.bridge.topic_controls.cleanup()
        self.assertEqual(self.api.keyboards[first]['inline_keyboard'],[])
        self.assertEqual(self.routes.get('account','a')['keyboard_message_id'],second)
        self.assertEqual(self.routes.get('account','a')['mode'],'human')
        self.assertEqual(len([call for call in self.api.calls if call[0]=='sendMessage']),before)

    def test_other_topic_keeps_its_keyboard(self):
        first=self.deliver('a'); other=self.deliver('b'); self.deliver('a')
        self.assertEqual(self.api.keyboards[first]['inline_keyboard'],[])
        self.assertEqual(len(self.api.keyboards[other]['inline_keyboard'][0]),2)

    def test_old_click_updates_latest_keyboard_and_never_restores_old_one(self):
        old=self.deliver('a'); latest=self.deliver('a')
        self.bridge.chat_callback({'update_id':101,'callback_query':{'id':'click','from':{'id':7},
            'message':{'message_id':old,'message_thread_id':10,'chat':{'id':-100123}},'data':'chat:bot'}})
        self.assertEqual(self.routes.get('account','a')['mode'],'returning')
        self.assertEqual(self.api.keyboards[old]['inline_keyboard'],[])
        self.assertEqual(self.api.keyboards[latest]['inline_keyboard'][0][0]['text'],'✓ auto')

    def test_new_take_cancels_a_queued_return_to_auto(self):
        latest=self.deliver('a')
        def click(action, ident):
            self.bridge.chat_callback({'update_id':ident,'callback_query':{'id':str(ident),'from':{'id':7},
                'message':{'message_id':latest,'message_thread_id':10,'chat':{'id':-100123}},'data':'chat:'+action}})
        click('bot',10); click('take',11)
        self.assertEqual(self.routes.get('account','a')['mode'],'human')
        with self.store.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM bridge_replies WHERE kind='switch_bot' AND status='queued'").fetchone())
        deadline=self.routes.get('account','a')['human_until']; click('take',11)
        self.assertEqual(self.routes.get('account','a')['human_until'],deadline)
