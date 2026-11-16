"""Environment-based configuration for the Telegram bookkeeping bot.

No credentials are stored in source code. Copy ``.env.example`` to ``.env``
and load it in your shell/service before starting the bot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


BASE_DIR = Path(__file__).resolve().parent


def _load_local_env() -> None:
    """Load simple KEY=VALUE pairs without adding another dependency."""

    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_local_env()


def _text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    raw = _text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = _text(name)
    if not raw:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _is_placeholder(value: str) -> bool:
    normalized = (value or "").strip().casefold()
    return not normalized or any(
        marker in normalized
        for marker in ("replace-me", "change-this-password", "your_", "example")
    )


def _int_set(name: str) -> set[int]:
    raw = _text(name)
    if not raw:
        return set()
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise RuntimeError(f"{name} contains a non-integer value: {item!r}") from exc
    return values


def _json(name: str, default: Any) -> Any:
    raw = _text(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON") from exc


def _accounts() -> dict[str, dict[str, str]]:
    """Return the legacy-compatible ``ACCOUNTS`` mapping.

    Accepted JSON forms:
    ``{"main": "api-key"}`` or
    ``{"main": {"API_KEY": "api-key"}}``.
    """

    raw = _json("NOVA_POSHTA_ACCOUNTS_JSON", {})
    if not raw:
        single_key = _text("NOVA_POSHTA_API_KEY")
        raw = {"default": single_key} if single_key else {}
    if not isinstance(raw, dict):
        raise RuntimeError("NOVA_POSHTA_ACCOUNTS_JSON must be a JSON object")

    accounts: dict[str, dict[str, str]] = {}
    for name, value in raw.items():
        key = value.get("API_KEY", "") if isinstance(value, dict) else value
        key = str(key or "").strip()
        if key:
            accounts[str(name)] = {"API_KEY": key}
    shipping_path = _text("REPLIQ_SHIPPING_SETTINGS")
    if shipping_path and Path(shipping_path).is_file():
        shipping = json.loads(Path(shipping_path).read_text(encoding="utf-8"))
        for name, profile in shipping.get("profiles", {}).items():
            key = str(profile.get("nova_api_key", "")).strip()
            if key:
                if name in accounts and accounts[name]["API_KEY"] != key:
                    raise RuntimeError("Nova Poshta profile name conflict")
                accounts[name] = {"API_KEY": key}
    return accounts


def _special_clients() -> dict[int, dict[str, str]]:
    raw = _json("SPECIAL_CLIENTS_JSON", {})
    if not isinstance(raw, dict):
        raise RuntimeError("SPECIAL_CLIENTS_JSON must be a JSON object")
    result: dict[int, dict[str, str]] = {}
    for user_id, value in raw.items():
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid SPECIAL_CLIENTS_JSON user id: {user_id!r}") from exc
        if isinstance(value, str):
            result[numeric_id] = {"display_name": value.strip()}
        elif isinstance(value, dict):
            result[numeric_id] = {
                "display_name": str(value.get("display_name", "")).strip()
            }
    return result


BOT_TOKEN = _text("BOT_TOKEN")
ACCOUNTS = _accounts()


def refresh_shipping_accounts() -> None:
    """Pick up a newly configured sender for tracking, without applying its calendar."""
    # Replace the snapshot instead of only adding new entries.  Otherwise a
    # profile removed from the private settings file would keep working with
    # stale credentials until the whole bot was restarted.
    fresh_accounts = _accounts()
    ACCOUNTS.clear()
    ACCOUNTS.update(fresh_accounts)

TG_GROUP_ID = _int("TG_GROUP_ID")
TG_SOURCE_THREAD_IDS = _int_set("TG_SOURCE_THREAD_IDS")
TG_EMPLOYEES_THREAD_ID = _int("TG_EMPLOYEES_THREAD_ID")
# Legacy setting retained for compatibility; no carrier routes to this topic.
TG_MANUAL_THREAD_ID = _int("TG_MANUAL_THREAD_ID")
TG_TRACKING_THREAD_ID = _int("TG_TRACKING_THREAD_ID")
TG_1C_THREAD_ID = _int("TG_1C_THREAD_ID")

TG_ALLOWED_PRIVATE_USER_IDS = _int_set("TG_ALLOWED_PRIVATE_USER_IDS")
TG_ADMIN_USER_IDS = _int_set("TG_ADMIN_USER_IDS")
TG_EMPLOYEE_USER_IDS = _int_set("TG_EMPLOYEE_USER_IDS")
TG_OWNER_USER_ID = _int("TG_OWNER_USER_ID")
SPECIAL_CLIENTS = _special_clients()
PRIVATE_FORWARD_TAG = _text("PRIVATE_FORWARD_TAG", "📦 ДО ВІДПРАВКИ")
ANALYTICS_PASSWORD_HASH = _text("ANALYTICS_PASSWORD_HASH")
ANALYTICS_UNLOCK_MINUTES = max(1, min(_int("ANALYTICS_UNLOCK_MINUTES", 15), 240))

_database_path = Path(
    _text("DATABASE_PATH", str(BASE_DIR / "data" / "tracking.sqlite3"))
).expanduser()
DATABASE_PATH = (
    _database_path if _database_path.is_absolute() else BASE_DIR / _database_path
).resolve()
# One database URL is used by the whole application.  The legacy
# BUSINESS_DATABASE_URL name remains accepted so an existing installation can
# be upgraded without an abrupt configuration break.
DATABASE_URL = _text(
    "DATABASE_URL",
    _text("BUSINESS_DATABASE_URL", f"sqlite:///{DATABASE_PATH}"),
)
BUSINESS_DATABASE_URL = DATABASE_URL
DATABASE_POOL_SIZE = max(1, min(_int("DATABASE_POOL_SIZE", 4), 20))
DATABASE_CONNECT_TIMEOUT_SECONDS = max(
    1,
    min(_int("DATABASE_CONNECT_TIMEOUT_SECONDS", 10), 60),
)

TG_RETURNS_THREAD_ID = _int("TG_RETURNS_THREAD_ID", TG_TRACKING_THREAD_ID)
TG_ALERTS_THREAD_ID = _int("TG_ALERTS_THREAD_ID", TG_TRACKING_THREAD_ID)

_backup_dir = Path(_text("BACKUP_DIR", str(BASE_DIR / "backups"))).expanduser()
BACKUP_DIR = (_backup_dir if _backup_dir.is_absolute() else BASE_DIR / _backup_dir).resolve()
BACKUP_RETENTION_DAYS = max(1, _int("BACKUP_RETENTION_DAYS", 30))
BACKUP_INTERVAL_HOURS = max(1, _int("BACKUP_INTERVAL_HOURS", 24))
BACKUP_WORKER_ENABLED = _bool("BACKUP_WORKER_ENABLED", True)

TIMEZONE = _text("TIMEZONE", "Europe/Kyiv")
LOG_LEVEL = _text("LOG_LEVEL", "INFO").upper()
REPLIQ_BRIDGE_DB = _text("REPLIQ_BRIDGE_DB")
TRACKER_BATCH_SIZE = max(1, _int("TRACKER_BATCH_SIZE", 20))
TRACKER_IDLE_SECONDS = max(5, _int("TRACKER_IDLE_SECONDS", 60))
TRACKER_LEASE_SECONDS = max(60, _int("TRACKER_LEASE_SECONDS", 300))


def is_private_user_allowed(user_id: int | None) -> bool:
    """The owner is always allowed; optional lists support future employees."""

    if not user_id:
        return False
    return user_id == TG_OWNER_USER_ID or user_id in TG_ALLOWED_PRIVATE_USER_IDS


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    return user_id == TG_OWNER_USER_ID or user_id in TG_ADMIN_USER_IDS


def is_owner(user_id: int | None) -> bool:
    return bool(user_id) and user_id == TG_OWNER_USER_ID


def is_employee(user_id: int | None) -> bool:
    if not user_id:
        return False
    return (
        user_id == TG_OWNER_USER_ID
        or user_id in TG_EMPLOYEE_USER_IDS
        or user_id in TG_ADMIN_USER_IDS
    )


def validate_config() -> None:
    missing: list[str] = []
    if _is_placeholder(BOT_TOKEN):
        missing.append("BOT_TOKEN")
    if not ACCOUNTS or any(
        _is_placeholder(account.get("API_KEY", "")) for account in ACCOUNTS.values()
    ):
        missing.append("NOVA_POSHTA_ACCOUNTS_JSON (or NOVA_POSHTA_API_KEY)")
    if not TG_GROUP_ID:
        missing.append("TG_GROUP_ID")
    # The owner can be discovered after the first start with /whoami.  Until
    # it is configured, private business actions remain deny-by-default.
    if not DATABASE_URL.startswith(("postgresql://", "postgres://", "sqlite:///")):
        missing.append("DATABASE_URL (postgresql://... or sqlite:///...)")
    elif DATABASE_URL.startswith(("postgresql://", "postgres://")):
        password = urlsplit(DATABASE_URL).password or ""
        if _is_placeholder(password):
            missing.append("DATABASE_URL password")
    if _is_placeholder(ANALYTICS_PASSWORD_HASH) or not ANALYTICS_PASSWORD_HASH.startswith(
        "scrypt$"
    ):
        missing.append("ANALYTICS_PASSWORD_HASH")
    for name, value in (
        ("TG_EMPLOYEES_THREAD_ID", TG_EMPLOYEES_THREAD_ID),
        ("TG_TRACKING_THREAD_ID", TG_TRACKING_THREAD_ID),
        ("TG_1C_THREAD_ID", TG_1C_THREAD_ID),
    ):
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))

