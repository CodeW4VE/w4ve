"""The local API, and the promises M8 makes about it.

Its criterion is the design: *an external client can inspect, plan and run
authorised operations without touching private files; its crash or its upgrade
does not stop servers.* So what is checked here is not that the routes answer,
it is the four things that would make the API dangerous if they were wrong:

- a token that may only read cannot stop a world;
- a wrong token is refused, and a missing one is refused differently;
- a bug in the API is a 500 and not a dead guardian;
- the API failing to start does not stop the server from starting.

Real sockets and a real HTTP client, because an API tested through its own
Python functions is an API whose HTTP layer is untested.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk" / "client"))

import api  # noqa: E402
from w4ve_client import W4VE, Denied, NotRunning, W4VEError  # noqa: E402


class FakeGuardian:
    """Answers like the real one, and remembers what it was asked."""

    def __init__(self):
        self.asked = []
        self.workers = None

    def handle(self, request):
        self.asked.append(request)
        cmd = request.get("cmd")
        if cmd == "status":
            return {"ok": True, "root": "/somewhere",
                    "processes": [{"name": "server", "state": "running",
                                   "ready": True, "pid": 1234}]}
        if cmd in ("start", "stop"):
            return {"ok": True, "message": "%s done" % cmd}
        if cmd == "send":
            return {"ok": True, "message": "sent", "lines": ["pong"]}
        return {"ok": False, "error": "unknown"}


class TokenCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-tok-"))
        (self.tmp / "w4ve").mkdir()
        self.tokens = api.Tokens(self.tmp)

    def test_a_token_is_written_600_and_readable_back(self):
        token = self.tokens.issue("dashboard", "read")
        self.assertTrue(token)
        mode = stat.S_IMODE(self.tokens.path.stat().st_mode)
        self.assertEqual(mode, 0o600, "a token has to be readable by nobody else")
        entry = self.tokens.check(token)
        self.assertEqual(entry["name"], "dashboard")
        self.assertEqual(entry["scope"], "read")

    def test_listing_never_shows_the_tokens_themselves(self):
        token = self.tokens.issue("dashboard", "read")
        text = json.dumps(self.tokens.listing())
        self.assertIn("dashboard", text)
        self.assertNotIn(token, text,
                         "listing tokens must never print the token")

    def test_a_wrong_token_is_nobody(self):
        self.tokens.issue("dashboard", "read")
        self.assertIsNone(self.tokens.check("not-a-real-token"))
        self.assertIsNone(self.tokens.check(""))

    def test_revoking_takes_it_away(self):
        token = self.tokens.issue("dashboard", "read")
        self.assertEqual(self.tokens.revoke("dashboard"), 1)
        self.assertIsNone(self.tokens.check(token))

    def test_a_scope_nobody_defined_is_refused(self):
        with self.assertRaises(ValueError):
            self.tokens.issue("x", "superuser")

    def test_scopes_are_cumulative_and_read_is_the_floor(self):
        self.assertTrue(api.allowed("admin", "write"))
        self.assertTrue(api.allowed("write", "read"))
        self.assertFalse(api.allowed("read", "write"),
                         "a dashboard must not be able to stop a world")
        self.assertFalse(api.allowed("write", "admin"))
        self.assertTrue(api.allowed("read", None), "hello needs nothing")


class ServedCase(unittest.TestCase):
    """Through real HTTP, over a real socket."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-"))
        (self.tmp / "w4ve" / "run").mkdir(parents=True)
        self.guardian = FakeGuardian()
        self.api = api.Api(self.tmp, guardian=self.guardian)
        ok, message = self.api.start()
        self.assertTrue(ok, message)
        self.client = W4VE(self.tmp)

    def tearDown(self):
        self.api.stop()

    def test_the_socket_is_ours_alone(self):
        mode = stat.S_IMODE(self.api.socket_path.stat().st_mode)
        self.assertEqual(mode, 0o600,
                         "the file mode is the whole authentication story here")

    def test_hello_says_what_this_is(self):
        answer = self.client.hello()
        self.assertEqual(answer["software"], "w4ve")
        self.assertEqual(answer["api"], api.VERSION)
        self.assertIn("GET /v1/status", answer["routes"])

    def test_status_comes_from_the_guardian_and_not_from_a_file(self):
        processes = self.client.processes()
        self.assertEqual(processes[0]["name"], "server")
        self.assertTrue(any(r.get("cmd") == "status"
                            for r in self.guardian.asked),
                        "it answered without asking the guardian")

    def test_a_command_reaches_the_guardian(self):
        answer = self.client.command("say hello")
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["lines"], ["pong"])
        sent = [r for r in self.guardian.asked if r.get("cmd") == "send"]
        self.assertEqual(sent[-1]["text"], "say hello")

    def test_a_route_nobody_defined_is_a_404_and_not_a_guess(self):
        with self.assertRaises(W4VEError):
            self.client.call("GET", "/v1/everything")

    def test_a_command_with_no_command_in_it_is_refused(self):
        with self.assertRaises(W4VEError):
            self.client.call("POST", "/v1/command", {})

    def test_a_body_that_is_not_json_does_not_take_it_down(self):
        # Sent by hand, because the client would never produce this.
        import http.client
        connection = api_connection(self.api.socket_path)
        connection.request("POST", "/v1/command", body=b"{not json",
                           headers={"Content-Type": "application/json",
                                    "Content-Length": "9"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 400)
        # And it is still there afterwards, which is the actual point.
        self.assertTrue(self.client.hello()["ok"])

    def test_a_bug_in_a_route_is_a_500_and_the_api_survives(self):
        def explode(route, body):
            raise RuntimeError("this is a bug")
        saved, self.api.run = self.api.run, explode
        try:
            with self.assertRaises(W4VEError) as caught:
                self.client.status()
            self.assertIn("this is a bug", str(caught.exception))
        finally:
            self.api.run = saved
        self.assertTrue(self.client.hello()["ok"], "the API died with the bug")


class ScopeCase(unittest.TestCase):
    """A token that may read must not be able to do anything else."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-scope-"))
        (self.tmp / "w4ve" / "run").mkdir(parents=True)
        self.guardian = FakeGuardian()
        self.api = api.Api(self.tmp, guardian=self.guardian,
                           tokens_required=True)
        ok, message = self.api.start()
        self.assertTrue(ok, message)
        self.reader = self.api.tokens.issue("dashboard", "read")
        self.writer = self.api.tokens.issue("operator", "write")

    def tearDown(self):
        self.api.stop()

    def client(self, token=""):
        return W4VE(self.tmp, token=token)

    def test_no_token_is_refused(self):
        with self.assertRaises(Denied):
            self.client().status()

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(Denied):
            self.client("made-up").status()

    def test_hello_answers_without_one(self):
        # So a client can find out what it is talking to before it has
        # credentials for it.
        self.assertEqual(self.client().hello()["software"], "w4ve")

    def test_a_reader_can_read(self):
        self.assertTrue(self.client(self.reader).processes())

    def test_a_reader_cannot_stop_the_server(self):
        with self.assertRaises(Denied) as caught:
            self.client(self.reader).stop()
        self.assertIn("write", str(caught.exception))
        self.assertFalse(any(r.get("cmd") == "stop" for r in self.guardian.asked),
                         "it was refused and the guardian was asked anyway")

    def test_a_writer_can(self):
        self.assertTrue(self.client(self.writer).stop()["ok"])

    def test_a_writer_cannot_reload_plugins(self):
        with self.assertRaises(Denied):
            self.client(self.writer).reload_plugin("greeter")


class SeparableCase(unittest.TestCase):
    """The API is an extra, and an extra that fails is still only an extra."""

    def test_a_port_that_is_taken_is_reported_and_not_raised(self):
        import socket as socket_module

        blocker = socket_module.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = "127.0.0.1:%d" % blocker.getsockname()[1]
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-busy-"))
        (tmp / "w4ve" / "run").mkdir(parents=True)
        try:
            instance = api.Api(tmp, address=taken)
            ok, message = instance.start()
            self.assertFalse(ok)
            self.assertIn("could not listen", message)
        finally:
            blocker.close()

    def test_listening_on_a_port_always_needs_a_token(self):
        # A socket is protected by its file mode. A port is not protected by
        # anything, so the default flips.
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-port-"))
        self.assertTrue(api.Api(tmp, address="127.0.0.1:8790").tokens_required)
        self.assertFalse(api.Api(tmp).tokens_required)

    def test_a_client_with_no_api_running_is_told_so_plainly(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-api-none-"))
        with self.assertRaises(NotRunning) as caught:
            W4VE(tmp).status()
        self.assertIn("w4ve run", str(caught.exception))


def api_connection(path):
    import http.client
    import socket as socket_module

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket_module.socket(socket_module.AF_UNIX,
                                             socket_module.SOCK_STREAM)
            self.sock.connect(str(path))

    return Connection("localhost", timeout=10)


if __name__ == "__main__":
    unittest.main()
