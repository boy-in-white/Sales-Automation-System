#!/usr/bin/env python3
"""Repliq: a single-user, loopback-only OLX OAuth bootstrap application."""

import argparse
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from contextlib import contextmanager
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OLX = "https://www.olx.ua"
SCOPE = "v2 read write"
CALLBACK_PATH = "/oauth/callback"
TTL = 600
MAX_API_BYTES = 4 * 1024 * 1024


class AppError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def matches(first, second):
    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def read_json(path):
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise AppError("Файл налаштувань пошкоджений.")
    return data


def write_private(path, data):
    """Replace the entire document atomically; secrets never use default permissions."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".saving-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def private_lock(path, blocking=True):
    """Coordinate the browser helper and console processes without exposing keys."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


def callback_url(value, port):
    parsed = urlsplit(value)
    local = parsed.hostname in ("localhost", "127.0.0.1")
    try:
        valid_port = parsed.port == port if local else parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (not parsed.hostname or parsed.username or parsed.password or
            parsed.path != CALLBACK_PATH or parsed.query or parsed.fragment or
            not valid_port or
            (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
        raise AppError(
            "Вкажи http://127.0.0.1:%s/oauth/callback або HTTPS-адресу "
            "твого сервера з шляхом /oauth/callback." % port
        )
    return value


def olx_request(path, payload=None, token=None):
    headers = {"Accept": "application/json", "Version": "2.0", "User-Agent": "Repliq/0.1"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(OLX + path, data=body, headers=headers)
    try:
        timeout = 5 if re.fullmatch(r'/api/partner/users/\d+', path) else 25
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise AppError("Відповідь OLX завелика для автоматичної обробки. Перевір діалог вручну.")
            result = json.loads(raw)
    except HTTPError as error:
        messages = {
            400: "Перевір Client ID, Client Secret і точний збіг URI з порталом OLX.",
            401: "Доступ відхилено. Перевір ключі або підключи обліковий запис повторно.",
            403: "OLX не дозволив цю дію. Перевір схвалення додатка та його дозволи.",
            429: "Забагато запитів. Повтори пізніше.",
        }
        raise AppError("OLX: HTTP %s. %s" % (
            error.code, messages.get(error.code, "Спробуй повторити пізніше.")), status=error.code) from None
    except (URLError, TimeoutError, OSError):
        raise AppError("Не вдалося зв’язатися з OLX. Перевір інтернет і повтори дію.") from None
    except (ValueError, UnicodeError):
        raise AppError("OLX повернув неочікувану відповідь. Повтори підключення.") from None
    if not isinstance(result, (dict, list)):
        raise AppError("OLX повернув неочікуваний формат відповіді.")
    return result


class Repliq:
    def __init__(self, port=8787, data_dir=None, request=olx_request):
        self.port = port
        self.local_url = "http://127.0.0.1:%s" % port
        self.data_dir = Path(data_dir) if data_dir else ROOT / "private"
        self.settings_path = self.data_dir / "settings.json"
        self.tokens_path = self.data_dir / "tokens.json"
        self.settings = read_json(self.settings_path)
        self.settings.setdefault("callback_uri", self.local_url + CALLBACK_PATH)
        callback_url(self.settings["callback_uri"], port)
        self.request = request
        self.admin_session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.pending = {}
        self.notice = ""
        self.request_lock = threading.RLock()

    def save_settings(self, fields):
        with private_lock(self.data_dir / "tokens.lock"):
            previous = read_json(self.settings_path) if self.settings_path.exists() else self.settings
            updated = {
                "callback_uri": callback_url(fields.get("callback_uri", "").strip(), self.port),
                "client_id": fields.get("client_id", "").strip() or previous.get("client_id", ""),
                "client_secret": fields.get("client_secret", "").strip() or previous.get("client_secret", ""),
            }
            if updated != previous:
                self.backup_oauth(previous, read_json(self.tokens_path))
                # A new tunnel changes only the next browser authorization.
                # Existing grants and refresh requests do not use redirect_uri.
                if any(updated[key] != previous.get(key, "")
                       for key in ("client_id", "client_secret")):
                    write_private(self.tokens_path, {})
                self.pending.clear()
            write_private(self.settings_path, updated)
            self.settings = updated

    def backup_oauth(self, settings, tokens):
        """Keep one private recovery copy; an empty grant never replaces it."""
        if (all(settings.get(key) for key in ("client_id", "client_secret")) and
                all(isinstance(tokens.get(key), str) and tokens[key]
                    for key in ("access_token", "refresh_token"))):
            write_private(self.data_dir / "oauth-backup.json", {
                "settings": settings, "tokens": tokens,
            })

    def credentials(self):
        if not self.settings.get("client_id") or not self.settings.get("client_secret"):
            raise AppError("Спочатку дочекайся схвалення OLX та збережи Client ID і Client Secret.")
        return {key: self.settings[key] for key in ("client_id", "client_secret")}

    def begin(self):
        self.credentials()
        self.pending = {key: item for key, item in self.pending.items() if item["expires"] > time.time()}
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = {"expires": time.time() + TTL, "stage": "ticket"}
        origin = urlsplit(self.settings["callback_uri"])
        return "%s://%s/oauth/begin?%s" % (origin.scheme, origin.netloc, urlencode({"ticket": ticket}))

    def authorize(self, ticket):
        pending = self.pending.pop(ticket, None)
        if not pending or pending["expires"] < time.time() or pending["stage"] != "ticket":
            raise AppError("Сеанс закінчився. Натисни «Підключити OLX» у Repliq ще раз.")
        state = secrets.token_urlsafe(32)
        browser_session = secrets.token_urlsafe(32)
        self.pending[state] = {"expires": time.time() + TTL, "stage": "callback", "browser": browser_session}
        parameters = {
            "client_id": self.settings["client_id"], "response_type": "code",
            "state": state, "scope": SCOPE, "redirect_uri": self.settings["callback_uri"],
        }
        return OLX + "/oauth/authorize/?" + urlencode(parameters), browser_session

    def finish(self, fields, browser_session):
        state = fields.get("state", "")
        pending = self.pending.get(state)
        if (not pending or pending["expires"] < time.time() or
                pending["stage"] != "callback" or not browser_session or
                not matches(pending["browser"], browser_session)):
            raise AppError("Не вдалося підтвердити сеанс. Почни підключення кнопкою в Repliq.")
        self.pending.pop(state)
        if fields.get("error"):
            raise AppError("Доступ на стороні OLX не надано. Можна почати підключення повторно.")
        code = fields.get("code")
        if not code:
            raise AppError("OLX не передав код. Почни підключення повторно.")
        payload = self.credentials()
        payload.update(grant_type="authorization_code", code=code, scope=SCOPE,
                       redirect_uri=self.settings["callback_uri"])
        self.store_tokens(self.request("/api/open/oauth/token", payload))

    def store_tokens(self, result, old=None):
        with private_lock(self.data_dir / "tokens.lock"):
            self._store_tokens(result, old)

    def _store_tokens(self, result, old=None):
        if not isinstance(result, dict):
            raise AppError("OLX не повернув коректні токени.")
        old = old or {}
        access = result.get("access_token")
        refresh = result.get("refresh_token") or old.get("refresh_token")
        try:
            duration = int(result["expires_in"])
        except (KeyError, TypeError, ValueError):
            duration = 0
        if (not isinstance(access, str) or not access or
                not isinstance(refresh, str) or not refresh or not 0 < duration <= 366 * 86400 or
                str(result.get("token_type", "bearer")).lower() != "bearer"):
            raise AppError("OLX не повернув повний набір токенів. Попередній доступ збережено.")
        # Only known fields are persisted. Neither token is ever rendered in HTML.
        tokens = {"access_token": access, "refresh_token": refresh,
                  "scope": result.get("scope", old.get("scope", "")),
                  "token_type": "bearer", "expires_at": time.time() + duration}
        # Persist the rotated refresh token in recovery storage before replacing
        # the main file, so an interrupted write still leaves a usable copy.
        self.backup_oauth(self.settings, tokens)
        write_private(self.tokens_path, tokens)

    def refresh(self):
        with private_lock(self.data_dir / "tokens.lock"):
            self._refresh_tokens(read_json(self.tokens_path))

    def _refresh_tokens(self, old):
        if not old.get("refresh_token"):
            raise AppError("Спочатку підключи обліковий запис OLX.")
        payload = self.credentials()
        payload.update(grant_type="refresh_token", refresh_token=old["refresh_token"])
        self._store_tokens(self.request("/api/open/oauth/token", payload), old)

    def access_token(self):
        with private_lock(self.data_dir / "tokens.lock"):
            tokens = read_json(self.tokens_path)
            if not tokens.get("access_token"):
                raise AppError("Спочатку авторизуй обліковий запис кнопкою «Підключити OLX» у Repliq.")
            if tokens.get("expires_at", 0) <= time.time() + 60:
                self._refresh_tokens(tokens)
                tokens = read_json(self.tokens_path)
            return tokens["access_token"]

    def verify(self):
        tokens = read_json(self.tokens_path)
        if not tokens.get("access_token"):
            raise AppError("Спочатку підключи обліковий запис OLX.")
        if tokens.get("expires_at", 0) <= time.time() + 30:
            self.refresh()
            tokens = read_json(self.tokens_path)
        self.request("/api/partner/users/me", token=tokens["access_token"])

    def render(self):
        tokens = read_json(self.tokens_path)
        connected = bool(tokens.get("access_token"))
        ready = bool(self.settings.get("client_id") and self.settings.get("client_secret"))
        expired = connected and tokens.get("expires_at", 0) <= time.time()
        status = "Токен потребує оновлення" if expired else (
            "Обліковий запис підключено" if connected else (
                "Готово до авторизації" if ready else "Підготуй заявку до OLX"))
        expires = datetime.fromtimestamp(tokens["expires_at"]).astimezone().strftime("%d.%m.%Y %H:%M %Z") if connected else "—"
        values = {
            "csrf": self.csrf, "callback_uri": self.settings["callback_uri"],
            "client_id": self.settings.get("client_id", ""), "status": status,
            "notice": self.notice, "notice_hidden": "" if self.notice else "hidden",
            "connect_disabled": "" if ready else "disabled",
            "token_disabled": "" if connected else "disabled", "expires": expires,
            "secret_hint": "Секрет збережено. Залиш поле порожнім, щоб зберегти його." if self.settings.get("client_secret") else "Буде доступний після схвалення OLX.",
            "scope": tokens.get("scope", "—"),
            "registration_uri": self.settings["callback_uri"] if self.settings["callback_uri"].startswith("https://") else "Спочатку запусти «Публічний запуск Repliq.command»",
            "callback_hint": "Тимчасова HTTPS-адреса працює, поки відкрито публічний запуск Repliq. Після зміни адреси онови її в OLX перед новою авторизацією. Збережений доступ при цьому залишається." if ".trycloudflare.com/" in self.settings["callback_uri"] else (
                "Адреса має точно збігатися з URI у заявці OLX." if self.settings["callback_uri"].startswith("https://") else "OLX не приймає локальні адреси. Для заявки потрібна публічна HTTPS-адреса."),
        }
        template = Template((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        return template.substitute({key: html.escape(str(value), quote=True) for key, value in values.items()})


class Handler(BaseHTTPRequestHandler):
    server_version = "Repliq"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Default access logs include callback codes and OAuth state in the URL.
        pass

    def cookies(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}
        return {key: item.value for key, item in jar.items()}

    def is_local_host(self):
        return self.headers.get("Host") in (
            "127.0.0.1:%s" % self.app.port, "localhost:%s" % self.app.port)

    def send(self, status, body="", headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        oauth_page = urlsplit(self.path).path in ("/oauth/begin", CALLBACK_PATH)
        # no-referrer makes normal browser form POSTs send Origin: null.
        # Keep the real origin on local forms; OAuth URLs must not be referred.
        referrer_policy = "no-referrer" if oauth_page or getattr(self.server, "public_only", False) else "same-origin"
        for name, value in {
            "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
            "Referrer-Policy": referrer_policy, "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Content-Length": str(len(data)), **(headers or {}),
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url, cookie=None):
        headers = {"Location": url}
        if cookie:
            headers["Set-Cookie"] = cookie
        self.send(303, headers=headers)

    def error_page(self, text):
        self.send(400, "<!doctype html><html lang='uk'><meta charset='utf-8'>"
                  "<title>Repliq</title><h1>Repliq</h1><p>%s</p>"
                  "<a href='%s'>Повернутися в Repliq</a></html>" % (
                      html.escape(text), self.app.local_url))

    def do_GET(self):
        with self.app.request_lock:
            self.get()

    def get(self):
        parsed = urlsplit(self.path)
        if getattr(self.server, "public_only", False):
            if parsed.path == "/":
                self.send(200, "<!doctype html><html lang='uk'><meta charset='utf-8'>"
                          "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                          "<title>Repliq · OLX</title><h1>Repliq</h1>"
                          "<p>Окремий застосунок для автоматизації відповідей на повідомлення OLX.</p>"
                          "<p>Публічний обробник авторизації OLX працює. Керування доступне локально.</p></html>")
                return
            if parsed.path not in ("/oauth/begin", CALLBACK_PATH):
                self.send(404, "Сторінку не знайдено.")
                return
        fields = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        try:
            if (parsed.path in ("/oauth/begin", CALLBACK_PATH) and
                    self.headers.get("Host") != urlsplit(self.app.settings["callback_uri"]).netloc):
                self.send(400, "Адреса повернення має збігатися з URI у налаштуваннях Repliq.")
                return
            if parsed.path == "/oauth/begin":
                target, browser_session = self.app.authorize(fields.get("ticket", ""))
                secure = "; Secure" if urlsplit(self.app.settings["callback_uri"]).scheme == "https" else ""
                self.redirect(target, "repliq_oauth=%s; Path=/oauth; HttpOnly; SameSite=Lax; Max-Age=%s%s" % (browser_session, TTL, secure))
            elif parsed.path == CALLBACK_PATH:
                if not fields:
                    self.send(200, "<!doctype html><html lang='uk'><meta charset='utf-8'>"
                              "<title>Repliq · OLX</title><h1>Repliq · OLX</h1>"
                              "<p>Адреса зворотного виклику працює. Почни підключення в Repliq.</p></html>")
                    return
                self.app.finish(fields, self.cookies().get("repliq_oauth", ""))
                self.app.notice = "OLX підключено. Тепер можна перевірити доступ до API."
                self.redirect(self.app.local_url, "repliq_oauth=; Path=/oauth; Max-Age=0; HttpOnly; SameSite=Lax")
            elif parsed.path == "/" and self.is_local_host():
                self.send(200, self.app.render(), {"Set-Cookie": "repliq_admin=%s; Path=/; HttpOnly; SameSite=Strict" % self.app.admin_session})
            elif parsed.path == "/health":
                self.send(200, "Repliq OK")
            else:
                self.send(404, "Сторінку не знайдено.")
        except AppError as error:
            self.error_page(str(error))
        except (OSError, ValueError):
            self.error_page("Не вдалося прочитати або зберегти локальні дані. Перевір папку private.")

    def do_POST(self):
        with self.app.request_lock:
            self.post()

    def post(self):
        if getattr(self.server, "public_only", False):
            self.send(404, "Сторінку не знайдено.")
            return
        if (not self.is_local_host() or
                not matches(self.cookies().get("repliq_admin", ""), self.app.admin_session) or
                self.headers.get("Origin", self.app.local_url) not in (
                    self.app.local_url, "http://localhost:%s" % self.app.port)):
            self.send(403, "<!doctype html><html lang='uk'><meta charset='utf-8'>"
                      "<meta name='referrer' content='same-origin'><title>Repliq</title>"
                      "<h1>Потрібно оновити сторінку Repliq</h1>"
                      "<p>Форму ще не збережено. Відкрий головну сторінку та повтори збереження.</p>"
                      "<a href='%s'>Відкрити головну сторінку Repliq</a></html>" % self.app.local_url)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.send(413, "Некоректний розмір запиту.")
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                self.send(415, "Некоректний формат запиту.")
                return
            fields = {key: value[0] for key, value in parse_qs(
                self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
            if not matches(fields.get("csrf", ""), self.app.csrf):
                self.send(403, "Онови сторінку і повтори дію.")
                return
            if self.path == "/settings":
                self.app.save_settings(fields)
                self.app.notice = "Налаштування збережено. URI має точно збігатися з адресою в заявці OLX."
            elif self.path == "/connect":
                self.redirect(self.app.begin())
                return
            elif self.path == "/refresh":
                self.app.refresh()
                self.app.notice = "Токени оновлено та збережено."
            elif self.path == "/verify":
                self.app.verify()
                self.app.notice = "Перевірка успішна: OLX API надав доступ до твого облікового запису."
            else:
                self.send(404, "Сторінку не знайдено.")
                return
        except AppError as error:
            self.app.notice = str(error)
        except (ValueError, UnicodeError, OSError):
            self.app.notice = "Не вдалося обробити запит або зберегти дані. Перевір налаштування і повтори."
        self.redirect("/")


def make_server(app, port=None, public_only=False):
    server = HTTPServer(("127.0.0.1", app.port if port is None else port), Handler)
    server.app = app
    server.public_only = public_only
    server.timeout = 1
    return server


def main():
    parser = argparse.ArgumentParser(description="Repliq — підключення OLX API")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="Відкрити програму в браузері")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Вкажи порт від 1024 до 65535.")
    try:
        app = Repliq(args.port)
        server = make_server(app)
    except (OSError, ValueError, AppError):
        parser.exit(1, "Не вдалося запустити Repliq: перевір порт і файли в private.\n")
    print("Repliq: " + app.local_url, flush=True)
    print("URI зворотного виклику: " + app.settings["callback_uri"], flush=True)
    print("Щоб зупинити програму, натисни Ctrl+C.", flush=True)
    if args.open:
        webbrowser.open(app.local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
