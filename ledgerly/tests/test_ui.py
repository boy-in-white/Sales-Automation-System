from __future__ import annotations

import unittest
from unittest.mock import patch

import handlers
import inventory
import main
import ui


class FakeCommandBot:
    def __init__(self) -> None:
        self.calls = []

    def set_my_commands(self, commands, **kwargs) -> None:
        self.calls.append((commands, kwargs))


class InterfaceTests(unittest.TestCase):
    def test_owner_command_order_and_help_are_consistent(self) -> None:
        bot = FakeCommandBot()
        with patch.object(main, "TG_OWNER_USER_ID", 108):
            main.register_commands(bot)
        owner_commands = [command.command for command in bot.calls[-1][0]]
        self.assertEqual(owner_commands[:2], ["add_product", "stats"])
        self.assertEqual(owner_commands[-1], "help")

        help_text = handlers.private_help_text(admin=True, owner=True)
        self.assertLess(help_text.index("/add_product"), help_text.index("/stats"))
        self.assertTrue(help_text.endswith("/help — показати цю підказку"))

        inventory_menu = inventory._menu_keyboard()
        first_button = inventory_menu.keyboard[0][0]
        self.assertEqual(first_button.text, "➕ Додати товар")
        self.assertEqual(first_button.callback_data, "inv:add:start")

    def test_callback_payloads_fit_telegram_limit(self) -> None:
        identifier = "12345678-1234-1234-1234-123456789012"
        keyboards = [
            ui.submission_keyboard(identifier),
            ui.awaiting_shipment_keyboard(identifier),
            ui.onec_keyboard(identifier),
            ui.return_keyboard(identifier),
            ui.clear_current_month_keyboard(),
        ]
        for keyboard in keyboards:
            for row in keyboard.keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
