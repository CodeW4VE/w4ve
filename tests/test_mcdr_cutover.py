"""The holes that showed up taking the rehearsal from eight plugins to thirteen.

Every case here is a real plugin refusing to work, written down so it cannot
come back. The plugin that found each one is named, because that is the only
evidence that the case is worth having: none of these were guessed from the
documentation, and none of them would have been found by a fixture written to
pass.

The fixture plugin is small on purpose. What is being tested is the API
underneath it, and the real plugins already proved the API is what breaks.
"""

import enum
import re
import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import w4ve  # noqa: E402
from mcdr.host import Host, Logger  # noqa: E402
from mcdr.serializer import Serializable, deserialize  # noqa: E402
from mcdr.text import (RColor, RText, RTextBase, RTextList,  # noqa: E402
                       RTextMCDRTranslation)


class FakeServer:
    """The guardian's process, reduced to what a load touches."""

    state = "running"
    ready = True
    pid = 1234

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True, "sent"

    def rcon(self):
        return None, "RCON is off in server.properties"


class HostFixture(unittest.TestCase):
    """A host with one plugin folder, ready for a plugin to be dropped in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-cutover-"))
        (self.tmp / "plugins").mkdir()
        self.lines = []

    def plugin(self, name, source):
        (self.tmp / "plugins" / name).write_text(source)

    def host(self):
        host = Host(self.tmp, server=FakeServer(),
                    on_output=self.lines.append)
        host.load_all()
        return host


# --------------------------------------------------------------- ChatBridge

LISTENER_PLUGIN = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'bridge', 'version': '1.0.0'}

heard = []


@event_listener('more_apis.death_message')
def on_death(server, message):
    heard.append(message)
'''


class EventListenerCase(HostFixture):
    """ChatBridge decorates a module level function with `@event_listener`.

    Without the name existing the plugin did not even import: `NameError:
    name 'event_listener' is not defined`. The decorator runs during the
    import, so it has to find the plugin that is loading right now.
    """

    def test_the_decorator_registers_while_the_module_imports(self):
        self.plugin("bridge.py", LISTENER_PLUGIN)
        host = self.host()
        self.assertEqual(host.problems, [])
        host.dispatch_custom("more_apis.death_message", ("somebody fell",))
        module = host.loaded["bridge"].module
        self.assertEqual(module.heard, ["somebody fell"])

    def test_outside_a_load_it_says_so_instead_of_going_quiet(self):
        from mcdr.interface import ServerInterface
        from mcdr.misc import event_listener

        self.plugin("bridge.py", LISTENER_PLUGIN)
        self.host()
        # The singleton survives the load; asking for a listener now belongs
        # to no plugin, and a listener nobody registered never fires.
        ServerInterface._instance._host._loading = None
        with self.assertRaises(RuntimeError):
            event_listener("some.event")(lambda *a: None)


SYNC_HANDLER_PLUGIN = '''
import logging

from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'noisy', 'version': '1.0.0'}

handler = SyncStdoutStreamHandler()
own = logging.getLogger('noisy-own')
own.addHandler(handler)
'''


class SyncHandlerCase(HostFixture):
    """ChatBridge swaps MCDR's stdout handler into every client it opens."""

    def test_the_handler_exists_and_shares_one_lock(self):
        self.plugin("noisy.py", SYNC_HANDLER_PLUGIN)
        host = self.host()
        self.assertEqual(host.problems, [])
        from mcdr.misc import SyncStdoutStreamHandler

        first, second = SyncStdoutStreamHandler(), SyncStdoutStreamHandler()
        self.assertIs(first._write_lock, second._write_lock,
                      "a per-instance lock would not stop interleaving")


# ------------------------------------------------------------- PrimeBackup

METADATA_PLUGIN = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'meta_user', 'version': '2.3.4', 'name': 'Meta User'}

seen = {}


def on_load(server, old):
    metadata: Metadata = PluginServerInterface.psi().get_self_metadata()
    seen['id'] = metadata.id
    seen['version'] = str(metadata.version)
