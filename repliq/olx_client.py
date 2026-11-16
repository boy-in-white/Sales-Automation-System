"""The small subset of OLX Partner API used by the console prototype."""

import re
import json
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from app import AppError, OLX

IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def identifier(item):
    value = item.get("uuid") or item.get("id")
    if value is None or not IDENTIFIER.fullmatch(str(value)):
        raise AppError("У відповіді OLX немає коректного ідентифікатора.")
    return str(value)


def unwrap_object(result):
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return result["data"]
    if isinstance(result, dict) and "error" not in result:
        return result
    raise AppError("OLX повернув неочікуваний формат об’єкта.")


def unpack_page(result):
    if isinstance(result, list):
        items, next_url = result, None
    elif isinstance(result, dict) and isinstance(result.get("data"), list):
        items = result["data"]
        links = result.get("links") or {}
        next_url = links.get("next") if isinstance(links, dict) else None
        if isinstance(next_url, dict):
            next_url = next_url.get("href")
    else:
        raise AppError("OLX повернув неочікуваний формат списку повідомлень або діалогів.")
    if any(not isinstance(item, dict) for item in items):
        raise AppError("OLX повернув пошкоджений елемент списку.")
    return items, next_url


def next_path(url, expected_path):
    parsed = urlsplit(str(url))
    if ((parsed.scheme or parsed.netloc) and
            (parsed.scheme != "https" or parsed.netloc != urlsplit(OLX).netloc)):
        raise AppError("OLX запропонував перехід на інший сервер. Перегляд зупинено.")
    if parsed.path != expected_path or parsed.fragment or parsed.username or parsed.password:
        raise AppError("OLX повернув неочікуване посилання на наступну сторінку.")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


class OlxClient:
    supports_advert_filter = True

    def __init__(self, app, minimum_interval=0.25):
        self.app = app
        self.minimum_interval = minimum_interval
        self.last_request = 0.0

    def _call(self, path, payload=None):
        delay = self.minimum_interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        token = self.app.access_token()
        self.last_request = time.monotonic()
        # POST is deliberately never retried, even if its response is lost.
        return self.app.request(path, payload=payload, token=token)

    def identity(self):
        profile = unwrap_object(self._call("/api/partner/users/me"))
        if profile.get("id") is None:
            raise AppError("OLX не передав ідентифікатор твого облікового запису.")
        return profile

    def user(self, user_id):
        """Public OLX display name from the documented /users/{id} endpoint."""
        if not str(user_id).isdecimal() or not 0 < int(user_id) < 10 ** 20:
            raise AppError('Некоректний ID користувача OLX.')
        profile = unwrap_object(self._call('/api/partner/users/' + str(user_id)))
        if str(profile.get('id')) != str(user_id):
            raise AppError('OLX повернув профіль іншого користувача.')
        return profile

    def advert(self, advert_id):
        """Read only; verify membership in the authenticated user's adverts."""
        if not str(advert_id).isdecimal() or not 0 < int(advert_id) < 10 ** 15:
            raise AppError("Потрібен числовий ID оголошення OLX.")
        found = False
        base = "/api/partner/adverts"
        path = base + "?limit=100&offset=0"
        visited = set()
        for page_number in range(20):
            if path in visited:
                raise AppError("OLX повторює сторінки оголошень.")
            visited.add(path)
            rows, next_url = unpack_page(self._call(path))
            if any(str(r.get("id")) == str(advert_id) for r in rows):
                found = True
                break
            if next_url:
                path = next_path(next_url, base)
            elif len(rows) == 100:
                path = base + "?" + urlencode({"limit": 100, "offset": (page_number + 1) * 100})
            else:
                break
        if not found:
            raise AppError("Це оголошення не знайдено серед оголошень підключеного акаунта OLX.")
        result = unwrap_object(self._call(base + "/" + str(advert_id)))
        if str(result.get("id")) != str(advert_id) or not isinstance(result.get("title"), str):
            raise AppError("OLX повернув інше оголошення або не передав його назву.")
        return result

    def threads(self, advert_id=None):
        base = "/api/partner/threads"
        params = {"limit": 100, "offset": 0}
        if advert_id:
            params["advert_id"] = str(advert_id)
        path = base + "?" + urlencode(params)
        items, identifiers, visited = [], set(), set()
        for _ in range(20):
            if path in visited:
                raise AppError("OLX повторює сторінку діалогів. Обробку зупинено.")
            visited.add(path)
            page, next_url = unpack_page(self._call(path))
            if next_url and advert_id and parse_qs(urlsplit(str(next_url)).query).get("advert_id") != [str(advert_id)]:
                raise AppError("OLX змінив фільтр оголошення. Обробку зупинено.")
            for thread in page:
                key = identifier(thread)
                if key in identifiers:
                    raise AppError("OLX повторює діалоги між сторінками. Повтори перевірку пізніше.")
                identifiers.add(key)
                items.append(thread)
            if next_url:
                path = next_path(next_url, base)
            elif len(page) == 100:
                try:
                    params["offset"] = int(parse_qs(urlsplit(path).query).get("offset", [0])[0]) + 100
                except (ValueError, TypeError):
                    raise AppError("OLX повернув некоректний номер сторінки діалогів.") from None
                path = base + "?" + urlencode(params)
            else:
                return items
        raise AppError("Забагато діалогів для цього прототипу. Запусти його з --advert-id для одного оголошення.")

    def messages(self, thread_id):
        if not IDENTIFIER.fullmatch(str(thread_id)):
            raise AppError("Некоректний ідентифікатор діалогу.")
        base = "/api/partner/threads/%s/messages" % quote(str(thread_id), safe="")
        path, items, seen, visited = base, [], set(), set()
        history_bytes = 0
        for _ in range(30):
            if path in visited:
                raise AppError("OLX повторює сторінку повідомлень.")
            visited.add(path)
            page, next_url = unpack_page(self._call(path))
            for message in page:
                key = identifier(message)
                if key in seen:
                    raise AppError("OLX повторив повідомлення між сторінками.")
                seen.add(key)
                history_bytes += len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                if history_bytes > 2 * 1024 * 1024:
                    raise AppError("Історія діалогу перевищує 2 МіБ. Перевір її вручну в OLX.")
                items.append(message)
                if len(items) > 5000:
                    raise AppError("Діалог має понад 5000 повідомлень. Потрібна ручна відповідь в OLX.")
            if not next_url:
                return items
            path = next_path(next_url, base)
        raise AppError("Не вдалося отримати повну історію діалогу. Відповідь не сформована.")

    def thread(self, thread_id):
        """One small metadata response for a fast poll, with no message history."""
        if not IDENTIFIER.fullmatch(str(thread_id)):
            raise AppError("Некоректний ідентифікатор діалогу.")
        item = unwrap_object(self._call("/api/partner/threads/%s" % quote(str(thread_id), safe="")))
        known = {str(item.get(key, "")).casefold() for key in ("uuid", "id")}
        if str(thread_id).casefold() not in known:
            raise AppError("OLX повернув метадані іншого діалогу.")
        return item

    def send(self, thread_id, text):
        if not IDENTIFIER.fullmatch(str(thread_id)):
            raise AppError("Некоректний ідентифікатор діалогу.")
        result = self._call("/api/partner/threads/%s/messages" % quote(str(thread_id), safe=""), {"text": text})
        message = unwrap_object(result)
        identifier(message)
        return message
