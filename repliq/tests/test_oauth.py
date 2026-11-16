import http.client
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app import AppError, Repliq, callback_url, make_server, read_json


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.response = {"access_token": "access-private", "refresh_token": "refresh-private",
                         "expires_in": 86400, "scope": "v2 read write", "token_type": "bearer"}

        def fake(path, payload=None, token=None):
            self.calls.append((path, payload, token))
            return dict(self.response)

        self.app = Repliq(data_dir=self.temp.name, request=fake)
        self.settings = {"callback_uri": "http://127.0.0.1:8787/oauth/callback",
                         "client_id": "client-test", "client_secret": "secret-private"}
        self.app.save_settings(self.settings)

    def flow(self):
        ticket = parse_qs(urlsplit(self.app.begin()).query)["ticket"][0]
        url, cookie = self.app.authorize(ticket)
        parameters = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        return parameters, cookie

    def finish(self):
        fields, cookie = self.flow()
        fields["code"] = "one-time-private"
        self.app.finish(fields, cookie)
        return fields, cookie

    def test_exchange_has_matching_redirect_and_server_only_credentials(self):
        fields, cookie = self.finish()
        path, payload, token = self.calls[0]
        self.assertEqual(path, "/api/open/oauth/token")
        self.assertEqual(payload["grant_type"], "authorization_code")
        self.assertEqual(payload["redirect_uri"], self.settings["callback_uri"])
        self.assertEqual(payload["code"], "one-time-private")
        self.assertEqual(payload["client_secret"], "secret-private")
        tokens = read_json(self.app.tokens_path)
        self.assertEqual(tokens["refresh_token"], "refresh-private")
        self.assertGreater(tokens["expires_at"], time.time())
        self.assertEqual(stat.S_IMODE(self.app.tokens_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.app.data_dir.stat().st_mode), 0o700)
        rendered = self.app.render()
        for private in ("secret-private", "access-private", "refresh-private", "one-time-private"):
            self.assertNotIn(private, rendered)

    def test_authorization_ticket_and_code_cannot_be_replayed(self):
        ticket = parse_qs(urlsplit(self.app.begin()).query)["ticket"][0]
        self.app.authorize(ticket)
        with self.assertRaises(AppError):
            self.app.authorize(ticket)
        fields, cookie = self.finish()
        with self.assertRaises(AppError):
            self.app.finish(fields, cookie)
        self.assertEqual(len(self.calls), 1)

    def test_state_must_be_bound_to_correct_browser_and_not_expired(self):
        fields, cookie = self.flow()
        fields["code"] = "code"
        for wrong in ("", "wrong-cookie", "неправильний"):
            with self.assertRaises(AppError):
                self.app.finish(fields, wrong)
        with self.assertRaises(AppError):
            self.app.finish({"state": "wrong", "code": "code"}, cookie)
        self.app.pending[fields["state"]]["expires"] = time.time() - 1
        with self.assertRaises(AppError):
            self.app.finish(fields, cookie)
        self.assertEqual(self.calls, [])

    def test_denied_or_missing_code_never_calls_token_api(self):
        for extra in ({"error": "access_denied"}, {}):
            fields, cookie = self.flow()
            fields.update(extra)
            with self.assertRaises(AppError):
                self.app.finish(fields, cookie)
        self.assertEqual(self.calls, [])

    def test_refresh_persists_rotated_refresh_token_and_keeps_it_if_omitted(self):
        self.finish()
        self.response.update(access_token="access-new", refresh_token="refresh-new")
        self.app.refresh()
        self.assertEqual(read_json(self.app.tokens_path)["refresh_token"], "refresh-new")
        self.assertEqual(self.calls[-1][1]["refresh_token"], "refresh-private")
        self.response.pop("refresh_token")
        self.app.refresh()
        self.assertEqual(self.calls[-1][1]["refresh_token"], "refresh-new")
        self.assertEqual(read_json(self.app.tokens_path)["refresh_token"], "refresh-new")

    def test_invalid_token_response_does_not_overwrite_valid_tokens(self):
        self.finish()
        before = self.app.tokens_path.read_bytes()
        for response in ({}, {"access_token": "new", "expires_in": "bad"},
                         {"access_token": "new", "expires_in": 0}):
            self.response = response
            with self.assertRaises(AppError):
                self.app.refresh()
            self.assertEqual(self.app.tokens_path.read_bytes(), before)

    def test_saving_blank_secret_preserves_it_and_changed_client_clears_tokens(self):
        self.finish()
        settings = dict(self.settings, client_secret="")
        self.app.save_settings(settings)
        self.assertEqual(self.app.settings["client_secret"], "secret-private")
        self.assertTrue(read_json(self.app.tokens_path).get("access_token"))
        self.app.save_settings(dict(settings, client_id="new-client"))
        self.assertEqual(read_json(self.app.tokens_path), {})
        self.assertEqual(self.app.pending, {})

    def test_new_cloudflare_address_keeps_existing_grant_and_refresh(self):
        self.finish()
        before = self.app.tokens_path.read_bytes()
        self.flow()
        self.app.save_settings(dict(self.settings,
                                   callback_uri="https://new-tunnel.trycloudflare.com/oauth/callback"))
        self.assertEqual(self.app.tokens_path.read_bytes(), before)
        self.assertEqual(self.app.pending, {})
        restarted = Repliq(data_dir=self.temp.name, request=self.app.request)
        restarted.refresh()
        self.assertEqual(self.calls[-1][1]["refresh_token"], "refresh-private")
        self.assertNotIn("redirect_uri", self.calls[-1][1])

    def test_blank_credentials_do_not_erase_saved_keys_or_tokens(self):
        self.finish()
        before = self.app.tokens_path.read_bytes()
        self.app.save_settings(dict(self.settings, client_id=" ", client_secret=""))
        self.assertEqual(self.app.settings, self.settings)
        self.assertEqual(self.app.tokens_path.read_bytes(), before)

    def test_credentials_change_keeps_private_recovery_copy(self):
        self.finish()
        old_tokens = read_json(self.app.tokens_path)
        self.app.save_settings(dict(self.settings, client_secret="new-secret"))
        self.assertEqual(read_json(self.app.tokens_path), {})
        backup_path = self.app.data_dir / "oauth-backup.json"
        backup = read_json(backup_path)
        self.assertEqual(backup, {"settings": self.settings, "tokens": old_tokens})
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.app.save_settings(dict(self.app.settings,
                                   callback_uri="https://another.example/oauth/callback"))
        self.assertEqual(read_json(backup_path), backup)
        for secret in ("secret-private", "refresh-private", "access-private"):
            self.assertNotIn(secret, self.app.render())

    def test_refresh_backup_contains_latest_rotated_token(self):
        self.finish()
        self.response.update(access_token="latest-access", refresh_token="latest-refresh")
        self.app.refresh()
        backup = read_json(self.app.data_dir / "oauth-backup.json")
        self.assertEqual(backup["tokens"], read_json(self.app.tokens_path))
        self.assertEqual(backup["tokens"]["refresh_token"], "latest-refresh")

    def test_invalid_response_does_not_replace_recovery_copy(self):
        self.finish()
        backup_path = self.app.data_dir / "oauth-backup.json"
        before = backup_path.read_bytes()
        self.response = {}
        with self.assertRaises(AppError):
            self.app.refresh()
        self.assertEqual(backup_path.read_bytes(), before)

    def test_verify_refreshes_expired_token_and_uses_read_only_endpoint(self):
        self.finish()
        tokens = read_json(self.app.tokens_path)
        tokens["expires_at"] = time.time() - 1
        from app import write_private
        write_private(self.app.tokens_path, tokens)
        self.response["access_token"] = "renewed-access"
        self.app.verify()
        self.assertEqual(self.calls[-2][1]["grant_type"], "refresh_token")
        self.assertEqual(self.calls[-1], ("/api/partner/users/me", None, "renewed-access"))

    def test_callback_validation_rejects_unsafe_or_wrong_routes(self):
        for value in ("http://example.com/oauth/callback", "https://example.com/wrong",
                      "https://user:secret@example.com/oauth/callback",
                      "https://example.com/oauth/callback?x=1", "http://127.0.0.1:9999/oauth/callback",
                      "https://example.com:bad/oauth/callback", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(AppError):
                callback_url(value, 8787)
        self.assertEqual(callback_url("https://example.com/oauth/callback", 8787),
                         "https://example.com/oauth/callback")

    def test_restart_keeps_credentials_but_requires_a_new_authorization_session(self):
        fields, cookie = self.flow()
        restarted = Repliq(data_dir=self.temp.name)
        self.assertEqual(restarted.settings, self.app.settings)
        with self.assertRaises(AppError):
            restarted.finish(dict(fields, code="code"), cookie)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = Repliq(port=0, data_dir=cls.temp.name, request=lambda *a, **kw: {})
        cls.server = make_server(cls.app)
        cls.port = cls.server.server_address[1]
        cls.app.port = cls.port
        cls.app.local_url = "http://127.0.0.1:%s" % cls.port
        cls.app.settings["callback_uri"] = cls.app.local_url + "/oauth/callback"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method, path, fields=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(headers or {})
        if fields is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=urlencode(fields) if fields is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
        connection.close()
        return result

    def test_home_and_empty_callback_work_without_olx_credentials(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Repliq", body)
        self.assertIn("Підготуй заявку", body)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn('<meta name="referrer" content="same-origin">', body)
        status, _, body = self.request("GET", "/oauth/callback")
        self.assertEqual(status, 200)
        self.assertIn("Адреса зворотного виклику працює", body)

    def test_browser_form_saves_keys_with_local_origin_cookie_and_csrf(self):
        # Use a separate server/data directory so the regression cannot alter
        # other tests or real credentials. Headers model a normal browser POST.
        with tempfile.TemporaryDirectory() as directory:
            app = Repliq(port=0, data_dir=directory)
            server = make_server(app)
            port = server.server_address[1]
            app.port = port
            app.local_url = "http://127.0.0.1:%s" % port
            app.settings["callback_uri"] = app.local_url + "/oauth/callback"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                cookie = response.getheader("Set-Cookie").split(";")[0]
                self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
                self.assertIn('<meta name="referrer" content="same-origin">', body)
                fields = {"csrf": app.csrf, "callback_uri": app.settings["callback_uri"],
                          "client_id": "test-client", "client_secret": "dummy-test-secret"}
                connection.request("POST", "/settings", body=urlencode(fields), headers={
                    "Cookie": cookie, "Origin": app.local_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                })
                response = connection.getresponse()
                result = response.read().decode("utf-8")
                self.assertEqual(response.status, 303)
                self.assertEqual(read_json(app.settings_path)["client_secret"], "dummy-test-secret")
                self.assertNotIn("dummy-test-secret", result)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_null_origin_does_not_bypass_form_protection(self):
        fields = {"csrf": self.app.csrf}
        cookie = "repliq_admin=" + self.app.admin_session
        status, _, body = self.request("POST", "/connect", fields, {"Cookie": cookie, "Origin": "null"})
        self.assertEqual(status, 403)
        self.assertIn("Відкрити головну сторінку Repliq", body)

    def test_forms_require_cookie_csrf_and_local_origin(self):
        valid = {"csrf": self.app.csrf}
        cookie = "repliq_admin=" + self.app.admin_session
        for fields, headers in ((valid, {}), ({"csrf": "wrong"}, {"Cookie": cookie}),
                                ({"csrf": "неправильно"}, {"Cookie": cookie}),
                                (valid, {"Cookie": cookie, "Origin": "https://evil.example"})):
            with self.subTest(fields=fields, headers=headers):
                status, _, _ = self.request("POST", "/connect", fields, headers)
                self.assertEqual(status, 403)
        status, headers, _ = self.request("POST", "/connect", valid, {"Cookie": cookie})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("схвалення OLX", self.app.notice)

    def test_admin_is_not_served_for_external_host(self):
        for path in ("/", "/settings"):
            status, _, _ = self.request("GET", path, headers={"Host": "public.example"})
            self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/oauth/callback", headers={"Host": "public.example"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
