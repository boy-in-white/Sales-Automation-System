import http.client
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from app import Repliq, make_server, read_json
from public import PUBLIC_URL


class PublicCallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        def fake(path, payload=None, token=None):
            self.calls.append((path, payload, token))
            return {"access_token": "private-access", "refresh_token": "private-refresh",
                    "expires_in": 86400, "token_type": "bearer", "scope": "v2 read write"}

        self.app = Repliq(port=8787, data_dir=self.temp.name, request=fake)
        self.app.settings = {"callback_uri": "https://public.example/oauth/callback",
                             "client_id": "client", "client_secret": "private-secret"}
        self.server = make_server(self.app, port=0, public_only=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, headers=None, fields=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Host": "public.example", **(headers or {})}
        if fields is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=urlencode(fields) if fields is not None else None,
                           headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
        connection.close()
        return result

    def test_forged_local_host_cannot_expose_admin_page_or_settings(self):
        headers = {"Host": "127.0.0.1:8787", "Cookie": "repliq_admin=" + self.app.admin_session}
        status, response_headers, body = self.request("GET", "/", headers)
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", response_headers)
        self.assertNotIn(self.app.csrf, body)
        self.assertNotIn("Client Secret", body)
        for path in ("/settings", "/connect", "/verify", "/refresh", "/private/settings.json", "/health"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path, headers)
                self.assertEqual(status, 404)
                status, _, _ = self.request("POST", path, headers, {"csrf": self.app.csrf})
                self.assertEqual(status, 404)
        self.assertEqual(self.calls, [])

    def test_public_callback_works_and_public_host_must_match(self):
        status, headers, body = self.request("GET", "/oauth/callback")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("Адреса зворотного виклику працює", body)
        status, _, _ = self.request("GET", "/oauth/callback", {"Host": "127.0.0.1:8787"})
        self.assertEqual(status, 400)

    def test_full_public_handshake_keeps_tokens_local_and_uses_exact_public_uri(self):
        begin_url = self.app.begin()
        status, headers, _ = self.request("GET", "/oauth/begin?" + urlsplit(begin_url).query)
        self.assertEqual(status, 303)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        parameters = parse_qs(urlsplit(headers["Location"]).query)
        self.assertEqual(parameters["redirect_uri"], ["https://public.example/oauth/callback"])
        status, headers, body = self.request("GET", "/oauth/callback?" + urlencode({
            "state": parameters["state"][0], "code": "private-code",
        }), {"Cookie": headers["Set-Cookie"].split(";")[0]})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], self.app.local_url)
        self.assertEqual(self.calls[0][1]["redirect_uri"], "https://public.example/oauth/callback")
        self.assertEqual(read_json(self.app.tokens_path)["access_token"], "private-access")
        for value in ("private-code", "private-secret", "private-access", "private-refresh"):
            self.assertNotIn(value, body + str(headers))

    def test_external_callback_without_browser_session_cannot_exchange_code(self):
        status, _, _ = self.request("GET", "/oauth/callback?code=arbitrary&state=arbitrary")
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    def test_only_expected_cloudflare_hostname_is_captured(self):
        match = PUBLIC_URL.search("| https://short-example-words.trycloudflare.com  |")
        self.assertEqual(match.group(0), "https://short-example-words.trycloudflare.com")
        self.assertIsNone(PUBLIC_URL.search("https://test.trycloudflare.com.evil.example"))
        self.assertIsNone(PUBLIC_URL.search("https://test.example.com"))


if __name__ == "__main__":
    unittest.main()
