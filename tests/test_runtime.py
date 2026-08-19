"""Tests for RFC-0001, run against processes that really exist.

Nothing here is mocked. The guardian is a separate process, the server is a
separate process, and the interesting cases (kill the guardian, ignore stop,
crash three times) are done for real, because those are the ones that decide
whether a runtime can be trusted with a world.
"""

import json
import os
import signal
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

FAKE = str(HERE / "fakeserver.py")
GUARD = str(HERE / "guard.py")


def wait_for(check, timeout=15, every=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(every)
    return None


class GuardianCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w4ve-test-")
        self.root = Path(self.tmp)
        self.guardians = []

    def tearDown(self):
        for proc in self.guardians:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        client = runtime.Client(self.root)
        state = client.offline_status() or {}
        for entry in state.get("processes", []):
            if entry.get("pid") and runtime.alive(entry["pid"]):
                try:
                    os.kill(entry["pid"], signal.SIGKILL)
                except OSError:
                    pass

    # ------------------------------------------------------------- utilities

    def guardian(self, *server_args, services=()):
        argv = [sys.executable, GUARD, str(self.root)]
        for name, command in services:
            argv += ["--service", name] + list(command) + ["--"]
        argv += ["--", sys.executable, FAKE] + list(server_args)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True)
        self.guardians.append(proc)
        client = runtime.Client(self.root)
        self.assertTrue(wait_for(lambda: client.call({"cmd": "ping"}).get("ok")),
                        "the guardian never answered its socket")
        return proc, client

    def server_status(self, client):
        reply = client.call({"cmd": "status"})
        if not reply.get("ok"):
            return {}
        for entry in reply["processes"]:
            if entry["name"] == "server":
                return entry
        return {}

    # ----------------------------------------------------------------- tests

    def test_start_reaches_running_and_ready(self):
        _, client = self.guardian("--boot", "0.4")
        status = wait_for(lambda: self.server_status(client).get("state") == "running"
                          and self.server_status(client))
        self.assertTrue(status, "the server never reached running")
        self.assertTrue(status["ready"])
        self.assertTrue(status["owned"])
        self.assertEqual(status["capabilities"]["flavor"], "fabric")
        self.assertTrue(runtime.alive(status["pid"]))

    def test_starting_is_not_running(self):
        """Readiness is not liveness: a booting server is alive and not ready."""
        _, client = self.guardian("--boot", "4")
        status = wait_for(lambda: self.server_status(client).get("pid")
                          and self.server_status(client))
        self.assertEqual(status["state"], "starting")
        self.assertIsNone(status["ready"])
        self.assertTrue(runtime.alive(status["pid"]))

    def test_clean_stop(self):
        _, client = self.guardian("--boot", "0.3")
        wait_for(lambda: self.server_status(client).get("state") == "running")
        pid = self.server_status(client)["pid"]
        reply = client.call({"cmd": "stop"}, timeout=20)
        self.assertTrue(reply.get("ok"), reply)
        self.assertEqual(self.server_status(client)["state"], "stopped")
        self.assertFalse(runtime.alive(pid))

    def test_stop_escalates_when_ignored(self):
        _, client = self.guardian("--boot", "0.3", "--ignore-stop", "--ignore-term")
        wait_for(lambda: self.server_status(client).get("state") == "running")
        pid = self.server_status(client)["pid"]
        reply = client.call({"cmd": "stop", "grace": 2}, timeout=40)
        self.assertTrue(reply.get("ok"), reply)
        self.assertFalse(runtime.alive(pid), "it should have been killed")
        self.assertEqual(self.server_status(client)["state"], "killed")
        journal = (self.root / "w4ve" / "run" / "journal.log").read_text()
        self.assertIn("SIGTERM", journal)
        self.assertIn("SIGKILL", journal)

    def test_killing_the_guardian_leaves_the_server_alive(self):
        proc, client = self.guardian("--boot", "0.3")
        wait_for(lambda: self.server_status(client).get("state") == "running")
        pid = self.server_status(client)["pid"]

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        time.sleep(0.5)
        self.assertTrue(runtime.alive(pid),
                        "killing the guardian must never kill the server")

        offline = runtime.Client(self.root).offline_status()
        server = [p for p in offline["processes"] if p["name"] == "server"][0]
        self.assertEqual(server["state"], "running")
        self.assertIsNone(server["ready"], "without a guardian readiness is unknown")

    def test_a_new_guardian_readopts_what_it_finds(self):
        proc, client = self.guardian("--boot", "0.3")
        wait_for(lambda: self.server_status(client).get("state") == "running")
        pid = self.server_status(client)["pid"]
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        # Second guardian, same directory, no server command: it should find
        # the orphan instead of starting a second one.
        argv = [sys.executable, GUARD, str(self.root)]
        second = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  universal_newlines=True)
        self.guardians.append(second)
        client = runtime.Client(self.root)
        self.assertTrue(wait_for(lambda: client.call({"cmd": "ping"}).get("ok")))

        status = wait_for(lambda: self.server_status(client).get("pid") and
                          self.server_status(client))
        self.assertEqual(status["pid"], pid, "it adopted the wrong process")
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["owned"], "an adopted process is not owned")
        sent = client.call({"cmd": "send", "text": "list"})
        self.assertFalse(sent["ok"], "an adopted process has no console")

    def test_stale_state_is_not_believed(self):
        """A pid from a previous life must not be mistaken for a live server."""
        run_dir = self.root / "w4ve" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({
            "schema": 1,
            "processes": {"server": {"pid": 999999, "starttime": 12345,
                                     "state": "running", "command": ["true"],
                                     "cwd": str(self.root)}},
        }))
        offline = runtime.Client(self.root).offline_status()
        server = offline["processes"][0]
        self.assertEqual(server["state"], "stopped")
        self.assertTrue(server["stale"])
        self.assertIsNone(server["pid"], "a dead pid must not be reported")

    def test_crash_loop_gives_up(self):
        guardian = runtime.Guardian(self.root)
        server = guardian.add_server(
            [sys.executable, FAKE, "--boot", "0.1", "--crash-after", "0.2"],
            str(self.root), flavor="fabric", restart="on-failure")
        got_it, _ = guardian.claim()
        self.assertTrue(got_it)
        try:
            server.start()
            self.assertTrue(wait_for(lambda: server.give_up, timeout=30),
                            "it kept restarting a server that keeps dying")
            self.assertEqual(server.state, "crashed")
            self.assertGreaterEqual(len(server.deaths), runtime.CRASH_LIMIT)
        finally:
            guardian.shutdown()
            guardian.release()

    def test_one_guardian_per_server(self):
        first = runtime.Guardian(self.root)
        got_it, _ = first.claim()
        self.assertTrue(got_it)
        second = runtime.Guardian(self.root)
        got_it, holder = second.claim()
        self.assertFalse(got_it, "two guardians took the same server")
        self.assertEqual(holder, str(os.getpid()))
        first.release()

    def test_services_are_watched_like_the_server(self):
        script = self.root / "service.py"
        script.write_text(
            "import time, sys\n"
            "print('service up', flush=True)\n"
            "time.sleep(30)\n")
        _, client = self.guardian(
            "--boot", "0.3",
            services=[("media", [sys.executable, str(script)])])
        # A declared service comes up with the guardian: nobody should have to
        # remember the second half of a piece.
        status = wait_for(lambda: [p for p in client.call({"cmd": "status"})["processes"]
                                   if p["name"] == "media" and p["pid"]])
        self.assertTrue(status, "the service was never started")
        self.assertTrue(runtime.alive(status[0]["pid"]))
        self.assertEqual(status[0]["state"], "running")
        self.assertIsNone(status[0]["ready"],
                          "with no health check, readiness is unknown")


