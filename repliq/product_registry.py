"""Repliq-only catalogue cache and advert bindings. No Ledgerly writes."""

import copy
import hashlib
import json
import re
import time
import uuid

from catalog import Catalog, normalize
from handoff_store import telegram_parts


def words(text):
    return " ".join(re.findall(r"[\w]+", normalize(text)))


class Registry:
    def __init__(self, store, account):
        self.store, self.account = store, str(account)
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS registry_products (
                    account TEXT NOT NULL, product_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(account,product_id)
                );
                CREATE TABLE IF NOT EXISTS advert_bindings (
                    account TEXT NOT NULL, advert TEXT NOT NULL, product_id TEXT,
                    active INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', price REAL,
                    updated REAL NOT NULL, PRIMARY KEY(account,advert)
                );
                CREATE TABLE IF NOT EXISTS effective_ads (
                    account TEXT NOT NULL, advert TEXT NOT NULL, product_id TEXT,
                    PRIMARY KEY(account,advert)
                );
                CREATE TABLE IF NOT EXISTS admin_jobs (
                    event_key TEXT PRIMARY KEY, account TEXT NOT NULL, owner INTEGER NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_outbox (
                    id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, account TEXT NOT NULL,
                    owner INTEGER NOT NULL, text TEXT NOT NULL, keyboard TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS admin_choices (
                    token TEXT PRIMARY KEY, account TEXT NOT NULL, owner INTEGER NOT NULL,
                    payload TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    account TEXT NOT NULL, owner INTEGER NOT NULL, action TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(account,owner)
                );
                CREATE TABLE IF NOT EXISTS registry_sizing (
                    account TEXT NOT NULL, product_id TEXT NOT NULL, data TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(account,product_id)
                );
                CREATE INDEX IF NOT EXISTS admin_queue ON admin_jobs(account,status,created);
                CREATE INDEX IF NOT EXISTS admin_delivery ON admin_outbox(account,owner,status,id);
                CREATE INDEX IF NOT EXISTS admin_choice_expiration ON admin_choices(expires);
            """)

    def snapshot(self, products):
        signature = hashlib.sha256(json.dumps(products, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.store.meta("products-sync:" + self.account).get("signature") == signature:
            self.store.set_meta("products-sync:" + self.account, {"updated": time.time(), "valid": True, "signature": signature})
            return
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM registry_products WHERE account=?", (self.account,))
            db.executemany("INSERT INTO registry_products VALUES(?,?,?)",
                           [(self.account, str(p["id"]), json.dumps(p, ensure_ascii=False)) for p in products])
        self.store.set_meta("products-sync:" + self.account, {"updated": time.time(), "valid": True, "signature": signature})

    def cleanup(self, days):
        with self.store.db() as db:
            db.execute("DELETE FROM admin_choices WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM admin_sessions WHERE updated<?", (time.time() - 600,))
            db.execute("DELETE FROM admin_jobs WHERE status IN ('done','failed','rejected') AND created<?", (time.time() - days * 86400,))

    def products(self):
        with self.store.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM registry_products WHERE account=? ORDER BY product_id", (self.account,))]

    def sizing_profiles(self):
        with self.store.db() as db:
            return {r['product_id']:json.loads(r['data']) for r in db.execute(
                'SELECT product_id,data FROM registry_sizing WHERE account=?',(self.account,))}

    def sizing_profile(self, product_id):
        with self.store.db() as db:
            row = db.execute('SELECT revision,data FROM registry_sizing WHERE account=? AND product_id=?',
                             (self.account,str(product_id))).fetchone()
            return (row['revision'],json.loads(row['data'])) if row else (0,{})

    def save_sizing(self, product_id, mode, patch, revision):
        allowed = ({'measurements','garment_measurements'} if mode=='measurements' else
                   {'sizing_basis','height_guidance','size_chart_verified','sizes'} if mode=='guide' else
                   {'description'} if mode=='description' else
                   {'size_substitution_enabled'} if mode=='size_substitution' else
                   {'facts'} if mode.startswith('fact:') and mode[5:] in EDITABLE_FACTS else set())
        if mode == "size_substitution" and type(patch.get("size_substitution_enabled")) is not bool:
            return False
        if not allowed or set(patch) != allowed or type(revision) is not int:
            return False
        if mode.startswith('fact:') and (not isinstance(patch.get('facts'), dict) or set(patch['facts']) != {mode[5:]}):
            return False
        if mode == 'description' or mode.startswith('fact:'):
            value = patch['description'] if mode == 'description' else patch['facts'][mode[5:]]
            limit = 400 if mode.startswith('fact:') else 600
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= limit):
                return False
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM registry_products WHERE account=? AND product_id=?',
                              (self.account,str(product_id))).fetchone():
                return False
            row = db.execute('SELECT revision,data FROM registry_sizing WHERE account=? AND product_id=?',
                             (self.account,str(product_id))).fetchone()
            if revision != (row['revision'] if row else 0):
                return False
            data = json.loads(row['data']) if row else {}
            if mode.startswith('fact:'):
                data.setdefault('facts', {}).update(patch['facts'])
            else:
                data.update(patch)
            db.execute('INSERT OR REPLACE INTO registry_sizing VALUES(?,?,?,?,?)',
                       (self.account,str(product_id),json.dumps(data,ensure_ascii=False),revision+1,time.time()))
            return True

    def bindings(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM advert_bindings WHERE account=? ORDER BY advert", (self.account,))]

    def bind(self, advert, product_id, title, price=None):
        with self.store.db() as db:
            db.execute("INSERT INTO advert_bindings VALUES(?,?,?,1,?,?,?) ON CONFLICT(account,advert) DO UPDATE SET "
                       "product_id=excluded.product_id,active=1,title=excluded.title,price=excluded.price,updated=excluded.updated",
                       (self.account, str(advert), str(product_id), title[:500], price, time.time()))

    def disable(self, advert):
        with self.store.db() as db:
            db.execute("INSERT INTO advert_bindings VALUES(?,?,NULL,0,'',NULL,?) ON CONFLICT(account,advert) "
                       "DO UPDATE SET active=0,updated=excluded.updated", (self.account, str(advert), time.time()))

    def publish(self, catalog):
        values = sorted((self.account, ad, catalog.products[key].get("ledgerly_product_id")) for ad, key in catalog.adverts.items())
        with self.store.db() as db:
            current = sorted(tuple(r) for r in db.execute("SELECT account,advert,product_id FROM effective_ads WHERE account=?", (self.account,)))
            if values == current:
                return
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM effective_ads WHERE account=?", (self.account,))
            db.executemany("INSERT INTO effective_ads VALUES(?,?,?)",
                           values)

    def allowed(self, advert):
        with self.store.db() as db:
            return db.execute("SELECT 1 FROM effective_ads WHERE account=? AND advert=?", (self.account, str(advert))).fetchone() is not None

    def tell(self, owner, key, text, keyboard=None):
        parts = list(telegram_parts(text, limit=3200))
        with self.store.db() as db:
            for index, part in enumerate(parts):
                markup = (keyboard or self.menu()) if index == len(parts) - 1 else {}
                db.execute("INSERT OR IGNORE INTO admin_outbox(event_key,account,owner,text,keyboard) VALUES(?,?,?,?,?)",
                           (key + ":" + str(index), self.account, owner, part, json.dumps(markup, ensure_ascii=False)))

    def job(self, owner, key, action, payload):
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO admin_jobs VALUES(?,?,?,?,?,'queued',?)",
                       (key, self.account, owner, action, json.dumps(payload, ensure_ascii=False), time.time()))

    def next_job(self, allowed=None, claim=False):
        with self.store.db() as db:
            if claim:
                db.execute('BEGIN IMMEDIATE')
            condition = " AND action IN (" + ','.join('?' for _ in allowed) + ")" if allowed else ""
            args = [self.account] + list(allowed or [])
            row = db.execute("SELECT * FROM admin_jobs WHERE account=? AND status='queued'" + condition + " ORDER BY created LIMIT 1", args).fetchone()
            if row and claim:
                db.execute("UPDATE admin_jobs SET status='working' WHERE event_key=? AND status='queued'", (row['event_key'],))
        return dict(row, payload=json.loads(row["payload"])) if row else None

    def finish(self, key, status="done"):
        with self.store.db() as db:
            db.execute("UPDATE admin_jobs SET status=? WHERE event_key=?", (status, key))

    def choice(self, owner, payload):
        token = uuid.uuid4().hex
        with self.store.db() as db:
            db.execute("INSERT INTO admin_choices VALUES(?,?,?,?,?,0)",
                       (token, self.account, owner, json.dumps(payload, ensure_ascii=False), time.time() + 600))
        return "product:" + token

    def select(self, owner, token, event_key):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_choices WHERE token=? AND account=? AND owner=? AND used=0 AND expires>?",
                             (token, self.account, owner, time.time())).fetchone()
            if not row:
                return False
            db.execute("UPDATE admin_choices SET used=1 WHERE token=?", (token,))
            payload = json.loads(row['payload'])
            action = payload.pop('_action','bind')
            if action not in ('bind','sizing_list','sizing_open','sizing_facts','sizing_edit','sizing_save','sizing_cancel'):
                return False
            db.execute("INSERT OR IGNORE INTO admin_jobs VALUES(?,?,?,?,?,'queued',?)",
                       (event_key, self.account, owner, action,json.dumps(payload,ensure_ascii=False), time.time()))
        return True

    @staticmethod
    def menu(catalog=False):
        rows = ([["➕ Додати оголошення", "✏️ Змінити оголошення"],
                 ["⛔ Вимкнути оголошення", "🔄 Оновити каталог"], ["📏 Заміри та розміри"], ["🏠 Головне меню"]] if catalog else
                [["🙋 Потрібен я", "💬 Діалоги"], ["📦 Товари", "📋 Оголошення"], ["💳 Стан mono", "⚙️ Керування каталогом"]])
        return {"keyboard": rows,
                "resize_keyboard": True, "input_field_placeholder": "Обери дію кнопкою нижче…"}

    def private_message(self, owner, text, key):
        text = text.strip()
        from product_sizing import input_message, session_keyboard
        if input_message(self,owner,text,key):
            return
        if text == "⚙️ Керування каталогом":
            with self.store.db() as db:
                db.execute("DELETE FROM admin_sessions WHERE account=? AND owner=?", (self.account, owner))
            self.tell(owner, key, "⚙️ Каталог Repliq\n\nОбери дію. Товари, ціни й залишки ведуться в Ledgerly. Тут — оголошення OLX, заміри одягу та сітка для підбору розміру.", self.menu(catalog=True))
            return
        labels = {"Товари": "/products", "Оголошення": "/ads", "Додати оголошення": "/add_ad", "Оновити з Ledgerly": "/refresh",
                  "📦 Товари": "/products", "📋 Оголошення": "/ads", "➕ Додати оголошення": "/add_ad",
                  "✏️ Змінити оголошення": "/edit_ad", "⛔ Вимкнути оголошення": "/remove_ad",
                  "🔄 Оновити каталог": "/refresh", "↩️ Скасувати": "/cancel", "📏 Заміри та розміри": "/sizing"}
        text = labels.get(text, text)
        with self.store.db() as db:
            row = db.execute("SELECT action,updated FROM admin_sessions WHERE account=? AND owner=?", (self.account, owner)).fetchone()
        if re.fullmatch(r"\d+(?:\s+.+)?", text) and row and row['action'] in ('add_ad','edit_ad','remove_ad') and time.time() - row["updated"] < 600:
            text = "/" + row["action"] + " " + text
        elif (row and row["action"] == "products" and time.time() - row["updated"] < 600 and
              text not in labels and not text.startswith("/") and text != "🏠 Головне меню"):
            text = "/products " + text
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].casefold() if parts else ""
        argument = parts[1] if len(parts) == 2 else ""
        actions = {"/products": "products", "/ads": "ads", "/refresh": "refresh", "/add_ad": "add_ad",
                   "/edit_ad": "edit_ad", "/remove_ad": "remove_ad", "/sizing": "sizing_list"}
        if command in ("/add_ad", "/edit_ad", "/remove_ad") and not argument:
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO admin_sessions VALUES(?,?,?,?)", (self.account, owner, actions[command], time.time()))
            self.tell(owner, key, "Надішли номер оголошення OLX 😊 За потреби після номера додай назву товару.\nПередумав? Натисни «↩️ Скасувати».", session_keyboard())
            return
        if command in actions:
            self.job(owner, key, actions[command], {"argument": argument[:500]})
            with self.store.db() as db:
                if command == "/products":
                    db.execute("INSERT OR REPLACE INTO admin_sessions VALUES(?,?,?,?)", (self.account, owner, "products", time.time()))
                else:
                    db.execute("DELETE FROM admin_sessions WHERE account=? AND owner=?", (self.account, owner))
            return
        if command == "/cancel":
            with self.store.db() as db:
                db.execute("DELETE FROM admin_sessions WHERE account=? AND owner=?", (self.account, owner))
            self.tell(owner,key,'Редагування скасовано. Обери наступну дію.',self.menu(catalog=True))
            return
        if command in ("/start", "/help", "/cancel") or text == "🏠 Головне меню":
            with self.store.db() as db:
                db.execute("DELETE FROM admin_sessions WHERE account=? AND owner=?", (self.account, owner))
        self.tell(owner, key, "Repliq · твій помічник OLX\n\n💬 Діалоги — відкрий потрібного клієнта й відповідай у його темі.\n"
                  "🙋 Потрібен я — лише звернення, де бот потребує допомоги. Оплата й відправлення — у Ledgerly.\n"
                  "📋 Оголошення — прив'язки OLX до товарів Ledgerly.\n\nУсі основні дії — кнопками нижче.")


EDITABLE_FACTS = {'material':'Склад тканини', 'lining':'Утеплення', 'fit':'Крій і посадка',
                  'waist':'Пояс і шнурок', 'rise':'Висота посадки', 'size_fit':'Маломірить / великомірить',
                  'print':'Принт і декор', 'transparency':'Чи просвічується', 'pilling':'Катишки',
                  'care':'Догляд', 'tags':'Бірки', 'package':'Комплектація', 'density':'Щільність тканини',
                  'photo_model':'Параметри моделі на фото'}


def runtime_catalog(base, products, bindings, stable_keys=None, sizing_profiles=None):
    data = copy.deepcopy(base.data)
    runtime, by_id = [], {}
    candidates, uses, legacy = {}, {}, {}
    stable_keys = stable_keys or {}
    def profile_name(value):
        # SQL products can be recreated as "Футболки" instead of "Футболка".
        # Only normalize known item nouns; never borrow facts by category alone.
        value = words(value)
        return re.sub(r'\b(?:футболки|футболку|футболок|футболочка)\b', 'футболка', value)
    for record in products:
        product_id, name = str(record["id"]), str(record["canonical_name"])
        matches = [p for p in base.products.values() if str(p.get("ledgerly_product_id")) == product_id]
        legacy[product_id] = bool(matches) or any(words(name) in {words(p['name']), *(words(a) for a in p['aliases'])} for p in base.products.values())
        if not matches:
            names = {profile_name(name), *(profile_name(a) for a in record.get('aliases', []) if isinstance(a,str))}
            matches = [p for p in base.products.values()
                       if names.intersection({profile_name(p['name']), *(profile_name(a) for a in p['aliases'])})]
        candidates[product_id] = matches
        for profile in matches:
            uses[profile["key"]] = uses.get(profile["key"], 0) + 1
    for record in products:
        product_id, name = str(record["id"]), str(record["canonical_name"])
        matches = candidates[product_id]
        if len(matches) == 1 and (uses[matches[0]["key"]] == 1 or str(matches[0].get("ledgerly_product_id")) == product_id):
            product = copy.deepcopy(matches[0])
            product['profile_key'] = product['key']
            if not legacy[product_id]:
                product['key'] = 'db-' + hashlib.sha256(product_id.encode()).hexdigest()[:24]
        else:
            kind_text = normalize(name + " " + str(record.get("category", "")))
            kind = "pants" if re.search(r"штан|брюк|pants", kind_text) else "tee" if re.search(r"футбол|shirt", kind_text) else "generic"
            product = {"key": "db-" + hashlib.sha256(product_id.encode()).hexdigest()[:24], "kind": kind,
                       "name": name[:120], "aliases": [], "advert_ids": [], "sizes": [], "size_chart_verified": False,
                       "price_uah": None, "description": None, "measurements": None, "authenticity": None}
        product["name"], product["ledgerly_product_id"] = name[:120], product_id
        if product_id in stable_keys:
            product['key'] = stable_keys[product_id]
        stop_words = {"штани", "штаны", "футболка", "футболки", "брюки", "джинси", "джинсы", "кофта", "куртка", "pants", "tee", "shirt"}
        distinctive = " ".join(w for w in words(name).split() if w not in stop_words)
        extra = [str(record.get("brand") or ""), distinctive] if len(distinctive) >= 4 else []
        product["aliases"] = list(dict.fromkeys(a.strip() for a in [name] + extra + product["aliases"] + record.get("aliases", [])
                                                if isinstance(a, str) and 1 <= len(a.strip()) <= 100))[:60]
        product["advert_ids"] = []
        overrides = copy.deepcopy((sizing_profiles or {}).get(product_id,{}))
        if 'facts' in overrides:
            facts = dict(product.get('facts', {}))
            for field, value in overrides.pop('facts').items():
                if value is None:
                    facts.pop(field, None)
                else:
                    facts[field] = value
            product['facts'] = facts
        product.update(overrides)
        runtime.append(product)
        by_id[product_id] = product
    links = {}
    for profile in base.products.values():
        target = next((p for p in runtime if p.get('profile_key',p['key']) == profile["key"]), None)
        if target:
            links.update((str(ad), target["ledgerly_product_id"]) for ad in profile["advert_ids"])
    for binding in bindings:
        links.pop(binding["advert"], None)
        if binding["active"] and binding["product_id"] in by_id:
            links[binding["advert"]] = binding["product_id"]
    for advert, product_id in links.items():
        by_id[product_id]["advert_ids"].append(int(advert))
    for product_id, product in by_id.items():
        prices = {b["price"] for b in bindings if b["active"] and b["product_id"] == product_id and b["price"] is not None}
        if product.get("price_uah") is None and len(prices) == 1:
            product["price_uah"] = prices.pop()
    data["products"] = runtime
    return Catalog(data)


def ranked_products(products, title, bindings):
    title = words(title)
    ranked = []
    for p in products:
        aliases = [p["canonical_name"]] + p.get("aliases", []) + [b["title"] for b in bindings if b["product_id"] == str(p["id"]) and b["title"]]
        score = 0
        for alias in aliases:
            alias = words(alias)
            if not alias:
                continue
            if alias == title:
                score = max(score, 100)
            elif " " + alias + " " in " " + title + " ":
                score = max(score, 90 if len(alias.split()) >= 2 else 60)
        if score:
            ranked.append((score, p))
    return sorted(ranked, key=lambda item: (-item[0], str(item[1]["id"])))
