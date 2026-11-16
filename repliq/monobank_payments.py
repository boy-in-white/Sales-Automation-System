"""Read-only mono statements, explicit account pinning and durable payment matching."""

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

from app import read_json

POLL_SECONDS = 65
WINDOW_SECONDS = 86400
MIN_DEPOSIT = 9400
MAX_DEPOSIT = 10500


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def destination(merchant):
    return fingerprint({k: v for k, v in merchant.items() if k != '_monobank'})


def scope(settings):
    return fingerprint([settings['client_id'], settings['account_id']])


def configured(private_dir, merchant=None):
    value = read_json(Path(private_dir) / 'monobank.json')
    if not value.get('enabled'):
        return {}
    for key in ('token', 'client_id', 'account_id', 'activation_id', 'destination_signature'):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 512:
            raise ValueError('mono_setup_incomplete')
    if (value['account_id'] == '0' or value.get('dedicated_account') is not True or
            type(value.get('enabled_at')) not in (int, float) or not 0 < value['enabled_at'] <= time.time() + 5):
        raise ValueError('mono_explicit_account_required')
    if merchant is not None and destination(merchant) != value['destination_signature']:
        raise ValueError('mono_payment_destination_changed')
    return value


def bank_marker(private_dir, merchant):
    try:
        settings = configured(private_dir, merchant)
    except Exception:
        # Settings failures must leave ordinary checkout usable. No token is logged.
        return None
    if not settings:
        return None
    return {'scope': scope(settings), 'activation_id': settings['activation_id'],
            'destination_signature': settings['destination_signature']}


def matches_account(account, merchant):
    number = str(merchant.get('account', '')).replace(' ', '')
    if merchant.get('kind') == 'iban':
        return bool(number) and number == account.get('iban')
    if merchant.get('kind') != 'card' or not re.fullmatch(r'\d{16,19}', number):
        return False
    return any(isinstance(pan, str) and re.fullmatch(r'\d{6}[*Xx]+\d{4}', pan) and len(pan) == len(number) and
               all(p in '*Xx' or p == digit for p, digit in zip(pan, number))
               for pan in account.get('maskedPan', []))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MonoError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = max(0, min(86400, retry_after))
        super().__init__('mono API: ' + str(code))


class MonoClient:
    """Only two GET endpoints; redirects cannot disclose X-Token to another host."""
    def __init__(self, token):
        self.token = token

    def get(self, path):
        if not (path == '/personal/client-info' or re.fullmatch(r'/personal/statement/[^/]+/\d+/\d+', path)):
            raise ValueError('Unsupported mono operation')
        request = Request('https://api.monobank.ua' + path,
                          headers={'X-Token': self.token, 'Accept': 'application/json'}, method='GET')
        try:
            with build_opener(NoRedirect()).open(request, timeout=8) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise MonoError('response_too_large')
                return json.loads(body)
        except HTTPError as error:
            retry = str((error.headers or {}).get('Retry-After',''))
            raise MonoError(error.code, int(retry) if retry.isdigit() else 0) from None
        except (URLError, TimeoutError, OSError):
            raise MonoError('network') from None
        except (ValueError, UnicodeError):
            raise MonoError('invalid_response') from None

    def info(self):
        return self.get('/personal/client-info')

    def statement(self, account, start, end):
        if not account or account == '0':
            raise ValueError('Explicit mono account is required')
        return self.get('/personal/statement/%s/%s/%s' % (quote(account, safe=''), int(start), int(end)))


def marker(settings_marker, invoice_key, requested_at=None):
    """Called only after OLX acknowledged sending these particular payment details."""
    return dict(settings_marker, invoice_key=invoice_key, requested_at=int(requested_at or time.time()))


