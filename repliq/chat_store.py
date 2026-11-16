"""Private SQLite queue, isolated by OLX account + conversation + draft ID."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import AppError


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise AppError("Невідома версія локальної бази Repliq.")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chats (
                    account TEXT NOT NULL, thread TEXT NOT NULL, buyer TEXT NOT NULL,
                    advert TEXT NOT NULL, context TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(account, thread)
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                    session TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS draft_queue ON drafts(account, session, status, created);
                CREATE INDEX IF NOT EXISTS draft_thread ON drafts(account, thread, status);
                CREATE INDEX IF NOT EXISTS draft_expiration ON drafts(status, expires);
                CREATE INDEX IF NOT EXISTS draft_retention ON drafts(created);
                CREATE INDEX IF NOT EXISTS chat_retention ON chats(updated);
                CREATE TABLE IF NOT EXISTS conversation_orders (
                    account TEXT NOT NULL, thread TEXT NOT NULL, invoice TEXT NOT NULL,
                    buyer TEXT NOT NULL, advert TEXT NOT NULL, context TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(account,thread,invoice)
                );
                CREATE INDEX IF NOT EXISTS conversation_order_retention ON conversation_orders(updated);
                CREATE TABLE IF NOT EXISTS handled (
                    account TEXT NOT NULL, thread TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(account, thread, message)
                );
                CREATE INDEX IF NOT EXISTS handled_retention ON handled(created);
                PRAGMA user_version=1;
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = getattr(self._local, "connection", None)
        if db is None:
            db = sqlite3.connect(str(self.path), timeout=3)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA cache_size=-512")
            self._local.connection = db
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        savepoint = "repliq_nested_%s" % depth
        try:
            if depth:
                db.execute("SAVEPOINT " + savepoint)
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK TO " + savepoint)
                    db.execute("RELEASE " + savepoint)
                    raise
                else:
                    db.execute("RELEASE " + savepoint)
            else:
                with db:
                    yield db
        finally:
            self._local.depth = depth

    def close_current(self):
        """Each thread closes only its own SQLite connection."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            if getattr(self._local, "depth", 0):
                raise RuntimeError("Cannot close SQLite during a transaction")
            connection.close()
            del self._local.connection

    def set_meta(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def meta(self, key):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def begin(self, account, session):
        with self.db() as db:
            db.execute("UPDATE drafts SET status='stale', note='Новий запуск: старі чернетки скасовано' "
                       "WHERE account=? AND status IN ('pending','approved')", (account,))
            db.execute("UPDATE drafts SET status='uncertain', note='Процес перервався під час надсилання; перевір OLX' "
                       "WHERE account=? AND status='sending'", (account,))

    def context(self, account, thread, buyer, advert, memory_days):
        with self.db() as db:
            row = db.execute("SELECT * FROM chats WHERE account=? AND thread=?", (account, thread)).fetchone()
        if not row or row["buyer"] != buyer or row["advert"] != advert or row["updated"] < time.time() - memory_days * 86400:
            return {}
        return json.loads(row["context"])

    def save_context(self, account, thread, buyer, advert, context):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?, ?, ?)",
                       (account, thread, buyer, advert, json.dumps(context, ensure_ascii=False), time.time()))

    def archive_order(self, account, thread, buyer, advert, context):
        if not context.get("invoice_id"):
            return
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO conversation_orders VALUES(?,?,?,?,?,?,?)",
                       (account, thread, context["invoice_id"], buyer, advert, json.dumps(context, ensure_ascii=False), time.time()))

    def order_context(self, account, thread, buyer, advert, invoice, memory_days=90):
        current = self.context(account, thread, buyer, advert, memory_days)
        if current.get("invoice_id") == invoice:
            return current
        with self.db() as db:
            row = db.execute("SELECT context FROM conversation_orders WHERE account=? AND thread=? AND buyer=? AND advert=? AND invoice=? AND updated>?",
                             (account, thread, buyer, advert, invoice, time.time() - memory_days * 86400)).fetchone()
        return json.loads(row[0]) if row else {}

    def latest_order(self, account, thread, buyer, advert, memory_days=90):
        with self.db() as db:
            row = db.execute("SELECT context FROM conversation_orders WHERE account=? AND thread=? AND buyer=? AND advert=? AND updated>? ORDER BY updated DESC LIMIT 1",
                             (account, thread, buyer, advert, time.time() - memory_days * 86400)).fetchone()
        return json.loads(row[0]) if row else {}

    def handled(self, account, thread, message):
        with self.db() as db:
            return db.execute("SELECT 1 FROM handled WHERE account=? AND thread=? AND message=?",
                              (account, thread, message)).fetchone() is not None

    def invalidate(self, account, thread, note="Діалог змінився"):
        with self.db() as db:
            db.execute("UPDATE drafts SET status='stale', note=? WHERE account=? AND thread=? "
                       "AND status IN ('pending','approved')", (note, account, thread))

    def stage(self, account, session, thread, payload, ttl, maximum):
        draft_id, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM drafts WHERE account=? AND status IN ('pending','approved')", (account,)).fetchone()[0]
            if count >= maximum:
                return None
            latest = payload["source_ids"][-1]
            if db.execute("SELECT 1 FROM handled WHERE account=? AND thread=? AND message=?", (account, thread, latest)).fetchone():
                return "handled"
            db.execute("UPDATE drafts SET status='stale', note='Є новіша чернетка' WHERE account=? AND thread=? "
                       "AND status IN ('pending','approved')", (account, thread))
            db.execute("INSERT INTO drafts(id,account,thread,session,status,payload,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                       (draft_id, account, thread, session, "pending", json.dumps(payload, ensure_ascii=False), now, now + ttl))
            db.executemany("INSERT OR IGNORE INTO handled VALUES(?,?,?,?)", [(account, thread, mid, now) for mid in payload["source_ids"]])
        return draft_id

    def next(self, account, session, status="pending"):
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE account=? AND session=? AND status=? AND expires>? "
                             "ORDER BY created LIMIT 1", (account, session, status, time.time())).fetchone()
        if row:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            return item
        return None

    def decide(self, draft_id, account, session, approve, replacement=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM drafts WHERE id=? AND account=? AND session=? AND status='pending' AND expires>?",
                             (draft_id, account, session, time.time())).fetchone()
            if not row:
                return False
            payload = json.loads(row[0])
            if replacement is not None:
                if not isinstance(replacement, str) or not 1 <= len(replacement.strip()) <= 2000:
                    raise AppError("Відповідь має містити від 1 до 2000 символів.")
                payload["text"] = replacement.strip()
                payload["edited"] = True
            db.execute("UPDATE drafts SET status=?, payload=? WHERE id=?",
                       ("approved" if approve else "skipped", json.dumps(payload, ensure_ascii=False), draft_id))
        return True

    def transition(self, draft_id, expected, status, note=""):
        with self.db() as db:
            result = db.execute("UPDATE drafts SET status=?, note=? WHERE id=? AND status=? "
                                "AND (? != 'sending' OR expires>?)",
                                (status, note, draft_id, expected, status, time.time()))
        return result.rowcount == 1

    def cleanup(self, days, purge=True):
        now = time.time()
        with self.db() as db:
            db.execute("UPDATE drafts SET status='expired', note='Час підтвердження минув' "
                       "WHERE status IN ('pending','approved') AND expires<=?", (now,))
            if not purge:
                return
            db.execute("DELETE FROM drafts WHERE status NOT IN ('pending','approved','sending','uncertain') AND created<?", (now - days * 86400,))
            db.execute("DELETE FROM handled WHERE created<?", (now - days * 86400,))
            db.execute("DELETE FROM chats WHERE updated<?", (now - days * 86400,))
            db.execute("DELETE FROM conversation_orders WHERE updated<?", (now - max(90, days) * 86400,))

    def report(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id, account, thread, status, note FROM drafts WHERE status IN "
                                               "('uncertain','sending') ORDER BY created DESC LIMIT 30")]
