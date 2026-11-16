#!/usr/bin/env python3
"""Local credential setup; no tracking calls, payments or bot startup."""

from getpass import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from config import BASE_DIR
from ukrposhta_api import SCRAPER_ID


def save_private(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(path.parent,0o700)
    descriptor,name=tempfile.mkstemp(dir=path.parent,prefix='.saving-')
    try:
        with os.fdopen(descriptor,'w') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    path=BASE_DIR/'private/ukrposhta.json'
    previous=json.loads(path.read_text()) if path.exists() else {}
    print('Укрпошта · автотрекінг через Parse.bot')
    print('У Parse.bot підпишись на Ukrposhta API. Ключ: Settings → Account → API Keys.')
    print('https://parse.bot/marketplace/c10d653e-b6d5-4a07-b4ea-81a421befd05/track-ukrposhta-ua-api')
    key=getpass('API-ключ Parse.bot (введення приховане; Enter — зберегти попередній): ').strip() or previous.get('api_key','')
    if not key or any(c.isspace() for c in key):
        print('Ключ не введений або некоректний. Налаштування не змінені.');return
    scraper=previous.get('scraper_id',SCRAPER_ID)
    if not isinstance(scraper,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',scraper):scraper=SCRAPER_ID
    updated=dict(previous,enabled=True,api_key=key,scraper_id=scraper,
                 monthly_limit=previous.get('monthly_limit',160),total_limit=previous.get('total_limit',180),
                 budget_id=previous.get('budget_id') or uuid.uuid4().hex)
    if previous:save_private(path.with_name('ukrposhta-backup.json'),previous)
    save_private(path,updated)
    print('Збережено. Перезапусти Ledgerly. Repliq для цього перезапускати не потрібно.')
    print('Пакети — до 30 ТТН. Ліміти: 160 спроб/місяць, 180 загалом без окремого збільшення.')
    print('Платні поповнення програма не виконує. Після вичерпання ліміту трекінг призупиниться.')
    print('Ключ збережений приватно. Доступ і реальну ТТН ще потрібно перевірити після підключення.')


if __name__=='__main__':
    try:main()
    except (EOFError,KeyboardInterrupt):print('\nНалаштування перервано.')
    except Exception:print('Налаштування не вдалося зберегти. Перевір права папки private та формат ukrposhta.json.')
