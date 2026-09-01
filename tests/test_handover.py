"""Replacing the guardian without restarting the world (RFC-0001, last item).

The server already survived its guardian dying: that is tested in
`test_runtime.py`. What it did NOT survive was an *upgrade*, because the new
guardian could only adopt what it found, and an adopted server has no console.
So updating the runtime meant restarting the world, which for a server with
people on it is the whole cost of the update.

This moves the two ends of the console over the control socket with
`SCM_RIGHTS`, and what follows is checked against two guardians and a server
that all really exist. The thing to prove is negative and specific: **the pid
never changes**.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402

GUARD = str(HERE / "guard.py")
FAKE = str(HERE / "fakeserver.py")


def wait_for(check, seconds=20, every=0.05):
    limit = time.time() + seconds
    while time.time() < limit:
        answer = check()
        if answer:
            return answer
        time.sleep(every)
    return None


class HandoverCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-handover-"))
        self.guardians = []

    def tearDown(self):
        for proc in self.guardians:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # ------------------------------------------------------------- utilities

    def start_guardian(self, take_over=False, boot="0.2"):
        argv = [sys.executable, GUARD, str(self.tmp)]
        if take_over:
            argv.append("--take-over")
        argv += ["--", sys.executable, FAKE, "--boot", boot]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True)
        self.guardians.append(proc)
        return proc

    def client(self):
        return runtime.Client(self.tmp)

    def server(self, client=None):
        reply = (client or self.client()).call({"cmd": "status"})
        if not reply.get("ok"):
            return {}
        for entry in reply.get("processes", []):
            if entry["name"] == "server":
                return entry
        return {}

    def wait_ready(self, client):
        self.assertTrue(wait_for(lambda: self.server(client).get("ready")),
                        "the server never came up")
        return self.server(client)

    def console_says(self, client, command, expected, seconds=10):
        """Send something and wait for it to show up on the console.

        This is the real proof that a guardian holds stdin: RCON is not set up
        here, so if the console were gone the command would have nowhere to go.
        """
        seen = []
        stop = time.time() + seconds
        thread_client = runtime.Client(self.tmp)

        import threading
        done = threading.Event()

        def follow():
            thread_client.tail(lambda name, line: (seen.append(line),
                                                   done.set() if expected in line
                                                   else None),
                               stop=lambda: done.is_set() or time.time() > stop)

        follower = threading.Thread(target=follow, daemon=True)
        follower.start()
        time.sleep(0.4)                     # let the subscription land
        reply = client.call({"cmd": "send", "text": command})
        self.assertTrue(reply.get("ok"), reply)
        done.wait(seconds)
        return any(expected in line for line in seen), seen

    # ----------------------------------------------------------------- tests

    def test_the_new_guardian_takes_the_console_and_the_pid_never_changes(self):
        first = self.start_guardian()
        one = self.client()
        self.assertTrue(wait_for(lambda: one.call({"cmd": "ping"}).get("ok")))
        before = self.wait_ready(one)
        self.assertTrue(before["owned"], "the first guardian should own it")

        heard, _ = self.console_says(one, "before", "Unknown or incomplete command: before")
        self.assertTrue(heard, "the console did not work before the handover")

        second = self.start_guardian(take_over=True)
        # Wait for the OLD one to leave before looking at anything. Asking the
        # control socket earlier answers from whichever guardian still holds
        # it, so a test that skips this can pass without a handover happening.
        self.assertTrue(wait_for(lambda: first.poll() is not None, seconds=20),
                        "the guardian that handed over never left")
        self.assertIsNone(second.poll(), "the new guardian died instead")
        two = self.client()
        self.assertTrue(wait_for(lambda: self.server(two).get("pid")),
                        "the second guardian never answered")

        # The whole point, in one assertion.
        after = self.server(two)
        self.assertEqual(after["pid"], before["pid"],
                         "the world was restarted, which is what this avoids")
        self.assertTrue(after["owned"],
                        "it inherited the process but not the console")
        self.assertEqual(after["state"], "running",
                         "the state came over wrong")

        heard, seen = self.console_says(two, "after", "Unknown or incomplete command: after")
        self.assertTrue(heard, "the new guardian cannot talk to the server: %s" % seen[-5:])

    def test_after_a_handover_stop_still_stops_it_properly(self):
        """A console you cannot stop with is not really a console."""
        first = self.start_guardian()
        one = self.client()
        self.assertTrue(wait_for(lambda: one.call({"cmd": "ping"}).get("ok")))
        pid = self.wait_ready(one)["pid"]

        self.start_guardian(take_over=True)
        self.assertTrue(wait_for(lambda: first.poll() is not None, seconds=20),
                        "the handover never finished")
        two = self.client()
        self.assertTrue(wait_for(lambda: self.server(two).get("pid") == pid))

        reply = two.call({"cmd": "stop"}, timeout=30)
        self.assertTrue(reply.get("ok"), reply)
        self.assertTrue(wait_for(lambda: not runtime.alive(pid), seconds=20),
                        "the inherited console could not stop the server")
        # And it went down as asked, not as a crash.
        self.assertTrue(wait_for(
            lambda: self.server(two).get("state") == "stopped", seconds=15),
            "a clean stop was recorded as something else")

    def test_taking_over_from_nobody_says_so_and_starts_normally(self):
        proc = self.start_guardian(take_over=True)
        client = self.client()
        self.assertTrue(wait_for(lambda: client.call({"cmd": "ping"}).get("ok")),
                        "an upgrade that finds nobody must still boot")
        status = self.wait_ready(client)
        self.assertTrue(status["owned"])
        self.assertIsNone(proc.poll(), "it should be running, not gone")

    def test_the_journal_says_a_handover_happened(self):
        """Two guardians and one world is exactly what you want written down."""
        first = self.start_guardian()
        one = self.client()
        self.assertTrue(wait_for(lambda: one.call({"cmd": "ping"}).get("ok")))
        self.wait_ready(one)
        self.start_guardian(take_over=True)

        # Wait for the LINE, not for the old guardian to die. Both write to
        # this journal, and the one that hands over is gone before the one
        # that takes over has finished writing its half.
        def journal():
            return (self.tmp / "w4ve" / "run" / "journal.log").read_text()

        self.assertTrue(wait_for(lambda: "without restarting it" in journal(),
                                 seconds=20),
                        "the handover never finished: %s" % journal())
        self.assertIn("handed server over", journal(),
                      "only one side of the handover was written down")


if __name__ == "__main__":
    unittest.main()
