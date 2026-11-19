"""Nova Poshta parcel creation. Writes are never retried after an unknown result."""

import json
import hashlib
import re
from datetime import datetime
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


class CarrierError(Exception):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def normalized(value):
    return re.sub(r"[^\w]", "", str(value).casefold().replace("ё", "е"))


def contents_description(items):
    colors = {'BLACK': 'чорний', 'WHITE': 'білий', 'GRAY': 'сірий', 'BLUE': 'синій',
              'BEIGE': 'бежевий', 'GREEN': 'зелений', 'RED': 'червоний', 'PURPLE': 'фіолетовий'}
    parts = []
    for item in items:
        name = str(item.get('name', '')).casefold()
        kind = 'Штани' if re.search(r'штан|брюк|pants', name) else 'Футболка' if re.search(r'футбол|t.?shirt|tee', name) else str(item.get('name', 'Товар'))
        kind = re.sub(r'[\x00-\x1f\x7f]', '', kind).strip()
        value = ', '.join(str(v) for v in (kind, (str(item.get('requested_size')) + '*' if item.get('size_substituted') else item.get('size')), colors.get(item.get('color'), item.get('color'))) if v)
        if value not in parts:
            parts.append(value)
    description = '; '.join(parts)
    if not description or len(description) > 100:
        raise CarrierError('parcel_description_needs_review')
    return description


