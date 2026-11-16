"""Cached public OLX names; shipping recipients are not used as profile names."""

import re
import time
from collections import OrderedDict
from app import AppError


def clean_name(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()[:40]


def topic_title(name, buyer, product='OLX'):
    value = '%s · ID %s · %s' % (clean_name(name) or 'Клієнт', buyer, product)
    return value.encode('utf-16-le', errors='replace')[:200].decode('utf-16-le', errors='ignore').strip()


class BuyerNames:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.pending = OrderedDict()
        self.next_lookup = 0.0

    @staticmethod
    def key(account, buyer):
        return 'olx-public-name:%s:%s' % (account, buyer)

    def cached(self, account, buyer):
        row = self.store.meta(self.key(account, buyer))
        if row.get('expires', 0) <= time.time() and len(self.pending) < 1000:
            self.pending.setdefault((str(account), str(buyer)), None)
        return clean_name(row.get('name'))

    def refresh(self):
        if not self.pending or time.monotonic() < self.next_lookup:
            return None
        account, buyer = self.pending.popitem(last=False)[0]
        self.next_lookup = time.monotonic() + 5
        old = self.store.meta(self.key(account, buyer))
        try:
            profile = self.client.user(buyer)
            if str(profile.get('id')) != buyer:
                raise AppError('OLX повернув інший профіль.')
            name, ttl = clean_name(profile.get('name')), 86400
        except (AppError, AttributeError, TypeError, ValueError):
            name, ttl = clean_name(old.get('name')), 3600
        self.store.set_meta(self.key(account, buyer), {'name': name, 'expires': time.time() + ttl})
        return account, buyer, name