'''


class PsiCase(HostFixture):
    """PrimeBackup asks for `psi()` and immediately uses it as a plugin's.

    `si()` and `psi()` were the same call, so it failed at load with
    `'ServerInterface' object has no attribute 'get_self_metadata'`.
    `Metadata` was also missing from the fake module, which PrimeBackup
    imports to type that very variable.
    """

    def test_psi_answers_with_the_loading_plugins_interface(self):
        self.plugin("meta_user.py", METADATA_PLUGIN)
        host = self.host()
        self.assertEqual(host.problems, [])
        self.assertEqual(host.loaded["meta_user"].module.seen,
                         {"id": "meta_user", "version": "2.3.4"})


class HashMethod(enum.Enum):
    """PrimeBackup's shape: an enum whose values are objects, not strings."""

    xxh128 = ("xxhash.xxh128", 32)
    md5 = ("hashlib.md5", 32)


class HashConfig(Serializable):
    hash_method: HashMethod = HashMethod.xxh128


class EnumCase(unittest.TestCase):
    """An enum is written by name. PrimeBackup's values are not JSON at all.

    Writing the value failed with `Object of type _HashMethodItem is not JSON
    serializable`, and the name is the word somebody types in the file anyway.
    """

    def test_written_by_name(self):
        self.assertEqual(HashConfig().serialize(), {"hash_method": "xxh128"})

    def test_read_back_by_name(self):
        config = HashConfig.deserialize({"hash_method": "md5"})
        self.assertIs(config.hash_method, HashMethod.md5)

    def test_a_name_that_does_not_exist_keeps_the_default_and_says_so(self):
        config, problems = HashConfig.deserialize_reporting(
            {"hash_method": "sha1"})
        self.assertIs(config.hash_method, HashMethod.xxh128)
        self.assertTrue(any("xxh128, md5" in p for p in problems), problems)

    def test_round_trip(self):
        config = HashConfig.deserialize(HashConfig().serialize())
        self.assertIs(config.hash_method, HashMethod.xxh128)


# ------------------------------------------------------- MinecraftDataAPI

class RegexConfig(Serializable):
    """MinecraftDataAPI declares its output parsers as compiled patterns."""

    list_output_regex: re.Pattern = re.compile(r"^There are (?P<amount>\d+)")


class PatternCase(unittest.TestCase):
    """A compiled regex is written as the pattern it came from.

    Without this MinecraftDataAPI died writing its very first config:
    `Object of type Pattern is not JSON serializable`.
    """

    def test_written_as_the_pattern_text(self):
        self.assertEqual(RegexConfig().serialize()["list_output_regex"],
                         r"^There are (?P<amount>\d+)")

    def test_read_back_compiled(self):
        config = RegexConfig.deserialize({"list_output_regex": r"^\d+ players"})
        self.assertTrue(config.list_output_regex.match("12 players"))

    def test_a_broken_pattern_keeps_the_default_and_says_so(self):
        config, problems = RegexConfig.deserialize_reporting(
            {"list_output_regex": "(unclosed"})
        self.assertTrue(config.list_output_regex.match("There are 4"),
                        "a typo in one line must not disarm the parser")
        self.assertTrue(any("regular expression" in p for p in problems),
                        problems)


# -------------------------------------------------------------------- Task

class TaskLike(Serializable):
    """Task's shape: an override that hands the rest back to the generic one."""

    name: str = ""
    done: bool = False

    @classmethod
    def deserialize(cls, data, **kwargs):
        data = dict(data)
        data.pop("sub_tasks", None)
        return deserialize(data=data, cls=cls, **kwargs)


class GenericDeserializeCase(unittest.TestCase):
    """The module level `deserialize` never calls the classmethod back.

    Task overrides `deserialize` and calls the generic function from inside
    it. When the generic function answered by calling the classmethod, the two
    called each other until `RecursionError: maximum recursion depth
    exceeded`. The keyword names matter too: Task passes `data=` and `cls=`,
    and a parameter named anything else is a TypeError at load.
    """

    def test_an_override_can_call_the_generic_one(self):
        task = TaskLike.deserialize({"name": "dig", "sub_tasks": [{}]})
        self.assertEqual(task.name, "dig")
        self.assertFalse(task.done)

    def test_the_arguments_are_named_data_and_cls(self):
        thing = deserialize(data={"name": "x"}, cls=TaskLike)
        self.assertEqual(thing.name, "x")

    def test_a_nested_field_goes_through_the_generic_path(self):
        class Holder(Serializable):
            task: TaskLike = None

        holder = Holder.deserialize({"task": {"name": "inner",
                                              "sub_tasks": [{}]}})
        self.assertEqual(holder.task.name, "inner")


class DefaultCase(unittest.TestCase):
    """`get_default()`, which Task and WhereIs both call at load."""

    def test_it_is_an_untouched_instance(self):
        first = TaskLike.get_default()
        self.assertEqual(first.name, "")
        first.name = "changed"
        self.assertEqual(TaskLike.get_default().name, "",
                         "the default must not be the same object twice")


# ----------------------------------------------------------------- WhereIs

class NoDefault(Serializable):
    """WhereIs declares `here_use_broadcast: bool` and never assigns it."""

    prefix: str = "!!here"
    here_use_broadcast: bool


class FieldWithoutADefaultCase(unittest.TestCase):
    """A field with no default is left out of the file, not written as null.

    WhereIs reads it as `self.serialize().get('here_use_broadcast', False)`,
    so a `null` in the file was not just noise: on the next load it failed its
    own type check with `should be true or false, not None`.
    """

    def test_it_is_not_written(self):
        self.assertEqual(NoDefault().serialize(), {"prefix": "!!here"})

    def test_a_value_in_the_file_is_read(self):
        config = NoDefault.deserialize({"here_use_broadcast": True})
        self.assertTrue(config.here_use_broadcast)
        self.assertIn("here_use_broadcast", config.serialize())

    def test_a_written_file_loads_again_without_complaining(self):
        _, problems = NoDefault.deserialize_reporting(NoDefault().serialize())
        self.assertEqual(problems, [])


# -------------------------------------------------------------------- Task

class JoinCase(unittest.TestCase):
    """`RTextBase.join`, which Task builds its whole `!!task` screen with."""

    def test_it_keeps_every_piece_styled(self):
        joined = RTextBase.join("\n", [RText("uno", RColor.gold),
                                       RText("dos"), "tres"])
        self.assertEqual("uno\ndos\ntres", joined.to_plain_text())
        colours = [part.get("color") for part in joined.to_json_object()[1:]]
        self.assertIn("gold", colours, "a plain join would lose this")

    def test_one_item_needs_no_divider(self):
        self.assertEqual("solo", RTextBase.join(", ", [RText("solo")]).to_plain_text())

    def test_nothing_joins_to_nothing(self):
        self.assertEqual("", RTextBase.join(", ", []).to_plain_text())


class PlainTextIsAlwaysAStringCase(unittest.TestCase):
    """Whatever a plugin puts inside a text object, a string comes out.

    Task passes text objects to `RText(...)` and resolves a translation to
    another text object. Both ended the same way, in the console rather than
    in the plugin: `TypeError: sequence item 6: expected str instance,
    RTextList found`, thrown while printing the answer.
    """

    def test_a_text_object_inside_an_rtext(self):
        nested = RText(RTextList(RText("a"), RText("b")))
        self.assertEqual("ab", nested.to_plain_text())
        self.assertEqual("abc", RTextList(nested, "c").to_plain_text())

    def test_a_translation_that_resolves_to_a_text_object(self):
        translation = RTextMCDRTranslation("some.key")
        translation.set_translator(
            lambda key, *a, **k: RTextList(RText("hola "), RText("mundo")))
        self.assertEqual("hola mundo", translation.to_plain_text())
        self.assertEqual("hola mundo!", RTextList(translation, "!").to_plain_text())

    def test_a_number_is_not_an_error(self):
        self.assertEqual("42", RText(42).to_plain_text())


class UnsetFileCase(unittest.TestCase):
    """`unset_file`, the other half of `set_file`, called by Task on unload."""

    def test_a_logger_can_close_its_file_again(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-logger-")) / "task.log"

        class Host:
            debug = False

            def log(self, line):
                pass

        logger = Logger(Host(), "mcd_task")
        logger.set_file(tmp)
        logger.info("something")
        logger.unset_file()
        self.assertIsNone(logger.file)
        self.assertIn("something", tmp.read_text())
        logger.unset_file()  # calling it twice must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)


class InitReadsMcdrCase(unittest.TestCase):
    """What `w4ve init` takes from MCDR's own config, and what it does not.

    Found by walking the cutover document on a real MCDR install: `init` had
    been guessing the layout from whether `server/mods` existed, which says "."
    for a vanilla server under MCDR, which is every one that runs no mods. A
    wrong game_dir is the quiet failure of the whole migration: plugins write
    to relative paths, so a backup lands in an empty folder and reports success.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-init-"))
        (self.tmp / "server").mkdir()
        (self.tmp / "server" / "server.jar").write_text("not really")

    def init(self, force=False):
        args = argparse.Namespace(dir=str(self.tmp), name="mirror", mc="1.21",
                                  client=False, force=force, dry_run=False,
                                  yes=True)
        w4ve.cmd_init(args, None)
        return (self.tmp / "w4ve.toml").read_text()

    def test_the_layout_comes_from_mcdr_when_mcdr_says_so(self):
        (self.tmp / "config.yml").write_text(
            "working_directory: server\n"
            "start_command: java -Xmx4G -jar server.jar nogui\n")
        text = self.init()
        self.assertIn('game_dir = "server"', text)

    def test_the_start_command_is_copied_and_not_rewritten(self):
        # Somebody's flags are somebody's tuning. A start command that is
        # nearly right is a different server.
        command = "/opt/jdk-21/bin/java -Xms8G -Xmx8G -XX:+UseG1GC -jar fabric.jar nogui"
        (self.tmp / "config.yml").write_text(
            "working_directory: server\nstart_command: %s\n" % command)
        text = self.init()
        self.assertIn(command, text)

    def test_a_vanilla_server_under_mcdr_is_not_read_as_flat(self):
        # No `server/mods`, because it runs no mods. The old test said "." here.
        (self.tmp / "config.yml").write_text("working_directory: server\n")
        self.assertIn('game_dir = "server"', self.init())

    def test_with_no_mcdr_the_jar_in_server_is_enough(self):
        self.assertIn('game_dir = "server"', self.init())

    def test_a_plain_folder_stays_plain(self):
        empty = Path(tempfile.mkdtemp(prefix="w4ve-plain-"))
        args = argparse.Namespace(dir=str(empty), name="x", mc="1.21",
                                  client=False, force=False, dry_run=False,
                                  yes=True)
        w4ve.cmd_init(args, None)
        self.assertIn('game_dir = "."', (empty / "w4ve.toml").read_text())
