"""Read-only, bounded Nova Poshta directory review before buyer confirmation."""

import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path

from app import read_json, AppError
from nova_shipping import NovaShipping, CarrierError, normalized
from customer_contact import clean_place

FIELDS = ("name", "phone", "place", "branch")


def details_key(details):
    value = [str(details.get(key, "")).strip() for key in FIELDS]
    if details.get('place_type'):
        value.append(details['place_type'])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def city_label(row):
    parts = [str(row.get("Description", ""))]
    area = str(row.get("AreaDescription", ""))
    region = str(row.get("RegionsDescription", ""))
    if area:
        parts.append(area if "обл" in area.casefold() else area + " область")
    if region:
        parts.append(region if "район" in region.casefold() else region + " район")
    return ", ".join(parts)


def branch_label(row):
    locker = row.get("CategoryOfWarehouse") == "Postomat" or "поштомат" in str(row.get("Description", "")).casefold()
    return ("Поштомат №" if locker else "Відділення №") + str(row["Number"])


def city_names(row):
    names = [str(row.get(key, "")) for key in ("Description", "DescriptionRu")]
    return [normalized(name) for value in names for name in (value, re.sub(r"\s*\([^)]*\)", "", value)) if name]


def city_type(row):
    value = normalized(row.get('SettlementTypeDescription') or row.get('SettlementTypeDescriptionRu') or '')
    if value in ('місто', 'город'):
        return 'місто'
    if value == 'село':
        return value
    if value.startswith('селищ') or value == 'смт':
        return 'селище'
    return ''


