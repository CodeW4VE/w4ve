"""The promise M3 makes, tested against plugins written to break it.

The criterion is one sentence: **plugins that are slow, that crash, and that
flood must not block the supervisor.** That is not something you can check by
reading code, and it is not something a well-behaved fixture can prove. So
`tests/badplugins/` holds five that misbehave on purpose, each one aimed at a
different way a plugin platform gets somebody's server stuck:

- `slow` answers nothing, ever;
- `crasher` exits mid-sentence, without an exception;
- `silent` starts and never speaks;
- `flooded` reads far slower than events arrive;
- `liar` asks for a protocol nobody speaks.

Everything below uses real processes. A mock cannot fail to answer.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import worker  # noqa: E402

BAD = HERE / "badplugins"
EXAMPLE = ROOT / "sdk" / "python" / "example"

# The real timeouts are minutes of waiting when a suite deliberately starts
# things that never answer. Shortened here; the behaviour under test is that
# the deadline is enforced at all, not what number it happens to be.
worker.HANDSHAKE_TIMEOUT = 8
worker.CALL_TIMEOUT = 5


class FakeGuardian:
    """Enough guardian to be talked to, and to remember what was said."""

    def __init__(self):
        self.lines = []
        self.sent = []
        self.watched = {}

    def log(self, text):
        self.lines.append(text)


class ManifestCase(unittest.TestCase):

    def test_a_real_manifest_reads(self):
        manifest = worker.read_manifest(EXAMPLE)
        self.assertEqual(manifest["id"], "greeter")
        self.assertEqual(manifest["command"][0], "python3")
        self.assertEqual(manifest["config"]["greeting"], "welcome back")

    def test_a_folder_with_no_manifest_says_so(self):
        empty = Path(tempfile.mkdtemp(prefix="w4ve-noman-"))
        with self.assertRaises(worker.WorkerError) as caught:
            worker.read_manifest(empty)
        self.assertIn("w4ve.plugin.toml", str(caught.exception))

    def test_a_plugin_from_the_future_is_refused_before_it_starts(self):
        # Refused at the manifest, not halfway through a conversation.
        with self.assertRaises(worker.WorkerError) as caught:
            worker.read_manifest(BAD / "liar")
        self.assertIn("protocol", str(caught.exception))

    def test_discover_reports_the_broken_ones_rather_than_stopping(self):
        found, problems = worker.discover(BAD)
        ids = {m["id"] for m in found}
        self.assertIn("slow", ids)
        self.assertIn("crasher", ids)
        self.assertTrue(any("liar" in p for p in problems), problems)


class HostCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-worker-"))
        self.guardian = FakeGuardian()
        self.host = worker.WorkerHost(self.tmp, guardian=self.guardian)
        self.started = []

    def tearDown(self):
        self.host.stop_all()

    def start(self, name):
        manifest = worker.read_manifest(BAD / name if (BAD / name).exists()
                                        else EXAMPLE)
        one = worker.Worker(manifest, self.tmp, host=self.host)
        self.host.workers[one.id] = one
        return one


class SlowCase(HostCase):
    """A plugin that never answers must produce an error, not a hang."""

    def test_a_command_that_is_never_answered_times_out(self):
        one = self.start("slow")
        ok, message = one.start()
        self.assertTrue(ok, message)
        began = time.time()
        answer = one.invoke("!!slow", [], timeout=2)
        took = time.time() - began
        self.assertFalse(answer["ok"], answer)
        self.assertIn("did not answer", answer["error"])
        # The number matters as much as the words: a deadline that is not
        # enforced looks exactly like one that is, until the day it does not.
        self.assertLess(took, 5, "the deadline was not enforced")

    def test_the_rest_of_the_world_keeps_working_meanwhile(self):
        slow = self.start("slow")
        slow.start()
        good = self.start("greeter")
        good.start()
        import threading

        def wait_forever():
            slow.invoke("!!slow", [], timeout=8)

        stuck = threading.Thread(target=wait_forever, daemon=True)
        stuck.start()
        time.sleep(0.5)
        began = time.time()
        answer = good.invoke("!!seen", [], timeout=5)
        self.assertTrue(answer["ok"], answer)
        self.assertLess(time.time() - began, 3,
                        "one plugin waiting held up another")


class CrashCase(HostCase):
    """A plugin that dies takes down its own process and nothing else."""

    def test_a_crash_is_noticed_and_does_not_spread(self):
        crasher = self.start("crasher")
        crasher.start()
        good = self.start("greeter")
        good.start()
        crasher.deliver("console", {"line": "anything"})
        time.sleep(1.5)
        # It is expected to be running again: dying once gets it restarted,
        # which is the point. What matters is that the death was seen.
        self.assertGreaterEqual(len(crasher.deaths), 1, "the crash went unnoticed")
        self.assertTrue(any("died" in line for line in self.guardian.lines),
                        self.guardian.lines)
        self.assertTrue(good.alive(), "one plugin's crash killed another")
        answer = good.invoke("!!seen", [], timeout=5)
        self.assertTrue(answer["ok"], answer)

    def test_a_caller_waiting_on_a_dying_plugin_is_answered(self):
        # The worst version of a hang: waiting for something that no longer
        # exists. It has to come back as an error, not never.
        crasher = self.start("crasher")
        crasher.start()
        crasher.deliver("console", {"line": "die"})
        time.sleep(1.2)
        answer = crasher.invoke("!!whatever", [], timeout=3)
        self.assertFalse(answer["ok"], answer)

    def test_something_that_dies_over_and_over_is_left_alone(self):
        crasher = self.start("crasher")
        crasher.start()
        for _ in range(worker.CRASH_LIMIT + 1):
            crasher.deliver("console", {"line": "die"})
            time.sleep(1.0)
        self.assertGreaterEqual(len(crasher.deaths), worker.CRASH_LIMIT)
        self.assertTrue(
            any("leaving it alone" in line for line in self.guardian.lines),
            self.guardian.lines)


class SilentCase(HostCase):
    """Running is not the same as alive, and the difference has to be said."""

    def test_a_plugin_that_never_says_hello_is_not_started(self):
        worker.HANDSHAKE_TIMEOUT, saved = 2, worker.HANDSHAKE_TIMEOUT
        try:
            silent = self.start("silent")
            ok, message = silent.start()
            self.assertFalse(ok)
            self.assertIn("said nothing", message)
            self.assertFalse(silent.ready)
        finally:
            worker.HANDSHAKE_TIMEOUT = saved


class FloodCase(HostCase):
    """A plugin that cannot keep up loses events instead of growing memory."""

    def test_events_are_dropped_rather_than_queued_for_ever(self):
        flooded = self.start("flooded")
        flooded.start()
        # ⚠️ Enough to fill the pipe as well as the queue. The operating
        # system buffers about 64K on its own, so a few hundred small events
        # are swallowed before anything backs up: a flood test that sends too
        # little proves nothing and passes anyway.
        fat = "x" * 400
        for index in range(worker.QUEUE_LIMIT * 5):
            flooded.deliver("console", {"line": "%d %s" % (index, fat)})
        self.assertGreater(flooded.dropped, 0,
                           "the queue grew without limit")
        self.assertTrue(
            any("cannot keep up" in line for line in self.guardian.lines),
            self.guardian.lines)

    def test_flooding_never_blocks_the_caller(self):
        # `deliver` is called from the guardian's console reader, which is also
        # how it learns that Minecraft died. Blocking there is the one thing
        # this design cannot allow.
        flooded = self.start("flooded")
        flooded.start()
        fat = "x" * 400
        began = time.time()
        for index in range(worker.QUEUE_LIMIT * 5):
            flooded.deliver("console", {"line": "%d %s" % (index, fat)})
        self.assertLess(time.time() - began, 5,
                        "delivering events blocked the caller")

    def test_it_says_it_once_and_not_for_every_event(self):
        flooded = self.start("flooded")
        flooded.start()
        fat = "x" * 400
        for index in range(worker.QUEUE_LIMIT * 5):
            flooded.deliver("console", {"line": "%d %s" % (index, fat)})
        complaints = [l for l in self.guardian.lines if "cannot keep up" in l]
        self.assertEqual(len(complaints), 1, complaints)


class WorkingCase(HostCase):
    """And with a plugin that behaves, all of it actually works."""

    def test_the_reference_plugin_registers_and_answers(self):
        one = self.start("greeter")
        ok, message = one.start()
        self.assertTrue(ok, message)
        self.assertIn("!!seen", one.commands)
        self.assertIn("!!greeter", one.commands)
        self.assertTrue(one.commands["!!seen"]["help"])
        answer = one.invoke("!!seen", [], timeout=5)
        self.assertTrue(answer["ok"], answer)

    def test_config_from_the_manifest_reaches_the_plugin(self):
        one = self.start("greeter")
        one.start()
        answer = one.invoke("!!greeter", [], timeout=5)
        self.assertTrue(answer["ok"], answer)
        text = "\n".join(answer["value"])
        self.assertIn("welcome back", text)

    def test_a_plugin_can_ask_the_core_things(self):
        # `!!greeter` calls server_status, which is a round trip the other way.
        one = self.start("greeter")
        one.start()
        answer = one.invoke("!!greeter", [], timeout=5)
        self.assertIn("server:", "\n".join(answer["value"]))

    def test_an_event_gets_through_and_is_acted_on(self):
        one = self.start("greeter")
        one.start()
        one.deliver("player_joined", {"player": "TVTvirus"})
        time.sleep(0.8)
        answer = one.invoke("!!seen", ["TVTvirus"], timeout=5)
        self.assertIn("1 times", str(answer["value"]))

    def test_state_survives_a_reload(self):
        one = self.start("greeter")
        one.start()
        one.deliver("player_joined", {"player": "Hugo"})
        time.sleep(0.8)
        ok, message = self.host.reload("greeter")
        self.assertTrue(ok, message)
        answer = self.host.workers["greeter"].invoke("!!seen", ["Hugo"], timeout=5)
        self.assertIn("1 times", str(answer["value"]),
                      "on_stop did not save, or on_start did not load")

    def test_the_host_routes_a_command_to_whoever_owns_it(self):
        one = self.start("greeter")
        one.start()
        claimed, lines = self.host.command("!!seen")
        self.assertTrue(claimed)
        self.assertTrue(lines)
        claimed, _ = self.host.command("!!nobody-registered-this")
        self.assertFalse(claimed, "it claimed a command nobody owns")

    def test_help_gathers_every_plugin(self):
        one = self.start("greeter")
        one.start()
        text = "\n".join(self.host.help_lines())
        self.assertIn("!!seen", text)
        self.assertIn("how many times", text)


class ProtocolCase(unittest.TestCase):
    """The wire itself, spoken by hand."""

    def test_a_plugin_says_hello_before_anything_else(self):
        proc = subprocess.Popen([sys.executable, "main.py"], cwd=str(EXAMPLE),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            first = json.loads(proc.stdout.readline())
            self.assertEqual(first["type"], "hello")
            self.assertEqual(first["protocol"], worker.PROTOCOL)
        finally:
            proc.kill()

    def test_the_conformance_kit_passes_against_the_reference_plugin(self):
        # If this ever fails, either the SDK broke or the kit did, and both
        # are worth stopping for.
        result = subprocess.run(
            [sys.executable, str(ROOT / "sdk" / "conformance.py"), str(EXAMPLE)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()


class FromAPackageCase(unittest.TestCase):
    """A native plugin that arrives inside a `.w4ve`, which is the whole point.

    Until the platform existed, a `plugin` component was refused on purpose:
    accepting one would have meant installing code nothing could run. Now it
    runs, so it installs, and what has to be right is that each plugin lands in
    a folder of its own. All of them piling into `w4ve/plugins/` would leave
    nothing for the loader to find.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-pkgplug-"))
        self.root = self.tmp / "server-root"
        (self.root / "server").mkdir(parents=True)
        (self.root / "w4ve").mkdir()
        (self.root / "w4ve.toml").write_text(
            '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n'
            '[pieces]\n')
        sys.path.insert(0, str(ROOT))
        import package
        import w4ve as cli
        self.package = package
        self.cli = cli
        self.server = cli.Server(self.root)

    def build(self):
        source = self.tmp / "src"
        (source / "plugin" / "greeter").mkdir(parents=True)
        for name in ("main.py", "w4ve.plugin.toml"):
            shutil.copy(EXAMPLE / name, source / "plugin" / "greeter" / name)
        (source / "w4ve.json").write_text(json.dumps({
            "schema": 1, "id": "greeter", "version": "1.0.0", "side": "server",
            "summary": "The reference plugin, in a package.",
            "components": [
                {"kind": "plugin", "name": "greeter",
                 "file": "plugin/greeter/main.py", "reload": "hot"},
                {"kind": "plugin", "name": "greeter",
                 "file": "plugin/greeter/w4ve.plugin.toml", "reload": "hot"},
            ],
        }))
        return self.package.pack(source, self.tmp)[0]

    def install(self):
        install = self.package.Install(self.build(), self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        record = install.apply(actions)
        install.commit()
        return record

    def test_a_plugin_component_is_no_longer_refused(self):
        self.assertEqual(self.package.NOT_YET, {},
                         "nothing is refused outright any more")

    def test_each_plugin_lands_in_a_folder_of_its_own(self):
        self.install()
        folder = self.root / "w4ve" / "plugins" / "greeter"
        self.assertTrue((folder / "main.py").exists())
        self.assertTrue((folder / "w4ve.plugin.toml").exists())

    def test_and_the_loader_then_finds_it(self):
        self.install()
        found, problems = worker.discover(self.root / "w4ve" / "plugins")
        self.assertEqual([m["id"] for m in found], ["greeter"], problems)

    def test_a_plugin_with_no_name_is_refused_before_installing(self):
        # Without a name there is no folder, and every file of every plugin
        # would land in the same place.
        bad = self.package.Manifest({
            "schema": 1, "id": "x", "version": "1", "summary": "s",
            "components": [{"kind": "plugin", "file": "plugin/a/main.py"}],
        })
        self.assertTrue(any("needs a `name`" in p for p in bad.problems()),
                        bad.problems())

    def test_removing_it_takes_the_whole_folder_with_it(self):
        self.install()
        self.package.remove(self.server, "greeter")
        left = list((self.root / "w4ve" / "plugins").rglob("*.py"))
        self.assertEqual(left, [], left)
