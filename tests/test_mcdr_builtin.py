"""Tests for the commands the host answers itself: !!w4ve, !!MCDR and !!help.

Two things here are worth more than the rest:

  * the built-ins have to win against a plugin, because the command that
    unloads a misbehaving plugin cannot be a command that plugin can take.
  * no clickable line may use `run_command`. Since MC 1.20.5 a click that runs
    a command not starting with `/` does nothing at all and says nothing, and
    that is exactly how `!!here`'s buttons broke. The test walks the JSON that
    would go to the client and checks every click event.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.host import Host, ConsoleCommandSource, PlayerCommandSource  # noqa: E402

TELLRAW = "[w4ve] would run: tellraw "


def _payload(entry):
    """The JSON out of `... tellraw <target> <json>`, target and all."""
    return entry[len(TELLRAW):].split(" ", 1)[1]


PLUGIN = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'digall', 'version': '4.2.1'}

def on_load(server, prev):
    server.register_help_message('!!dig-all', 'dig a vein in one go')
    server.register_command(Literal('!!dig-all').runs(lambda s: s.reply('digging')))
'''

# A plugin that tries to own the management command, on purpose.
GREEDY = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'greedy', 'version': '1.0.0'}

def on_load(server, prev):
    server.register_command(Literal('!!w4ve').runs(lambda s: s.reply('mine now')))
'''


class BuiltinTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        (self.plugins / "digall.py").write_text(PLUGIN)
        self.output = []
        self.host = Host(self.root, plugins_dir=self.plugins,
                         on_output=self.output.append)
        self.host.load_all()
        self.host.set_permission("TVTvirus", "owner")
        self.console = ConsoleCommandSource(self.host)
        self.owner = PlayerCommandSource(self.host, "TVTvirus")
        self.player = PlayerCommandSource(self.host, "Random")

    def say(self, source, line):
        """Run a line and return what came back, as plain text."""
        mark = len(self.output)
        claimed = self.host.run_command(source, line)
        said = []
        for entry in self.output[mark:]:
            if entry.startswith(TELLRAW):
                said.append(_plain(json.loads(_payload(entry))))
            else:
                said.append(entry)
        return claimed, said

    def json_of(self, source, line):
        """The raw tellraw payloads a line produced."""
        mark = len(self.output)
        self.host.run_command(source, line)
        out = []
        for entry in self.output[mark:]:
            if entry.startswith(TELLRAW):
                out.append(json.loads(_payload(entry)))
        return out

    # ---------------------------------------------------------------- basics

    def test_status_answers_the_console(self):
        claimed, said = self.say(self.console, "!!w4ve status")
        self.assertTrue(claimed)
        self.assertTrue(any("1 loaded" in line for line in said), said)

    def test_mcdr_is_not_a_command_here(self):
        """The user decided against keeping the old name. It manages nothing.

        It still answers, with one line pointing at `!!w4ve`, because the
        alternative is Minecraft saying "Unknown command" to muscle memory.
        """
        _, said = self.say(self.console, "!!MCDR")
        self.assertTrue(any("no !!MCDR" in line for line in said), said)
        self.assertTrue(any("!!w4ve" in line for line in said), said)

    def test_mcdr_subcommands_manage_nothing(self):
        claimed, said = self.say(self.console, "!!MCDR plugin unload digall")
        self.assertTrue(claimed, "it must not fall through to Minecraft")
        self.assertEqual(["digall"], self.host.plugin_ids(),
                         "!!MCDR must not be able to unload anything")

    def test_even_somebody_without_permission_is_told_where_it_went(self):
        _, said = self.say(self.player, "!!MCDR")
        self.assertTrue(any("!!w4ve" in line for line in said), said)

    def test_a_plugin_cannot_steal_the_management_command(self):
        (self.plugins / "greedy.py").write_text(GREEDY)
        self.host.refresh_changed_plugins()
        self.assertIn("greedy", self.host.plugin_ids())
        _, said = self.say(self.console, "!!w4ve status")
        self.assertNotIn("mine now", said)

    def test_plugins_still_get_their_own_commands(self):
        claimed, said = self.say(self.console, "!!dig-all")
        self.assertTrue(claimed)
        self.assertEqual(["digging"], said)

    # ----------------------------------------------------------- permissions

    def test_a_player_without_the_level_is_told_so(self):
        _, said = self.say(self.player, "!!w4ve status")
        self.assertTrue(any("admin" in line for line in said), said)
        self.assertFalse(any("loaded" in line for line in said), said)

    def test_an_owner_can_manage(self):
        _, said = self.say(self.owner, "!!w4ve status")
        self.assertTrue(any("loaded" in line for line in said), said)

    def test_permission_set_writes_the_file(self):
        _, said = self.say(self.owner, "!!w4ve permission set Manke helper")
        self.assertTrue(any("helper" in line for line in said), said)
        self.assertEqual(2, self.host.permission_of("Manke"))
        self.assertIn("Manke", (self.root / "permission.yml").read_text())

    def test_nobody_hands_out_a_level_above_their_own(self):
        self.host.set_permission("Mid", "admin")
        mid = PlayerCommandSource(self.host, "Mid")
        _, said = self.say(mid, "!!w4ve permission set Mid owner")
        self.assertTrue(any("above your own" in line for line in said), said)
        self.assertEqual(3, self.host.permission_of("Mid"))

    def test_a_level_that_is_not_a_level_is_refused(self):
        _, said = self.say(self.owner, "!!w4ve permission set Manke boss")
        self.assertTrue(any("not a level" in line for line in said), said)
        self.assertEqual(1, self.host.permission_of("Manke"))

    def test_permission_remove_drops_them_to_the_default(self):
        self.host.set_permission("Manke", "admin")
        _, said = self.say(self.owner, "!!w4ve permission remove Manke")
        self.assertEqual(1, self.host.permission_of("Manke"))

    def test_permission_list_shows_who_is_what(self):
        self.host.set_permission("Manke", "helper")
        _, said = self.say(self.console, "!!w4ve permission list")
        text = " ".join(said)
        self.assertIn("TVTvirus", text)
        self.assertIn("Manke", text)

    # -------------------------------------------------------- managing them

    def test_reload_through_the_command(self):
        _, said = self.say(self.console, "!!w4ve reload plugin digall")
        self.assertTrue(any("reloaded digall" in line for line in said), said)
        self.assertIn("digall", self.host.plugin_ids())

    def test_unload_and_the_command_stops_answering(self):
        self.say(self.console, "!!w4ve plugin unload digall")
        claimed, _ = self.say(self.console, "!!dig-all")
        self.assertFalse(claimed)

    def test_disable_then_enable_through_the_command(self):
        _, said = self.say(self.console, "!!w4ve plugin disable digall")
        self.assertEqual([], self.host.plugin_ids())
        _, said = self.say(self.console, "!!w4ve plugin enable digall.py")
        self.assertEqual(["digall"], self.host.plugin_ids())

    def test_refresh_says_when_nothing_moved(self):
        _, said = self.say(self.console, "!!w4ve refresh")
        self.assertTrue(any("nothing changed" in line for line in said), said)

    def test_plugin_list_names_them(self):
        _, said = self.say(self.console, "!!w4ve plugin list")
        self.assertTrue(any("digall" in line for line in said), said)

    # ---------------------------------------------------------------- !!help

    def test_help_shows_what_you_may_run(self):
        _, said = self.say(self.player, "!!help")
        self.assertTrue(any("!!dig-all" in line for line in said), said)
        # No management line for somebody who cannot manage.
        self.assertFalse(any("!!w4ve" in line for line in said), said)

    def test_help_shows_management_to_an_admin(self):
        _, said = self.say(self.owner, "!!help")
        self.assertTrue(any("!!w4ve" in line for line in said), said)

    # ------------------------------------------------ the 1.20.5 click trap

    def test_no_button_uses_run_command(self):
        lines = []
        for command in ("!!w4ve", "!!w4ve status", "!!w4ve plugin list",
                        "!!w4ve permission list", "!!help",
                        "!!w4ve reload"):
            lines.extend(self.json_of(self.owner, command))
        clicks = list(_clicks(lines))
        self.assertTrue(clicks, "no clickable line was produced at all")
        for action, value in clicks:
            self.assertEqual("suggest_command", action,
                             "%r would silently do nothing since 1.20.5" % value)


def _plain(node):
    if isinstance(node, list):
        return "".join(_plain(item) for item in node)
    if isinstance(node, dict):
        return str(node.get("text", ""))
    return str(node)


def _clicks(node):
    if isinstance(node, list):
        for item in node:
            for found in _clicks(item):
                yield found
    elif isinstance(node, dict):
        event = node.get("clickEvent")
        if event:
            yield event.get("action"), event.get("value")
        for value in node.values():
            for found in _clicks(value):
                yield found


if __name__ == "__main__":
    unittest.main()