class ShippingDirectory:
    def __init__(self, private_dir):
        self.private_dir = Path(private_dir)
        self.cache = OrderedDict()

    @property
    def enabled(self):
        return bool(read_json(self.private_dir / "shipping-settings.json").get("enabled"))

    def client(self):
        settings = read_json(self.private_dir / "shipping-settings.json")
        # Directory reads are shared; the creation calendar is handled only by Fulfillment.
        profiles = [p for p in settings.get("profiles", {}).values() if p.get("nova_api_key")]
        if not settings.get("enabled") or not profiles:
            raise CarrierError("directory_not_configured")
        return NovaShipping(profiles[0], timeout=3)

    def read(self, client, method, props):
        key = (method, json.dumps(props, sort_keys=True), hashlib.sha256(client.settings["nova_api_key"].encode()).hexdigest())
        old = self.cache.get(key)
        if old and old[0] > time.monotonic():
            self.cache.move_to_end(key)
            return copy.deepcopy(old[1])
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CarrierError("directory_timeout")
        client.timeout = min(3, remaining)
        rows = client.call("Address", method, props)
        fields = ("Ref", "Description", "DescriptionRu", "AreaDescription", "AreaDescriptionRu", "RegionsDescription",
                  'SettlementTypeDescription', 'SettlementTypeDescriptionRu', "CityRef", "Number", "CategoryOfWarehouse", "WarehouseStatus")
        rows = [{key: row[key] for key in fields if key in row} for row in rows]
        self.cache[key] = (time.monotonic() + 600, copy.deepcopy(rows))
        self.cache.move_to_end(key)
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        return rows

    def choices(self, kind, rows, details):
        if kind == "city" and len(rows) > 3:
            return {"status": "city", "kind": "city", "key": details_key(details), "details": copy.deepcopy(details),
                    "prompt": "Є кілька населених пунктів із цією назвою. Уточніть, будь ласка, область і район. Можна одним рядком: «Місто: назва, область, район»."}
        options = rows[:3]
        heading = ("Уточніть, будь ласка, населений пункт — у довіднику Нової пошти є такі варіанти:" if kind == "city" else
                   "За цим номером не можу однозначно вибрати адресу. Можливо, ви мали на увазі:")
        labels = [(city_type(r) + ' ' + city_label(r)).strip() if kind == 'city' else str(r.get('Description') or branch_label(r)) for r in options]
        prompt = heading + "\n" + "\n".join("%s. %s" % (i + 1, label) for i, label in enumerate(labels))
        prompt += "\nНапишіть номер правильного варіанта або повністю вкажіть потрібну адресу."
        return {"status": "choice", "kind": kind, "key": details_key(details), "options": options, "prompt": prompt, "details": copy.deepcopy(details)}

    def review(self, details, pending=None, text=""):
        self.deadline = time.monotonic() + 6
        details = {k: copy.deepcopy(v) for k, v in details.items() if k in FIELDS or k in ('city_ref', 'place_type')}
        if (pending and pending.get("key") == details_key(details) and (pending.get("status") == "city" or pending.get("kind") == "city")
                and re.search(r"область|\bобл\.?|район|р-н|(?:ська|цька|ский|ская)$", text, re.I)
                and not re.search(r"місто\s*[:—]|город\s*[:—]|село\s*[:—]|телефон|поштомат|відділення", text, re.I)
                and len(text) <= 150):
            details["place"] = details["place"].split(",", 1)[0] + ", " + text.strip()
            details.pop("city_ref", None)
        selected = None
        if pending and pending.get("key") == details_key(details) and re.fullmatch(r"[123][.! ]*", text):
            index = int(text[0]) - 1
            options = pending.get("options", [])
            if index < len(options):
                selected = (pending.get("kind"), options[index])
        try:
            return self._review(details, selected)
        except (CarrierError, AppError, OSError, ValueError, KeyError, TypeError):
            return {"status": "unavailable", "key": details_key(details), "prompt":
                    "Дані отримав, дякую. Зараз не вдається звірити адресу з Новою поштою — передам власнику, щоб правильно оформити відправлення."}

    def _review(self, details, selected):
        original = dict(details)
        client = self.client()
        parts = [p.strip() for p in clean_place(details["place"]).split(",") if p.strip()]
        name = re.sub(r"^(?:м\.?|місто|город|с\.?|село|смт\.?|селище)\s+", "", parts[0], flags=re.I)
        rows = self.read(client, "getCities", {"FindByString": name, "Page": "1", "Limit": "100"})
        if not rows and len(name) >= 4:
            rows = self.read(client, "getCities", {"FindByString": name[:4], "Page": "1", "Limit": "100"})
        exact = [r for r in rows if normalized(name) in city_names(r)]
        if not exact:
            ranked = [(max((SequenceMatcher(None, normalized(name), alias).ratio() for alias in city_names(r)), default=0), r) for r in rows]
            exact = [r for score, r in sorted(ranked, key=lambda pair: -pair[0]) if score >= .82]
        matches = exact
        area = normalized(" ".join(parts[1:]))
        if area:
            tokens = [normalized(p) for p in re.split(r'[\s,]+', ' '.join(parts[1:]))
                      if not re.fullmatch(r'(?:обл(?:асть|асті|асти)?|район(?:у|а|і|е)?|рн)?', normalized(p))]
            matches = [r for r in exact if all(token in normalized(city_label(r)) or
                       max(SequenceMatcher(None, token, normalized(r.get(k))).ratio() for k in ("AreaDescription", "AreaDescriptionRu", "RegionsDescription")) >= .83 for token in tokens)]
        kind = details.get('place_type')
        if kind and len(matches) > 1:
            typed = [r for r in matches if city_type(r) == kind]
            if typed:
                matches = typed
        elif kind and matches and city_type(matches[0]) not in ('', kind) and not selected and not details.get('city_ref'):
            return self.choices('city', matches, details)
        if selected and selected[0] == "city":
            matches = [selected[1]]
        elif details.get("city_ref"):
            matches = [r for r in rows if r.get("Ref") == details["city_ref"]]
        if len(matches) != 1:
            if matches or exact:
                return self.choices("city", matches or exact, details)
            return {"status": "city", "key": details_key(details), "details": details, "prompt":
                    "Не знайшов цей населений пункт у довіднику Нової пошти. Напишіть, будь ласка: «Місто: назва, область». Якщо це село — вкажіть також район."}
        city = matches[0]
        details.update(place=city_label(city), city_ref=city["Ref"])
        number = re.search(r"\d{1,6}", details["branch"])
        if not number:
            return {"status": "branch", "key": details_key(details), "details": details, "prompt": "Напишіть, будь ласка, тип і номер відділення або поштомата."}
        rows = self.read(client, "getWarehouses", {"CityRef": city["Ref"], "FindByString": number[0], "Page": "1", "Limit": "100"})
        working = lambda r: r.get("CityRef") == city["Ref"] and r.get("WarehouseStatus", "Working") == "Working"
        matches = [r for r in rows if working(r) and str(r.get("Number")) == number[0]]
        if selected and selected[0] == "branch":
            matches = [r for r in [selected[1]] if working(r)]
        elif any(details['branch'].startswith('Поштомат') != branch_label(r).startswith('Поштомат') for r in matches):
            matching_type = [r for r in matches if details['branch'].startswith('Поштомат') == branch_label(r).startswith('Поштомат')]
            if not matching_type:
                return self.choices('branch', matches, original)
            matches = matching_type
        if len(matches) != 1:
            if not matches and len(number[0]) >= 2:
                near = self.read(client, "getWarehouses", {"CityRef": city["Ref"], "FindByString": number[0][:-1], "Page": "1", "Limit": "100"})
                matches = [r for r in near if working(r) and len(str(r.get("Number"))) == len(number[0]) and
                           sum(a != b for a, b in zip(str(r.get("Number")), number[0])) == 1]
            if matches:
                return self.choices("branch", matches, details)
            return {"status": "branch", "key": details_key(details), "details": details, "prompt":
                    "Не знайшов чинного відділення чи поштомата №%s для населеного пункту «%s». Перевірте номер і напишіть його разом із типом: відділення чи поштомат." % (number[0], city.get("Description", ""))}
        branch = matches[0]
        canonical = dict(details, place=city_label(city), branch=branch_label(branch), city_ref=city["Ref"],
                         warehouse_ref=branch["Ref"], branch_address=str(branch.get("Description") or branch_label(branch))[:500])
        canonical.pop('place_type', None)
        return {"status": "ready", "details": canonical, "key": details_key(canonical),
                "corrected": any(canonical[k] != original[k] for k in ("place", "branch"))}
