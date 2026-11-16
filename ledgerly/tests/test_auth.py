from __future__ import annotations

import unittest

from auth import AnalyticsAccess, create_password_hash, verify_password


class AuthenticationTests(unittest.TestCase):
    def test_password_hash_verification(self) -> None:
        encoded = create_password_hash("quiet-password")
        self.assertTrue(verify_password("quiet-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotIn("quiet-password", encoded)

    def test_only_owner_gets_a_short_session(self) -> None:
        now = [100.0]
        access = AnalyticsAccess(
            123,
            create_password_hash("quiet-password"),
            unlock_minutes=1,
            clock=lambda: now[0],
        )
        self.assertEqual(access.unlock(999, "quiet-password")[0], "denied")
        self.assertEqual(access.unlock(123, "quiet-password")[0], "ok")
        self.assertTrue(access.is_unlocked(123))
        self.assertFalse(access.is_unlocked(999))
        now[0] += 61
        self.assertFalse(access.is_unlocked(123))

    def test_repeated_wrong_passwords_are_throttled(self) -> None:
        access = AnalyticsAccess(123, create_password_hash("quiet-password"))
        statuses = [access.unlock(123, "wrong")[0] for _ in range(5)]
        self.assertEqual(statuses[-1], "cooldown")


if __name__ == "__main__":
    unittest.main()
