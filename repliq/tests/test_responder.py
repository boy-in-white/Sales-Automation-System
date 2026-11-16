import copy
import tempfile
import unittest
from pathlib import Path

from app import AppError, private_lock, read_json
from olx_client import OlxClient, next_path
from responder import REPLY, ConsoleResponder, Journal, StopRequested, snapshot, terminal_text


def message(source_id="incoming-1", text="Добрий день, бажаю замовити штани. Чи є в наявності?", direction="received", timestamp="2026-11-05 12:00:00"):
    return {"uuid": source_id, "type": direction, "text": text, "created_at": timestamp}


class FakeClient:
    def __init__(self):
        self.history = [message()]
        self.sent = []
        self.fail_send = False
        self.after_read = None
        self.account_id = "account-1"
        self.read_threads = []
        self.metadata_threads = []

    def identity(self):
        return {"id": self.account_id, "name": "Test"}

    def threads(self, advert_id=None):
        return [{"uuid": "thread-1", "advert_id": "101", "total_count": len(self.history), "unread_count": 1}]

    def messages(self, thread_id):
        self.read_threads.append(thread_id)
        return copy.deepcopy(self.history)

    def thread(self, thread_id):
        self.metadata_threads.append(thread_id)
        return {"uuid": thread_id, "advert_id": "101", "total_count": len(self.history), "unread_count": 1}

    def send(self, thread_id, text):
        self.sent.append((thread_id, text))
        if self.fail_send:
            raise AppError("Timeout")
        return {"uuid": "outgoing-1"}


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.client = FakeClient()
        self.output = []
        self.journal = Journal(self.path, "account-1")

    def worker(self, answers, include_existing=True, journal=None):
        stream = iter(answers)
        return ConsoleResponder(self.client, journal or self.journal, lambda prompt: next(stream), self.output.append, include_existing)

    def test_exact_customer_example_is_printed_before_explicit_confirmation(self):
        def confirm(prompt):
            self.assertEqual(self.client.sent, [])
            self.assertTrue(any("бажаю замовити штани" in line for line in self.output))
            self.assertTrue(any(REPLY in line for line in self.output))
            return "так"
        worker = ConsoleResponder(self.client, self.journal, confirm, self.output.append, True)
        self.assertEqual(worker.scan(), 1)
        self.assertEqual(self.client.sent, [("thread-1", "Так, в наявності.")])

    def test_enter_no_and_quit_do_not_send(self):
        for answer in ("", "ні", "no"):
            with self.subTest(answer=answer):
                journal = Journal(Path(self.temp.name) / ("state-" + str(len(answer)) + ".json"), "account-1")
                self.worker([answer], journal=journal).scan()
                self.assertEqual(self.client.sent, [])
        journal = Journal(Path(self.temp.name) / "quit.json", "account-1")
        with self.assertRaises(StopRequested):
            self.worker(["вихід"], journal=journal).scan()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(journal.get("thread-1", "incoming-1")["status"], "pending")

    def test_old_history_is_baselined_then_new_message_is_offered(self):
        worker = self.worker(["так"], include_existing=False)
        worker.scan()
        self.assertEqual(self.client.sent, [])
        self.client.history.append(message("incoming-2", timestamp="2026-11-05 12:00:10"))
        worker.scan()
        self.assertEqual(len(self.client.sent), 1)

    def test_restart_does_not_repeat_a_sent_or_skipped_reply(self):
        self.worker(["так"]).scan()
        restarted = Journal(self.path, "account-1")
        self.worker([], journal=restarted).scan()
        self.assertEqual(len(self.client.sent), 1)
        self.client.history.append(message("incoming-2", timestamp="2026-11-05 12:00:10"))
        self.worker(["ні"], journal=restarted).scan()
        self.worker([], journal=Journal(self.path, "account-1")).scan()
        self.assertEqual(len(self.client.sent), 1)

    def test_timeout_after_post_is_persisted_and_never_retried(self):
        self.client.fail_send = True
        self.worker(["так"]).scan()
        self.assertEqual(self.journal.get("thread-1", "incoming-1")["status"], "uncertain")
        self.worker([], journal=Journal(self.path, "account-1")).scan()
        self.assertEqual(len(self.client.sent), 1)

    def test_crash_after_persisted_sending_status_cannot_repeat_post(self):
        self.journal.mark("thread-1", "incoming-1", "sending")
        self.worker([]).scan()
        self.assertEqual(self.client.sent, [])

    def test_new_buyer_message_while_approving_cancels_old_draft(self):
        def confirm(prompt):
            self.client.history.append(message("incoming-2", "Вже не потрібно", timestamp="2026-11-05 12:00:05"))
            return "так"
        ConsoleResponder(self.client, self.journal, confirm, self.output.append, True).scan()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.journal.get("thread-1", "incoming-1")["status"], "stale")

    def test_seller_reply_while_approving_cancels_draft(self):
        def confirm(prompt):
            self.client.history.append(message("outgoing-1", "Уже відповів", "sent", "2026-11-05 12:00:06"))
            return "так"
        ConsoleResponder(self.client, self.journal, confirm, self.output.append, True).scan()
        self.assertEqual(self.client.sent, [])

    def test_account_change_after_confirmation_cancels_send(self):
        def confirm(prompt):
            self.client.account_id = "other-account"
            return "так"
        with self.assertRaises(AppError):
            ConsoleResponder(self.client, self.journal, confirm, self.output.append, True).scan()
        self.assertEqual(self.client.sent, [])

    def test_own_messages_older_incoming_and_unknown_types_are_ignored(self):
        cases = [
            [message(direction="sent")],
            [message(), message("sent", "Так", "sent", "2026-11-05 12:01:00")],
            [message(direction="system")],
        ]
        for history in cases:
            self.client.history = history
            self.worker([]).scan()
        self.assertEqual(self.client.sent, [])

    def test_any_new_buyer_text_gets_a_draft_with_confirmation(self):
        for index, text in enumerate(("Привіт", "Яка ціна?", "Хочу замовити", "", "Чи є в наявності?")):
            with self.subTest(text=text):
                self.client = FakeClient()
                self.client.history = [message(text=text)]
                journal = Journal(Path(self.temp.name) / ("any-%s.json" % index), "account-1")
                self.worker(["так"], journal=journal).scan()
                self.assertEqual(self.client.sent, [("thread-1", REPLY)])

    def test_reply_typed_before_draft_is_cleared_before_new_confirmation(self):
        queued = ["так"]
        def prepare():
            queued.clear()
        def confirm(prompt):
            self.assertEqual(queued, [])
            self.assertEqual(self.client.sent, [])
            return "ні"
        worker = ConsoleResponder(self.client, self.journal, confirm, self.output.append, True, prepare_input=prepare)
        worker.scan()
        self.assertEqual(self.client.sent, [])

    def test_unchanged_threads_are_cached_but_new_message_is_read(self):
        self.client.total_count = 1
        self.client.reads = 0
        original_messages = self.client.messages
        def threads(advert_id=None):
            return [{"uuid": "thread-1", "advert_id": "101", "total_count": self.client.total_count, "unread_count": 1}]
        def messages(thread_id):
            self.client.reads += 1
            return original_messages(thread_id)
        self.client.threads = threads
        self.client.messages = messages
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        worker.scan()
        self.assertEqual(self.client.reads, 0)
        self.client.total_count = 2
        self.client.history.append(message("incoming-2", text="Привіт", timestamp="2026-11-05 12:01:00"))
        worker.scan()
        self.assertEqual(self.client.reads, 1)
        self.assertEqual(self.journal.get("thread-1", "incoming-2")["status"], "skipped")

    def test_old_pending_draft_is_not_offered_after_restart(self):
        self.client.threads = lambda advert_id=None: [{"uuid": "thread-1", "total_count": 1}]
        worker = self.worker(["вихід"], include_existing=False)
        worker.scan()
        self.client.history.append(message("incoming-2", text="Привіт", timestamp="2026-11-05 12:01:00"))
        self.client.threads = lambda advert_id=None: [{"uuid": "thread-1", "total_count": 2}]
        with self.assertRaises(StopRequested):
            worker.scan()
        restarted = Journal(self.path, "account-1")
        self.worker([], include_existing=False, journal=restarted).scan()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(restarted.get("thread-1", "incoming-2")["status"], "pending")

    def test_restart_ignores_messages_received_while_program_was_closed(self):
        self.worker([], include_existing=False).scan()
        self.client.history.append(message("offline-message", text="Привіт", timestamp="2026-11-05 12:01:00"))
        restarted = Journal(self.path, "account-1")
        worker = self.worker(["так"], include_existing=False, journal=restarted)
        worker.scan()
        self.assertEqual(self.client.sent, [])
        worker.scan()
        self.assertEqual(self.client.sent, [])
        self.client.history.append(message("new-live-message", text="Привіт ще раз", timestamp="2026-11-05 12:02:00"))
        worker.scan()
        self.assertEqual(self.client.sent, [("thread-1", REPLY)])
        self.assertEqual(restarted.get("thread-1", "new-live-message")["status"], "sent")
        self.assertEqual(restarted.get("thread-1", "offline-message"), {})

    def test_old_pending_draft_does_not_reappear_in_later_scans(self):
        self.journal.mark("thread-1", "incoming-1", "pending")
        worker = self.worker([], include_existing=False)
        worker.scan()
        worker.scan()
        self.assertEqual(self.client.sent, [])
        self.assertFalse(any("Пропонована відповідь" in line for line in self.output))

    def test_unread_threads_are_checked_before_other_threads(self):
        threads = [{"uuid": "old-thread", "total_count": 1, "unread_count": 0},
                   {"uuid": "new-thread", "total_count": 1, "unread_count": 1}]
        self.client.threads = lambda advert_id=None: copy.deepcopy(threads)
        checked = []
        def messages(thread_id):
            checked.append(thread_id)
            return []
        self.client.messages = messages
        worker = self.worker([], include_existing=False)
        worker.scan()
        self.assertEqual(checked, [])
        for thread in threads:
            thread["total_count"] += 1
        worker.scan()
        self.assertEqual(checked, ["new-thread", "old-thread"])

    def test_291_dialogs_need_no_history_requests_at_startup_or_while_idle(self):
        self.client.threads = lambda advert_id=None: [
            {"uuid": "thread-%s" % number, "total_count": 8, "unread_count": 3}
            for number in range(291)
        ]
        worker = self.worker([], include_existing=False)
        worker.scan()
        worker.scan()
        worker.scan()
        self.assertEqual(self.client.read_threads, [])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.journal.account["last_scan"]["checked"], 0)
        self.assertEqual(self.journal.account["last_scan"]["changed"], 0)

    def test_only_one_changed_thread_out_of_291_is_read(self):
        totals = {"thread-%s" % number: 8 for number in range(291)}
        self.client.threads = lambda advert_id=None: [
            {"uuid": thread_id, "total_count": total, "unread_count": 1}
            for thread_id, total in totals.items()
        ]
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        totals["thread-144"] += 1
        worker.scan()
        self.assertEqual(self.client.read_threads, ["thread-144"])
        self.assertEqual(self.journal.get("thread-144", "incoming-1")["status"], "skipped")

    def test_marking_old_messages_unread_does_not_load_history(self):
        thread = {"uuid": "thread-1", "total_count": 3, "unread_count": 0}
        self.client.threads = lambda advert_id=None: [dict(thread)]
        worker = self.worker([], include_existing=False)
        worker.scan()
        thread.update(unread_count=3, updated_at="2026-11-05 13:00:00")
        worker.scan()
        self.assertEqual(self.client.read_threads, [])
        self.assertEqual(self.client.sent, [])

    def test_new_thread_is_read_without_loading_existing_thread_histories(self):
        threads = [{"uuid": "old-thread", "total_count": 5}]
        self.client.threads = lambda advert_id=None: copy.deepcopy(threads)
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        threads.append({"uuid": "fresh-thread", "total_count": 1})
        worker.scan()
        self.assertEqual(self.client.read_threads, ["fresh-thread"])

    def test_missing_counter_is_not_permission_to_read_old_history(self):
        thread = {"uuid": "thread-1"}
        self.client.threads = lambda advert_id=None: [dict(thread)]
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        worker.scan()
        thread["total_count"] = 3
        worker.scan()
        self.assertEqual(self.client.read_threads, [])
        thread["total_count"] = 4
        worker.scan()
        self.assertEqual(self.client.read_threads, ["thread-1"])

    def test_counter_drop_and_rebound_cannot_trigger_an_old_reply(self):
        thread = {"uuid": "thread-1", "total_count": 8}
        self.client.threads = lambda advert_id=None: [dict(thread)]
        worker = self.worker([], include_existing=False)
        worker.scan()
        thread["total_count"] = 6
        worker.scan()
        thread["total_count"] = 8
        worker.scan()
        self.assertEqual(self.client.read_threads, [])
        self.assertEqual(self.client.sent, [])

    def test_failed_history_read_retries_only_the_changed_thread(self):
        thread = {"uuid": "thread-1", "total_count": 1}
        self.client.threads = lambda advert_id=None: [dict(thread)]
        attempts = []
        def messages(thread_id):
            attempts.append(thread_id)
            if len(attempts) == 1:
                raise AppError("Тимчасова помилка читання")
            return [message("incoming-2", text="Привіт", timestamp="2026-11-05 12:01:00")]
        self.client.messages = messages
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        thread["total_count"] = 2
        worker.scan()
        worker.scan()
        self.assertEqual(attempts, ["thread-1", "thread-1"])
        self.assertEqual(self.journal.get("thread-1", "incoming-2")["status"], "skipped")

    def test_fast_idle_check_uses_one_metadata_call_and_does_not_rewrite_journal(self):
        worker = self.worker([], include_existing=False)
        worker.scan()
        before = self.path.read_bytes()
        self.client.threads = lambda advert_id=None: self.fail("Fast polling fetched the full inbox")
        worker.scan_active(["thread-1"])
        self.assertEqual(self.client.metadata_threads, ["thread-1"])
        self.assertEqual(self.client.read_threads, [])
        self.assertEqual(before, self.path.read_bytes())

    def test_fast_poll_reads_history_only_when_that_chat_count_grows(self):
        worker = self.worker(["ні"], include_existing=False)
        worker.scan()
        self.client.history.append(message("incoming-2", text="Привіт", timestamp="2026-11-05 12:01:00"))
        self.client.threads = lambda advert_id=None: self.fail("Fast polling fetched the full inbox")
        worker.scan_active(["thread-1"])
        self.assertEqual(self.client.metadata_threads, ["thread-1"])
        self.assertEqual(self.client.read_threads, ["thread-1"])
        self.assertEqual(self.journal.get("thread-1", "incoming-2")["status"], "skipped")

    def test_new_incoming_and_confirmed_send_both_refresh_active_window(self):
        events = []
        worker = ConsoleResponder(self.client, self.journal, lambda prompt: "так", self.output.append,
                                  include_existing=True, on_activity=events.append)
        worker.scan()
        self.assertEqual(events, ["thread-1", "thread-1"])

    def test_unavailable_chat_does_not_keep_fast_polling(self):
        removed = []
        worker = ConsoleResponder(self.client, self.journal, lambda prompt: "ні", self.output.append,
                                  on_unavailable=removed.append)
        worker.scan()
        def missing(thread_id):
            raise AppError("Not found", status=404)
        self.client.thread = missing
        worker.scan_active(["thread-1"])
        self.assertEqual(removed, ["thread-1"])
        self.assertEqual(self.client.read_threads, [])

    def test_fast_poll_cannot_establish_a_partial_startup_baseline(self):
        worker = self.worker([], include_existing=False)
        with self.assertRaises(AppError):
            worker.scan_active(["thread-1"])
        self.assertEqual(self.client.metadata_threads, [])

    def test_ambiguous_timestamp_is_not_answered(self):
        with self.assertRaises(AppError):
            snapshot([message("first"), message("second")])

    def test_unsorted_history_and_numeric_legacy_ids_are_supported(self):
        history = [{"id": 2, "type": "received", "text": "Чи є в наявності?", "created_at": "2026-11-05 12:02:00"},
                   {"id": 1, "type": "sent", "text": "Добрий день", "created_at": "2026-11-05 12:01:00"}]
        self.assertEqual(snapshot(history)["id"], "2")

    def test_console_strips_terminal_control_codes_and_journal_has_no_customer_text(self):
        self.assertNotIn("\x1b", terminal_text("\x1b[2Jhello\x00"))
        self.worker(["ні"]).scan()
        raw = self.path.read_text()
        self.assertNotIn("бажаю замовити", raw)
        self.assertNotIn("Так, в наявності", raw)

    def test_one_worker_lock_is_exclusive(self):
        path = Path(self.temp.name) / "worker.lock"
        with private_lock(path, blocking=False):
            with self.assertRaises(BlockingIOError):
                with private_lock(path, blocking=False):
                    self.fail("Second worker acquired the same lock")