class PaymentChecker:
    def __init__(self, private_dir, queue, stop, report=None, client_factory=MonoClient, clock=time.time, health=None):
        self.private_dir, self.queue, self.stop = Path(private_dir), queue, stop
        self.report = report or (lambda message: None)
        self.client_factory, self.clock = client_factory, clock
        self.thread = None
        self.next_tick = 0
        self.validated = None
        self.last_error = None
        self.health = health or (lambda value: None)
        self.health_value, self.health_at = None, 0
        if queue:
            with queue.db() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS mono_scans (
                        scope TEXT PRIMARY KEY, last_scan REAL NOT NULL DEFAULT 0,
                        next_scan REAL NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS mono_transactions (
                        scope TEXT NOT NULL, transaction_id TEXT NOT NULL,
                        happened INTEGER NOT NULL, amount INTEGER NOT NULL,
                        valid INTEGER NOT NULL, refs TEXT NOT NULL,
                        updated REAL NOT NULL,
                        PRIMARY KEY(scope,transaction_id)
                    );
                    CREATE INDEX IF NOT EXISTS mono_recent ON mono_transactions(scope,happened);
                    CREATE TABLE IF NOT EXISTS mono_orders (
                        parcel_id TEXT NOT NULL, invoice_key TEXT NOT NULL,
                        scope TEXT NOT NULL, payload TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                        PRIMARY KEY(parcel_id,invoice_key)
                    );
                    CREATE INDEX IF NOT EXISTS mono_pending_orders ON mono_orders(scope,active,created);
                    CREATE TABLE IF NOT EXISTS mono_info_limits (
                        client_key TEXT PRIMARY KEY, next_check REAL NOT NULL,
                        verified_key TEXT NOT NULL DEFAULT '', verified_at REAL NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS mono_retry (
                        scope TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                        next_try REAL NOT NULL, code TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS mono_matches (
                        scope TEXT NOT NULL, transaction_id TEXT NOT NULL,
                        parcel_id TEXT NOT NULL, invoice_key TEXT NOT NULL,
                        amount INTEGER NOT NULL, credited INTEGER NOT NULL,
                        state TEXT NOT NULL DEFAULT 'paid', created REAL NOT NULL,
                        PRIMARY KEY(scope,transaction_id), UNIQUE(parcel_id,invoice_key)
                    );
                ''')

    def watch_invoice(self, account, thread, context, catalog):
        if not self.queue:
            return
        from order_summary import snapshot, invoice_key
        order = snapshot(catalog, context)
        tag = context.get('bank_payment', {})
        if not order.get('id'):
            return
        parcel_id = fingerprint([str(account),str(thread),order['id']])[:24]
        key = invoice_key(order)
        with self.queue.db() as db:
            db.execute('UPDATE mono_orders SET active=2 WHERE parcel_id=? AND invoice_key!=?', (parcel_id,key))
            if (not order['issued'] or tag.get('invoice_key') != key or order['method'] != 'cod' or
                    Decimal(str(order['amount'])) != 100 or order.get('delivery_prepay')):
                return
            data = {'version':1,'invoice_id':order['id'],'invoice_key':key,'method':order['method'],
                    'advance_cents':int(Decimal(str(order['amount']))*100),'bank_payment':tag}
            db.execute('INSERT OR IGNORE INTO mono_orders VALUES(?,?,?,?,1,?)',
                       (parcel_id,key,tag['scope'],json.dumps(data),self.clock()))

    def cancel_invoice(self, account, thread, invoice_id):
        if self.queue and invoice_id:
            with self.queue.db() as db:
                db.execute('UPDATE mono_orders SET active=2 WHERE parcel_id=?',
                           (fingerprint([str(account),str(thread),invoice_id])[:24],))

    def start(self):
        if self.queue and not self.thread:
            self.thread = threading.Thread(target=self.run, name='mono-payments', daemon=True)
            self.thread.start()

    def run(self):
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(5)

    def publish_health(self, state, settings=None, code='', next_check=0, success=False):
        value = {'state': state, 'scope': scope(settings) if settings else '', 'code': str(code),
                 'activation_id': settings.get('activation_id', '') if settings else '',
                 'next_check': next_check, 'updated': self.clock()}
        if success:
            value['last_success'] = self.clock()
        signature = (state, value['scope'], value['activation_id'], str(code), next_check)
        if success or signature != self.health_value or self.clock() - self.health_at >= 60:
            try:
                if settings and self.queue:
                    with self.queue.db() as db:
                        value['matched_payments'] = db.execute("SELECT COUNT(*) FROM mono_matches WHERE scope=? AND state='paid'", (scope(settings),)).fetchone()[0]
                        value['awaiting_receipts'] = db.execute("SELECT COUNT(*) FROM parcels WHERE payment_source='monobank_pending_receipt' AND paid=0 AND nova_status!='cancelled' AND json_extract(payload,'$.bank_payment.scope')=?", (scope(settings),)).fetchone()[0]
                self.health(value)
                self.health_value, self.health_at = signature, self.clock()
            except Exception:
                pass  # Owner diagnostics cannot interrupt payment matching.

    def tick(self):
        if self.stop.is_set() or self.clock() < self.next_tick:
            return
        self.next_tick = self.clock() + 5
        if not self.queue:
            self.publish_health('manual')
            return
        settings = {}
        try:
            from checkout import payment_settings
            merchant = payment_settings(self.private_dir, with_bank=False)
            settings = configured(self.private_dir, merchant)
            if not settings:
                self.publish_health('not_configured')
                return
            self.reconcile(settings)
            if self.pending_since(settings) is None:
                self.publish_health('idle', settings)
                return
            with self.queue.db() as db:
                retry = db.execute('SELECT next_try,code FROM mono_retry WHERE scope=?', (scope(settings),)).fetchone()
                if retry and self.clock() < retry['next_try']:
                    self.publish_health('retry', settings, retry['code'], retry['next_try'])
                    return
            if self.poll(settings, merchant):
                with self.queue.db() as db:
                    db.execute('DELETE FROM mono_retry WHERE scope=?', (scope(settings),))
                self.last_error = None
                self.publish_health('ok', settings, success=True)
            else:
                self.publish_health('waiting', settings)
            self.reconcile(settings)
        except Exception as error:
            code = str(error.code) if isinstance(error, MonoError) else type(error).__name__
            delay = 300 if code in ('401','403','429') else POLL_SECONDS
            try:
                if settings:
                    with self.queue.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        prior = db.execute('SELECT failures FROM mono_retry WHERE scope=?', (scope(settings),)).fetchone()
                        failures = min(10, prior['failures'] + 1) if prior else 1
                        delay = min(900, (300 if code in ('401','403','429') else POLL_SECONDS) * 2 ** (failures-1))
                        delay = max(delay, error.retry_after if isinstance(error,MonoError) else 0)
                        db.execute('INSERT OR REPLACE INTO mono_retry VALUES(?,?,?,?)',
                                   (scope(settings),failures,self.clock()+delay,code))
            except (sqlite3.Error, KeyError):
                pass
            self.next_tick = self.clock() + (300 if code in ('401','403','429') else POLL_SECONDS)
            self.publish_health('retry', settings, code, self.clock() + delay if settings else self.next_tick)
            if self.last_error != code:
                try:
                    self.report('mono: автоматична перевірка тимчасово недоступна (%s). Відповіді OLX продовжуються; оплату можна перевірити в Ledgerly.' % code)
                except Exception:
                    pass
                self.last_error = code

    def pending_since(self, settings):
        """Find live deposit requests using local data; idle means no bank calls."""
        now = self.clock()
        with self.queue.db() as db:
            row = db.execute("""
                SELECT MIN(json_extract(x.payload,'$.bank_payment.requested_at')) AS requested
                FROM (
                    SELECT id,invoice_key,payload FROM parcels
                    WHERE paid=0 AND payment_manual_lock=0 AND amendment IS NULL
                      AND nova_status IN ('pending','resolving','creating_contact','creating','created')
                      AND ledgerly_status NOT IN ('cancelled','shipped','uncertain')
                    UNION ALL
                    SELECT parcel_id AS id,invoice_key,payload FROM mono_orders o
                    WHERE active=1 AND NOT EXISTS(SELECT 1 FROM parcels p WHERE p.id=o.parcel_id)
                ) x
                WHERE json_extract(x.payload,'$.version')=1
                  AND json_extract(x.payload,'$.method')='cod'
                  AND json_extract(x.payload,'$.advance_cents')=10000
                  AND json_extract(x.payload,'$.bank_payment.scope')=?
                  AND json_extract(x.payload,'$.bank_payment.activation_id')=?
                  AND json_extract(x.payload,'$.bank_payment.destination_signature')=?
                  AND json_extract(x.payload,'$.bank_payment.invoice_key')=x.invoice_key
                  AND json_type(x.payload,'$.bank_payment.requested_at')='integer'
                  AND json_extract(x.payload,'$.bank_payment.requested_at') BETWEEN ? AND ?
                  AND NOT EXISTS(SELECT 1 FROM mono_matches m WHERE m.parcel_id=x.id AND m.invoice_key=x.invoice_key)
                  AND NOT EXISTS(SELECT 1 FROM mono_orders o WHERE o.parcel_id=x.id AND o.invoice_key=x.invoice_key AND o.active=2)
            """, (scope(settings),settings['activation_id'],settings['destination_signature'],
                  max(int(settings['enabled_at']),now-WINDOW_SECONDS),now)).fetchone()
        return row['requested']

    def poll(self, settings, merchant):
        pending = self.pending_since(settings)
        if pending is None:
            return False
        now, bank_scope = int(self.clock()), scope(settings)
        key = fingerprint([settings['token'], settings['client_id'], settings['account_id'],
                           settings['activation_id'], settings['destination_signature']])
        client = self.client_factory(settings['token'])
        if self.validated != key:
            with self.queue.db() as db:
                db.execute('BEGIN IMMEDIATE')
                info_key = fingerprint(settings['client_id'])
                last = db.execute('SELECT * FROM mono_info_limits WHERE client_key=?', (info_key,)).fetchone()
                if last and last['verified_key'] == key and now - last['verified_at'] < 3600:
                    self.validated = key
                elif now < max(last['next_check'] if last else 0, settings.get('setup_info_at',0) + POLL_SECONDS):
                    return
                else:
                    db.execute("INSERT INTO mono_info_limits(client_key,next_check) VALUES(?,?) ON CONFLICT(client_key) DO UPDATE SET next_check=excluded.next_check",
                               (info_key, now + POLL_SECONDS))
        if self.validated != key:
            info = client.info()
            accounts = info.get('accounts', []) if isinstance(info, dict) else []
            selected = next((a for a in accounts if a.get('id') == settings['account_id'] and
                             a.get('currencyCode') == 980 and a.get('type') != 'fop'), None)
            if not selected or info.get('clientId') != settings['client_id'] or not matches_account(selected, merchant):
                raise ValueError('mono_selected_account_does_not_match_payment_details')
            self.validated = key
            with self.queue.db() as db:
                db.execute('UPDATE mono_info_limits SET verified_key=?,verified_at=? WHERE client_key=?', (key,now,info_key))
        with self.queue.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO mono_scans(scope) VALUES(?)', (bank_scope,))
            state = db.execute('SELECT * FROM mono_scans WHERE scope=?', (bank_scope,)).fetchone()
            if now < state['next_scan']:
                return
            # Reserve the API slot BEFORE networking; restart cannot cause a burst.
            db.execute('UPDATE mono_scans SET next_scan=? WHERE scope=?', (now + POLL_SECONDS, bank_scope))
        # Keep the entire waiting window: bank transfers can appear late with
        # their original timestamps, including after an outage or restart.
        start = max(int(settings['enabled_at']), now - WINDOW_SECONDS, int(pending))
        if start >= now:
            return
        items = client.statement(settings['account_id'], start, now)
        if not isinstance(items, list) or len(items) > 20000:
            raise MonoError('invalid_statement')
        self.ingest(settings, items)
        with self.queue.db() as db:
            db.execute('UPDATE mono_scans SET last_scan=? WHERE scope=?', (now, bank_scope))
        return True

    def ingest(self, settings, items):
        now, bank_scope = self.clock(), scope(settings)
        own = set(settings.get('own_iban_hashes', []))
        seen, conflicts = {}, set()
        for item in items:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and 1 <= len(item['id']) <= 200:
                signature = fingerprint({key: item.get(key) for key in ('time','amount','currencyCode','hold','counterIban','comment','description')})
                if item['id'] in seen and seen[item['id']] != signature:
                    conflicts.add(item['id'])
                seen[item['id']] = signature
        with self.queue.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not 1 <= len(item['id']) <= 200 or
                        type(item.get('time')) is not int or type(item.get('amount')) is not int):
                    continue
                relevant = MIN_DEPOSIT <= item['amount'] <= MAX_DEPOSIT and item.get('currencyCode') == 980
                known = db.execute('SELECT 1 FROM mono_transactions WHERE scope=? AND transaction_id=?',
                                   (bank_scope,item['id'])).fetchone()
                if not relevant and not known:
                    continue  # Other payments are neither stored nor used.
                body = ' '.join(str(item.get(k, '')) for k in ('comment', 'description'))[:2000]
                refs = sorted(set(re.findall(r'\bRQ-[A-F0-9]{12}\b', body.upper())))
                sender = re.sub(r'\s+', '', str(item.get('counterIban', ''))).upper()
                valid = (relevant and item.get('hold') is False and item['id'] not in conflicts and
                         int(settings['enabled_at']) <= item['time'] <= now + 5 and
                         (not sender or fingerprint(sender) not in own) and
                         not re.search(r'кешбек|кэшбэк|cashback|повернен|повернення|возврат|refund|власн\w*\s+кошт|між\s+своїми', body, re.I))
                if valid:
                    matched = db.execute('SELECT COALESCE(p.payload,o.payload) AS payload FROM mono_matches m '
                                         'LEFT JOIN parcels p ON p.id=m.parcel_id AND p.invoice_key=m.invoice_key '
                                         'LEFT JOIN mono_orders o ON o.parcel_id=m.parcel_id AND o.invoice_key=m.invoice_key '
                                         "WHERE m.scope=? AND m.transaction_id=? AND m.state='paid'", (bank_scope,item['id'])).fetchone()
                    if matched:
                        try:
                            valid = self.compatible({'data': json.loads(matched['payload'])},
                                                    {'happened': item['time'], 'amount': item['amount'], 'refs': json.dumps(refs)})
                        except (ValueError, TypeError, KeyError):
                            valid = False
                db.execute('INSERT INTO mono_transactions VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,transaction_id) DO UPDATE SET '
                           'happened=excluded.happened,amount=excluded.amount,valid=excluded.valid,refs=excluded.refs,updated=excluded.updated',
                           (bank_scope, item['id'], item['time'], item['amount'], int(valid), json.dumps(refs), now))

    @staticmethod
    def compatible(order, transaction):
        data, tag = order['data'], order['data'].get('bank_payment', {})
        at = tag.get('requested_at', 0)
        if not at <= transaction['happened'] <= at + WINDOW_SECONDS:
            return False
        refs = json.loads(transaction['refs'])
        if refs and refs != [data['invoice_id']]:
            return False
        amount = transaction['amount']
        return data['method'] == 'cod' and data['advance_cents'] == 10000 and MIN_DEPOSIT <= amount <= MAX_DEPOSIT

    def reconcile(self, settings):
        """One DB transaction consumes credits and verifies the exact frozen invoices."""
        now, bank_scope = self.clock(), scope(settings)
        with self.queue.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A bank amendment invalidates an automatic decision, never an owner's override.
            for old in db.execute('SELECT m.*,t.valid,t.amount AS actual FROM mono_matches m JOIN mono_transactions t '
                                  'ON t.scope=m.scope AND t.transaction_id=m.transaction_id WHERE m.scope=? AND m.state=\'paid\' AND m.credited=10000 '
                                  'AND (t.valid=0 OR t.amount!=m.amount)', (bank_scope,)).fetchall():
                db.execute("UPDATE mono_matches SET state='review' WHERE scope=? AND transaction_id=?", (bank_scope, old['transaction_id']))
                db.execute("UPDATE parcels SET paid=0,payment_source='monobank_review',payment_revision=payment_revision+1,updated=? "
                           "WHERE id=? AND invoice_key=? AND payment_source IN ('monobank','monobank_pending_receipt') AND payment_manual_lock=0", (now, old['parcel_id'], old['invoice_key']))
            rows = db.execute("SELECT * FROM parcels WHERE paid=0 AND payment_manual_lock=0 AND "
                              "json_extract(payload,'$.method')='cod' AND json_extract(payload,'$.advance_cents')=10000 AND "
                              "nova_status IN ('pending','resolving','creating_contact','creating','created') AND "
                              "ledgerly_status NOT IN ('cancelled','shipped','uncertain') AND amendment IS NULL AND "
                              "json_extract(payload,'$.bank_payment.scope')=? AND "
                              "NOT EXISTS(SELECT 1 FROM mono_orders o WHERE o.parcel_id=parcels.id AND o.invoice_key=parcels.invoice_key AND o.active=2) AND "
                              "NOT EXISTS(SELECT 1 FROM mono_matches m WHERE m.parcel_id=parcels.id AND m.invoice_key=parcels.invoice_key) "
                              "ORDER BY created,id LIMIT 101", (bank_scope,)).fetchall()
            if len(rows) > 100:
                return 0  # Never approve a truncated view of competing orders.
            waiting = [dict(row, id=row['parcel_id'], data=json.loads(row['payload'])) for row in db.execute(
                "SELECT * FROM mono_orders o WHERE scope=? AND active=1 AND created>=? AND "
                "json_extract(payload,'$.method')='cod' AND json_extract(payload,'$.advance_cents')=10000 AND "
                "NOT EXISTS(SELECT 1 FROM parcels p WHERE p.id=o.parcel_id) AND "
                "NOT EXISTS(SELECT 1 FROM mono_matches m WHERE m.parcel_id=o.parcel_id AND m.invoice_key=o.invoice_key) "
                "ORDER BY created,parcel_id LIMIT 101", (bank_scope,now-WINDOW_SECONDS))]
            if len(rows) + len(waiting) > 100:
                return 0
            shadows = [self.queue.decode(row) for row in db.execute(
                "SELECT * FROM parcels WHERE json_extract(payload,'$.bank_payment.scope')=? AND "
                "json_extract(payload,'$.method')='cod' AND json_extract(payload,'$.advance_cents')=10000 AND "
                "(payment_manual_lock=1 OR nova_status IN ('cancelled','needs_review') OR ledgerly_status='cancelled') AND "
                "NOT EXISTS(SELECT 1 FROM mono_matches m WHERE m.parcel_id=parcels.id AND m.invoice_key=parcels.invoice_key) "
                "AND created>=? LIMIT 101", (bank_scope, now-WINDOW_SECONDS))]
            shadows += [dict(row, data=json.loads(row['payload'])) for row in db.execute(
                "SELECT * FROM mono_orders WHERE scope=? AND active=2 AND created>=? AND json_extract(payload,'$.method')='cod' AND json_extract(payload,'$.advance_cents')=10000 LIMIT 101", (bank_scope,now-WINDOW_SECONDS))]
            orders = []
            for row in [self.queue.decode(row) for row in rows] + waiting:
                data, tag = row['data'], row['data'].get('bank_payment', {})
                if (data.get('version') == 1 and data.get('method') == 'cod' and data.get('advance_cents') == 10000 and
                        tag.get('activation_id') == settings['activation_id'] and
                        tag.get('destination_signature') == settings['destination_signature'] and
                        tag.get('invoice_key') == row['invoice_key'] and type(tag.get('requested_at')) is int and
                        int(settings['enabled_at']) <= tag['requested_at'] <= now and now - tag['requested_at'] <= WINDOW_SECONDS):
                    orders.append(row)
            credits = [dict(r) for r in db.execute("SELECT t.* FROM mono_transactions t WHERE t.scope=? AND valid=1 AND happened>=? AND amount BETWEEN 9400 AND 10500 "
                       "AND NOT EXISTS(SELECT 1 FROM mono_matches m WHERE m.scope=t.scope AND m.transaction_id=t.transaction_id) "
                       "ORDER BY happened,transaction_id LIMIT 4001", (bank_scope, now-WINDOW_SECONDS))]
            if len(credits) > 4000:
                return 0
            # Explicit order references take precedence over pooled, anonymous transfers.
            pairs, remaining = [], set(range(len(orders)))
            used = set()
            for index, transaction in enumerate(credits):
                if not json.loads(transaction['refs']):
                    continue
                candidates = [o for o in remaining if self.compatible(orders[o], transaction)]
                if len(candidates) == 1:
                    o = candidates[0]
                    pairs.append((o,index)); remaining.remove(o); used.add(index)
            reserved = {i for i,t in enumerate(credits) if not json.loads(t['refs']) and
                        (len(shadows) > 100 or any(self.compatible(shadow,t) for shadow in shadows))}
            edges = {o: {i for i,t in enumerate(credits) if i not in used and i not in reserved and not json.loads(t['refs']) and self.compatible(orders[o],t)} for o in remaining}
            # Resolve whole connected groups only. One anonymous payment for two
            # simultaneous buyers cannot mark either one as definitely paid.
            while edges:
                group = {next(iter(edges))}
                payments = set()
                changed = True
                while changed:
                    changed = False
                    payments.update(*(edges[o] for o in group))
                    for o,e in edges.items():
                        if o not in group and e.intersection(payments):
                            group.add(o); changed = True
                matched = {}

                def augment(o, seen):
                    for i in sorted(edges[o]):
                        if i in seen:
                            continue
                        seen.add(i)
                        if i not in matched or augment(matched[i], seen):
                            matched[i] = o
                            return True
                    return False

                if all(augment(o, set()) for o in sorted(group)):
                    pairs.extend((o,i) for i,o in matched.items())
                for o in group:
                    del edges[o]
            for o,i in pairs:
                order, credit = orders[o], credits[i]
                db.execute('INSERT INTO mono_matches(scope,transaction_id,parcel_id,invoice_key,amount,credited,created) VALUES(?,?,?,?,?,?,?)',
                           (bank_scope, credit['transaction_id'], order['id'], order['invoice_key'], credit['amount'], order['data']['advance_cents'], now))
            # Payments can arrive before the buyer sends an address. Reserve them
            # against that invoice now, apply only once its confirmed parcel exists.
            for match in db.execute("SELECT m.*,p.payload AS parcel_payload,p.payment_source AS old_source,p.bank_payment AS old_proof FROM mono_matches m JOIN parcels p ON p.id=m.parcel_id AND p.invoice_key=m.invoice_key "
                                    "WHERE m.scope=? AND m.state='paid' AND p.paid=0 AND p.payment_manual_lock=0 AND p.amendment IS NULL "
                                    "AND p.nova_status IN ('pending','resolving','creating_contact','creating','created') "
                                    "AND p.ledgerly_status NOT IN ('cancelled','shipped','uncertain') "
                                    "AND NOT EXISTS(SELECT 1 FROM mono_orders o WHERE o.parcel_id=p.id AND o.invoice_key=p.invoice_key AND o.active=2) "
                                    "AND json_extract(p.payload,'$.bank_payment.scope')=? AND "
                                    "json_extract(p.payload,'$.bank_payment.invoice_key')=p.invoice_key AND "
                                    "json_extract(p.payload,'$.advance_cents')=m.credited AND "
                                    "json_extract(p.payload,'$.method')='cod' AND m.credited=10000 AND m.amount BETWEEN 9400 AND 10500", (bank_scope,bank_scope)).fetchall():
                required = json.loads(match['parcel_payload']).get('receipt_required') is True
                archived = db.execute("SELECT 1 FROM receipt_archives a JOIN parcels p ON p.id=a.parcel_id AND p.invoice_key=a.invoice_key "
                                      "WHERE a.parcel_id=? AND a.invoice_key=? AND a.state='sent' AND a.permalink!='' "
                                      "AND a.source_url IN (SELECT value FROM json_each(p.receipt_links)) LIMIT 1",
                                      (match['parcel_id'],match['invoice_key'])).fetchone()
                ready = not required or bool(archived)
                source = 'monobank' if ready else 'monobank_pending_receipt'
                proof = {'source':'monobank','actual_cents':match['amount'],'credited_cents':match['credited'],
                         'transaction_id':match['transaction_id'],'checked_at':match['created']}
                encoded = json.dumps(proof)
                if not ready and match['old_source'] == source and match['old_proof'] == encoded:
                    continue
                db.execute("UPDATE parcels SET paid=?,paid_by=NULL,payment_source=?,bank_payment=?,"
                           "payment_revision=payment_revision+1,updated=? WHERE id=? AND invoice_key=? AND paid=0 AND payment_manual_lock=0",
                           (int(ready),source,encoded, now, match['parcel_id'], match['invoice_key']))
            return len(pairs)
