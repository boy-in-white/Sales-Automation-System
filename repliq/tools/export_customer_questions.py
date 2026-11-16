"""Read-only OLX research export. Customer questions only; no seller reply text."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Repliq, olx_request, write_private
from olx_client import OlxClient, identifier, unpack_page, next_path


def read_only_request(path, payload=None, token=None):
    route = urlsplit(path)
    if route.scheme or route.netloc or route.fragment:
        raise AppError('Research export rejected a non-local API path.')
    if payload is not None:
        if route.path != '/api/open/oauth/token' or payload.get('grant_type') != 'refresh_token':
            raise AppError('Research export permits only reading chats and refreshing existing access.')
    elif not re.fullmatch(r'/api/partner/(?:users/me|threads(?:/[A-Za-z0-9_-]+(?:/messages)?)?|adverts)', route.path):
        raise AppError('Research export rejected a non-read endpoint.')
    for attempt in range(4):
        try:
            return olx_request(path, payload, token)
        except AppError as error:
            # Refresh requests must never be repeated after an ambiguous failure.
            if payload is not None or error.status not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep((5, 15, 30)[attempt])


def metadata(thread):
    advert = thread.get('advert') or {}
    return {'thread': identifier(thread), 'advert_id': str(thread.get('advert_id') or (advert.get('id') if isinstance(advert, dict) else '') or ''),
            'advert_title': str(thread.get('advert_title') or (advert.get('title') if isinstance(advert, dict) else '') or '')[:300],
            'updated_at': thread.get('updated_at') or thread.get('last_message_at') or thread.get('created_at')}


def export(data_dir, output, limit):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    app = Repliq(data_dir=data_dir, request=read_only_request)
    client = OlxClient(app, minimum_interval=0.6)
    client.identity()  # Verify current access; do not render profile identifiers.
    threads, seen, pages = [], set(), set()
    base, path, offset = '/api/partner/threads', '/api/partner/threads?limit=100&offset=0', 0
    while path and len(threads) < limit:
        if path in pages:
            raise AppError('OLX repeated a page; export stopped safely.')
        pages.add(path)
        items, next_url = unpack_page(client._call(path))
        for row in items:
            key = identifier(row)
            if key not in seen:
                seen.add(key)
                threads.append(metadata(row))
        print('Threads listed:', len(threads), flush=True)
        if not items:
            break
        if next_url:
            path = next_path(next_url, base)
        elif len(items) == 100:
            offset += len(items)
            path = base + '?' + urlencode({'limit': 100, 'offset': offset})
        else:
            path = None
    threads = threads[:limit]
    write_private(output/'thread-index.json', threads)
    target = output/'customer-chats.jsonl'
    completed = set()
    total, messages, errors = 0, 0, []
    if target.exists():
        for line in target.read_text(encoding='utf-8').splitlines():
            row=json.loads(line);completed.add(row['thread']);total+=1;messages+=len(row['buyer_messages'])
    with os.fdopen(os.open(target, os.O_CREAT|os.O_WRONLY|os.O_APPEND, 0o600), 'a', encoding='utf-8') as stream:
        os.chmod(target, 0o600)
        for index, thread in enumerate(threads, 1):
            if thread['thread'] in completed:
                continue
            try:
                history = client.messages(thread['thread'])
            except AppError as error:
                errors.append({'thread':thread['thread'], 'status':error.status or 0})
                if error.status in (401,403,429) or len(errors)>=10:
                    print('Export paused, API status:', error.status or 0, flush=True)
                    break
                continue
            received = [{'message':identifier(m),'date':m.get('created_at'), 'text':str(m.get('text') or ''),
                         'attachments':len(m.get('attachments') or [])}
                        for m in history if m.get('type') == 'received']
            received.sort(key=lambda m:str(m['date'] or ''))
            record=dict(thread,buyer_messages=received,total_messages=len(history),seller_messages=sum(m.get('type')=='sent' for m in history))
            stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
            total+=1;messages+=len(received)
            if index%20 == 0 or index==len(threads):
                print('Chats exported: %s/%s; customer messages: %s' % (total,len(threads),messages), flush=True)
    summary={'listed_threads':len(threads),'downloaded_threads':total,'customer_messages':messages,'errors':errors,
             'seller_text_saved':False,'attachments_downloaded':False,'read_only':True,'exported_at':time.time()}
    write_private(output/'summary.json', summary)
    print('Export complete:', json.dumps({k:v for k,v in summary.items() if k not in ('errors','exported_at')},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=1500)
    args=parser.parse_args()
    if not 1<=args.limit<=1500:parser.error('limit must be 1–1500')
    try:
        export(args.data_dir,args.output,args.limit)
    except AppError as error:
        print('Export stopped:',str(error),file=sys.stderr)
        raise SystemExit(1)