class ClientTests(unittest.TestCase):
    def client(self, responses):
        class App:
            def access_token(self):
                return "access"

            def request(self, path, payload=None, token=None):
                calls.append((path, payload, token))
                return responses(path, payload) if callable(responses) else responses.pop(0)
        calls = []
        return OlxClient(App(), minimum_interval=0), calls

    def test_wrapped_profile_messages_and_post_use_documented_paths(self):
        client, calls = self.client([
            {"data": {"id": 9}},
            {"data": [message()]},
            {"data": {"uuid": "sent-uuid"}},
        ])
        self.assertEqual(client.identity()["id"], 9)
        self.assertEqual(client.messages("thread-1")[0]["uuid"], "incoming-1")
        client.send("thread-1", REPLY)
        self.assertEqual(calls[-1], ("/api/partner/threads/thread-1/messages", {"text": REPLY}, "access"))
        self.assertNotIn("phone", calls[-1][1])

    def test_fast_thread_request_returns_metadata_without_messages(self):
        client, calls = self.client([{"data": {"uuid": "thread-1", "id": 9, "total_count": 3}}])
        self.assertEqual(client.thread("thread-1")["total_count"], 3)
        self.assertEqual(calls, [("/api/partner/threads/thread-1", None, "access")])

    def test_fast_thread_request_rejects_an_unrelated_conversation(self):
        client, _ = self.client([{"data": {"uuid": "other-thread", "total_count": 3}}])
        with self.assertRaises(AppError):
            client.thread("thread-1")

    def test_thread_pagination_keeps_uuid_and_rejects_repeated_items(self):
        client, calls = self.client([
            {"data": [{"uuid": "first", "id": 1}], "links": {"next": "https://www.olx.ua/api/partner/threads?limit=100&offset=100"}},
            {"data": [{"uuid": "second", "id": 2}], "links": {"next": None}},
        ])
        self.assertEqual([item["uuid"] for item in client.threads()], ["first", "second"])
        self.assertIn("offset=100", calls[1][0])
        client, _ = self.client([
            {"data": [{"uuid": "first"}], "links": {"next": "/api/partner/threads?offset=100"}},
            {"data": [{"uuid": "first"}]},
        ])
        with self.assertRaises(AppError):
            client.threads()

    def test_291_threads_use_three_list_requests(self):
        pages = [[{"uuid": "thread-%s" % number, "total_count": 5}
                  for number in range(start, min(start + 100, 291))]
                 for start in (0, 100, 200)]
        client, calls = self.client(pages)
        self.assertEqual(len(client.threads()), 291)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(path.startswith("/api/partner/threads?") for path, _, _ in calls))

    def test_messages_follow_valid_pagination_and_stop_at_external_host(self):
        client, _ = self.client([
            {"data": [message("first")], "links": {"next": "/api/partner/threads/thread-1/messages?offset=100"}},
            {"data": [message("second")]},
        ])
        self.assertEqual(len(client.messages("thread-1")), 2)
        for url in ("https://evil.example/api/partner/threads", "https://user:pass@www.olx.ua/api/partner/threads", "/api/other"):
            with self.subTest(url=url), self.assertRaises(AppError):
                next_path(url, "/api/partner/threads")

    def test_post_with_unknown_response_is_not_treated_as_confirmed(self):
        client, calls = self.client([{}])
        with self.assertRaises(AppError):
            client.send("thread-1", REPLY)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