class WatchCase(unittest.TestCase):
    """A piece can ask for a restart, and stop needing a plugin to get one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-watch-"))
        self.guardian = runtime.Guardian(self.tmp)
        got_it, _ = self.guardian.claim()
        self.assertTrue(got_it)

    def tearDown(self):
        self.guardian.shutdown()
        self.guardian.release()

    def test_the_server_comes_back_when_the_piece_asks(self):
        watches = [{"name": "regioncast-restart",
                    "on": r"\[RegionCast\] RESTART_REQUESTED",
                    "do": "restart",
                    "why": "it stops the server to copy region files"}]
        server = self.guardian.add_server(
            [sys.executable, FAKE, "--boot", "0.2", "--ask-restart-after", "1"],
            str(self.tmp), flavor="fabric", watches=watches)
        server.start()
        first = wait_for(lambda: server.pid if server.state == "running" else None)
        self.assertTrue(first)

        second = wait_for(lambda: (server.pid if server.pid != first
                                   and server.state == "running" else None),
                          timeout=30)
        self.assertTrue(second, "it never came back up")
        self.assertNotEqual(second, first)
        journal = (self.tmp / "w4ve" / "run" / "journal.log").read_text()
        self.assertIn("matched 'regioncast-restart'", journal)
        self.assertIn("asked it to come back", journal)
        # It was not a crash, so nothing counts against the crash budget.
        self.assertEqual(server.deaths, [])
        self.assertFalse(server.give_up)
        server.stop()


class SocketPathCase(unittest.TestCase):
    """A server folder nested deep still gets a control socket.

    Unix socket paths live in a fixed size field, about 108 bytes, and a real
    server under a container volume path plus a temp prefix goes over it. This
    used to blow up in a thread and leave the guardian running with no way to
    talk to it, which looks exactly like everything being fine.
    """

    def test_a_long_path_moves_the_socket_somewhere_short(self):
        deep = Path(tempfile.mkdtemp(prefix="w4ve-long-"))
        deep = deep / ("nested/" * 12)
        run_dir = deep / "w4ve" / "run"
        run_dir.mkdir(parents=True)
        chosen = runtime.control_socket_for(run_dir)
        self.assertNotEqual(chosen.parent, run_dir)
        self.assertLessEqual(len(str(chosen)), runtime.SOCK_MAX)
        self.assertEqual(runtime.control_socket_of(deep), chosen,
                         "the client has to find the socket the guardian used")

    def test_a_normal_path_keeps_the_socket_next_to_everything_else(self):
        root = Path(tempfile.mkdtemp(prefix="w4ve-short-"))
        run_dir = root / "w4ve" / "run"
        run_dir.mkdir(parents=True)
        self.assertEqual(runtime.control_socket_for(run_dir),
                         run_dir / "control.sock")
        self.assertEqual(runtime.control_socket_of(root),
                         run_dir / "control.sock")


class HandlerCase(unittest.TestCase):

    def test_ready_lines_of_both_flavors(self):
        fabric = runtime.handler_for("fabric")
        paper = runtime.handler_for("paper")
        vanilla_line = ('[13:24:11] [Server thread/INFO]: Done (12.345s)! '
                        'For help, type "help"')
        paper_line = '[13:24:11 INFO]: Done (12.345s)! For help, type "help"'
        self.assertTrue(fabric.ready(vanilla_line))
        self.assertTrue(paper.ready(paper_line))
        self.assertFalse(fabric.ready("[13:24:11] [Server thread/INFO]: Done loading"))

    def test_stopping_lines(self):
        handler = runtime.handler_for("fabric")
        self.assertTrue(handler.stopping("[13:24:11] [Server thread/INFO]: Stopping server"))
        self.assertTrue(handler.stopping("[13:24:11 INFO]: Stopping the server"))
        self.assertFalse(handler.stopping("Stopping a boat"))

    def test_paper_says_what_it_cannot_do(self):
        caps = runtime.handler_for("paper").capabilities()
        self.assertTrue(caps["managed"], "Paper is managed, not just observed")
        self.assertEqual(caps["packages"], "none")
        self.assertFalse(caps["companion"])
        self.assertTrue(caps["rcon"])

    def test_flavor_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mods").mkdir()
            self.assertEqual(runtime.detect_flavor(root), "fabric")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plugins").mkdir()
            (root / "paper-1.20.1-196.jar").write_text("")
            self.assertEqual(runtime.detect_flavor(root), "paper")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runtime.detect_flavor(tmp), "vanilla")


class IdentityCase(unittest.TestCase):

    def test_pid_alone_is_not_an_identity(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            starttime = runtime.starttime_of(proc.pid)
            self.assertIsNotNone(starttime)
            self.assertTrue(runtime.alive(proc.pid, starttime))
            self.assertFalse(runtime.alive(proc.pid, starttime + 1),
                             "a recycled pid must not pass as the same process")
        finally:
            proc.kill()
            proc.wait()

    def test_dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(runtime.alive(proc.pid, 12345))


if __name__ == "__main__":
    unittest.main(verbosity=2)
