from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import incoming_group_listener


class GroupAccessTests(unittest.TestCase):
    def _message(self, *, thread_id: int, user_id: int = 1, is_bot: bool = False):
        return SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            message_thread_id=thread_id,
            from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        )

    def test_to_ship_topic_cannot_bypass_confirmation_button(self) -> None:
        with (
            patch.object(incoming_group_listener, "TG_GROUP_ID", -1001),
            patch.object(incoming_group_listener, "TG_EMPLOYEES_THREAD_ID", 10),
            patch.object(incoming_group_listener, "TG_SOURCE_THREAD_IDS", set()),
            patch.object(incoming_group_listener, "is_employee", return_value=True),
        ):
            self.assertFalse(incoming_group_listener.should_process(self._message(thread_id=10)))

    def test_configured_source_topic_requires_employee(self) -> None:
        with (
            patch.object(incoming_group_listener, "TG_GROUP_ID", -1001),
            patch.object(incoming_group_listener, "TG_SOURCE_THREAD_IDS", {20}),
            patch.object(incoming_group_listener, "is_employee", return_value=False),
        ):
            self.assertFalse(incoming_group_listener.should_process(self._message(thread_id=20)))

        with (
            patch.object(incoming_group_listener, "TG_GROUP_ID", -1001),
            patch.object(incoming_group_listener, "TG_SOURCE_THREAD_IDS", {20}),
            patch.object(incoming_group_listener, "is_employee", return_value=True),
        ):
            self.assertTrue(incoming_group_listener.should_process(self._message(thread_id=20)))


if __name__ == "__main__":
    unittest.main()
