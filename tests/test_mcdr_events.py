"""Tests for plugin to plugin events, and for the API holes real plugins fell
into.

None of these were guessed. Each one is a plugin MineWave has installed
refusing to load, found by putting the twelve real plugins in a folder and
loading them:

  * QuickBackupM and TimedQBM import `LiteralEvent` at module level and talk
    to each other through it, so without events they do not even import.
  * MultiRconAPI does `class Rcon(RconConnection)`, so that name has to be a
    class, not a helper.
  * QuickBackupM keeps a dict of `SlotInfo` objects in its config, and saving
    it failed with "Object of type SlotInfo is not JSON serializable".
  * TimedQBM asks its own metadata for `get_description(language)`.
  * Task imports `CommandSource` from `mcdreforged.command.command_source`
    and `deserialize` from `mcdreforged.api.utils`.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.event import (LiteralEvent, MCDRPluginEvents,  # noqa: E402
                        PluginEvent, is_builtin, name_of)
from mcdr.host import Host, ConsoleCommandSource  # noqa: E402
from mcdr.rcon_api import RconConnection  # noqa: E402
from mcdr.serializer import Serializable, deserialize, serialize  # noqa: E402

# Two plugins that only know each other through an event, the way QuickBackupM
# and TimedQBM do.
ANNOUNCER = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'announcer', 'version': '1.0.0'}

DONE = LiteralEvent('announcer.done')

def on_load(server, prev):
    server.register_command(
        Literal('!!announce').runs(
            lambda src: server.dispatch_event(DONE, ('slot5', 42))))
'''

LISTENER = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'listener', 'version': '1.0.0'}

got = []

def on_load(server, prev):
    server.register_event_listener(
        LiteralEvent('announcer.done'),
        lambda srv, slot, size: got.append((slot, size)))
    server.register_event_listener(
        MCDRPluginEvents.PLAYER_JOINED,
        lambda srv, player, info: got.append('joined:' + player))
'''


class EventNameTest(unittest.TestCase):

    def test_the_three_ways_of_naming_an_event_agree(self):
        self.assertEqual("on_player_joined", name_of(MCDRPluginEvents.PLAYER_JOINED))
        self.assertEqual("on_player_joined", name_of("mcdr.player_joined"))
        self.assertEqual("on_player_joined", name_of("on_player_joined"))

    def test_a_plugins_own_event_keeps_its_id(self):
        self.assertEqual("qbm.backup_done", name_of(LiteralEvent("qbm.backup_done")))
        self.assertFalse(is_builtin(LiteralEvent("qbm.backup_done")))

    def test_events_with_the_same_id_are_the_same_event(self):
        self.assertEqual(PluginEvent("a.b"), LiteralEvent("a.b"))
        self.assertEqual(1, len({PluginEvent("a.b"), LiteralEvent("a.b")}))


class DispatchTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        (self.plugins / "announcer.py").write_text(ANNOUNCER)
        (self.plugins / "listener.py").write_text(LISTENER)
        self.output = []
        self.host = Host(self.root, plugins_dir=self.plugins,
                         on_output=self.output.append)
        self.host.load_all()

    def got(self):
        return self.host.plugin_instance("listener").got

    def test_one_plugin_hears_another(self):
        self.host.run_command(ConsoleCommandSource(self.host), "!!announce")
        self.assertEqual([("slot5", 42)], self.got())

    def test_a_listener_registered_by_event_object_gets_server_events(self):
        self.host.dispatch("on_player_joined", "TVTvirus", None)
        self.assertIn("joined:TVTvirus", self.got())

    def test_a_plugins_own_event_is_not_reported_as_a_missing_feature(self):
        # The bucket for "we do not implement this" and "this is somebody
        # else's event" is the same bucket until you separate them, and
        # reporting the second as a problem makes the report untrustworthy.
        self.assertEqual([], self.host.problems)

    def test_unloading_stops_the_plugin_events_too(self):
        self.host.unload("listener")
        self.host.run_command(ConsoleCommandSource(self.host), "!!announce")
        self.assertEqual([], self.host.problems)


class RconConnectionTest(unittest.TestCase):

    def test_it_is_a_class_a_plugin_can_subclass(self):
        class Mine(RconConnection):
            pass

        connection = Mine("127.0.0.1", 25575, "secret")
        self.assertEqual(25575, connection.port)

    def test_a_connection_that_cannot_be_made_says_so_instead_of_raising(self):
        # Port 1 is not going to answer, and a plugin asking RCON a question
        # while the server is down must get None, not a traceback.
        connection = RconConnection("127.0.0.1", 1, "secret")
        self.assertFalse(connection.connect())
        self.assertIsNone(connection.send_command("list", max_retry_time=1))


class SerializerTest(unittest.TestCase):

    def test_nested_serializables_in_containers_come_out_as_json(self):
        class Slot(Serializable):
            comment: str = ""
            size: int = 0

        class Config(Serializable):
            slots: dict = {}
            history: list = []

        config = Config()
        config.slots = {"slot1": Slot.deserialize({"comment": "a", "size": 1})}
        config.history = [Slot.deserialize({"comment": "b", "size": 2})]
        data = config.serialize()
        self.assertEqual({"comment": "a", "size": 1}, data["slots"]["slot1"])
        self.assertEqual([{"comment": "b", "size": 2}], data["history"])
        # And the whole point: it can actually be written.
        self.assertIn('"comment"', config.to_json_str())

    def test_nested_serializables_survive_a_round_trip(self):
        # The bug this covers only appeared on the *second* load: the first
        # one writes defaults, and reading them back turned every SlotInfo
        # into a plain dict.
        class Slot(Serializable):
            delete_protection: int = 0

        class Config(Serializable):
            slots: dict = {}
            history: list = []

        Config.__annotations__["slots"] = __import__("typing").Dict[str, Slot]
        Config.__annotations__["history"] = __import__("typing").List[Slot]
        first = Config.deserialize({"slots": {"a": {"delete_protection": 3}},
                                    "history": [{"delete_protection": 5}]})
        self.assertIsInstance(first.slots["a"], Slot)
        self.assertEqual(3, first.slots["a"].delete_protection)
        self.assertIsInstance(first.history[0], Slot)
        # And round tripping keeps it that way.
        again = Config.deserialize(first.serialize())
        self.assertEqual(5, again.history[0].delete_protection)

    def test_a_bad_element_is_dropped_not_the_whole_collection(self):
        import typing

        class Slot(Serializable):
            delete_protection: int = 0

        class Config(Serializable):
            history: list = []

        Config.__annotations__["history"] = typing.List[Slot]
        config, problems = Config.deserialize_reporting(
            {"history": [{"delete_protection": 1}, "not a block", 
                         {"delete_protection": 2}]})
        # The bad one costs its own entry and nothing else.
        self.assertTrue(problems)
        self.assertEqual(2, len(config.history))
        self.assertEqual([1, 2], [slot.delete_protection for slot in config.history])

    def test_lists_of_plain_types_are_left_alone(self):
        import typing

        class Config(Serializable):
            names: list = []

        Config.__annotations__["names"] = typing.List[str]
        config = Config.deserialize({"names": ["session.lock", "x"]})
        self.assertEqual(["session.lock", "x"], config.names)

    def test_the_module_level_helpers_agree_with_the_methods(self):
        class Thing(Serializable):
            name: str = "x"

        thing = Thing()
        self.assertEqual(thing.serialize(), serialize(thing))
        self.assertEqual("y", deserialize({"name": "y"}, Thing).name)


class MetadataTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()

    def metadata(self, description):
        import json

        folder = self.plugins / "thing"
        folder.mkdir()
        (folder / "mcdreforged.plugin.json").write_text(json.dumps(
            {"id": "thing", "version": "1.0.0", "entrypoint": "thing",
             "description": description}))
        (folder / "thing.py").write_text(
            "from mcdreforged.api.all import *\n")
        host = Host(self.root, plugins_dir=self.plugins, on_output=lambda _: None)
        host.load_all()
        return host.plugin_metadata("thing")

    def test_a_plain_description(self):
        meta = self.metadata("just words")
        self.assertEqual("just words", meta.get_description())
        self.assertEqual("just words", meta.get_description("zh_cn"))

    def test_a_description_per_language(self):
        meta = self.metadata({"en_us": "words", "zh_cn": "字"})
        self.assertEqual("words", meta.get_description("en_us"))
        self.assertEqual("字", meta.get_description("zh_cn"))
        # An unknown language falls back rather than answering nothing.
        self.assertEqual("words", meta.get_description("es_es"))

    def test_no_description_at_all(self):
        meta = self.metadata(None)
        self.assertEqual("", meta.get_description())


class ImportPathTest(unittest.TestCase):
    """The deeper module paths plugins import from, which have to be real."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "plugins").mkdir()
        self.host = Host(self.root, on_output=lambda _: None)
        self.host.install_module()

    def test_command_source_is_importable_from_its_own_module(self):
        from mcdreforged.command.command_source import (  # noqa: F401
            CommandSource, ConsoleCommandSource, PlayerCommandSource)

    def test_utils_carries_the_serializer_helpers(self):
        from mcdreforged.api.utils import deserialize, serialize  # noqa: F401
        from mcdreforged.utils.serializer import Serializable  # noqa: F401

    def test_the_logger_class_is_importable_and_writes_to_a_file(self):
        from mcdreforged.api.all import MCDReforgedLogger

        path = self.root / "logs" / "task.log"
        logger = MCDReforgedLogger(self.host, "task")
        logger.set_file(path)
        logger.info("hello")
        self.assertIn("hello", path.read_text())
        logger.close_file()


if __name__ == "__main__":
    unittest.main()
