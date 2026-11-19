"""Validated, editable shop facts. No customer or database secrets live here."""

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import AppError

SIZES = ("XS", "S", "M", "L", "XL", "XXL")
COLORS = {"BLACK": "чорний", "WHITE": "білий", "GRAY": "сірий", "BLUE": "синій", "BEIGE": "бежевий",
          "GREEN": "зелений", "RED": "червоний", "BROWN": "коричневий", "PINK": "рожевий", "YELLOW": "жовтий", "CREAM": "молочний",
          "PURPLE": "фіолетовий", "ORANGE": "помаранчевий", "BURGUNDY": "бордовий", "TURQUOISE": "бірюзовий",
          "KHAKI": "хакі", "OLIVE": "оливковий", "LAVENDER": "лавандовий", "GOLD": "золотий", "SILVER": "срібний"}


def normalize(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text.replace("’", "'").replace("ʼ", "'")).strip()


def contains(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(normalize(phrase)) + r"(?!\w)", text) is not None


class Catalog:
    def __init__(self, data):
        try:
            if data.get("version") != 1 or not 0 <= len(data["products"]) <= 2000:
                raise ValueError()
            self.data = data
            self.products = {}
            self.adverts = {}
            for p in data["products"]:
                if not re.fullmatch(r"[a-z0-9-]{1,60}", p["key"]) or p["key"] in self.products:
                    raise ValueError()
                if not isinstance(p["name"], str) or not 1 <= len(p["name"]) <= 120:
                    raise ValueError()
                if p["kind"] not in ("pants", "tee", "generic") or not isinstance(p["size_chart_verified"], bool):
                    raise ValueError()
                if not isinstance(p["aliases"], list) or not all(isinstance(a, str) and 1 <= len(a) <= 100 for a in p["aliases"]):
                    raise ValueError()
                if p.get("ledgerly_product_id") is not None and not re.fullmatch(r"[\w-]{1,128}", str(p["ledgerly_product_id"])):
                    raise ValueError()
                price = p.get("price_uah")
                if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price <= 1000000):
                    raise ValueError()
                rows = p["sizes"]
                if not 0 <= len(rows) <= 6 or len({r["size"] for r in rows}) != len(rows):
                    raise ValueError()
                for row in rows:
                    if row["size"] not in SIZES:
                        raise ValueError()
                    for name, lower, upper in (("height_cm", 120, 230), ("weight_kg", 35, 200)):
                        limits = row[name]
                        if (not isinstance(limits, list) or len(limits) != 2 or
                                any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in limits) or
                                not lower <= limits[0] <= limits[1] <= upper):
                            raise ValueError()
                for name in ("description", "measurements", "authenticity"):
                    if p.get(name) is not None and (not isinstance(p[name], str) or len(p[name]) > 1500):
                        raise ValueError()
                facts = p.get("facts", {})
                if not isinstance(facts, dict) or set(facts) - {"material", "waist", "lining", "pilling", "quality", "seasons", "fit", "rise", "size_fit", "print", "transparency", "comfort_temperature_c", "tags", "density", "care", "package", "photo_model"}:
                    raise ValueError()
                for key, value in facts.items():
                    if key == "seasons":
                        if (not isinstance(value, list) or len(value) != len(set(value)) or
                                any(s not in ("spring", "summer", "autumn", "winter") for s in value)):
                            raise ValueError()
                    elif key == "comfort_temperature_c":
                        if (not isinstance(value, list) or len(value) != 2 or
                                any(type(v) not in (int, float) for v in value) or not -80 <= value[0] <= value[1] <= 80):
                            raise ValueError()
                    elif not isinstance(value, str) or not 1 <= len(value) <= 400:
                        raise ValueError()
                if type(p.get("size_substitution_enabled", False)) is not bool:
                    raise ValueError()
                excluded = p.get("excluded_sizes", [])
                if not isinstance(excluded, list) or len(excluded) != len(set(excluded)) or any(size not in SIZES for size in excluded):
                    raise ValueError()
                if p.get("waist_measurement_kind", "unspecified") not in ("unspecified", "flat_width", "circumference"):
                    raise ValueError()
                if p.get("waist_measurement_state", "unspecified") not in ("unspecified", "relaxed", "stretched"):
                    raise ValueError()
                measures = p.get("garment_measurements", {})
                if not isinstance(measures, dict) or len(measures) > 6:
                    raise ValueError()
                for size, values in measures.items():
                    if size not in SIZES or not isinstance(values, dict) or not values:
                        raise ValueError()
                    for key, value in values.items():
                        if (key not in ("length_cm", "width_cm", "waist_cm", "leg_width_cm") or
                                type(value) not in (int, float) or not 0 < value <= 300):
                            raise ValueError()
                for size, limits in p.get("height_guidance", {}).items():
                    if (size not in SIZES or not isinstance(limits, list) or len(limits) != 2 or
                            any(type(v) not in (int, float) for v in limits) or not 120 <= limits[0] <= limits[1] <= 230):
                        raise ValueError()
                if p.get("sizing_basis", "body") not in ("body", "height"):
                    raise ValueError()
                for advert in p["advert_ids"]:
                    if isinstance(advert, bool) or not str(advert).isdecimal() or int(advert) <= 0 or str(advert) in self.adverts:
                        raise ValueError()
                    self.adverts[str(advert)] = p["key"]
                self.products[p["key"]] = p
            self.shop = data["shop"]
            if self.shop.get('cod_payment', 'deposit') not in ('deposit', 'carrier_delivery'):
                raise ValueError()
            for field in ('deposit_refund', 'pickup', 'dispatch_city', 'delivery_cost'):
                if field in self.shop and (not isinstance(self.shop[field], str) or not 1 <= len(self.shop[field]) <= 1000):
                    raise ValueError()
            if "owner_telegram" in self.shop and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", str(self.shop["owner_telegram"])):
                raise ValueError()
            ZoneInfo(self.shop.get("timezone", "Europe/Kyiv"))
            for name in ("delivery", "payment", "returns", "order_instructions", "discount"):
                value = self.shop.get(name)
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 1500):
                    raise ValueError()
            settings = data["polling"]
            ranges = {"idle_seconds": (5, 3600), "active_seconds": (2, 60),
                      "active_window_seconds": (2, 600), "max_active_chats": (1, 8), "debounce_seconds": (1, 10)}
            for name, (low, high) in ranges.items():
                if type(settings[name]) is not int or not low <= settings[name] <= high:
                    raise ValueError()
            if settings["active_seconds"] > settings["idle_seconds"] or settings["active_window_seconds"] < settings["active_seconds"]:
                raise ValueError()
            for name, low, high in (("draft_ttl_seconds", 60, 3600), ("memory_days", 1, 90), ("max_pending_drafts", 1, 1000)):
                if type(data["limits"][name]) is not int or not low <= data["limits"][name] <= high:
                    raise ValueError()
            self.timezone = ZoneInfo(data["olx_timezone"])
        except (KeyError, TypeError, ValueError, AttributeError, ZoneInfoNotFoundError):
            raise AppError("Перевір shop.json: товари, таблиці розмірів, часовий пояс та межі інтервалів.") from None
        self.signature = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def load(cls, path):
        try:
            with Path(path).open(encoding="utf-8") as source:
                return cls(json.load(source))
        except (OSError, ValueError, UnicodeError):
            raise AppError("Не вдалося прочитати shop.json. Перевір JSON і розташування файла.") from None

    def explicit_products(self, text):
        text = normalize(text)
        found = [p for p in self.products.values() if any(contains(text, a) for a in p["aliases"])]
        if found:
            return found
        kinds = set()
        if re.search(r"штан|брюк|pants", text):
            kinds.add("pants")
        if re.search(r"футбол|t.?shirt|майк", text):
            kinds.add("tee")
        return [p for p in self.products.values() if p["kind"] in kinds]

    def product_signature(self, product_key, advert_id):
        product = self.products.get(product_key)
        facts = {k: v for k, v in product.items() if k != "advert_ids"} if product else None
        value = [facts, self.adverts.get(str(advert_id)), self.shop]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
