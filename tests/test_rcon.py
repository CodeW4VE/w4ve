"""Tests for the RCON fallback: talking to a server we do not own.

Against a server that really speaks the protocol, including the parts that are
easy to get wrong: a refused password, an answer long enough to arrive in
several packets, and settings read out of a real server.properties.
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import rcon  # noqa: E402
from fakercon import FakeRcon  # noqa: E402


class ProtocolCase(unittest.TestCase):

    def setUp(self):
        self.server = FakeRcon(password="hunter2", answers={
            "list": "There are 2 of a max of 20 players online: TVTvirus, Karla",
            "long": "x" * 900,
        })
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_a_command_and_its_answer(self):
        with rcon.Rcon("127.0.0.1", self.server.port, "hunter2") as console:
            answer = console.command("list")
        self.assertIn("TVTvirus", answer)
        self.assertEqual(self.server.seen, ["list"])

    def test_an_answer_too_long_for_one_packet(self):
        """Long replies arrive split, with nothing marking the end of them."""
        with rcon.Rcon("127.0.0.1", self.server.port, "hunter2") as console:
            answer = console.command("long")
        self.assertEqual(len(answer), 900)

    def test_a_wrong_password_is_refused_and_says_so(self):
        with self.assertRaises(rcon.RconError) as caught:
            rcon.Rcon("127.0.0.1", self.server.port, "wrong").connect()
        self.assertIn("refused the password", str(caught.exception))

    def test_nothing_listening(self):
        import socket

        with socket.socket() as probe:      # a port nobody is on
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with self.assertRaises(rcon.RconError) as caught:
            rcon.Rcon("127.0.0.1", closed, "hunter2", timeout=2).connect()
        self.assertIn("cannot reach RCON", str(caught.exception))

    def test_a_server_that_dies_mid_conversation(self):
        console = rcon.Rcon("127.0.0.1", self.server.port, "hunter2").connect()
        self.server.stop()
        with self.assertRaises(rcon.RconError):
            for _ in range(5):              # one of these hits the dead socket
                console.command("list")


class SettingsCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-rcon-"))

    def write(self, text):
        (self.tmp / "server.properties").write_text(text)

    def test_reads_what_the_server_actually_has(self):
        self.write("enable-rcon=true\nrcon.port=25579\nrcon.password=hunter2\n"
                   "# a comment\nmotd=A Minecraft Server\n")
        creds, why = rcon.credentials_from(self.tmp)
        self.assertEqual(why, "")
        self.assertEqual(creds["port"], 25579)
        self.assertEqual(creds["password"], "hunter2")
        self.assertEqual(creds["host"], "127.0.0.1")

    def test_says_why_when_there_is_no_rcon(self):
        self.write("enable-rcon=false\nrcon.password=hunter2\n")
        creds, why = rcon.credentials_from(self.tmp)
        self.assertIsNone(creds)
        self.assertIn("off in server.properties", why)

    def test_says_why_when_there_is_no_password(self):
        self.write("enable-rcon=true\nrcon.password=\n")
        creds, why = rcon.credentials_from(self.tmp)
        self.assertIsNone(creds)
        self.assertIn("no password", why)

    def test_no_file_at_all(self):
        creds, why = rcon.credentials_from(self.tmp)
        self.assertIsNone(creds)
        self.assertIn("no server.properties", why)

    def test_a_password_never_reaches_a_log_line(self):
        self.assertEqual(rcon.redact("failed for hunter2 at 25575", "hunter2"),
                         "failed for *** at 25575")


class FallbackCase(unittest.TestCase):
    """The guardian uses RCON exactly where RFC-0001 says it should."""

    def setUp(self):
        import runtime

        self.runtime = runtime
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-fallback-"))
        self.server = FakeRcon(password="hunter2",
                               answers={"stop": "Stopping the server",
                                        "list": "There are 0 players online"})
        self.server.start()
        (self.tmp / "server.properties").write_text(
            "enable-rcon=true\nrcon.port=%d\nrcon.password=hunter2\n" % self.server.port)
        self.guardian = runtime.Guardian(self.tmp)
        got_it, _ = self.guardian.claim()
        self.assertTrue(got_it)

    def tearDown(self):
        self.guardian.shutdown(stop_processes=False)
        self.guardian.release()
        self.server.stop()

    def adopted_server(self):
        """A server the guardian is watching but did not start."""
        import subprocess

        proc = self.guardian.add_server(["true"], str(self.tmp), flavor="fabric")
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(live.kill)
        proc.adopt(live.pid, self.runtime.starttime_of(live.pid))
        return proc

    def test_an_adopted_server_is_still_reachable(self):
        proc = self.adopted_server()
        ok, answer = proc.send("list")
        self.assertTrue(ok, answer)
        self.assertIn("0 players", answer)
        self.assertEqual(self.server.seen, ["list"])

    def test_status_says_whether_there_is_rcon(self):
        proc = self.adopted_server()
        self.assertTrue(proc.status()["rcon"])

    def test_without_rcon_it_says_so_instead_of_pretending(self):
        (self.tmp / "server.properties").write_text("enable-rcon=false\n")
        proc = self.adopted_server()
        ok, message = proc.send("list")
        self.assertFalse(ok)
        self.assertIn("no console and no RCON", message)
        self.assertIn("off in server.properties", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
