"""Batched Ukrposhta polling, durable credit caps and notification retries."""

from datetime import datetime, timezone
import json
import logging
import math
import time

import tracking_db as db
from ukrposhta_api import settings_at, request_batch, decode_batch, normalize_ukr_ttn, TrackingUnavailable

LOGGER=logging.getLogger(__name__)


def cached_snapshot(ttn):
    try:
        with db._LOCK,db._transaction() as conn:
            row=conn.one('SELECT snapshot FROM ukr_tracking_cache WHERE ttn=?',(ttn,))
        return json.loads(row['snapshot']) if row else None
    except Exception:
        LOGGER.debug('Could not read cached Ukrposhta status for %s',ttn,exc_info=True)
        return None


class UkrTracker:
    def __init__(self, request=request_batch, load_settings=settings_at, clock=time.time):
        self.request,self.load_settings,self.clock=request,load_settings,clock
        self.initialized=False
        self.error=None
        self.next_tick=0

    def initialize(self):
        if self.initialized:return
        with db._LOCK,db._transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS ukr_provider_state (
                provider TEXT PRIMARY KEY, period TEXT NOT NULL DEFAULT '', used_month INTEGER NOT NULL DEFAULT 0,
                used_total INTEGER NOT NULL DEFAULT 0, next_round BIGINT NOT NULL DEFAULT 0,
                next_call BIGINT NOT NULL DEFAULT 0, pending TEXT NOT NULL DEFAULT '[]',
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')''')
            conn.execute('''CREATE TABLE IF NOT EXISTS ukr_tracking_cache (
                ttn TEXT PRIMARY KEY, snapshot TEXT NOT NULL, pending SMALLINT NOT NULL DEFAULT 1,
                next_retry BIGINT NOT NULL DEFAULT 0)''')
        self.initialized=True

    def eligible(self):
        now=int(self.clock())
        with db._LOCK,db._transaction() as conn:
            rows=conn.all("SELECT * FROM shipments WHERE length(ttn)=13 AND created_at>=? AND "
                          "(active=1 OR (last_status='Ручне відстеження' AND received_at=0 AND onec_sent_at=0)) "
                          "ORDER BY created_at,ttn LIMIT 1001",(now-45*86400,))
        if len(rows)>1000:raise TrackingUnavailable('too_many_shipments')
        from business_db import get_order_by_ttn
        eligible=[]
        for row in rows:
            if not normalize_ukr_ttn(row['ttn']):continue
            order=get_order_by_ttn(row['ttn'])
            if order and order.get('status') in ('DELIVERED','REFUSED','CANCELLED','RETURNED','CLOSED'):
                continue
            eligible.append(row)
        return eligible

    def reserve(self, settings, eligible):
        now=int(self.clock())
        period=datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m')
        provider='parsebot:'+str(settings.get('budget_id','initial-free'))
        with db._LOCK,db._transaction() as conn:
            if conn.backend=='sqlite':conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO ukr_provider_state(provider) VALUES(?) ON CONFLICT(provider) DO NOTHING',(provider,))
            state=conn.one('SELECT * FROM ukr_provider_state WHERE provider=?'+(' FOR UPDATE' if conn.backend=='postgresql' else ''),(provider,))
            if now<state['next_call']:return None
            pending=json.loads(state['pending'])
            live={r['ttn'] for r in eligible}
            pending=[n for n in pending if n in live]
            if not pending:
                if not live or now<state['next_round']:return None
                pending=[r['ttn'] for r in eligible]
                interval=21600*max(1,math.ceil(len(pending)/30))
                conn.execute('UPDATE ukr_provider_state SET next_round=? WHERE provider=?',(now+interval,provider))
            used=state['used_month'] if state['period']==period else 0
            if used>=settings['monthly_limit'] or state['used_total']>=settings['total_limit']:
                raise TrackingUnavailable('free_credit_limit_reached')
            batch=pending[:30]
            # Reserve credits BEFORE the remote call. Unknown outcomes remain charged
            # locally so a timeout/crash cannot cause an uncontrolled bill.
            conn.execute('UPDATE ukr_provider_state SET period=?,used_month=?,used_total=used_total+1,'
                         'next_call=?,pending=? WHERE provider=?',
                         (period,used+1,now+30,json.dumps(pending[30:]),provider))
        return provider,batch

    def cache_results(self, snapshots):
        with db._LOCK,db._transaction() as conn:
            for ttn,snapshot in snapshots.items():
                if snapshot is None:continue
                old=conn.one('SELECT snapshot FROM ukr_tracking_cache WHERE ttn=?',(ttn,))
                if old and json.loads(old['snapshot']).get('event_at',0)>snapshot.get('event_at',0):continue
                conn.execute('INSERT INTO ukr_tracking_cache(ttn,snapshot,pending,next_retry) VALUES(?,?,1,0) '
                             'ON CONFLICT(ttn) DO UPDATE SET snapshot=excluded.snapshot,pending=1,next_retry=0',
                             (ttn,json.dumps(snapshot,ensure_ascii=False)))

    def deliver_cached(self, bot):
        from tracking_service import apply_snapshot
        now=int(self.clock())
        with db._LOCK,db._transaction() as conn:
            rows=conn.all('SELECT * FROM ukr_tracking_cache WHERE pending=1 AND next_retry<=? ORDER BY next_retry LIMIT 30',(now,))
        for cached in rows:
            shipment=db.get_shipment(cached['ttn'])
            if not shipment:
                # The shipment can be removed while a cached Telegram retry is
                # waiting.  Do not scan that orphan on every worker cycle.
                with db._LOCK,db._transaction() as conn:
                    conn.execute('UPDATE ukr_tracking_cache SET pending=0 WHERE ttn=?',(cached['ttn'],))
                continue
            from business_db import get_order_by_ttn
            order=get_order_by_ttn(cached['ttn'])
            if order and order.get('status') in ('CANCELLED','CLOSED','RETURNED'):
                with db._LOCK,db._transaction() as conn:
                    conn.execute('UPDATE ukr_tracking_cache SET pending=0 WHERE ttn=?',(cached['ttn'],))
                continue
            snapshot=json.loads(cached['snapshot'])
            if snapshot.get('event_at',0)<int(shipment.get('created_at') or 0)-45*86400:
                db.update_shipment(cached['ttn'],last_error='ukr_event_too_old_for_order')
                with db._LOCK,db._transaction() as conn:
                    conn.execute('UPDATE ukr_tracking_cache SET pending=0 WHERE ttn=?',(cached['ttn'],))
                continue
            if snapshot.get('event_at',0)<int(shipment.get('last_event_at') or 0):
                with db._LOCK,db._transaction() as conn:
                    conn.execute('UPDATE ukr_tracking_cache SET pending=0 WHERE ttn=?',(cached['ttn'],))
                continue
            success=False
            try:
                apply_snapshot(bot,shipment,snapshot)
                current=db.get_shipment(cached['ttn']) or {}
                success=(bool(current.get('tracking_message_id')) and
                         current.get('last_error')!='telegram_notification_pending')
                if snapshot.get('terminal'):
                    success=success and not bool(current.get('active'))
            except Exception:
                LOGGER.warning('Ukrposhta notification deferred for %s',cached['ttn'])
            with db._LOCK,db._transaction() as conn:
                conn.execute('UPDATE ukr_tracking_cache SET pending=?,next_retry=? WHERE ttn=?',
                             (0 if success else 1,now+300,cached['ttn']))

    def tick(self, bot):
        if self.clock()<self.next_tick:return
        self.next_tick=self.clock()+30
        try:
            settings=self.load_settings()
            if not settings:return
            self.initialize()
            self.deliver_cached(bot)
            eligible=self.eligible()
            reserved=self.reserve(settings,eligible)
            if not reserved:
                self.next_tick=self.clock()+60
                return
            provider,batch=reserved
            try:
                response=self.request(settings,batch)
                snapshots=decode_batch(response,batch)
                self.cache_results(snapshots)
                with db._LOCK,db._transaction() as conn:
                    conn.execute("UPDATE ukr_provider_state SET failures=0,last_error='' WHERE provider=?",(provider,))
                self.deliver_cached(bot)
                self.error=None
            except Exception as error:
                reason=str(error) if isinstance(error,TrackingUnavailable) else type(error).__name__
                with db._LOCK,db._transaction() as conn:
                    state=conn.one('SELECT failures,pending FROM ukr_provider_state WHERE provider=?',(provider,))
                    failures=min(10,state['failures']+1)
                    # No immediate retries and no automatic paid fallback.
                    conn.execute('UPDATE ukr_provider_state SET failures=?,last_error=?,next_call=?,pending=? WHERE provider=?',
                                 (failures,reason,int(self.clock())+min(21600,900*2**(failures-1)),
                                  json.dumps(batch+json.loads(state['pending'])),provider))
                raise TrackingUnavailable(reason) from None
        except Exception as error:
            reason=str(error) if isinstance(error,TrackingUnavailable) else type(error).__name__
            if reason=='free_credit_limit_reached':self.next_tick=self.clock()+3600
            if self.error!=reason:
                LOGGER.warning('Ukrposhta auto tracking paused: %s',reason)
                if reason in ('free_credit_limit_reached','http_401','http_402','http_403','unsupported_batch_response','barcode_mismatch'):
                    try:
                        from alerts import notify_admin
                        notify_admin(bot,'📮 Автотрекінг Укрпошти потребує уваги: '+reason+'. Попередні статуси збережені. Перевір ключ, безкоштовний залишок Parse.bot або формат відповіді.')
                    except Exception:
                        pass
                self.error=reason
