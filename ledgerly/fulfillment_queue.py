"""Private, local IPC between Repliq and Ledgerly. No Telegram bot-to-bot messages."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ParcelQueue:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS parcels (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                    invoice_id TEXT NOT NULL, invoice_key TEXT NOT NULL, content_key TEXT NOT NULL,
                    payload TEXT NOT NULL, amendment TEXT,
                    nova_status TEXT NOT NULL DEFAULT 'pending', ttn TEXT UNIQUE, nova_ref TEXT,
                    ledgerly_status TEXT NOT NULL DEFAULT 'pending', submission_id TEXT UNIQUE,
                    message_id INTEGER, paid INTEGER NOT NULL DEFAULT 0, paid_by INTEGER,
                    payment_revision INTEGER NOT NULL DEFAULT 0, notified_revision INTEGER NOT NULL DEFAULT -1,
                    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(account,thread,invoice_id)
                );
                CREATE INDEX IF NOT EXISTS nova_jobs ON parcels(nova_status,created);
                CREATE INDEX IF NOT EXISTS ledgerly_jobs ON parcels(ledgerly_status,nova_status,created);
                CREATE INDEX IF NOT EXISTS parcel_updates ON parcels(updated,id);
                CREATE INDEX IF NOT EXISTS parcel_notifications ON parcels(updated)
                  WHERE message_id IS NOT NULL AND notified_revision!=payment_revision;
                CREATE TABLE IF NOT EXISTS bridge_consumers (
                    name TEXT PRIMARY KEY, version INTEGER NOT NULL, heartbeat REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_archives (
                    parcel_id TEXT NOT NULL, invoice_key TEXT NOT NULL, url_hash TEXT NOT NULL,
                    source_url TEXT NOT NULL, state TEXT NOT NULL, permalink TEXT NOT NULL DEFAULT '',
                    message_id INTEGER, error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                    PRIMARY KEY(parcel_id,invoice_key,url_hash)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(parcels)")}
            for name in ("nova_profile", "nova_profile_key"):
                if name not in columns:
                    db.execute("ALTER TABLE parcels ADD COLUMN " + name + " TEXT")
            if "receipt_links" not in columns:
                db.execute("ALTER TABLE parcels ADD COLUMN receipt_links TEXT NOT NULL DEFAULT '[]'")
            for name, kind in (("review_message_id", "INTEGER"), ("review_state", "TEXT NOT NULL DEFAULT 'new'"), ("review_signature", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute("ALTER TABLE parcels ADD COLUMN " + name + " " + kind)
            for name, kind in (("cancel_state", "TEXT NOT NULL DEFAULT ''"), ("cancel_error", "TEXT NOT NULL DEFAULT ''"), ("cancel_due", "REAL NOT NULL DEFAULT 0"), ("cancel_attempts", "INTEGER NOT NULL DEFAULT 0"), ("nova_api_fingerprint", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute("ALTER TABLE parcels ADD COLUMN " + name + " " + kind)
            for name, kind in (("delivery_state", "TEXT NOT NULL DEFAULT ''"), ("delivery_checked", "REAL NOT NULL DEFAULT 0"),
                               ("delivery_next", "REAL NOT NULL DEFAULT 0"), ("delivery_cost_cents", "INTEGER"),
                               ("delivery_outstanding_cents", "INTEGER"), ("delivery_reason", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute("ALTER TABLE parcels ADD COLUMN " + name + " " + kind)
            db.execute("CREATE INDEX IF NOT EXISTS carrier_payment_jobs ON parcels(nova_status,delivery_next)")
            for name, kind in (("payment_source", "TEXT NOT NULL DEFAULT ''"),
                               ("payment_manual_lock", "INTEGER NOT NULL DEFAULT 0"),
                               ("bank_payment", "TEXT NOT NULL DEFAULT '{}'"),
                               ("payment_review_revision", "INTEGER NOT NULL DEFAULT 0"),
                               ("payment_review_receipts", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in columns:
                    db.execute("ALTER TABLE parcels ADD COLUMN " + name + " " + kind)
            db.execute("CREATE INDEX IF NOT EXISTS parcel_cancellation ON parcels(cancel_state,cancel_due)")
        os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            try:
                os.chmod(sidecar, 0o600)
            except FileNotFoundError:
                pass  # SQLite removes these when the last connection closes.

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + 3
            while True:
                try:
                    if str(db.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'wal':
                        db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as error:
                    if not any(word in str(error).lower() for word in ('locked','busy')) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.03)
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        return dict(row, data=json.loads(row["payload"])) if row else None

    def get(self, parcel_id):
        with self.db() as db:
            return self.decode(db.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone())

    def by_submission(self, submission):
        with self.db() as db:
            return self.decode(db.execute("SELECT * FROM parcels WHERE submission_id=?", (submission,)).fetchone())

    def put(self, payload):
        identity = [payload["account"], payload["thread"], payload["invoice_id"]]
        parcel_id, key = digest(identity)[:24], digest(payload)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone()
            if not old:
                db.execute("INSERT INTO parcels(id,account,thread,invoice_id,invoice_key,content_key,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                           (parcel_id, *identity, payload["invoice_key"], key, json.dumps(payload, ensure_ascii=False), time.time(), time.time()))
            elif old["content_key"] == key and old["nova_status"] == "invalid" and old["error"] == "order_requires_confirmation" and not old["ttn"]:
                db.execute("UPDATE parcels SET nova_status='pending',error='',updated=? WHERE id=?", (time.time(), parcel_id))
            elif old["content_key"] != key:
                if old["nova_status"] in ("pending", "resolving", "invalid") and not old["ttn"]:
                    same_invoice = old["invoice_key"] == payload["invoice_key"]
                    db.execute("UPDATE parcels SET content_key=?,payload=?,invoice_key=?,nova_status='pending',error='',paid=?,paid_by=?,payment_source=?,payment_manual_lock=?,bank_payment=?,updated=? WHERE id=?",
                               (key, json.dumps(payload, ensure_ascii=False), payload["invoice_key"], old["paid"] if same_invoice else 0,
                                old["paid_by"] if same_invoice else None, old['payment_source'] if same_invoice else '',
                                old['payment_manual_lock'] if same_invoice else 0, old['bank_payment'] if same_invoice else '{}', time.time(), parcel_id))
                else:
                    db.execute("UPDATE parcels SET amendment=?,nova_status='needs_review',error='order_changed',payment_revision=payment_revision+1,updated=? WHERE id=?",
                               (json.dumps(payload, ensure_ascii=False), time.time(), parcel_id))
        return self.get(parcel_id)

    def announce_consumer(self, version=2):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO bridge_consumers VALUES(?,?,?)', ('ledgerly', version, time.time()))

    def consumer_ready(self):
        with self.db() as db:
            row = db.execute("SELECT version,heartbeat FROM bridge_consumers WHERE name='ledgerly'").fetchone()
            return bool(row and row['version'] >= 2 and time.time()-180 <= row['heartbeat'] <= time.time()+5)

    def claim_nova(self, allow_carrier=True):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM parcels WHERE nova_status='pending' AND (? OR COALESCE(json_extract(payload,'$.payment_policy'),'')!='carrier_delivery') ORDER BY created LIMIT 1", (int(allow_carrier),)).fetchone()
            if row:
                db.execute("UPDATE parcels SET nova_status='resolving',updated=? WHERE id=?", (time.time(), row["id"]))
            return self.decode(row)

    def before_write(self, parcel_id, key, state):
        if state not in ("creating_contact", "creating"):
            raise ValueError("Unsupported carrier write")
        with self.db() as db:
            return db.execute("UPDATE parcels SET nova_status=?,updated=? WHERE id=? AND content_key=? AND nova_status IN ('resolving','creating_contact')",
                              (state, time.time(), parcel_id, key)).rowcount == 1

    def profile(self, parcel_id, content_key, name, settings_key, api_fingerprint=''):
        with self.db() as db:
            return db.execute("UPDATE parcels SET nova_profile=?,nova_profile_key=?,nova_api_fingerprint=? WHERE id=? AND content_key=? AND nova_status='resolving'",
                              (name, settings_key, api_fingerprint, parcel_id, content_key)).rowcount == 1

    def carrier_done(self, parcel_id, key, ttn, ref):
        with self.db() as db:
            return db.execute("UPDATE parcels SET payment_revision=payment_revision+CASE WHEN nova_status='cancelled' THEN 1 ELSE 0 END,cancel_state=CASE WHEN nova_status='cancelled' AND cancel_state='waiting' THEN 'pending' ELSE cancel_state END,nova_status=CASE WHEN nova_status IN ('needs_review','cancelled') THEN nova_status ELSE 'created' END,ttn=?,nova_ref=?,updated=? WHERE id=? AND content_key=? AND nova_status IN ('creating','needs_review','cancelled')",
                              (ttn, ref, time.time(), parcel_id, key)).rowcount == 1

    def fail(self, parcel_id, error, uncertain=False):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status=?,error=?,updated=? WHERE id=? AND nova_status NOT IN ('needs_review','cancelled') AND error!='order_requires_confirmation'",
                       ("uncertain" if uncertain else "invalid", error[:120], time.time(), parcel_id))

    def recover_carrier(self):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status='pending' WHERE nova_status='resolving'")
            db.execute("UPDATE parcels SET nova_status='uncertain',error='carrier_result_unknown' WHERE nova_status IN ('creating','creating_contact')")
            db.execute("UPDATE parcels SET cancel_state='pending' WHERE cancel_state='checking'")
            db.execute("UPDATE parcels SET cancel_state='uncertain',cancel_error='delete_result_unknown',payment_revision=payment_revision+1 WHERE cancel_state='deleting'")
            db.execute("UPDATE parcels SET cancel_state='uncertain',cancel_error='creation_result_unknown_after_cancel',payment_revision=payment_revision+1 WHERE cancel_state='waiting' AND ttn IS NULL")

    def claim_ledgerly(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM parcels WHERE nova_status='created' AND ledgerly_status IN ('pending','preparing') ORDER BY created LIMIT 1").fetchone()
            if row:
                submission = digest([row["id"], "ledgerly"])[:16]
                db.execute("UPDATE parcels SET ledgerly_status='preparing',submission_id=?,updated=? WHERE id=?", (submission, time.time(), row["id"]))
                row = db.execute("SELECT * FROM parcels WHERE id=?", (row["id"],)).fetchone()
            return self.decode(row)

    def before_publish(self, parcel_id):
        with self.db() as db:
            return db.execute("UPDATE parcels SET ledgerly_status='sending',updated=? WHERE id=? AND nova_status='created' AND ledgerly_status='preparing'",
                              (time.time(), parcel_id)).rowcount == 1

    def delivered(self, parcel_id, message_id):
        with self.db() as db:
            db.execute("UPDATE parcels SET ledgerly_status='sent',message_id=?,review_state='merged',updated=? WHERE id=?", (message_id, time.time(), parcel_id))

    def delivery_failed(self, parcel_id, retry=False):
        with self.db() as db:
            db.execute("UPDATE parcels SET ledgerly_status=?,error='ledgerly_delivery_unknown',updated=? WHERE id=?",
                       ("pending" if retry else "uncertain", time.time(), parcel_id))

    def recover_ledgerly(self):
        with self.db() as db:
            db.execute("UPDATE parcels SET ledgerly_status='uncertain',error='ledgerly_delivery_unknown' WHERE ledgerly_status='sending'")
            db.execute("UPDATE parcels SET review_state='uncertain' WHERE review_state='sending'")

    def claim_review(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM parcels WHERE review_state='new' AND ledgerly_status='pending' AND nova_status NOT IN ('created','cancelled') AND created BETWEEN ? AND ? ORDER BY created LIMIT 1", (time.time()-86400, time.time()-5)).fetchone()
            if row:
                db.execute("UPDATE parcels SET review_state='sending' WHERE id=?", (row['id'],))
            return self.decode(row)

    def review_result(self, parcel_id, message_id=None, signature=''):
        with self.db() as db:
            db.execute("UPDATE parcels SET review_state=?,review_message_id=?,review_signature=? WHERE id=?",
                       ('sent' if message_id else 'uncertain', message_id, signature, parcel_id))

    def review_updates(self):
        with self.db() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM parcels WHERE review_state='sent' AND review_message_id IS NOT NULL AND ledgerly_status IN ('pending','cancelled') ORDER BY updated DESC LIMIT 20")]

    def payment(self, parcel_id, invoice_key, owner, verified):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM parcels WHERE id=? AND invoice_key=?', (parcel_id, invoice_key)).fetchone()
            if not row or row['nova_status'] in ('needs_review', 'cancelled') or row['ledgerly_status'] in ('shipped', 'cancelled'):
                return False
            from carrier_payment import direct_delivery
            carrier = direct_delivery(json.loads(row['payload']))
            request_receipt = bool(owner and not verified and not carrier)
            if (request_receipt and not row['paid'] and row['payment_manual_lock'] and
                    row['payment_source'] == 'owner' and row['payment_review_revision'] > 0 and
                    row['payment_review_receipts'] == row['receipt_links']):
                return True  # Repeated clicks for the same evidence do not notify twice.
            if carrier:
                if not owner or row['nova_status'] != 'created' or row['ledgerly_status'] == 'shipped':
                    return False
                db.execute("UPDATE parcels SET delivery_state=?,delivery_checked=?,delivery_next=?,delivery_reason='owner_attestation' WHERE id=?",
                           ('manual' if verified else 'waiting', time.time() if verified else 0, time.time()+15, parcel_id))
            db.execute("UPDATE parcels SET paid=?,paid_by=?,payment_source='owner',payment_manual_lock=1,payment_revision=payment_revision+1,"
                       "payment_review_revision=payment_review_revision+?,payment_review_receipts=CASE WHEN ? THEN receipt_links ELSE payment_review_receipts END,updated=? WHERE id=?",
                       (int(verified), owner, int(request_receipt), int(request_receipt), time.time(), parcel_id))
            return True

    def request_delivery_check(self, parcel_id, key):
        with self.db() as db:
            return db.execute("UPDATE parcels SET delivery_next=0 WHERE id=? AND invoice_key=? AND nova_status='created' AND ledgerly_status!='cancelled'",
                              (parcel_id, key)).rowcount == 1

    def claim_delivery_checks(self, limit=25):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM parcels WHERE nova_status='created' AND ledgerly_status NOT IN ('cancelled','shipped') AND delivery_next<=? AND created>? AND json_extract(payload,'$.payment_policy')='carrier_delivery' ORDER BY delivery_next,created LIMIT ?",
                              (time.time(), time.time()-7*86400, min(25, max(1, limit)))).fetchall()
            for row in rows:
                db.execute('UPDATE parcels SET delivery_next=? WHERE id=?', (time.time()+30, row['id']))
            return [self.decode(row) for row in rows]

    def delivery_result(self, expected, result, error=False):
        from carrier_payment import FRESH_SECONDS
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM parcels WHERE id=? AND content_key=? AND ttn=? AND nova_status='created' AND ledgerly_status NOT IN ('cancelled','shipped')",
                             (expected['id'], expected['content_key'], expected['ttn'])).fetchone()
            if not row or row['nova_api_fingerprint'] != expected['nova_api_fingerprint']:
                return False
            state = result.get('state', 'unknown')
            checked = row['delivery_checked'] if error else now
            cost = row['delivery_cost_cents'] if error else result.get('cost_cents')
            due = row['delivery_outstanding_cents'] if error else result.get('outstanding_cents')
            reason = str(result.get('reason', 'carrier_unavailable'))[:100]
            # A temporary outage must neither invent payment nor extend its freshness.
            keep = row['delivery_state']=='manual' or row['delivery_state']=='paid' and now-row['delivery_checked'] <= FRESH_SECONDS
            identity_failed = reason in ('tracking_identity_mismatch','tracking_reference_mismatch','tracking_phone_mismatch','original_profile_unavailable')
            if state != 'paid' and state != 'unavailable' and not identity_failed and keep and (error or row['delivery_state']=='manual'):
                state, checked = row['delivery_state'], row['delivery_checked']
                cost, due = row['delivery_cost_cents'], row['delivery_outstanding_cents']
            elif state != 'paid' and row['paid']:
                state = 'revoked' if state != 'unavailable' else state
            paid = int(state in ('paid','manual'))
            changed = (state, paid, cost, due) != (row['delivery_state'], row['paid'], row['delivery_cost_cents'], row['delivery_outstanding_cents'])
            interval = 15 if now-row['created'] < 120 else 60
            if error:
                interval = 120
            db.execute("UPDATE parcels SET delivery_state=?,delivery_checked=?,delivery_next=?,delivery_cost_cents=?,delivery_outstanding_cents=?,delivery_reason=?,paid=?,paid_by=?,payment_revision=payment_revision+?,updated=CASE WHEN ? THEN ? ELSE updated END WHERE id=?",
                       (state, checked, now+interval, cost, due, reason, paid,
                        row['paid_by'] if state=='manual' else None, int(changed), int(changed), now, row['id']))
            return True

    def attach_receipts(self, parcel_id, links):
        from urllib.parse import urlsplit
        clean = []
        for url in links[-3:]:
            if not isinstance(url, str) or len(url) > 2000:
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
                clean.append(url)
        encoded = json.dumps(clean)
        with self.db() as db:
            # Receipt metadata never changes goods, payment verification or an existing label.
            db.execute("UPDATE parcels SET receipt_links=?,payment_revision=payment_revision+1,updated=? WHERE id=? AND receipt_links!=?", (encoded, time.time(), parcel_id, encoded))

    def updates(self, account=None, after=0):
        with self.db() as db:
            rows = db.execute("SELECT * FROM parcels WHERE (? IS NULL OR account=?) AND updated>=? ORDER BY updated,id LIMIT 200", (account, account, after)).fetchall()
            return [self.decode(row) for row in rows]

    def notifications(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM parcels WHERE message_id IS NOT NULL AND notified_revision!=payment_revision AND ledgerly_status IN ('sent','confirmed','shipped','cancelled') ORDER BY updated LIMIT 20").fetchall()
            return [self.decode(row) for row in rows]

    def restart_before_document(self, parcel_id):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status='pending',nova_profile=NULL,nova_profile_key=NULL WHERE id=? AND nova_status='creating_contact'", (parcel_id,))

    def retry_configuration(self, profile):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status='pending',error='',updated=? WHERE nova_status='invalid' AND ttn IS NULL AND error IN (?, 'sender_not_configured','profile_changed_during_setup') AND created>?",
                       (time.time(), 'active_profile_not_configured:' + str(profile), time.time()-86400))

    def reap_leases(self):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status='pending' WHERE nova_status='resolving' AND updated<?", (time.time()-120,))
            db.execute("UPDATE parcels SET nova_status='uncertain',error='carrier_result_unknown',updated=? WHERE nova_status IN ('creating','creating_contact') AND updated<?", (time.time(),time.time()-120))

    def cancel(self, account, thread, invoice_id):
        with self.db() as db:
            return db.execute("UPDATE parcels SET cancel_state=CASE WHEN ttn IS NOT NULL THEN 'pending' WHEN nova_status='creating' THEN 'waiting' ELSE 'not_needed' END,cancel_due=0,cancel_error='',nova_status='cancelled',ledgerly_status='cancelled',payment_revision=payment_revision+1,updated=? WHERE account=? AND thread=? AND invoice_id=? AND ledgerly_status NOT IN ('shipped','cancelled')",
                              (time.time(), account, thread, invoice_id)).rowcount == 1

    def claim_cancellation(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM parcels WHERE ledgerly_status='cancelled' AND nova_status='cancelled' AND cancel_state='pending' AND cancel_due<=? AND ttn IS NOT NULL ORDER BY cancel_due,created LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE parcels SET cancel_state='checking',cancel_attempts=cancel_attempts+1 WHERE id=?", (row['id'],))
            return self.decode(row)

    def before_delete(self, parcel_id):
        with self.db() as db:
            return db.execute("UPDATE parcels SET cancel_state='deleting' WHERE id=? AND cancel_state='checking' AND ledgerly_status='cancelled'", (parcel_id,)).rowcount == 1

    def cancellation_result(self, parcel_id, state, error=''):
        if state not in ('deleted','blocked','uncertain','pending','not_needed'):
            raise ValueError('Invalid cancellation state')
        with self.db() as db:
            db.execute("UPDATE parcels SET cancel_state=?,cancel_error=?,cancel_due=?,payment_revision=payment_revision+1,updated=? WHERE id=?",
                       (state, error[:100], time.time()+15 if state=='pending' else 0, time.time(), parcel_id))

    def finish_chat(self, account, thread, invoice_id):
        # Ending a conversation must not cancel an already prepared shipment.
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status='cancelled',ledgerly_status='cancelled',payment_revision=payment_revision+1,updated=? WHERE account=? AND thread=? AND invoice_id=? AND ttn IS NULL AND nova_status IN ('pending','resolving','invalid')",
                       (time.time(), account, thread, invoice_id))

    def hold(self, account, thread, invoice_id):
        with self.db() as db:
            db.execute("UPDATE parcels SET nova_status=CASE WHEN nova_status IN ('pending','resolving','invalid') AND ttn IS NULL THEN 'invalid' ELSE 'needs_review' END,error='order_requires_confirmation',payment_revision=payment_revision+1,updated=? WHERE account=? AND thread=? AND invoice_id=? AND nova_status!='cancelled' AND ledgerly_status!='shipped' AND error!='order_requires_confirmation'",
                       (time.time(), account, thread, invoice_id))

    def recover_receipts(self):
        # Telegram has no idempotency key: an interrupted upload is never retried blindly.
        with self.db() as db:
            db.execute("UPDATE receipt_archives SET state='uncertain',error='delivery_result_unknown' WHERE state='sending'")

    def claim_receipt(self):
        from urllib.parse import urlsplit
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT p.* FROM parcels p WHERE nova_status!='cancelled' AND ledgerly_status!='cancelled' "
                              "AND json_extract(payload,'$.receipt_required')=1 "
                              "AND receipt_links!='[]' AND EXISTS (SELECT 1 FROM json_each(p.receipt_links) j "
                              "WHERE j.type='text' AND NOT EXISTS (SELECT 1 FROM receipt_archives a "
                              "WHERE a.parcel_id=p.id AND a.invoice_key=p.invoice_key AND a.source_url=j.value)) "
                              "ORDER BY updated LIMIT 100").fetchall()
            for row in rows:
                try:
                    urls = json.loads(row['receipt_links'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(urls, list):
                    continue
                for url in urls[-3:]:
                    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 32 for c in url):
                        continue
                    try:
                        parsed = urlsplit(url)
                    except ValueError:
                        continue
                    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                        continue
                    key = digest(url)
                    if db.execute('SELECT 1 FROM receipt_archives WHERE parcel_id=? AND invoice_key=? AND url_hash=?',
                                  (row['id'],row['invoice_key'],key)).fetchone():
                        continue
                    db.execute("INSERT INTO receipt_archives(parcel_id,invoice_key,url_hash,source_url,state,updated) VALUES(?,?,?,?,'sending',?)",
                               (row['id'],row['invoice_key'],key,url,time.time()))
                    return dict(self.decode(row), receipt_url=url, receipt_hash=key)

    def receipt_result(self, parcel_id, invoice_key, key, state, group=None, message_id=None, error=''):
        if state not in ('sent','failed','uncertain'):
            raise ValueError('Invalid receipt archive state')
        permalink = ''
        if state == 'sent':
            if not str(group).startswith('-100') or type(message_id) is not int or message_id <= 0:
                raise ValueError('Receipt message requires a private supergroup permalink')
            permalink = 'https://t.me/c/%s/%s' % (str(group)[4:],message_id)
        with self.db() as db:
            changed = db.execute("UPDATE receipt_archives SET state=?,permalink=?,message_id=?,error=?,updated=? "
                                 "WHERE parcel_id=? AND invoice_key=? AND url_hash=? AND state='sending'",
                                 (state,permalink,message_id,error[:100],time.time(),parcel_id,invoice_key,key)).rowcount
            if changed:
                db.execute('UPDATE parcels SET payment_revision=payment_revision+1,updated=? WHERE id=? AND invoice_key=?',
                           (time.time(),parcel_id,invoice_key))
            return bool(changed)

    def receipts(self, parcel_id):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT a.state,a.permalink,a.message_id,a.error FROM receipt_archives a "
                    "JOIN parcels p ON p.id=a.parcel_id AND p.invoice_key=a.invoice_key "
                    "WHERE p.id=? AND a.source_url IN (SELECT value FROM json_each(p.receipt_links)) ORDER BY a.updated", (parcel_id,))]
