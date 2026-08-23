"""Tests for the plugin lifecycle: unload, reload, refresh, disable.

The thing being tested is not that a module can be imported twice. It is that
a plugin takes everything it brought back out with it: its commands stop
answering, its help line disappears, its listeners stop firing and its
translations stop resolving, and none of that disturbs the plugin next to it.
That is the difference between a reload and a memory leak with extra steps.

Every plugin here is written on disk and edited on disk, because a reload that
is only tested against an object in memory tests nothing about reloading.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.host import Host, ConsoleCommandSource, PlayerCommandSource  # noqa: E402

GREETER = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'greeter', 'version': '%(version)s'}

said = []

def on_load(server, prev):
    server.register_help_message('!!greet', 'greets you', permission=%(perm)d)
    server.register_command(
        Literal('!!greet').runs(lambda src: src.reply('%(word)s')))
    server.register_translation('en_us', {'greeter.word': '%(word)s'})
    server.register_event_listener(
        'on_player_joined',
        lambda srv, player, info: said.append(player))

def on_unload(server):
    said.append('unloaded')
'''

OTHER = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'other', 'version': '1.0.0'}

def on_load(server, prev):
    server.register_help_message('!!other', 'the other one')
    server.register_command(Literal('!!other').runs(lambda src: src.reply('other')))
'''

DEPENDENT = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'follower', 'version': '1.0.0',
                   'dependencies': {'greeter': '*'}}

def on_load(server, prev):
    server.register_command(Literal('!!follow').runs(lambda src: src.reply('following')))
'''


def greeter(version="1.0.0", word="hello", perm=0):
    return GREETER % {"version": version, "word": word, "perm": perm}


class LifecycleTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.output = []
        self.host = Host(self.root, plugins_dir=self.plugins,
                         on_output=self.output.append)

    def write(self, name, text):
        path = self.plugins / name
        path.write_text(text)
        # Stamps are mtime based, and two writes inside the same clock tick
        # would look like no change at all on a coarse filesystem.
        time.sleep(0.01)
        return path

    def replies(self, line, source=None):
        """What a command line makes the host say."""
        mark = len(self.output)
        self.host.run_command(source or ConsoleCommandSource(self.host), line)
        return self.output[mark:]

    # ------------------------------------------------------------------ unload

    def test_unloading_takes_the_command_with_it(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        self.assertEqual(["hello"], self.replies("!!greet"))
        self.host.unload("greeter")
        self.assertEqual([], self.replies("!!greet"))
        self.assertEqual([], self.host.help_messages)
        self.assertEqual({}, self.host.translations)

    def test_unloading_one_leaves_the_other_alone(self):
        self.write("greeter.py", greeter())
        self.write("other.py", OTHER)
        self.host.load_all()
        self.host.unload("greeter")
        self.assertEqual(["other"], self.host.plugin_ids())
        self.assertEqual(["other"], self.replies("!!other"))
        self.assertEqual(["!!other"],
                         [item["prefix"] for item in self.host.help_messages])

    def test_unloading_stops_the_listeners(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        module = self.host.plugin_instance("greeter")
        self.host.dispatch("on_player_joined", "TVTvirus", None)
        self.assertIn("TVTvirus", module.said)
        self.host.unload("greeter")
        self.host.dispatch("on_player_joined", "Manke", None)
        self.assertNotIn("Manke", module.said)

    # ------------------------------------------------------------------ reload

    def test_reload_picks_up_the_edit(self):
        self.write("greeter.py", greeter(word="hello"))
        self.host.load_all()
        self.assertEqual(["hello"], self.replies("!!greet"))
        self.write("greeter.py", greeter(version="2.0.0", word="goodbye"))
        ok, problems = self.host.reload("greeter")
        self.assertTrue(ok, problems)
        self.assertEqual([], problems)
        self.assertEqual(["goodbye"], self.replies("!!greet"))
        self.assertEqual("2.0.0", self.host.plugin_metadata("greeter").version)

    def test_reload_does_not_register_the_command_twice(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        self.host.reload("greeter")
        self.host.reload("greeter")
        self.assertEqual(1, len(self.host.commands))
        self.assertEqual(1, len(self.host.help_messages))
        self.assertEqual(["hello"], self.replies("!!greet"))

    def test_reload_runs_on_unload_first(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        module = self.host.plugin_instance("greeter")
        self.host.reload("greeter")
        self.assertIn("unloaded", module.said)

    def test_a_plugin_that_breaks_on_reload_stays_out(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        self.write("greeter.py", "raise RuntimeError('boom')\n"
                                 "PLUGIN_METADATA = {'id': 'greeter'}\n")
        ok, problems = self.host.reload("greeter")
        self.assertFalse(ok)
        self.assertTrue(any("boom" in line for line in problems), problems)
        # Gone, not half there: the old command must not answer either.
        self.assertEqual([], self.replies("!!greet"))
        self.assertNotIn("greeter", self.host.plugin_ids())

    def test_reloading_something_that_is_not_there_says_so(self):
        ok, problems = self.host.reload("nobody")
        self.assertFalse(ok)
        self.assertTrue(problems)

    def test_reload_brings_the_dependents_back_too(self):
        self.write("greeter.py", greeter())
        self.write("follower.py", DEPENDENT)
        self.host.load_all()
        self.assertEqual(["follower"], self.host.dependents("greeter"))
        self.host.reload("greeter")
        self.assertIn("follower", self.host.plugin_ids())
        self.assertEqual(["following"], self.replies("!!follow"))

    # ----------------------------------------------------------------- refresh

    def test_refresh_sees_the_three_cases(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        self.write("other.py", OTHER)                       # new
        self.write("greeter.py", greeter(word="hi"))        # changed
        new, changed, gone = self.host.changed_on_disk()
        self.assertEqual(["other"], [p.id for p in new])
        self.assertEqual(["greeter"], changed)
        self.assertEqual([], gone)
        result = self.host.refresh_changed_plugins()
        self.assertEqual(["other"], result["loaded"])
        self.assertEqual(["greeter"], result["reloaded"])
        self.assertEqual(["hi"], self.replies("!!greet"))
        self.assertEqual(["other"], self.replies("!!other"))

    def test_refresh_drops_a_deleted_plugin(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        (self.plugins / "greeter.py").unlink()
        result = self.host.refresh_changed_plugins()
        self.assertEqual(["greeter"], result["unloaded"])
        self.assertEqual([], self.host.plugin_ids())
        self.assertEqual([], self.replies("!!greet"))

    def test_refresh_with_nothing_to_do_does_nothing(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        result = self.host.refresh_changed_plugins()
        self.assertEqual([], result["loaded"] + result["reloaded"] + result["unloaded"])
        self.assertEqual(["hello"], self.replies("!!greet"))

    def test_refresh_all_reloads_everything(self):
        self.write("greeter.py", greeter())
        self.write("other.py", OTHER)
        self.host.load_all()
        result = self.host.refresh_all_plugins()
        self.assertEqual(["greeter", "other"], result["loaded"])
        self.assertEqual(2, len(self.host.commands))

    # ---------------------------------------------------------- enable/disable

    def test_disable_renames_and_unloads_it(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        ok, name = self.host.disable_plugin("greeter")
        self.assertTrue(ok, name)
        self.assertEqual("greeter.py.disabled", name)
        self.assertEqual([], self.host.plugin_ids())
        self.assertEqual(["greeter.py.disabled"], self.host.disabled_plugins())
        # And it does not come back on a refresh, which is the whole point.
        self.host.refresh_changed_plugins()
        self.assertEqual([], self.host.plugin_ids())

    def test_enable_puts_it_back_and_loads_it(self):
        self.write("greeter.py", greeter())
        self.host.load_all()
        self.host.disable_plugin("greeter")
        ok, name = self.host.enable_plugin("greeter.py")
        self.assertTrue(ok, name)
        self.assertEqual(["greeter"], self.host.plugin_ids())
        self.assertEqual(["hello"], self.replies("!!greet"))

    def test_loading_one_that_is_already_loaded_is_refused(self):
        path = self.write("greeter.py", greeter())
        self.host.load_all()
        ok, problems = self.host.load_new(path)
        self.assertFalse(ok)
        self.assertTrue(any("already loaded" in line for line in problems))

    # ------------------------------------------------------------- permissions

    def test_help_is_filtered_by_permission(self):
        self.write("greeter.py", greeter(perm=3))
        self.write("other.py", OTHER)
        self.host.load_all()
        console = ConsoleCommandSource(self.host)
        player = PlayerCommandSource(self.host, "Random")
        self.assertEqual(["!!greet", "!!other"],
                         sorted(item["prefix"] for item in self.host.help_for(console)))
        self.assertEqual(["!!other"],
                         [item["prefix"] for item in self.host.help_for(player)])
        self.host.set_permission("Random", "admin")
        self.assertEqual(["!!greet", "!!other"],
                         sorted(item["prefix"] for item in self.host.help_for(player)))


if __name__ == "__main__":
    unittest.main()
