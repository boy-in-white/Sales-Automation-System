"""Owner-only password gate for private financial commands."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from collections.abc import Callable

from config import (
    ANALYTICS_PASSWORD_HASH,
    ANALYTICS_UNLOCK_MINUTES,
    TG_OWNER_USER_ID,
)


_SCRYPT_N = 16_384
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LENGTH = 32


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_password_hash(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must contain at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LENGTH,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded_hash.split("$", 5)
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        if (
            algorithm != "scrypt"
            or n < _SCRYPT_N
            or n > 262_144
            or n & (n - 1)
            or not 1 <= r <= 16
            or not 1 <= p <= 4
        ):
            return False
        salt = _decode(raw_salt)
        expected = _decode(raw_digest)
        if len(salt) < 16 or len(expected) != _KEY_LENGTH:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class AnalyticsAccess:
    """Short in-memory unlock sessions with brute-force throttling."""

    def __init__(
        self,
        owner_user_id: int,
        password_hash: str,
        unlock_minutes: int = 15,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.owner_user_id = int(owner_user_id or 0)
        self.password_hash = password_hash
        self.unlock_seconds = max(60, int(unlock_minutes) * 60)
        self.clock = clock
        self._unlocked_until = 0.0
        self._failed_attempts = 0
        self._blocked_until = 0.0
        self._lock = threading.RLock()

    def is_unlocked(self, user_id: int) -> bool:
        with self._lock:
            return int(user_id or 0) == self.owner_user_id and self.clock() < self._unlocked_until

    def unlock(self, user_id: int, password: str) -> tuple[str, int]:
        if not self.owner_user_id or int(user_id or 0) != self.owner_user_id:
            return "denied", 0
        now = self.clock()
        with self._lock:
            if now < self._blocked_until:
                return "cooldown", max(1, int(self._blocked_until - now))
            if not verify_password(password, self.password_hash):
                self._failed_attempts += 1
                if self._failed_attempts >= 5:
                    self._failed_attempts = 0
                    self._blocked_until = now + 5 * 60
                    return "cooldown", 5 * 60
                return "invalid", 0
            self._failed_attempts = 0
            self._blocked_until = 0.0
            self._unlocked_until = now + self.unlock_seconds
            return "ok", self.unlock_seconds

    def lock(self, user_id: int) -> bool:
        if int(user_id or 0) != self.owner_user_id:
            return False
        with self._lock:
            self._unlocked_until = 0.0
        return True


ANALYTICS_ACCESS = AnalyticsAccess(
    TG_OWNER_USER_ID,
    ANALYTICS_PASSWORD_HASH,
    ANALYTICS_UNLOCK_MINUTES,
)
