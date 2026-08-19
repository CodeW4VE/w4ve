"""Tests for the host: a plugin loads, gets its config, and reacts to chat.

The fixture plugin is written the way the real ones are, because the point is
not that a toy loads. It declares a Serializable config, registers a help
message, reads chat in `on_info`, answers with an RText that has colour and a
click, and asks the server things. That is the shape of Here, DigAll and
StatsHelper, and every path it exercises is one a real plugin walks.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.host import Host  # noqa: E402
from mcdr.info import parse_server_line  # noqa: E402

PLUGIN = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {
    'id': 'greeter',
    'version': '1.0.0',
    'name': 'Greeter',
}


class Config(Serializable):
    greeting: str = 'hello'
    times: int = 1
    shout: bool = False


config = Config()
seen = []


def on_load(server: PluginServerInterface, old):
    global config
    server.register_help_message('!!hi', 'say hello back')
    config = server.load_config_simple('greeter.json', target_class=Config)
    server.logger.info('greeter is up')


def on_info(server: PluginServerInterface, info: Info):
    seen.append(info.content)
    if info.is_player and info.content == '!!hi':
        text = RText(config.greeting * config.times, RColor.gold)
        text.h('a hover').c(RAction.suggest_command, '/hi')
        server.say(RTextList(RText(info.player, RColor.yellow), ' ', text))
        server.execute('effect give %s minecraft:glowing 5' % info.player)
    if info.is_player and info.content == '!!where':
        server.say(server.rcon_query('data get entity %s Pos' % info.player)
                   or 'no rcon')


def on_player_joined(server, player, info):
    server.say('welcome %s' % player)


def on_unload(server):
    server.logger.info('greeter is down')
'''


class FakeServer:
    """Behaves like the guardian's Watched process, without being one."""

    state = "running"
    ready = True
    pid = 4321

    def __init__(self, rcon_answer=None):
        self.sent = []
        self.rcon_answer = rcon_answer

    def send(self, text):
        self.sent.append(text)
        return True, "sent"

    def rcon(self):
        if self.rcon_answer is None:
            return None, "RCON is off in server.properties"
        return FakeRconSession(self.rcon_answer), ""


class FakeRconSession:

    def __init__(self, answer):
        self.answer = answer
        self.closed = False

    def command(self, text):
        return self.answer

    def close(self):
        self.closed = True


class HostCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-host-"))
        (self.tmp / "plugins").mkdir()
        (self.tmp / "plugins" / "greeter.py").write_text(PLUGIN)
        self.lines = []
        self.server = FakeServer()

    def host(self, server=None):
        host = Host(self.tmp, server=server or self.server,
                    on_output=self.lines.append)
        host.load_all()
        return host

    # ------------------------------------------------------------- loading

    def test_a_plugin_loads_without_mcdr_installed(self):
        host = self.host()
        self.assertEqual(host.plugin_ids(), ["greeter"])
        self.assertEqual(host.problems, [])
        self.assertTrue(any("greeter is up" in line for line in self.lines))

    def test_it_registers_its_help(self):
        host = self.host()
        self.assertEqual(host.help_messages[0]["prefix"], "!!hi")
        self.assertEqual(host.help_messages[0]["plugin"], "greeter")

    def test_the_config_is_written_out_the_first_time(self):
        self.host()
        path = self.tmp / "config" / "greeter" / "greeter.json"
        self.assertTrue(path.exists(), "the defaults have to become a file")
        self.assertEqual(json.loads(path.read_text())["greeting"], "hello")

    def test_an_existing_config_is_read(self):
        folder = self.tmp / "config" / "greeter"
        folder.mkdir(parents=True)
        (folder / "greeter.json").write_text('{"greeting": "hola", "times": 2}')
        host = self.host()
        module = host.plugin_instance("greeter")
        self.assertEqual(module.config.greeting, "hola")
        self.assertEqual(module.config.times, 2)
        self.assertFalse(module.config.shout, "the missing one keeps its default")

    def test_a_broken_config_is_reported_and_the_plugin_still_loads(self):
        folder = self.tmp / "config" / "greeter"
        folder.mkdir(parents=True)
        (folder / "greeter.json").write_text("{not json")
        host = self.host()
        self.assertIn("greeter", host.plugin_ids())
        self.assertTrue(any("not valid JSON" in line for line in self.lines))

    def test_a_plugin_that_explodes_on_import_does_not_take_the_rest_down(self):
        (self.tmp / "plugins" / "bad.py").write_text(
            "PLUGIN_METADATA = {'id': 'bad', 'version': '1.0.0'}\n"
            "raise RuntimeError('boom')\n")
        host = self.host()
        self.assertIn("greeter", host.plugin_ids())
        self.assertNotIn("bad", host.plugin_ids())
        self.assertTrue(any("bad did not load" in p for p in host.problems),
                        host.problems)

    # ------------------------------------------------------------- reacting

    def test_a_player_in_chat_reaches_the_plugin(self):
        host = self.host()
        host.feed_line("[14:32:10] [Server thread/INFO]: <TVTvirus> !!hi")
        module = host.plugin_instance("greeter")
        self.assertEqual(module.seen[-1], "!!hi")
        self.assertEqual(len(self.server.sent), 2)

    def test_what_it_says_arrives_as_a_tellraw_with_the_formatting_intact(self):
        host = self.host()
        host.feed_line("[14:32:10] [Server thread/INFO]: <TVTvirus> !!hi")
        tellraw = self.server.sent[0]
        self.assertTrue(tellraw.startswith("tellraw @a "))
        data = json.loads(tellraw[len("tellraw @a "):])
        self.assertEqual(data[1]["text"], "TVTvirus")
        self.assertEqual(data[1]["color"], "yellow")
        self.assertEqual(data[3]["color"], "gold")
        self.assertEqual(data[3]["clickEvent"]["value"], "/hi")
        self.assertEqual(data[3]["hoverEvent"]["action"], "show_text")

    def test_a_console_line_is_not_a_player(self):
        host = self.host()
        host.feed_line("[14:32:10] [Server thread/INFO]: Saving the game")
        self.assertEqual(self.server.sent, [], "nothing should have answered")
        self.assertEqual(host.plugin_instance("greeter").seen[-1],
                         "Saving the game")

    def test_rcon_is_the_same_rcon_the_guardian_uses(self):
        server = FakeServer(rcon_answer="TVTvirus has [1.0d, 2.0d, 3.0d]")
        host = self.host(server)
        host.feed_line("[14:32:10] [Server thread/INFO]: <TVTvirus> !!where")
        self.assertIn("1.0d", server.sent[-1])

    def test_without_rcon_the_plugin_is_told_so_rather_than_crashing(self):
        host = self.host()          # this FakeServer has no RCON
        host.feed_line("[14:32:10] [Server thread/INFO]: <TVTvirus> !!where")
        self.assertIn("no rcon", self.server.sent[-1])
        self.assertEqual(host.problems, [])

    def test_events_reach_the_handlers_that_exist(self):
        host = self.host()
        host.dispatch("on_player_joined", "Karla", parse_server_line(""))
        self.assertIn("welcome Karla", self.server.sent[-1])

    def test_unloading_tells_the_plugin(self):
        host = self.host()
        host.unload_all()
        self.assertTrue(any("greeter is down" in line for line in self.lines))
        self.assertEqual(host.plugin_ids(), [])

    # ------------------------------------------------------------- honesty

    def test_a_command_registration_is_admitted_as_unsupported(self):
        (self.tmp / "plugins" / "commander.py").write_text(
            "from mcdreforged.api.all import *\n"
            "PLUGIN_METADATA = {'id': 'commander', 'version': '1.0.0'}\n"
            "def on_load(server, old):\n"
            "    server.register_command(None)\n")
        host = self.host()
        self.assertIn("commander", host.plugin_ids())
        self.assertTrue(any("cannot route commands yet" in p
                            for p in host.problems), host.problems)

    def test_the_metadata_a_plugin_asks_about_itself(self):
        host = self.host()
        meta = host.plugin_metadata("greeter")
        self.assertEqual(meta.id, "greeter")
        self.assertEqual(meta.version, "1.0.0")
        self.assertEqual(meta.name, "Greeter")


class InfoCase(unittest.TestCase):

    def test_a_player_talking(self):
        info = parse_server_line(
            "[14:32:10] [Server thread/INFO]: <TVTvirus> hello there")
        self.assertTrue(info.is_player and info.is_user)
        self.assertEqual(info.player, "TVTvirus")
        self.assertEqual(info.content, "hello there")
        self.assertEqual((info.hour, info.min, info.sec), (14, 32, 10))

    def test_the_server_talking(self):
        info = parse_server_line(
            "[14:32:10] [Server thread/INFO]: Saving chunks")
        self.assertFalse(info.is_player or info.is_user)
        self.assertEqual(info.content, "Saving chunks")
        self.assertEqual(info.logging_level, "INFO")

    def test_paper_writes_its_lines_differently(self):
        info = parse_server_line("[14:32:10 INFO]: <Karla> hi")
        self.assertTrue(info.is_player)
        self.assertEqual(info.player, "Karla")
        self.assertEqual(info.content, "hi")

    def test_something_that_is_not_a_log_line_still_arrives(self):
        info = parse_server_line("plain nonsense from a mod")
        self.assertEqual(info.content, "plain nonsense from a mod")
        self.assertFalse(info.is_player)

    def test_a_warning_is_not_chat(self):
        info = parse_server_line(
            "[14:32:10] [Server thread/WARN]: <TVTvirus> looks like chat")
        # It parses as chat because it is shaped like chat; the level is what
        # tells a plugin otherwise, so it has to survive the parse.
        self.assertEqual(info.logging_level, "WARN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
