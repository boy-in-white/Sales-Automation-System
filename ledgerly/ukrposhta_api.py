"""Independent Parse.bot client; no website automation or carrier credentials."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

from config import BASE_DIR

SETTINGS_PATH = BASE_DIR / 'private' / 'ukrposhta.json'
SCRAPER_ID = '2ff64a00-8724-4e97-a4e0-d498c7e3d428'


class TrackingUnavailable(Exception):
    pass


def normalize_ukr_ttn(value):
    value = re.sub(r'\s+', '', str(value or '')).upper()
    return value if re.fullmatch(r'(?:\d{13}|[A-Z]{2}\d{9}[A-Z]{2})', value) else None


def settings_at(path=None):
    path = Path(path or SETTINGS_PATH)
    if not path.exists():
        return {}
    if path.stat().st_size > 64000:
        raise TrackingUnavailable('invalid_configuration')
    value = json.loads(path.read_text())
    if not isinstance(value,dict):
        raise TrackingUnavailable('invalid_configuration')
    if not value.get('enabled'):
        return {}
    if not isinstance(value.get('api_key'),str) or not value['api_key'].strip():
        raise TrackingUnavailable('api_key_missing')
    scraper = value.get('scraper_id',SCRAPER_ID)
    if not isinstance(scraper,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',scraper):
        raise TrackingUnavailable('invalid_scraper_id')
    for key,default,maximum in (('monthly_limit',160,180),('total_limit',180,1000000)):
        count=value.get(key,default)
        if type(count) is not int or not 1 <= count <= maximum:
            raise TrackingUnavailable('invalid_credit_limit')
    return dict(value,scraper_id=scraper,monthly_limit=value.get('monthly_limit',160),total_limit=value.get('total_limit',180))


def configured():
    try:
        return bool(settings_at())
    except (OSError,ValueError,TrackingUnavailable):
        return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_batch(settings, barcodes):
    numbers=list(dict.fromkeys(barcodes))
    if not 1 <= len(numbers) <= 30 or any(normalize_ukr_ttn(n)!=n for n in numbers):
        raise TrackingUnavailable('invalid_tracking_numbers')
    if not re.fullmatch(r'[a-fA-F0-9-]{36}',str(settings.get('scraper_id',''))):
        raise TrackingUnavailable('invalid_scraper_id')
    request=Request('https://api.parse.bot/scraper/'+settings['scraper_id']+'/track_multiple_parcels',
                    data=json.dumps({'lang':'UA','barcodes':','.join(numbers)}).encode(),
                    headers={'X-API-Key':settings['api_key'],'Content-Type':'application/json','Accept':'application/json'},method='POST')
    try:
        with build_opener(NoRedirect()).open(request,timeout=20) as response:
            body=response.read(4*1024*1024+1)
            if len(body)>4*1024*1024:
                raise TrackingUnavailable('response_too_large')
            return json.loads(body)
    except HTTPError as error:
        raise TrackingUnavailable('http_'+str(error.code)) from None
    except (URLError,TimeoutError,OSError):
        raise TrackingUnavailable('network') from None
    except (ValueError,UnicodeError):
        raise TrackingUnavailable('invalid_json') from None


def fields(value):
    return {re.sub(r'[^a-z0-9]','',str(k).lower()):v for k,v in value.items()}


def event_time(value):
    if not isinstance(value,str):
        return None
    for pattern in (None,'%d.%m.%Y %H:%M:%S','%d.%m.%Y %H:%M'):
        try:
            date=datetime.fromisoformat(value.replace('Z','+00:00')) if pattern is None else datetime.strptime(value,pattern)
            if date.tzinfo is None:date=date.replace(tzinfo=ZoneInfo('Europe/Kyiv'))
            stamp=int(date.timestamp())
            return stamp if 0 < stamp <= time.time()+300 else None
        except (ValueError,OverflowError,OSError):pass
    return None


def parcel_snapshot(barcode, value):
    if isinstance(value,dict) and isinstance(value.get('data'),dict):value=value['data']
    if isinstance(value,list):events=value
    elif isinstance(value,dict):
        identity=fields(value).get('barcode')
        if identity is not None and normalize_ukr_ttn(identity)!=barcode:
            raise TrackingUnavailable('barcode_mismatch')
        if value.get('date_by_barcode') is False:return None
        events=value.get('result',value.get('events',[]))
    else:return None
    if not isinstance(events,list) or len(events)>1000:
        return None
    history=[]
    for event in events:
        if not isinstance(event,dict):continue
        row=fields(event)
        identity=row.get('barcode')
        if identity is not None and normalize_ukr_ttn(identity)!=barcode:
            raise TrackingUnavailable('barcode_mismatch')
        stamp=event_time(row.get('date') or row.get('eventdate') or row.get('datetime'))
        text=str(row.get('eventname') or row.get('eventdescription') or row.get('status') or '').strip()
        code=str(row.get('eventcode') or row.get('code') or row.get('event') or '')
        if not stamp or not text or len(text)>1000:continue
        history.append((stamp,text,code,row))
    if not history:return None  # Empty/not-found is NEVER proof of delivery.
    history.sort(key=lambda e:(e[0],int(e[3]['step']) if str(e[3].get('step','')).isdigit() else 0))
    stamp,text,code,row=history[-1]
    low=text.lower()
    returning=any(re.search(r'відмов|повернен|поверну|вручен.{0,8}відправник|refus|return\s+to\s+sender|delivered\s+to\s+(?:the\s+)?sender',e[1],re.I) for e in history)
    negative=bool(re.search(r'не\s+вруч|невруч|не\s+отрим|недостав|not\s+deliver|unsuccess',low))
    delivered=not negative and (code=='41000' or bool(re.fullmatch(r'вручення(?:\s*[:—-].*)?|(?:відправлення\s+)?вручено(?:\s*[:—-].*)?|(?:shipment\s+)?delivered(?:\s+to\s+(?:the\s+)?recipient)?[.!]?',low)))
    if delivered:
        label,terminal=('Відмова',True) if returning else ('Отримано',True)
    elif returning:
        label,terminal='Відмова',False
    elif code=='10601' or re.search(r'створен.{0,15}онлайн|очіку.{0,20}(?:прийм|прийнят)|pre.advice|awaiting\s+(?:acceptance|posting)',low):
        label,terminal='Очікує приймання',False
    elif code=='21700' or re.search(r'надходження до відділення|прибул.{0,12}відділен|arrived.{0,12}(?:office|branch)',low):
        label,terminal='Прибув у відділення',False
    elif code in ('10100','20800','21500') or re.search(r'^приймання\b|прийнято|транзит|відправлено|in transit|accepted',low):
        label,terminal='Відправлено',False
    else:
        label,terminal=text,False
    doc={'Status':text,'StatusCode':code,'ActualDeliveryDate':str(row.get('date') or row.get('eventdate') or '') if delivered else ''}
    result={'profile_name':'ukrposhta:parsebot','carrier':'Ukrposhta','doc':doc,'status_label':label,
            'terminal':terminal,'status_code':code,'place':str(row.get('name') or row.get('eventplace') or '')[:300],
            'event_at':stamp,'poll_seconds':21600}
    from tracking_service import snapshot_hash
    result['hash']=snapshot_hash(result)
    return result


def decode_batch(response, barcodes):
    if not isinstance(response,dict) or str(response.get('status','success')).lower() not in ('success','ok','200'):
        raise TrackingUnavailable('provider_error')
    value=response.get('data',response)
    if not isinstance(value,dict):raise TrackingUnavailable('unsupported_batch_response')
    result=value.get('result')
    output={}
    if isinstance(result,dict):
        if isinstance(result.get('found'),dict):result=result['found']
        normalized={}
        for key,entry in result.items():
            number=normalize_ukr_ttn(key)
            if not number:continue
            if number in normalized:raise TrackingUnavailable('duplicate_barcode')
            normalized[number]=entry
        for barcode in barcodes:
            if barcode in normalized:output[barcode]=parcel_snapshot(barcode,normalized[barcode])
        if result and not output and 'message' not in result:
            raise TrackingUnavailable('unsupported_batch_response')
    elif isinstance(result,list) and len(barcodes)==1 and ('from_to' in value or all(isinstance(e,dict) and fields(e).get('barcode')==barcodes[0] for e in result)):
        output[barcodes[0]]=parcel_snapshot(barcodes[0],value)
    elif isinstance(result,list):
        grouped={}
        for entry in result:
            if not isinstance(entry,dict):continue
            barcode=normalize_ukr_ttn(fields(entry).get('barcode'))
            if barcode not in barcodes:continue
            if 'result' in entry or 'events' in entry:
                if barcode in output:raise TrackingUnavailable('duplicate_barcode')
                output[barcode]=parcel_snapshot(barcode,entry)
            else:grouped.setdefault(barcode,[]).append(entry)
        output.update({b:parcel_snapshot(b,events) for b,events in grouped.items()})
    elif 'message' not in value:
        raise TrackingUnavailable('unsupported_batch_response')
    return output
