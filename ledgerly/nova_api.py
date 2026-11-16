"""Minimal read-only Nova Poshta tracking client.

The old recipient, contact and InternetDocument creation methods were removed.
This module can only request an existing tracking document.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import requests


API_URL = "https://api.novaposhta.ua/v2.0/json/"
LOGGER = logging.getLogger(__name__)
_LOCAL = threading.local()


def _session() -> requests.Session:
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "olx-1c-bookkeeping-bot/2"})
        _LOCAL.session = session
    return session


def _error(message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"success": False, "errors": [message], "retryable": retryable, "data": []}


def _response_is_retryable(data: dict[str, Any]) -> bool:
    text = " ".join(str(item) for item in data.get("errors", [])).lower()
    return any(word in text for word in ("unavailable", "temporarily", "timeout", "too many"))


def np_request(
    api_key: str,
    model: str,
    method: str,
    props: dict[str, Any],
    retries: int = 3,
) -> dict[str, Any]:
    """Call the API without ever logging the API key or full response payload."""

    if not api_key:
        return _error("Nova Poshta API key is missing")
    payload = {
        "apiKey": api_key,
        "modelName": model,
        "calledMethod": method,
        "methodProperties": props,
    }
    last_error = _error("Nova Poshta request failed", retryable=True)
    for attempt in range(max(1, retries)):
        try:
            LOGGER.debug("Nova Poshta request: %s.%s (attempt %s)", model, method, attempt + 1)
            response = _session().post(API_URL, json=payload, timeout=(5, 25))
            if response.status_code == 429 or response.status_code >= 500:
                last_error = _error(
                    f"Nova Poshta HTTP {response.status_code}", retryable=True
                )
            elif response.status_code >= 400:
                return _error(f"Nova Poshta HTTP {response.status_code}")
            else:
                try:
                    data = response.json()
                except ValueError:
                    return _error("Nova Poshta returned invalid JSON")
                if not isinstance(data, dict):
                    return _error("Nova Poshta returned an unexpected response")
                if data.get("success", False) or not _response_is_retryable(data):
                    return data
                last_error = data
        except (requests.Timeout, requests.ConnectionError) as exc:
            LOGGER.warning("Temporary Nova Poshta connection error: %s", type(exc).__name__)
            last_error = _error(type(exc).__name__, retryable=True)
        except requests.RequestException as exc:
            LOGGER.error("Nova Poshta request error: %s", type(exc).__name__)
            return _error(type(exc).__name__)

        if attempt + 1 < max(1, retries):
            time.sleep((1.5 * (2**attempt)) + random.uniform(0, 0.4))
    return last_error


def get_tracking_document(api_key: str, ttn: str) -> dict[str, Any]:
    normalized = re.sub(r"\D", "", str(ttn or ""))
    if len(normalized) not in (13, 14):
        return _error("Tracking number must contain 13 or 14 digits")
    return np_request(
        api_key,
        "TrackingDocument",
        "getStatusDocuments",
        {"Documents": [{"DocumentNumber": normalized}]},
    )

