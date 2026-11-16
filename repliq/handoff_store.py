"""Durable human ownership and two outboxes. Business SQL is never modified."""

import json
import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

HUMAN_IDLE_SECONDS = 40 * 60


class Handoffs:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS handoffs (
                    account TEXT NOT NULL, thread TEXT NOT NULL, buyer TEXT NOT NULL, advert TEXT NOT NULL,
                    mode TEXT NOT NULL, last_count INTEGER NOT NULL, title TEXT NOT NULL,
                    group_id INTEGER, topic_id INTEGER, topic_state TEXT NOT NULL DEFAULT 'new',
                    updated REAL NOT NULL, PRIMARY KEY(account,thread), UNIQUE(group_id,topic_id)
                );
                CREATE TABLE IF NOT EXISTS bridge_outbox (
                    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL, thread TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    message_id INTEGER, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bridge_delivery ON bridge_outbox(status,id);
                CREATE TABLE IF NOT EXISTS bridge_seen (
                    account TEXT NOT NULL, thread TEXT NOT NULL, message TEXT NOT NULL,
                    PRIMARY KEY(account,thread,message)
                );
                CREATE TABLE IF NOT EXISTS bridge_refs (
                    group_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    account TEXT NOT NULL, thread TEXT NOT NULL, kind TEXT NOT NULL,
                    PRIMARY KEY(group_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS bridge_replies (
                    event_key TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                    group_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'reply'
                );
                CREATE INDEX IF NOT EXISTS bridge_reply_queue ON bridge_replies(account,status,created);
                CREATE INDEX IF NOT EXISTS bridge_reply_expiration ON bridge_replies(status,created);
                CREATE INDEX IF NOT EXISTS bridge_account_delivery ON bridge_outbox(account,status,id);
                CREATE INDEX IF NOT EXISTS bridge_conversation_messages ON bridge_outbox(account,thread,kind,id);
                CREATE INDEX IF NOT EXISTS handoff_topic_queue ON handoffs(account,mode,topic_state,updated);
                CREATE TABLE IF NOT EXISTS bridge_actions (event_key TEXT PRIMARY KEY, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_links (
                    token TEXT PRIMARY KEY,account TEXT NOT NULL,thread TEXT NOT NULL,buyer TEXT NOT NULL,advert TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS olx_sent (
                    account TEXT NOT NULL, thread TEXT NOT NULL, message TEXT NOT NULL, speaker TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(account,thread,message)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if "control_message_id" not in {row[1] for row in db.execute("PRAGMA table_info(handoffs)")}:
                db.execute("ALTER TABLE handoffs ADD COLUMN control_message_id INTEGER")
            columns = {row[1] for row in db.execute("PRAGMA table_info(handoffs)")}
            for name in ("human_since", "human_until", "attention"):
                if name not in columns:
                    db.execute("ALTER TABLE handoffs ADD COLUMN " + name + " REAL NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS human_expiration ON handoffs(account,mode,human_until)")
            if 'telegram_title' not in columns:
                db.execute("ALTER TABLE handoffs ADD COLUMN telegram_title TEXT NOT NULL DEFAULT ''")
            setting = db.execute("SELECT value FROM meta WHERE key='human-idle-seconds'").fetchone()
            previous = json.loads(setting[0]).get('seconds', 7200) if setting else 7200
            if type(previous) is not int or previous <= 0:
                previous = 7200
            if previous != HUMAN_IDLE_SECONDS:
                db.execute("UPDATE handoffs SET human_until=MAX(0,human_until-?) WHERE mode IN ('human','opening') AND human_until>0",
                           (previous - HUMAN_IDLE_SECONDS,))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('human-idle-seconds',?)",
                           (json.dumps({'seconds': HUMAN_IDLE_SECONDS}),))

    def title(self, account, thread, title):
        with self.store.db() as db:
            return db.execute('UPDATE handoffs SET title=? WHERE account=? AND thread=? AND title!=?', (title[:100], account, thread, title[:100])).rowcount == 1

    def get(self, account, thread):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM handoffs WHERE account=? AND thread=?", (account, thread)).fetchone()
        return dict(row) if row else None

    def all(self, account):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM handoffs WHERE account=?", (account,))]

    def waiting(self, account, thread, buyer, advert, count, title):
        with self.store.db() as db:
            db.execute("INSERT INTO handoffs(account,thread,buyer,advert,mode,last_count,title,updated) "
                       "VALUES(?,?,?,?,'waiting',?,?,?) ON CONFLICT(account,thread) DO UPDATE SET "
                       "mode='waiting',last_count=excluded.last_count,title=excluded.title,updated=excluded.updated "
                       "WHERE handoffs.mode='bot' AND handoffs.buyer=excluded.buyer AND handoffs.advert=excluded.advert",
                       (account, thread, buyer, advert, count, title[:100], time.time()))
        self.store.invalidate(account, thread, "Оформлення: чекаємо відповіді для передачі власнику")

    def customer_requests_only(self, account):
        """Retire old automatic help flags while preserving explicit requests and manual sessions."""
        from conversation_policy import requests_owner

        key = 'customer-request-policy:' + account
        if self.store.meta(key).get('applied'):
            return
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            routes = db.execute("SELECT thread FROM handoffs WHERE account=? AND mode='bot' AND attention=1",
                                (account,)).fetchall()
            for route in routes:
                previous = db.execute("SELECT payload FROM drafts WHERE account=? AND thread=? AND status='sent' "
                                      "AND json_extract(payload,'$.on_send.handoff_now')=1 ORDER BY created DESC LIMIT 20",
                                      (account, route['thread'])).fetchall()
                requested = any(requests_owner('\n'.join(json.loads(row['payload']).get('texts', [])))
                                for row in previous)
                if requested:
                    chat = db.execute('SELECT context FROM chats WHERE account=? AND thread=?',
                                      (account, route['thread'])).fetchone()
                    if chat:
                        context = json.loads(chat['context'])
                        context['owner_request_reason'] = 'customer'
                        db.execute('UPDATE chats SET context=? WHERE account=? AND thread=?',
                                   (json.dumps(context, ensure_ascii=False), account, route['thread']))
                    continue
                db.execute('UPDATE handoffs SET attention=0 WHERE account=? AND thread=?',
                           (account, route['thread']))
                db.execute("UPDATE handoffs SET topic_state='dormant' WHERE account=? AND thread=? AND topic_id IS NULL",
                           (account, route['thread']))
                db.execute("UPDATE bridge_outbox SET status='stored' WHERE account=? AND thread=? AND status='pending' "
                           "AND kind IN ('attention','history','customer','seller','prompt')", (account, route['thread']))
                for old in db.execute("SELECT id FROM drafts WHERE account=? AND thread=? AND status='sent' "
                                      "AND json_extract(payload,'$.on_send.handoff_now')=1", (account, route['thread'])).fetchall():
                    db.execute("UPDATE admin_outbox SET status='cancelled' WHERE account=? AND event_key=? AND status='pending'",
                               (account, 'needs-owner:' + old['id'] + ':0'))
            db.execute("UPDATE admin_outbox SET status='cancelled' WHERE account=? AND status='pending' AND event_key LIKE 'feedback:%'",
                       (account,))
            self.store.set_meta(key, {'applied': True})

    def watch(self, account, thread, buyer, advert, count, title):
        with self.store.db() as db:
            created = db.execute("INSERT OR IGNORE INTO handoffs(account,thread,buyer,advert,mode,last_count,title,updated,topic_state) "
                                 "VALUES(?,?,?,?,'bot',?,?,?,'dormant')", (account, thread, buyer, advert, count, title[:100], time.time())).rowcount
            if created:
                self.enqueue(db, "watch:%s:%s" % (account, thread), account, thread, "control", status_text("bot"))

    def advance(self, account, thread, count):
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET last_count=MAX(last_count,?), updated=? WHERE account=? AND thread=?",
                       (count, time.time(), account, thread))

    def cleanup(self, days, purge=True):
        cutoff = time.time() - days * 86400
        with self.store.db() as db:
            db.execute("UPDATE bridge_replies SET status='stale',note='Минув час відповіді' WHERE status='queued' AND created<?",
                       (time.time() - 900,))
            if not purge:
                return
            # Closed routes stay closed until the owner explicitly returns them to the bot.
            db.execute("DELETE FROM bridge_outbox WHERE status IN ('sent','cancelled','stored') AND created<?", (cutoff,))
            db.execute("DELETE FROM bridge_outbox WHERE status='pending' AND created<? AND EXISTS(SELECT 1 FROM handoffs h WHERE h.account=bridge_outbox.account AND h.thread=bridge_outbox.thread AND h.topic_state='dormant')", (cutoff,))
            db.execute("DELETE FROM bridge_replies WHERE status IN ('sent','stale','cancelled') AND created<?", (cutoff,))
            db.execute("DELETE FROM bridge_actions WHERE created<?", (cutoff,))
            db.execute("DELETE FROM olx_sent WHERE created<?", (cutoff,))

    @staticmethod
    def enqueue(db, key, account, thread, kind, text):
        db.execute("INSERT OR IGNORE INTO bridge_outbox(event_key,account,thread,kind,text,created) VALUES(?,?,?,?,?,?)",
                   (key, account, thread, kind, text, time.time()))

    def notice(self, account, thread, text, key):
        with self.store.db() as db:
            self.enqueue(db, key, account, thread, "notice", text)

    def control(self, account, thread, text, key):
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM bridge_outbox WHERE event_key=?", (key,)).fetchone():
                return
            db.execute("UPDATE bridge_outbox SET status='cancelled' WHERE account=? AND thread=? AND kind='control' AND status='pending'",
                       (account, thread))
            self.enqueue(db, key, account, thread, "control", text)

    def recent_context(self, account, thread, key, sticky=False):
        """One small, owner-requested recap; ordinary auto conversations stay silent."""
        with self.store.db() as db:
            rows = db.execute("SELECT text FROM bridge_outbox WHERE account=? AND thread=? "
                              "AND kind IN ('customer','seller') AND status='stored' ORDER BY id DESC LIMIT 6",
                              (account, thread)).fetchall()
            blocks = []
            for row in reversed(rows):
                parts = list(telegram_parts(row['text'], 450))
                blocks.append(parts[0] + ('…' if len(parts) > 1 else ''))
            if blocks:
                self._blocks(db, account, thread, blocks, ('manual-history:open:' if sticky else 'manual-history:context:') + key)

    def take(self, account, thread, key):
        with self.store.db() as db:
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            route = self.get(account, thread)
            if not route:
                return None
            previous = route['mode']
            if previous != 'human':
                sources = list(self.store.meta('manual-pending:%s:%s' % (account, thread)).get('source_ids', []))
                for pending in db.execute("SELECT payload FROM drafts WHERE account=? AND thread=? AND status IN ('pending','approved') AND expires>?", (account, thread, time.time())).fetchall():
                    payload = json.loads(pending['payload'])
                    if (str(payload.get('buyer')), str(payload.get('advert'))) == (route['buyer'], route['advert']):
                        sources.extend(payload.get('source_ids', []))
                self.store.set_meta('manual-pending:%s:%s' % (account, thread), {'source_ids': list(dict.fromkeys(sources))[-100:]})
            self.mode(account, thread, 'human')
            if previous not in ('human', 'returning') or not route.get('human_since'):
                self.recent_context(account, thread, key)
            self.control(account, thread, status_text('human'), key)
            return self.get(account, thread)

    def refresh_controls(self, account):
        with self.store.db() as db:
            routes = db.execute("SELECT h.* FROM handoffs h WHERE h.account=? AND h.topic_id IS NOT NULL AND (h.mode IN ('human','waiting','opening','paused','blocked','closed') OR EXISTS(SELECT 1 FROM chats c WHERE c.account=h.account AND c.thread=h.thread))", (account,)).fetchall()
        for route in routes:
            self.control(account, route["thread"], status_text(route["mode"]),
                         "interface:v9:%s:%s" % (account, route["thread"]))

    def capture(self, account, thread, count, history, latest=None):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE handoffs SET mode='bot',last_count=?,updated=? "
                                 "WHERE account=? AND thread=? AND mode='waiting'", (count, time.time(), account, thread)).rowcount
            if not changed:
                return
            db.execute("UPDATE handoffs SET human_since=0,human_until=0,attention=1 WHERE account=? AND thread=?", (account, thread))
            db.execute("UPDATE handoffs SET topic_state='new' WHERE account=? AND thread=? AND topic_id IS NULL", (account, thread))
            key = "start:%s:%s:%s" % (account, thread, count)
            self.enqueue(db, key, account, thread, "control", status_text("bot"))
            self.enqueue(db, key + ":attention", account, thread, "attention", "🙋 Потрібна твоя допомога. Repliq продовжує відповідати, поки ти не натиснеш «я».\nПроблемне звернення — нижче.")
            wanted = {str(value).casefold() for value in latest} if isinstance(latest, (list, tuple, set)) else set()
            if not wanted:
                # The last bot message may be the handoff acknowledgement. Include only
                # the client's most recent run, never the entire sales conversation.
                tail = list(history)
                while tail and tail[-1].get('type') != 'received':
                    tail.pop()
                for message in reversed(tail):
                    if message.get('type') != 'received':
                        break
                    wanted.add(message['_id'].casefold())
            problem = [m for m in history if m.get('type') == 'received' and m['_id'].casefold() in wanted]
            self._history(db, account, thread, problem, key + ':problem')
        self.store.invalidate(account, thread, "Власника сповіщено про запит допомоги")

    def mark_sent(self, account, thread, message, speaker, seen=False):
        with self.store.db() as db:
            for field in ("uuid", "id"):
                key = message.get(field)
                if key is not None:
                    key = str(key).casefold()
                    db.execute("INSERT OR REPLACE INTO olx_sent VALUES(?,?,?,?,?)",
                               (account, thread, key, speaker, time.time()))
                    if seen:
                        db.execute("INSERT OR IGNORE INTO bridge_seen VALUES(?,?,?)", (account, thread, key))

    def quoted(self, db, account, thread, message):
        if message.get("type") == "received":
            speaker = "👤 Клієнт"
        else:
            ids = [str(message.get(k, "")).casefold() for k in ("uuid", "id", "_id")]
            row = db.execute("SELECT speaker FROM olx_sent WHERE account=? AND thread=? AND message IN (?,?,?)",
                             (account, thread, *ids)).fetchone()
            speaker = {"bot": "🤖 Бот", "owner": "✍️ Ти"}.get(row[0] if row else None, "💬 Продавець OLX")
        return "%s\n%s" % (speaker, display_message(message))

    def _history(self, db, account, thread, history, key, unseen_only=False):
        blocks = []
        for message in history:
            if unseen_only and db.execute("SELECT 1 FROM bridge_seen WHERE account=? AND thread=? AND message=?",
                                          (account, thread, message["_id"].casefold())).fetchone():
                continue
            text = self.quoted(db, account, thread, message)
            label, body = text.split("\n", 1)
            blocks.extend(label + "\n" + part for part in telegram_parts(body, 3300))
            for field in ("_id", "uuid", "id"):
                if message.get(field) is not None:
                    db.execute("INSERT OR IGNORE INTO bridge_seen VALUES(?,?,?)",
                               (account, thread, str(message[field]).casefold()))
        self._blocks(db, account, thread, blocks, key)

    def _blocks(self, db, account, thread, blocks, key):
        batch, units, part = [], 0, 0
        for block in blocks:
            width = utf16_length(block) + (2 if batch else 0)
            if batch and (units + width > 3400 or len(batch) >= 40):
                self.enqueue(db, "%s:%s" % (key, part), account, thread, "history", json.dumps(batch, ensure_ascii=False))
                batch, units, part = [], 0, part + 1
            units += utf16_length(block) + (2 if batch else 0)
            batch.append(block)
        if batch:
            self.enqueue(db, "%s:%s" % (key, part), account, thread, "history", json.dumps(batch, ensure_ascii=False))

    def history(self, account, thread, history, key):
        with self.store.db() as db:
            self._history(db, account, thread, history, key)

    def problem(self, account, thread, history, source_ids):
        if not source_ids:
            return
        wanted = {str(value).casefold() for value in source_ids}
        selected = [m for m in history if m.get('type') == 'received' and m['_id'].casefold() in wanted]
        with self.store.db() as db:
            self._history(db, account, thread, selected, 'start:%s:%s:%s:problem' % (account, thread, source_ids[-1]))

    def _append(self, db, account, thread, message):
        key = message["_id"].casefold()
        aliases = [str(message.get(k, "")).casefold() for k in ("uuid", "id", "_id")]
        if db.execute("SELECT 1 FROM bridge_seen WHERE account=? AND thread=? AND message IN (?,?,?)",
                      (account, thread, *aliases)).fetchone():
            return
        seen = db.execute("INSERT OR IGNORE INTO bridge_seen VALUES(?,?,?)", (account, thread, key)).rowcount
        if not seen:
            return
        text = self.quoted(db, account, thread, message)
        label, body = text.split("\n", 1)
        route = db.execute('SELECT mode FROM handoffs WHERE account=? AND thread=?', (account, thread)).fetchone()
        for index, part in enumerate(telegram_parts(body, 3300)):
            event = "message:%s:%s:%s:%s" % (account, thread, key, index)
            self.enqueue(db, event, account, thread,
                         "customer" if message.get("type") == "received" else "seller", label + "\n" + part)
            if not route or route['mode'] == 'bot' or label.startswith('🤖'):
                db.execute("UPDATE bridge_outbox SET status='stored' WHERE event_key=? AND status='pending'", (event,))

    def append(self, account, thread, messages, count):
        with self.store.db() as db:
            for message in messages:
                self._append(db, account, thread, message)
            db.execute("UPDATE handoffs SET last_count=MAX(last_count,?),updated=? WHERE account=? AND thread=?",
                       (count, time.time(), account, thread))

    def recover(self, account, interrupted_auto=()):
        with self.store.db() as db:
            interrupted = {r['thread'] for r in db.execute("SELECT thread FROM bridge_replies WHERE account=? AND status='sending' AND kind='reply'", (account,))}
            for item in interrupted_auto:
                try:
                    payload = json.loads(item['payload'])
                except (ValueError, TypeError):
                    continue
                route = self.get(account, item['thread'])
                if route and (route['buyer'], route['advert']) == (str(payload.get('buyer')), str(payload.get('advert'))):
                    interrupted.add(item['thread'])
            db.execute("UPDATE handoffs SET human_since=updated,human_until=updated+? WHERE account=? AND mode IN ('human','opening') AND human_until=0", (HUMAN_IDLE_SECONDS, account))
            # Old versions queued ordinary bot conversations even for already opened topics.
            db.execute("UPDATE bridge_outbox SET status='stored' WHERE account=? AND status='pending' AND kind IN ('customer','seller') AND EXISTS(SELECT 1 FROM handoffs h WHERE h.account=bridge_outbox.account AND h.thread=bridge_outbox.thread AND (h.mode='bot' OR bridge_outbox.created<h.human_since))", (account,))
            db.execute("UPDATE bridge_outbox SET status='stored' WHERE account=? AND status='pending' AND kind='history' AND event_key NOT LIKE 'manual-history:%' AND EXISTS(SELECT 1 FROM handoffs h WHERE h.account=bridge_outbox.account AND h.thread=bridge_outbox.thread AND (h.mode='bot' OR bridge_outbox.created<h.human_since) AND NOT(h.attention=1 AND bridge_outbox.event_key LIKE 'start:%:problem:%'))", (account,))
            db.execute("UPDATE bridge_outbox SET status='uncertain' WHERE account=? AND status='sending'", (account,))
            db.execute("UPDATE bridge_replies SET status='uncertain',note='Перервано під час надсилання; перевір OLX' "
                       "WHERE account=? AND status='sending'", (account,))
            for thread in interrupted:
                self.mode(account, thread, 'blocked')
                self.control(account, thread, '⚠️ Надсилання перервалося під час перезапуску. Звір останню відповідь в OLX; автоматичного повтору немає.',
                             'interrupted-send:%s:%s:%s' % (account, thread, time.time_ns()))
            db.execute("UPDATE handoffs SET topic_state='uncertain' WHERE account=? AND topic_state='creating'", (account,))
            # Upgrade unsent TXT history to in-chat quotations without resending delivered history.
            old = db.execute("SELECT * FROM bridge_outbox WHERE account=? AND kind='document' AND status='pending'", (account,)).fetchall()
            for item in old:
                self._blocks(db, account, item["thread"], telegram_parts(item["text"]), "upgrade:" + item["event_key"])
                db.execute("UPDATE bridge_outbox SET status='cancelled' WHERE id=?", (item["id"],))
            # The old version handed every delivery choice straight to the owner.
            # Resume only successfully sent checkout offers, never an uncertain POST.
            for row in db.execute("SELECT thread,buyer,advert FROM handoffs WHERE account=? AND mode='waiting'", (account,)).fetchall():
                context = self.store.context(account, row["thread"], row["buyer"], row["advert"], 90)
                if context.get("checkout_offered") and not context.get("handoff_now"):
                    db.execute("UPDATE handoffs SET mode='bot' WHERE account=? AND thread=?", (account, row["thread"]))

    def reply(self, event_key, route, owner, text, kind="reply"):
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO bridge_replies VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (event_key, route["account"], route["thread"], route["group_id"], route["topic_id"],
                        owner, text, "queued", time.time(), "", kind))

    def next_reply(self, account):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM bridge_replies WHERE account=? AND status='queued' ORDER BY created LIMIT 1", (account,)).fetchone()
        return dict(row) if row else None

    def finish_reply(self, key, status, note="", expected="queued"):
        with self.store.db() as db:
            return db.execute("UPDATE bridge_replies SET status=?,note=? WHERE event_key=? AND status=?",
                              (status, note, key, expected)).rowcount == 1

    def route(self, group_id, topic_id, account):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM handoffs WHERE group_id=? AND topic_id=? AND account=?",
                             (group_id, topic_id, account)).fetchone()
        return dict(row) if row else None

    def mode(self, account, thread, mode, drain=False):
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET mode=?,updated=? WHERE account=? AND thread=?", (mode, time.time(), account, thread))
            if mode == "human":
                db.execute("UPDATE handoffs SET human_since=CASE WHEN human_until=0 THEN ? ELSE human_since END,human_until=? WHERE account=? AND thread=?",
                           (time.time(), time.time()+HUMAN_IDLE_SECONDS, account, thread))
                db.execute("UPDATE bridge_replies SET status='cancelled',note='Власник знову підключився' "
                           "WHERE account=? AND thread=? AND status='queued' AND kind IN ('return_bot','switch_bot')", (account, thread))
            elif mode == "bot":
                db.execute("UPDATE handoffs SET human_since=0,human_until=0,attention=0 WHERE account=? AND thread=?", (account, thread))
                db.execute("UPDATE bridge_outbox SET status='stored' WHERE account=? AND thread=? AND status='pending' AND (kind IN ('customer','seller','attention','prompt') OR (kind='history' AND event_key NOT LIKE 'manual-history:%'))", (account, thread))
                db.execute("UPDATE bridge_outbox SET status='stored' WHERE account=? AND thread=? AND status='pending' AND kind='history' AND event_key LIKE 'manual-history:context:%'", (account, thread))
            db.execute("UPDATE bridge_outbox SET status='cancelled' WHERE account=? AND thread=? AND status='pending' AND kind='control'",
                       (account, thread))
            if mode != "human" and not (mode == 'returning' and drain):
                waiting = db.execute("SELECT event_key FROM bridge_replies WHERE account=? AND thread=? AND status='queued' AND kind='reply'", (account, thread)).fetchall()
                db.execute("UPDATE bridge_replies SET status='cancelled',note='Зупинено власником' WHERE account=? AND thread=? AND status='queued' AND kind NOT IN ('return_bot','switch_bot')", (account, thread))
                if waiting:
                    self.enqueue(db, 'cancelled-reply:' + waiting[-1]['event_key'], account, thread, 'notice',
                                 'Ручні повідомлення в черзі (%s) не надіслано: режим змінено. Твій текст залишився вище в Telegram.' % len(waiting))
            if mode in ("closed", "blocked"):
                db.execute("UPDATE bridge_outbox SET status='cancelled' WHERE account=? AND thread=? AND status='pending' "
                           "AND kind NOT IN ('control','notice')", (account, thread))
        self.store.invalidate(account, thread, "Діалогом керує власник")

    def renew_human(self, account, thread):
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET human_until=? WHERE account=? AND thread=? AND mode='human'", (time.time()+HUMAN_IDLE_SECONDS, account, thread))

    def expired_humans(self, account):
        with self.store.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM handoffs WHERE account=? AND mode IN ('human','opening') AND human_until>0 AND human_until<=? ORDER BY human_until LIMIT 20", (account, time.time()))]

    def link_token(self, route):
        token = hashlib.sha256(json.dumps([route[k] for k in ("account", "thread", "buyer", "advert")]).encode()).hexdigest()[:24]
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO conversation_links VALUES(?,?,?,?,?)", (token, route["account"], route["thread"], route["buyer"], route["advert"]))
        return token

    def request_topic(self, account, token, key=None):
        with self.store.db() as db:
            row = db.execute("SELECT h.* FROM conversation_links l JOIN handoffs h ON h.account=l.account AND h.thread=l.thread AND h.buyer=l.buyer AND h.advert=l.advert WHERE l.account=? AND l.token=?", (account, token)).fetchone()
            if row and row["topic_id"] is None:
                db.execute("UPDATE handoffs SET topic_state='new' WHERE account=? AND thread=? AND topic_state IN ('dormant','failed')", (account, row["thread"]))
            if row:
                event = key or 'open:%s:%s' % (token, row['last_count'])
                self.control(account, row['thread'], status_text(row['mode']), 'open-control:' + event)
                self.recent_context(account, row['thread'], event, sticky=True)
                return self.get(account, row['thread'])
        return None

    def compact_pending(self, account, thread):
        """Publish a dormant conversation in readable batches, not hundreds of single posts."""
        with self.store.db() as db:
            while True:
                rows = db.execute("SELECT id,text FROM bridge_outbox WHERE account=? AND thread=? AND status='pending' AND kind IN ('customer','seller','owner') ORDER BY id LIMIT 500", (account, thread)).fetchall()
                if not rows:
                    return
                self._blocks(db, account, thread, [row['text'] for row in rows], 'compact:%s:%s:%s:%s' % (account, thread, rows[0]['id'], rows[-1]['id']))
                db.executemany("UPDATE bridge_outbox SET status='cancelled' WHERE id=? AND status='pending'", [(row['id'],) for row in rows])


def status_text(mode, route=None):
    if mode == "bot":
        return "auto · відповідає Repliq\nЩоб підключитися, просто напиши клієнту в цій темі або натисни «я». Оплата й відправлення — у Ledgerly."
    if mode == "returning":
        return "⏳ Повертаю auto\nСпершу завершу надсилання твоїх повідомлень у черзі, потім відповідає бот."
    if mode == "paused":
        return "⏸ Відповіді призупинені\nНові повідомлення клієнта видно в цій темі."
    if mode == "blocked":
        return "⚠️ Перевір останні повідомлення в OLX\nРоботу зупинено для ручної перевірки. Віднови її кнопкою нижче, коли звіриш діалог."
    if mode == "closed":
        return "🔒 Діалог зупинено. Щоб знову відповідати на нові звернення, увімкни бота кнопкою нижче."
    until = (route or {}).get('human_until', 0)
    deadline = ('\nПовернення auto: ' + datetime.fromtimestamp(until, ZoneInfo('Europe/Kyiv')).strftime('%H:%M') +
                ' — після 40 хвилин без твоїх відповідей.') if until else '\nЧерез 40 хвилин без твоїх відповідей Repliq повернеться сам.'
    return 'я · відповідаєш ти\nПиши тут — повідомлення підуть клієнту. «auto» повертає бота зі збереженням замовлення.' + deadline


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def quote_entities(blocks):
    text, entities = "", []
    for block in blocks:
        if text:
            text += "\n\n"
        offset, width = utf16_length(text), utf16_length(block)
        if width:
            entities.append({"type": "blockquote", "offset": offset, "length": width})
            label = block.split("\n", 1)[0]
            entities.append({"type": "bold", "offset": offset, "length": utf16_length(label)})
        text += block
    return text, entities


def display_message(message):
    text = str(message.get("text") or "")
    attachments = message.get("attachments")
    if attachments:
        from urllib.parse import urlsplit
        links = []
        for item in attachments if isinstance(attachments, list) else [attachments]:
            url = item.get("url") if isinstance(item, dict) else None
            if isinstance(url, str):
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                    links.append(url)
        text += "\n[Вкладення OLX]" + ("\n" + "\n".join(links) if links else " Переглянь в OLX.")
    return text or "[Без тексту]"


def transcript(history):
    return "\n\n".join("%s | %s | %s\n%s" % (m.get("created_at", ""),
                           "Клієнт" if m.get("type") == "received" else "Продавець / OLX", m["_id"], display_message(m)) for m in history)


def telegram_parts(text, limit=3400):
    part, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += width
    if part:
        yield "".join(part)
