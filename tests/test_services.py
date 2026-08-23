"""Services that depend on each other, with real processes and a real port.

Both cases here come from ChatBridge, which is three processes that have to
find each other. The daemon binds a TCP port and speaks its own protocol, so
it can never answer a health URL; the !!online client has nothing to talk to
until that port is open.

Nothing is mocked: the fixtures really do bind a port, and really do take a
moment to do it, because the moment is the whole problem.
"""

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402

# Binds a port after a delay, the way a daemon does: the process exists long
# before it is any use to anybody.
SLOW_LISTENER = '''
import socket, sys, time
time.sleep(float(sys.argv[1]))
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[2])))
s.listen(5)
print("listening", flush=True)
time.sleep(300)
'''

QUIET = '''
import time
print("up", flush=True)
time.sleep(300)
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceDependencyCase(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="w4ve-services-"))
        self.guardian = runtime.Guardian(self.root)
        self.started = []

    def tearDown(self):
        self.guardian.stopping.set()
        for proc in list(self.guardian.watched.values()):
            if proc.pid and runtime.alive(proc.pid, proc.starttime):
                proc.stop(grace=1)

    def script(self, name, source):
        path = self.root / name
        path.write_text(source)
        return str(path)

    # ------------------------------------------------------------- health

    def test_a_service_can_be_asked_on_a_tcp_port(self):
        """A daemon with no health URL is not a daemon with no readiness.

        ChatBridge's speaks its own protocol on a port. Asked for HTTP, its
        readiness stayed unknown forever, and a service whose readiness never
        arrives is one nobody can wait for.
        """
        port = free_port()
        service = self.guardian.add_service(
            "daemon", [sys.executable, self.script("slow.py", SLOW_LISTENER),
                       "0.5", str(port)],
            self.root, health="tcp://127.0.0.1:%d" % port)
        service.start()
        deadline = time.time() + 15
        while time.time() < deadline and not service.ready:
            time.sleep(0.1)
        self.assertTrue(service.ready, "the port was open and nobody noticed")

    def test_an_open_port_is_asked_once_and_then_left_alone(self):
        """⚠️ Every check is a connection somebody else has to explain.

        ChatBridge's daemon logged `Failed reading client's login packet` for
        each one, so a check every thirty seconds was two thousand nine
        hundred false errors a day in its log. An open port answers "it
        started", not "it is well", and that answer does not change by itself.
        """
        port = free_port()
        service = self.guardian.add_service(
            "daemon", [sys.executable, self.script("slow.py", SLOW_LISTENER),
                       "0.2", str(port)],
            self.root, health="tcp://127.0.0.1:%d" % port, health_every=1)
        knocks = []
        real = service._check_health

        def counted():
            answer = real()
            knocks.append(answer)
            return answer

        service._check_health = counted
        service.start()
        deadline = time.time() + 15
        while time.time() < deadline and not service.ready:
            time.sleep(0.1)
        self.assertTrue(service.ready)
        after_ready = len(knocks)
        time.sleep(3)
        self.assertEqual(after_ready, len(knocks),
                         "it kept knocking on a door that already answered")

    def test_a_port_nobody_opened_is_not_ready(self):
        port = free_port()
        service = self.guardian.add_service(
            "quiet", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, health="tcp://127.0.0.1:%d" % port)
        service.start()
        time.sleep(1)
        self.assertFalse(service.ready)

    # -------------------------------------------------------------- after

    def test_a_service_waits_for_the_one_it_comes_after(self):
        """⚠️ Order was never the problem: `start()` returns immediately.

        The !!online client was declared after the daemon and still came up
        first, failed to connect three times, and reconnected on its own ten
        seconds later. It healed itself and filled the console with errors on
        every single boot.
        """
        port = free_port()
        daemon = self.guardian.add_service(
            "daemon", [sys.executable, self.script("slow.py", SLOW_LISTENER),
                       "1", str(port)],
            self.root, health="tcp://127.0.0.1:%d" % port)
        client = self.guardian.add_service(
            "client", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, after=["daemon"])
        daemon.start()
        started = time.time()
        self.guardian.wait_for_dependencies(client)
        waited = time.time() - started
        client.start()
        self.assertTrue(daemon.ready, "it started before the daemon was up")
        self.assertGreater(waited, 0.9, "it did not wait at all")
        self.assertLess(waited, 20)

    def test_it_does_not_wait_forever_for_a_readiness_that_never_comes(self):
        """A boot that hangs is worse than a service that starts too early."""
        port = free_port()
        never = self.guardian.add_service(
            "never", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, health="tcp://127.0.0.1:%d" % port)
        client = self.guardian.add_service(
            "client", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, after=["never"])
        never.start()
        started = time.time()
        self.guardian.wait_for_dependencies(client, timeout=2)
        self.assertLess(time.time() - started, 10, "the boot hung")
        self.assertFalse(never.ready)

    def test_waiting_for_something_nobody_declared_is_said_out_loud(self):
        said = []
        self.guardian.log = said.append
        client = self.guardian.add_service(
            "client", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, after=["a-typo"])
        self.guardian.wait_for_dependencies(client, timeout=1)
        self.assertTrue(any("a-typo" in line for line in said), said)

    def test_a_service_with_no_health_check_only_has_to_be_alive(self):
        """Waiting for readiness that cannot arrive is worse than not waiting."""
        first = self.guardian.add_service(
            "first", [sys.executable, self.script("quiet.py", QUIET)],
            self.root)
        client = self.guardian.add_service(
            "client", [sys.executable, self.script("quiet.py", QUIET)],
            self.root, after=["first"])
        first.start()
        started = time.time()
        self.guardian.wait_for_dependencies(client, timeout=5)
        self.assertLess(time.time() - started, 3,
                        "it waited for a readiness that was never coming")

    # --------------------------------------------------------- declaration

    def test_after_is_read_from_the_toml_both_ways(self):
        services = runtime.services_from({"services": {
            "one": {"command": "x", "after": "daemon"},
            "two": {"command": "y", "after": ["daemon", "other"]},
            "three": {"command": "z"},
        }})
        by_name = {s["name"]: s for s in services}
        self.assertEqual(["daemon"], by_name["one"]["after"])
        self.assertEqual(["daemon", "other"], by_name["two"]["after"])
        self.assertEqual([], by_name["three"]["after"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
