from __future__ import annotations

import unittest
from unittest.mock import patch

import config


class ConfigTests(unittest.TestCase):
    def test_shipping_account_refresh_removes_deleted_profiles(self) -> None:
        accounts = {
            "old": {"API_KEY": "old-key"},
            "main": {"API_KEY": "main-key"},
        }
        with (
            patch.object(config, "ACCOUNTS", accounts),
            patch.object(
                config,
                "_accounts",
                return_value={"main": {"API_KEY": "new-main-key"}},
            ),
        ):
            config.refresh_shipping_accounts()
        self.assertEqual(accounts, {"main": {"API_KEY": "new-main-key"}})

    def test_owner_can_be_discovered_after_first_start(self) -> None:
        with (
            patch.object(config, "BOT_TOKEN", "real-bot-token"),
            patch.object(config, "ACCOUNTS", {"main": {"API_KEY": "real-api-key"}}),
            patch.object(config, "TG_GROUP_ID", -100123),
            patch.object(config, "TG_OWNER_USER_ID", 0),
            patch.object(
                config,
                "ANALYTICS_PASSWORD_HASH",
                "scrypt$16384$8$1$test-salt$test-digest",
            ),
            patch.object(config, "TG_EMPLOYEES_THREAD_ID", 10),
            # The old separate Ukrposhta topic is no longer required.
            patch.object(config, "TG_MANUAL_THREAD_ID", 0),
            patch.object(config, "TG_TRACKING_THREAD_ID", 12),
            patch.object(config, "TG_1C_THREAD_ID", 13),
            patch.object(
                config,
                "DATABASE_URL",
                "postgresql://olx_bot:strong-password@127.0.0.1/olx_business",
            ),
        ):
            config.validate_config()


if __name__ == "__main__":
    unittest.main()
