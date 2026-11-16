import io
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import AppError, MAX_API_BYTES, olx_request
from chat_store import Store
from olx_client import OlxClient


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close_current)

    def test_connection_is_reused_but_transactions_rollback(self):
        with self.store.db() as db:
            first = db
        with self.assertRaises(RuntimeError):
            with self.store.db() as db:
                self.assertIs(db, first)
                db.execute("INSERT INTO meta VALUES('uncommitted','{}')")
                raise RuntimeError("rollback")
        self.assertEqual(self.store.meta("uncommitted"), {})
        with self.store.db() as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='uncommitted'").fetchone())
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_threads_do_not_share_connections_and_writes_are_visible(self):
        output = queue.Queue()
        with self.store.db() as db:
            main_connection = db
        def worker():
            try:
                with self.store.db() as db:
                    output.put(db is main_connection)
                self.store.set_meta("from-thread", {"ok": True})
            finally:
                self.store.close_current()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(output.get(timeout=1))
        self.assertEqual(self.store.meta("from-thread"), {"ok": True})

    def test_nested_success_does_not_commit_before_outer_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.db() as db:
                db.execute("INSERT INTO meta VALUES('outer','{}')")
                self.store.set_meta("inner", {"ok": True})
                raise RuntimeError("rollback both")
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM meta WHERE key IN ('outer','inner')").fetchone()[0], 0)

    def test_ttl_is_enforced_even_when_archive_purge_is_deferred(self):
        draft_id = self.store.stage("account", "session", "thread", {"source_ids": ["m"]}, 900, 200)
        with self.store.db() as db:
            db.execute("UPDATE drafts SET expires=? WHERE id=?", (time.time() - 1, draft_id))
        self.store.cleanup(30, purge=False)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()[0], "expired")


class ResponseLimitTests(unittest.TestCase):
    def test_oversized_olx_response_is_rejected(self):
        response = io.BytesIO(b" " * (MAX_API_BYTES + 1))
        with patch("app.urlopen", return_value=response):
            with self.assertRaises(AppError):
                olx_request("/api/partner/threads")

    def test_regular_json_response_is_accepted(self):
        with patch("app.urlopen", return_value=io.BytesIO(b'{"id":123}')):
            self.assertEqual(olx_request("/api/partner/users/me"), {"id": 123})

    def test_message_budget_includes_attachment_metadata(self):
        client = OlxClient(None)
        payload = [{"uuid": "m1", "text": "", "attachments": [{"url": "https://example.test/" + "a" * (2 * 1024 * 1024)}]}]
        with patch.object(client, "_call", return_value=payload):
            with self.assertRaises(AppError):
                client.messages("thread")


if __name__ == "__main__":
    unittest.main()
