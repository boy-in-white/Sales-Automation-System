"""One moving auto/manual control per topic; failed edits are retried durably."""
import time

from telegram_errors import TelegramError


class TopicControls:
    def __init__(self, store, account, group, api):
        self.store, self.account, self.group, self.api = store, account, group, api
        self.next_cleanup = 0
        with store.db() as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(handoffs)')}
            for name in ('keyboard_message_id', 'topic_tail_id'):
                if name not in columns:
                    db.execute('ALTER TABLE handoffs ADD COLUMN ' + name + ' INTEGER')
            db.execute('CREATE TABLE IF NOT EXISTS bridge_markup_cleanup ('
                       'group_id INTEGER, message_id INTEGER, account TEXT, attempts INTEGER NOT NULL DEFAULT 0, '
                       'retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(group_id,message_id))')
            # Retire keyboards created by earlier versions, including old history
            # blocks. Message text and the operator's notes are left intact.
            key = 'single-topic-controls:v1:' + account
            if not db.execute('SELECT 1 FROM meta WHERE key=?', (key,)).fetchone():
                db.execute("INSERT OR IGNORE INTO bridge_markup_cleanup(group_id,message_id,account) "
                           "SELECT r.group_id,r.message_id,r.account FROM bridge_refs r JOIN handoffs h "
                           "ON h.account=r.account AND h.thread=r.thread WHERE r.account=? AND r.group_id=? "
                           "AND r.kind IN ('control','customer','history','attention','prompt')", (account, group))
                db.execute("INSERT INTO meta(key,value) VALUES(?, '{}')", (key,))

    def attached(self, route, message):
        with self.store.db() as db:
            old = db.execute('SELECT keyboard_message_id FROM handoffs WHERE account=? AND thread=?',
                             (self.account, route['thread'])).fetchone()
            if old and old[0] and old[0] != message:
                db.execute('INSERT OR IGNORE INTO bridge_markup_cleanup(group_id,message_id,account) VALUES(?,?,?)',
                           (route['group_id'], old[0], self.account))
            db.execute('DELETE FROM bridge_markup_cleanup WHERE group_id=? AND message_id=?', (route['group_id'], message))
            db.execute('UPDATE handoffs SET keyboard_message_id=?,topic_tail_id=? WHERE account=? AND thread=?',
                       (message, message, self.account, route['thread']))

    def owner_message(self, route, message):
        with self.store.db() as db:
            db.execute('UPDATE handoffs SET topic_tail_id=MAX(COALESCE(topic_tail_id,0),?) WHERE account=? AND thread=?',
                       (message, self.account, route['thread']))

    def refresh(self, route, markup):
        """A click on an old message must never restore its old keyboard."""
        message = route.get('keyboard_message_id')
        if not message or message != route.get('topic_tail_id'):
            return
        try:
            self.api.call('editMessageReplyMarkup', {'chat_id': route['group_id'], 'message_id': message, 'reply_markup': markup})
        except TelegramError as error:
            if error.reason != 'not_modified':
                raise

    def cleanup(self):
        if time.monotonic() < self.next_cleanup:
            return
        with self.store.db() as db:
            row = db.execute('SELECT * FROM bridge_markup_cleanup WHERE account=? AND group_id=? AND retry_at<=? '
                             'ORDER BY message_id DESC LIMIT 1', (self.account, self.group, time.time())).fetchone()
            if not row:
                return
            current = db.execute('SELECT 1 FROM handoffs WHERE account=? AND group_id=? AND keyboard_message_id=?',
                                 (self.account, row['group_id'], row['message_id'])).fetchone()
        self.next_cleanup = time.monotonic() + 3.2
        try:
            if not current:
                self.api.call('editMessageReplyMarkup', {'chat_id': row['group_id'], 'message_id': row['message_id'],
                                                        'reply_markup': {'inline_keyboard': []}})
        except TelegramError as error:
            if error.reason not in ('message_missing', 'not_modified'):
                delay = max(5, min(300, 5 * 2 ** min(row['attempts'], 6)), error.retry_after)
                with self.store.db() as db:
                    db.execute('UPDATE bridge_markup_cleanup SET attempts=attempts+1,retry_at=? WHERE group_id=? AND message_id=?',
                               (time.time() + delay, row['group_id'], row['message_id']))
                return
        with self.store.db() as db:
            db.execute('DELETE FROM bridge_markup_cleanup WHERE group_id=? AND message_id=?', (row['group_id'], row['message_id']))