class NovaShipping:
    def __init__(self, settings, request=None, timeout=20):
        self.settings, self.transport = settings, request
        self.timeout = timeout

    def call(self, model, method, props, write=False):
        payload = {"apiKey": self.settings["nova_api_key"], "modelName": model,
                   "calledMethod": method, "methodProperties": props}
        if self.transport:
            result = self.transport(model, method, props, write)
        else:
            request = Request("https://api.novaposhta.ua/v2.0/json/", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "User-Agent": "Repliq/1"}, method="POST")
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise CarrierError("carrier_response_limit", write)
                result = json.loads(data)
            except HTTPError as error:
                raise CarrierError("carrier_http_%s" % error.code, write and error.code >= 500) from None
            except (URLError, OSError, ValueError):
                raise CarrierError("carrier_result_unknown" if write else "carrier_unavailable", write) from None
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise CarrierError("carrier_result_unknown", write)
        if not result.get("success"):
            raise CarrierError("carrier_rejected")
        return result["data"]

    def city(self, raw):
        parts = [part.strip() for part in str(raw).split(",") if part.strip()]
        if not parts:
            raise CarrierError("recipient_city_missing")
        name = re.sub(r"^(?:м\.?|місто|город|с\.?|село|смт\.?|селище)\s+", "", parts[0], flags=re.I).strip()
        rows = self.call("Address", "getCities", {"FindByString": name, "Page": "1", "Limit": "100"})
        matches = [row for row in rows if normalized(row.get("Description")) == normalized(name) or
                   normalized(row.get("DescriptionRu")) == normalized(name)]
        area = [normalized(token) for token in " ".join(parts[1:]).split()
                if normalized(token) not in ("область", "обл", "район", "рн")]
        if area:
            matches = [row for row in matches if all(token in normalized(str(row.get("AreaDescription", "")) + str(row.get("AreaDescriptionRu", "")) + str(row.get("RegionsDescription", ""))) for token in area)]
        if len(matches) != 1 or not matches[0].get("Ref"):
            raise CarrierError("recipient_city_ambiguous")
        return matches[0]

    def warehouse(self, city_ref, branch):
        number = re.search(r"\d{1,6}", str(branch))
        if not number:
            raise CarrierError("recipient_branch_missing")
        rows = self.call("Address", "getWarehouses", {"CityRef": city_ref, "FindByString": number[0], "Page": "1", "Limit": "100"})
        locker = bool(re.search(r"поштомат|почтомат", branch, re.I))
        matches = []
        for row in rows:
            is_locker = row.get("CategoryOfWarehouse") == "Postomat" or "поштомат" in str(row.get("Description", "")).casefold()
            if (str(row.get("Number")) == number[0] and is_locker == locker and row.get("CityRef") == city_ref
                    and row.get("WarehouseStatus", "Working") == "Working"):
                matches.append(row)
        if len(matches) != 1 or not matches[0].get("Ref"):
            raise CarrierError("recipient_branch_ambiguous")
        return matches[0]

    def resolve(self, parcel):
        recipient = parcel["recipient"]
        city = self.city(recipient["place"])
        warehouse = self.warehouse(city["Ref"], recipient["branch"])
        if (recipient.get("city_ref") and recipient["city_ref"] != city["Ref"] or
                recipient.get("warehouse_ref") and recipient["warehouse_ref"] != warehouse["Ref"]):
            raise CarrierError("recipient_address_changed")
        name = recipient["name"].split()
        phone = re.sub(r"\D", "", recipient["phone"])
        if not 2 <= len(name) <= 4 or not re.fullmatch(r"380\d{9}", phone):
            raise CarrierError("recipient_details_invalid")
        return {"city": city["Ref"], "warehouse": warehouse["Ref"], "phone": phone,
                "last": name[0], "first": name[1], "middle": " ".join(name[2:])}

    def configure_sender(self, place, branch):
        senders = self.call("Counterparty", "getCounterparties", {"CounterpartyProperty": "Sender", "Page": "1"})
        if len(senders) != 1 or not senders[0].get("Ref"):
            raise CarrierError("sender_selection_required")
        sender = senders[0]
        contacts = self.call("Counterparty", "getCounterpartyContactPersons", {"Ref": sender["Ref"], "Page": "1"})
        if len(contacts) != 1 or not contacts[0].get("Ref") or not contacts[0].get("Phones"):
            raise CarrierError("sender_contact_selection_required")
        city = self.city(place)
        warehouse = self.warehouse(city["Ref"], branch)
        prepared = dict(self.settings)
        prepared["sender"] = {"city_ref": city["Ref"], "warehouse_ref": warehouse["Ref"], "counterparty_ref": sender["Ref"],
                              "contact_ref": contacts[0]["Ref"], "phone": re.sub(r"\D", "", contacts[0]["Phones"])}
        prepared["api_key_fingerprint"] = hashlib.sha256(prepared["nova_api_key"].encode()).hexdigest()
        prepared["cod_mode"] = prepared.get("cod_mode") or ("afterpayment" if sender.get("CounterpartyType") == "Organization" else "money_transfer")
        return prepared

    def recipient(self, resolved):
        rows = self.call("Counterparty", "save", {"CityRef": resolved["city"], "FirstName": resolved["first"],
                         "LastName": resolved["last"], "MiddleName": resolved["middle"], "Phone": resolved["phone"],
                         "Email": "", "CounterpartyType": "PrivatePerson", "CounterpartyProperty": "Recipient"}, write=True)
        if len(rows) != 1 or not rows[0].get("Ref"):
            raise CarrierError("recipient_result_unknown", True)
        recipient = rows[0]
        contacts = recipient.get("ContactPerson", {}).get("data", [])
        if not contacts:
            contacts = self.call("Counterparty", "getCounterpartyContactPersons", {"Ref": recipient["Ref"], "Page": "1"})
        contacts = [contact for contact in contacts if contact.get("Ref") and
                    re.sub(r"\D", "", str(contact.get("Phones", resolved["phone"]))) == resolved["phone"]]
        if len(contacts) != 1:
            raise CarrierError("recipient_contact_ambiguous", True)
        return recipient["Ref"], contacts[0]["Ref"]

    def document_properties(self, parcel, resolved, recipient, contact):
        sender, package = self.settings["sender"], self.settings["parcel"]
        weight = Decimal(str(package["weight_kg"]))
        length, width, height = (Decimal(str(package[key])) for key in ("length_cm", "width_cm", "height_cm"))
        if not all(value.is_finite() and value > 0 for value in (weight, length, width, height)):
            raise CarrierError("parcel_dimensions_invalid")
        quantity = sum(item["quantity"] for item in parcel["items"])
        # Several garments can need a larger parcel; scale one dimension, not the number of parcels.
        height *= quantity
        if (quantity > int(package.get("max_automatic_items", 3)) or length > 60 or width > 40 or
                height > 30 or weight * quantity > 20):
            raise CarrierError("parcel_dimensions_need_review")
        volume = length * width * height / Decimal(1000000)
        values = {"PayerType": "Recipient", "PaymentMethod": "Cash", "CargoType": "Parcel",
                  "ServiceType": "WarehouseWarehouse", "SeatsAmount": "1", "Weight": str(weight * quantity),
                  "VolumeGeneral": str(volume), "Description": contents_description(parcel['items']), "Cost": str(Decimal(parcel["total_cents"]) / 100),
                  "DateTime": datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y"),
                  "CitySender": sender["city_ref"], "Sender": sender["counterparty_ref"],
                  "SenderAddress": sender["warehouse_ref"], "ContactSender": sender["contact_ref"], "SendersPhone": sender["phone"],
                  "CityRecipient": resolved["city"], "Recipient": recipient, "RecipientAddress": resolved["warehouse"],
                  "ContactRecipient": contact, "RecipientsPhone": resolved["phone"],
                  "InfoRegClientBarcodes": parcel["invoice_id"],
                  "OptionsSeat": [{"volumetricVolume": str(volume), "volumetricWidth": str(width),
                                   "volumetricLength": str(length), "volumetricHeight": str(height), "weight": str(weight * quantity)}]}
        due = parcel["cod_cents"]
        if due:
            amount = str(Decimal(due) / 100)
            if self.settings.get("cod_mode") == "afterpayment":
                values["AfterpaymentOnGoodsCost"] = amount
            elif self.settings.get("cod_mode") == "money_transfer":
                values["BackwardDeliveryData"] = [{"PayerType": "Recipient", "CargoType": "Money", "RedeliveryString": amount}]
            else:
                raise CarrierError("cod_mode_not_configured")
        return values

    def create(self, props):
        rows = self.call("InternetDocument", "save", props, write=True)
        if len(rows) != 1 or not re.fullmatch(r"\d{14}", str(rows[0].get("IntDocNumber", ""))) or not rows[0].get("Ref"):
            raise CarrierError("waybill_result_unknown", True)
        return str(rows[0]["IntDocNumber"]), str(rows[0]["Ref"])

    def document_status(self, ttn):
        if not re.fullmatch(r'\d{14}', str(ttn)):
            raise CarrierError('invalid_waybill_number')
        rows = self.call('TrackingDocument', 'getStatusDocuments', {'Documents': [{'DocumentNumber': str(ttn)}]})
        if len(rows) != 1 or str(rows[0].get('Number', '')) != str(ttn):
            raise CarrierError('tracking_identity_unknown')
        status = str(rows[0].get('StatusCode', ''))
        if status == '2' and not re.search(r'видален|удален|deleted', str(rows[0].get('Status', '')), re.I):
            raise CarrierError('tracking_identity_unknown')
        return status

    def payment_documents(self, parcels):
        documents = []
        for row in parcels:
            if not re.fullmatch(r'[0-9]{14}', str(row.get('ttn', ''))):
                raise CarrierError('invalid_waybill_number')
            phone = re.sub(r'\D', '', row['data']['recipient']['phone'])
            documents.append({'DocumentNumber': row['ttn'], 'Phone': phone})
        if not 1 <= len(documents) <= 25:
            raise CarrierError('tracking_batch_invalid')
        return self.call('TrackingDocument', 'getStatusDocuments', {'Documents': documents})

    def delete_document(self, ref):
        if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', str(ref)):
            raise CarrierError('invalid_document_reference')
        rows = self.call('InternetDocument', 'delete', {'DocumentRefs': [ref]}, write=True)
        if len(rows) != 1 or str(rows[0].get('Ref', '')).lower() != str(ref).lower():
            raise CarrierError('delete_result_unknown', True)
