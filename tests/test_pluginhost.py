"""Tests for the plugin host attached to a guardian.

The unit of value here is not "a plugin loaded". It is that the guardian
stays a guardian while plugins run in it:

  * a slow plugin must not delay the console reader, because that reader is
    how the guardian learns the server is ready and how it notices a crash;
  * a flood of console lines must not grow a queue forever;
  * `!!something` typed at the guardian must be answered by the plugins and
    never forwarded to Minecraft, where it only produces "Unknown command".

Most of these run against a guardian with no process, which is enough for
everything except the last test, and keeps the suite from spending a minute
starting fake servers.
"""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402
from pluginhost import PluginRuntime  # noqa: E402

TALKER = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'talker', 'version': '1.0.0'}

heard = []
events = []

def on_load(server, prev):
    server.logger.info('loaded')
    server.register_command(Literal('!!say-hi').runs(lambda s: s.reply('hi')))

def on_info(server, info):
    heard.append(info.content)

def on_player_joined(server, player, info):
    events.append('joined:' + player)

def on_server_startup(server):
    events.append('startup')

def on_server_stop(server):
    events.append('stop')

def on_unload(server):
    events.append('unloaded')
'''

SLOW = '''
import time
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'slow', 'version': '1.0.0'}

def on_info(server, info):
    if info.content == 'slow':
        time.sleep(1.5)
