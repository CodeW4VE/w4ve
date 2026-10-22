"""Tests for `w4ve http check`: asking a public endpoint whether it is really up.

Writing an nginx file is not deploying it, so the doctor stopped claiming it
knows. What replaces that claim is this probe, and it has to be honest in both
directions: a certificate it cannot verify is a failure even when the body is
right, and a body that says `ready: false` is a failure even when TLS is fine.
It sends nothing that identifies anybody and follows no redirect.

The TLS cases run a real HTTPS server on loopback with a throwaway certificate
made by openssl. Without openssl they are skipped, not faked.
"""

import http.server
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import httpprobe  # noqa: E402


class WhatIsRefusedBeforeConnecting(unittest.TestCase):

    def test_plain_http_is_not_a_public_endpoint(self):
        self.assertEqual(httpprobe.check("http://example.com/health")["reason"],
                         "invalid_https_endpoint")

    def test_credentials_query_and_fragment_never_leave_this_machine(self):
        for url in ("https://user:pw@example.com/", "https://example.com/?token=x",
                    "https://example.com/#x"):
            self.assertEqual(httpprobe.check(url)["reason"], "invalid_https_endpoint", url)

    def test_a_timeout_longer_than_ten_seconds_is_refused(self):
        self.assertEqual(httpprobe.check("https://example.com/", timeout=60)["reason"],
                         "invalid_probe")

    def test_dry_run_validates_and_connects_to_nothing(self):
        result = httpprobe.check("https://203.0.113.1/health", dry_run=True)
        self.assertEqual(result, {"ok": True, "checked": False, "reason": "dry_run"})


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        Handler.seen.append(dict(self.headers))
        status, body = Handler.routes.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status in (301, 302):
            self.send_header("Location", "/ok")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a test certificate")
class AgainstARealHttpsServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        cls.cert, key = folder / "cert.pem", folder / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cls.cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"https://localhost:{cls.server.server_address[1]}"
        Handler.routes = {
            "/ok": (200, json.dumps({"ok": True, "software": "wavechat",
                                     "capabilities": {"chat": True, "music": False}}).encode()),
            "/booting": (200, b'{"ready": false}'),
            "/list": (200, b"[1, 2]"),
            "/broken": (200, b"not json"),
            "/huge": (200, b" " * 20000),
            "/moved": (302, b""),
            "/gone": (410, b""),
        }

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def probe(self, path, **kwargs):
        kwargs.setdefault("ca_file", self.cert)
        return httpprobe.check(self.base + path, **kwargs)

    def test_a_ready_endpoint_with_its_software_and_capability_passes(self):
        result = self.probe("/ok", software="wavechat", capabilities=["chat"])
        self.assertEqual(result, {"ok": True, "reason": "https_json_verified", "status": 200})

    def test_a_certificate_nobody_vouched_for_fails_even_with_the_right_body(self):
        result = httpprobe.check(self.base + "/ok")
        self.assertEqual(result["reason"], "certificate_verification_failed")

    def test_the_wrong_software_on_the_right_url_is_not_the_piece(self):
        self.assertEqual(self.probe("/ok", software="other")["reason"], "software_mismatch")

    def test_a_capability_that_is_off_or_missing_fails(self):
        self.assertEqual(self.probe("/ok", capabilities=["music"])["reason"], "capability_unavailable")
        self.assertEqual(self.probe("/ok", capabilities=["dm"])["reason"], "capability_unavailable")

    def test_up_but_not_ready_is_a_failure(self):
        self.assertEqual(self.probe("/booting")["reason"], "endpoint_not_ready")

    def test_a_body_that_is_not_a_json_object_fails(self):
        self.assertEqual(self.probe("/list")["reason"], "invalid_json")
        self.assertEqual(self.probe("/broken")["reason"], "invalid_json")

    def test_a_body_too_large_is_cut_and_refused(self):
        self.assertEqual(self.probe("/huge")["reason"], "response_too_large")

    def test_a_redirect_is_reported_not_followed(self):
        result = self.probe("/moved")
        self.assertEqual((result["reason"], result["status"]), ("unexpected_status", 302))

    def test_a_status_other_than_200_can_be_the_expected_answer(self):
        result = self.probe("/gone", expected_status=410)
        self.assertEqual(result, {"ok": True, "reason": "https_status_verified", "status": 410})

    def test_the_request_carries_no_credentials_or_cookies(self):
        Handler.seen.clear()
        self.probe("/ok")
        sent = {name.lower() for name in Handler.seen[-1]}
        self.assertFalse(sent & {"authorization", "cookie", "proxy-authorization"}, sent)

    def test_nothing_listening_is_unavailable_not_a_crash(self):
        result = httpprobe.check("https://localhost:1/health", ca_file=self.cert, timeout=2)
        self.assertFalse(result["ok"])
        # Linux refuses at once; Windows retries a refused SYN until the deadline.
        self.assertIn(result["reason"], ("endpoint_unavailable", "deadline_exceeded"))


if __name__ == "__main__":
    unittest.main()
