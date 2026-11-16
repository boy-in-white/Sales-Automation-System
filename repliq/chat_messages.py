"""Strict message boundaries: only counted new buyer messages may create drafts."""

import hashlib
import json
from datetime import datetime

from app import AppError
from olx_client import identifier
from customer_phrasing import platform_notice


def count(thread):
    value = thread.get("total_count")
    return int(value) if type(value) in (int, str) and str(value).isdecimal() else None


def ordered(messages, thread_id, timezone):
    items, ids = [], set()
    for message in messages:
        key = identifier(message)
        if key in ids:
            raise AppError("OLX повторив повідомлення.")
        ids.add(key)
        bound = message.get("thread_uuid")
        if bound and str(bound).casefold() != str(thread_id).casefold():
            # Numeric legacy thread IDs are checked by the client endpoint instead.
            if not str(thread_id).isdecimal():
                raise AppError("OLX повернув повідомлення іншого діалогу.")
        try:
            date = datetime.fromisoformat(str(message.get("created_at")).replace("Z", "+00:00"))
            epoch = date.replace(tzinfo=timezone).timestamp() if date.tzinfo is None else date.timestamp()
        except (ValueError, TypeError, OverflowError):
            raise AppError("Невідомий час повідомлення; потрібна ручна перевірка діалогу.") from None
        text = str(message.get("text") or "")
        if len(text) > 16000:
            raise AppError("Повідомлення завелике; переглянь його вручну в OLX.")
        items.append(dict(message, _id=key, _time=epoch, text=text))
    items.sort(key=lambda m: (m["_time"], int(m["id"]) if str(m.get("id", "")).isdecimal() else 0))
    for left, right in zip(items, items[1:]):
        if left["_time"] == right["_time"] and not all(str(m.get("id", "")).isdecimal() for m in (left, right)):
            raise AppError("Повідомлення мають однаковий час без порядкових ID; перевір діалог вручну.")
    return items


def fingerprint(messages):
    values = [(m["_id"], m.get("type"), m["text"], m.get("created_at"), m.get("attachments")) for m in messages]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate(messages, allowed, include_delivery_notices=False):
    # OLX purchase notifications can arrive with type=received. They are not a
    # buyer utterance and must not cause a conversational reply or fake payment.
    original = messages
    if not include_delivery_notices:
        messages = [m for m in messages if not platform_notice(m.get('text', ''))]
    if not messages or messages[-1].get("type") != "received":
        return None
    run = []
    for item in reversed(messages):
        if item.get("type") != "received":
            break
        if item["_id"] in allowed:
            run.append(item)
    run.reverse()
    if not run or run[-1]["_id"] != messages[-1]["_id"]:
        return None
    from urllib.parse import urlsplit
    links = []
    buyer_run = [m for m in run if not platform_notice(m.get('text', ''))]
    for message in buyer_run:
        attachments = message.get("attachments") or []
        for item in attachments if isinstance(attachments, list) else [attachments]:
            url = item.get("url") if isinstance(item, dict) else None
            if isinstance(url, str) and len(url) <= 2000:
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and url not in links:
                    links.append(url)
    return {"source_ids": [m["_id"] for m in run], "texts": [m["text"] for m in buyer_run], "receipt_links": links[-3:],
            "has_attachments": any(bool(m.get("attachments")) for m in buyer_run), "fingerprint": fingerprint(original),
            "delivery_notices": [m['_id'] for m in run if platform_notice(m.get('text', ''))]}
