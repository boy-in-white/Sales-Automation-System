import unittest

from polling import AdaptivePolling


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.schedule = AdaptivePolling(clock=self.clock)

    def test_idle_inbox_is_checked_every_thirty_seconds(self):
        self.assertEqual(self.schedule.due(), (True, []))
        self.schedule.inbox_checked()
        self.assertEqual(self.schedule.delay(), 30)
        self.clock.now = 29
        self.assertEqual(self.schedule.due(), (False, []))
        self.clock.now = 30
        self.assertEqual(self.schedule.due(), (True, []))

    def test_activity_schedules_only_that_chat_every_two_seconds(self):
        self.schedule.inbox_checked()
        self.schedule.activate("chat-a")
        self.assertEqual(self.schedule.delay(), 2)
        self.clock.now = 2
        self.assertEqual(self.schedule.due(), (False, ["chat-a"]))
        self.schedule.active_checked(["chat-a"])
        self.assertEqual(self.schedule.delay(), 2)
        self.assertEqual(self.schedule.next_inbox_at, 30)

    def test_checks_do_not_extend_activity_but_new_messages_do(self):
        self.schedule.activate("chat-a")
        self.clock.now = 100
        self.schedule.active_checked(["chat-a"])
        self.assertEqual(self.schedule.active["chat-a"]["until"], 120)
        self.schedule.activate("chat-a")
        self.assertEqual(self.schedule.active["chat-a"]["until"], 220)
        self.clock.now = 121
        self.assertEqual(self.schedule.active_count, 1)
        self.clock.now = 220
        self.assertEqual(self.schedule.active_count, 0)

    def test_quiet_chat_returns_to_idle_after_two_minutes(self):
        self.schedule.activate("chat-a")
        self.clock.now = 119
        self.schedule.inbox_checked()
        self.clock.now = 120
        self.assertEqual(self.schedule.due(), (False, []))
        self.assertEqual(self.schedule.delay(), 29)

    def test_regular_inbox_poll_continues_while_fast_chats_exist(self):
        self.schedule.inbox_checked()
        self.schedule.activate("chat-a")
        self.clock.now = 30
        inbox, active = self.schedule.due()
        self.assertTrue(inbox)
        self.assertEqual(active, ["chat-a"])
        self.schedule.inbox_checked()
        self.schedule.active_checked()
        self.assertEqual(self.schedule.due(), (False, []))
        self.assertEqual(self.schedule.delay(), 2)

    def test_active_chat_limit_preserves_most_recent_conversations(self):
        for number in range(9):
            self.clock.now = number
            self.schedule.activate("chat-%s" % number)
        self.assertEqual(self.schedule.active_count, 8)
        self.assertNotIn("chat-0", self.schedule.active)
        self.assertIn("chat-8", self.schedule.active)

    def test_unavailable_chat_is_removed_without_changing_inbox_schedule(self):
        self.schedule.inbox_checked()
        self.schedule.activate("chat-a")
        self.schedule.deactivate("chat-a")
        self.assertEqual(self.schedule.active_count, 0)
        self.assertEqual(self.schedule.delay(), 30)

    def test_unsafe_request_intervals_are_rejected(self):
        for options in ({"idle_interval": 1}, {"active_interval": 1},
                        {"active_window": 1}, {"max_active_chats": 100}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AdaptivePolling(clock=self.clock, **options)


if __name__ == "__main__":
    unittest.main()