'''


class PluginRuntimeTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "plugins").mkdir()
        self.guardian = runtime.Guardian(self.root)
        self.runtimes = []

    def tearDown(self):
        for plugins in self.runtimes:
            plugins.stop()

    def plugin(self, name, text):
        (self.root / "plugins" / name).write_text(text)

    def start(self):
        plugins = PluginRuntime(self.guardian)
        ok, message = plugins.start()
        self.assertTrue(ok, message)
        self.runtimes.append(plugins)
        self.settle(plugins)
        return plugins

    def settle(self, plugins, timeout=5):
        """Wait for the plugin thread to run everything queued so far."""
        done = threading.Event()
        plugins.submit(done.set)
        self.assertTrue(done.wait(timeout), "the plugin thread is stuck")

    def module(self, plugins, plugin_id):
        return plugins.host.plugin_instance(plugin_id)

    # ------------------------------------------------------------------ basics

    def test_it_loads_the_plugins_of_the_server_it_watches(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        self.assertEqual(["talker"], plugins.status()["loaded"])

    def test_a_server_with_no_plugins_folder_is_not_an_error(self):
        (self.root / "plugins").rmdir()
        ok, message = PluginRuntime(self.guardian).start()
        self.assertFalse(ok)
        self.assertIn("no plugins folder", message)

    def test_console_lines_become_events(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        plugins.feed("server", "[12:00:00] [Server thread/INFO]: hello world")
        plugins.feed("server", "[12:00:00] [Server thread/INFO]: TVTvirus joined the game")
        self.settle(plugins)
        module = self.module(plugins, "talker")
        self.assertIn("hello world", module.heard)
        self.assertIn("joined:TVTvirus", module.events)

    def test_lines_from_another_process_are_not_the_servers(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        plugins.feed("mw-upload", "[12:00:00] [Server thread/INFO]: hello")
        self.settle(plugins)
        self.assertEqual([], self.module(plugins, "talker").heard)

    def test_state_changes_become_lifecycle_events(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        plugins.state_changed("server", "starting", None)
        plugins.state_changed("server", "running", True)
        plugins.state_changed("server", "stopped", False)
        self.settle(plugins)
        events = self.module(plugins, "talker").events
        self.assertIn("startup", events)
        self.assertIn("stop", events)

    def test_the_same_state_twice_is_one_event(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        for _ in range(3):
            plugins.state_changed("server", "running", True)
        self.settle(plugins)
        events = self.module(plugins, "talker").events
        self.assertEqual(1, events.count("startup"))

    def test_stopping_gives_the_plugins_their_on_unload(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        module = self.module(plugins, "talker")
        plugins.stop()
        self.assertIn("unloaded", module.events)

    # ------------------------------------------------------------ typed lines

    def test_a_plugin_command_is_answered_not_forwarded(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        claimed, lines = plugins.console("!!say-hi")
        self.assertTrue(claimed)
        self.assertIn("hi", lines)

    def test_a_minecraft_command_is_not_ours(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        claimed, lines = plugins.console("list")
        self.assertFalse(claimed, "a plain command must go to the server")
        self.assertEqual([], lines)

    def test_an_unknown_plugin_command_is_not_claimed(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        claimed, _ = plugins.console("!!nothing-registers-this")
        self.assertFalse(claimed)

    def test_the_builtin_tree_answers_through_the_guardian(self):
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        claimed, lines = plugins.console("!!w4ve plugin list")
        self.assertTrue(claimed)
        self.assertTrue(any("talker" in line for line in lines), lines)

    # ------------------------------------------------- not blocking the guardian

    def test_a_slow_plugin_does_not_block_the_feeder(self):
        self.plugin("slow.py", SLOW)
        plugins = self.start()
        started = time.time()
        for _ in range(20):
            plugins.feed("server", "[12:00:00] [Server thread/INFO]: slow")
        # Feeding is a queue put. If this took a second, the console reader
        # would have been held up by a plugin, which is the bug this exists to
        # prevent.
        self.assertLess(time.time() - started, 0.5)

    def test_a_flood_is_dropped_instead_of_queued_forever(self):
        self.plugin("slow.py", SLOW)
        plugins = self.start()
        plugins.feed("server", "[12:00:00] [Server thread/INFO]: slow")
        for _ in range(3000):
            plugins.feed("server", "[12:00:00] [Server thread/INFO]: x")
        self.assertGreater(plugins.dropped, 0)
        self.assertLessEqual(plugins.queue.qsize(), 2000)

    def test_a_plugin_that_raises_does_not_kill_the_thread(self):
        self.plugin("boom.py", "from mcdreforged.api.all import *\n"
                               "PLUGIN_METADATA = {'id': 'boom'}\n"
                               "def on_info(server, info):\n"
                               "    raise RuntimeError('boom')\n")
        self.plugin("talker.py", TALKER)
        plugins = self.start()
        plugins.feed("server", "[12:00:00] [Server thread/INFO]: hello")
        self.settle(plugins)
        self.assertIn("hello", self.module(plugins, "talker").heard)
        self.assertTrue(any("boom" in line for line in plugins.host.problems))


class GuardianWithPluginsTest(unittest.TestCase):
    """One end to end run: a real guardian, a real process, real plugins."""

    def test_a_server_started_by_the_guardian_has_its_plugins(self):
        root = Path(tempfile.mkdtemp())
        (root / "plugins").mkdir()
        (root / "plugins" / "talker.py").write_text(TALKER)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "guard.py"), str(root), "--",
             sys.executable, str(ROOT / "tests" / "fakeserver.py"), "--boot", "0.5"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            client = runtime.Client(root)
            deadline = time.time() + 15
            while time.time() < deadline and not client.connected():
                time.sleep(0.2)
            self.assertTrue(client.connected(), "the guardian never came up")

            status = {}
            while time.time() < deadline:
                status = client.call({"cmd": "status"})
                if status.get("plugins", {}).get("loaded"):
                    break
                time.sleep(0.2)
            self.assertEqual(["talker"], status.get("plugins", {}).get("loaded"))

            reply = client.call({"cmd": "send", "text": "!!say-hi"})
            self.assertTrue(reply.get("ok"))
            self.assertIn("hi", reply.get("lines") or [])

            # And the journal has what the plugin said, so it is findable
            # tomorrow by somebody who was not attached today.
            client.call({"cmd": "shutdown"})
            time.sleep(1.5)
            journal = (root / "w4ve" / "run" / "journal.log").read_text()
            self.assertIn("[talker] [INFO] loaded", journal)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
