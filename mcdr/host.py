"""The host: it loads MCDR plugins and gives them somewhere real to land.

A plugin does `from mcdreforged.api.all import *` and expects a module to be
there. This builds that module in memory, points it at the pieces W4VE
implements, loads the plugin, and calls its events. Underneath, everything ends
up at the guardian from RFC-0001: the console it writes to is the one W4VE
owns, the RCON it queries is `rcon.py`, and the state it keeps is W4VE's.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.

Two limits stated up front, because pretending otherwise is how a
compatibility layer earns distrust:

  * a plugin runs in this process, with no sandbox. It is Python: it can do
    whatever Python can do, exactly like under MCDR.
  * the events dispatched are the nine that the measured plugins use. Anything
    else is reported as unsupported at load time rather than silently ignored,
    so a plugin that will not work is known before the cutover, not after.
"""

import importlib.util
import sys
import threading
import traceback
import types
import zipfile
from pathlib import Path

from mcdr import loader
from mcdr.info import Info, parse_server_line
from mcdr.interface import PermissionLevel, PluginServerInterface, ServerInterface
from mcdr.misc import FunctionThread, Version, VersionRequirement, new_thread
from mcdr.serializer import Serializable
from mcdr.text import (RAction, RColor, RStyle, RText, RTextBase, RTextList,
                       RTextMCDRTranslation, RTextTranslation)

# The events this host dispatches. Measured, not guessed: these are the nine
# the plugins MineWave runs actually implement.
EVENTS = ("on_load", "on_unload", "on_info", "on_user_info",
          "on_player_joined", "on_player_left",
          "on_server_start", "on_server_startup", "on_server_stop")

# API compatibility is frozen at MCDR 2.x on purpose, per W4VE-DISENO section
# 5: a moving target cannot be a compatibility promise.
CLAIMED_MCDR_VERSION = "2.15.5"


class Logger:
    """What `server.logger` is. Named, so an operator knows who said it."""

    def __init__(self, host, plugin_id=None):
        self.host = host
        self.plugin_id = plugin_id

    def _say(self, level, message):
        who = self.plugin_id or "mcdr"
        self.host.log("[%s] [%s] %s" % (who, level, message))

    def info(self, message, *args):
        self._say("INFO", message % args if args else message)

    def warning(self, message, *args):
        self._say("WARN", message % args if args else message)

    warn = warning

    def error(self, message, *args):
        self._say("ERROR", message % args if args else message)

    def debug(self, message, *args, **kwargs):
        if self.host.debug:
            self._say("DEBUG", message % args if args else message)

    def exception(self, message, *args):
        self._say("ERROR", message % args if args else message)
        for line in traceback.format_exc().splitlines():
            self._say("ERROR", "    " + line)


class LoadedPlugin:
    """A plugin that is in memory, with the module and interface it got."""

    def __init__(self, plugin, module, interface):
        self.plugin = plugin
        self.module = module
        self.interface = interface
        self.help_messages = []
        self.commands = []

    @property
    def id(self):
        return self.plugin.id

    def handler(self, event):
        return getattr(self.module, event, None)


class Host:
    """Loads MCDR plugins and runs their events against a real server.

    `server` is anything that behaves like the guardian's Watched process: it
    can be sent console lines and asked whether it is running. Passing None is
    how the tests and `w4ve plugins --check` load plugins with nothing behind
    them, which is enough to find out whether they load at all.
    """

    def __init__(self, server_root, plugins_dir=None, config_dir=None,
                 server=None, on_output=None, language="en_us", debug=False):
        self.server_root = Path(server_root)
        self.plugins_dir = Path(plugins_dir or self.server_root / "plugins")
        self.config_dir = Path(config_dir or self.server_root / "config")
        self.server = server
        self.language = language
        self.debug = debug
        self._output = on_output or (lambda line: print(line))
        self.loaded = {}                 # id -> LoadedPlugin
        self.problems = []
        self.translations = {}
        self._executor = threading.current_thread()
        self._help = []

    # ------------------------------------------------------------------ output

    def log(self, line):
        self._output(line)

    def logger_for(self, plugin_id):
        return Logger(self, plugin_id)

    # ------------------------------------------------------ the fake mcdreforged

    def install_module(self):
        """Put a `mcdreforged` package into sys.modules for plugins to import.

        Built rather than shipped: what a plugin imports has to be exactly the
        names it expects, and building it here keeps that list in one place
        next to the things it points at.
        """
        # `ServerInterface.si()` is how a plugin reaches the interface from
        # code that was never handed one. It is a singleton in MCDR, so it has
        # to be one here: DigAll calls it from a module level helper.
        ServerInterface._instance = ServerInterface(self)
        names = {
            # text
            "RText": RText, "RTextList": RTextList, "RTextBase": RTextBase,
            "RTextTranslation": RTextTranslation,
            "RTextMCDRTranslation": RTextMCDRTranslation,
            "RColor": RColor, "RStyle": RStyle, "RAction": RAction,
            # config
            "Serializable": Serializable,
            # the server
            "ServerInterface": ServerInterface,
            "PluginServerInterface": PluginServerInterface,
            "PermissionLevel": PermissionLevel,
            "Info": Info,
            # Found by loading real plugins: DigAll needs the decorator,
            # MinecraftDataAPI needs to compare versions.
            "new_thread": new_thread,
            "FunctionThread": FunctionThread,
            "Version": Version,
            "VersionRequirement": VersionRequirement,
        }

        root = types.ModuleType("mcdreforged")
        root.__path__ = []
        api = types.ModuleType("mcdreforged.api")
        api.__path__ = []
        modules = {"mcdreforged": root, "mcdreforged.api": api}

        for leaf in ("all", "rtext", "command", "utils", "types", "decorator",
                     "event", "rcon"):
            module = types.ModuleType("mcdreforged.api." + leaf)
            module.__path__ = []
            for name, value in names.items():
                setattr(module, name, value)
            module.__all__ = sorted(names)
            modules["mcdreforged.api." + leaf] = module
            setattr(api, leaf, module)

        # Plugins reach for these deeper paths too: PrimeBackup imports
        # `mcdreforged.command.command_source`, ChatBridge imports
        # `mcdreforged.utils.serializer`.
        serializer = types.ModuleType("mcdreforged.utils.serializer")
        serializer.Serializable = Serializable
        utils = types.ModuleType("mcdreforged.utils")
        utils.__path__ = []
        utils.serializer = serializer
        utils.Serializable = Serializable
        command = types.ModuleType("mcdreforged.command")
        command.__path__ = []
        command_source = types.ModuleType("mcdreforged.command.command_source")
        command.command_source = command_source
        modules.update({
            "mcdreforged.utils": utils,
            "mcdreforged.utils.serializer": serializer,
            "mcdreforged.command": command,
            "mcdreforged.command.command_source": command_source,
        })
        root.api, root.utils, root.command = api, utils, command
        # api.utils.serializer is a real path plugins import from.
        api.utils.serializer = serializer
        modules["mcdreforged.api.utils.serializer"] = serializer

        sys.modules.update(modules)
        return modules

    # ------------------------------------------------------------------ loading

    def load_all(self):
        """Load every plugin in the folder, in dependency order."""
        self.install_module()
        found = loader.report(self.plugins_dir)
        self.problems.extend(found["problems"])
        for plugin in found["order"]:
            self.load(plugin)
        return self.loaded

    def load(self, plugin):
        """Load one plugin, or record why it did not load.

        A plugin that blows up on import must not take the rest with it: the
        point of the whole exercise is knowing which ones work before a
        cutover, and one exception would hide the other twenty answers.
        """
        try:
            module = self._import(plugin)
        except ModuleNotFoundError as exc:
            missing = exc.name or "something"
            if missing.startswith("mcdreforged"):
                # A corner of MCDR this host does not provide yet, which is
                # our problem and worth naming as such.
                self.problems.append("%s needs %s, which this host does not "
                                     "provide yet" % (plugin.id, missing))
            else:
                # A plugin's own dependency from pip. Not a compatibility hole:
                # it would be missing under MCDR too, and telling somebody to
                # `pip install hjson` is a different fix from telling them the
                # plugin will not run.
                self.problems.append("%s needs the Python module %s, which is "
                                     "not installed (it is the plugin's own "
                                     "dependency)" % (plugin.id, missing))
            return None
        except Exception as exc:
            self.problems.append("%s did not load: %s: %s"
                                 % (plugin.id, type(exc).__name__, exc))
            return None

        interface = PluginServerInterface(self, plugin)
        entry = LoadedPlugin(plugin, module, interface)
        self.loaded[plugin.id] = entry

        unsupported = [name for name in dir(module)
                       if name.startswith("on_") and name not in EVENTS
                       and callable(getattr(module, name, None))
                       and name in loader.__dict__.get("MCDR_EVENTS", set())]
        for name in unsupported:
            self.problems.append("%s wants %s, which this host does not "
                                 "dispatch yet" % (plugin.id, name))

        handler = entry.handler("on_load")
        if handler:
            self._call(entry, "on_load", handler, interface, None)
        return entry

    def _import(self, plugin):
        """Import a plugin whatever shape it is in."""
        if plugin.shape == "file":
            return self._import_file(plugin.id, plugin.path)
        if plugin.shape == "folder":
            return self._import_package(plugin, plugin.path)
        return self._import_archive(plugin)

    def _import_file(self, name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def _import_package(self, plugin, root):
        """A folder plugin: its own directory goes on the path, then import.

        The entrypoint is a dotted module name relative to that folder, so
        `here.entry` means the `entry` module inside the `here` package.
        """
        root = str(Path(root).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module(plugin.entrypoint)

    def _import_archive(self, plugin):
        """A `.mcdr` or `.pyz` is a zip, and a zip can go straight on the path."""
        path = str(Path(plugin.path).resolve())
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        if path not in sys.path:
            sys.path.insert(0, path)
        entry = plugin.entrypoint
        if entry and any(n.startswith(entry.split(".")[0] + "/") for n in names):
            return importlib.import_module(entry)
        return importlib.import_module(plugin.id)

    def unload_all(self):
        for entry in list(self.loaded.values()):
            handler = entry.handler("on_unload")
            if handler:
                self._call(entry, "on_unload", handler, entry.interface)
        self.loaded.clear()

    # ---------------------------------------------------------------- dispatch

    # Names that only exist in the command tree, which this host does not have
    # yet. A plugin failing on one of these is not broken: it is waiting for a
    # piece of W4VE, and the difference matters to whoever reads the report.
    COMMAND_NAMES = ("Literal", "Text", "QuotableText", "GreedyText", "Integer",
                     "Float", "Number", "Boolean", "Enumeration",
                     "SimpleCommandBuilder", "CountingLiteral", "ArgumentNode",
                     "AbstractNode", "CommandContext", "CommandSource",
                     "ParseResult", "IllegalArgument", "InvalidEnumeration")

    def _call(self, entry, event, handler, *args):
        """Call one plugin's handler, and never let it take the host down."""
        try:
            handler(*args)
        except NameError as exc:
            wanted = str(exc).split("'")[1] if "'" in str(exc) else ""
            if wanted in self.COMMAND_NAMES:
                self.problems.append("%s uses the command tree (%s), which "
                                     "this host does not have yet"
                                     % (entry.id, wanted))
            else:
                self.problems.append("%s failed in %s: NameError: %s"
                                     % (entry.id, event, exc))
        except Exception as exc:
            self.problems.append("%s failed in %s: %s: %s"
                                 % (entry.id, event, type(exc).__name__, exc))
            entry.interface.logger.exception("error in %s" % event)

    def dispatch(self, event, *args):
        """Send an event to every plugin that implements it."""
        for entry in list(self.loaded.values()):
            handler = entry.handler(event)
            if handler:
                self._call(entry, event, handler, entry.interface, *args)

    def feed_line(self, line):
        """One line of server output, turned into the events plugins expect.

        This is the whole reason MCDR exists and the thing W4VE means to
        outgrow: the only way to know a player said something is to recognise
        the shape of a log line. RFC-0003 replaces it with typed events from a
        companion mod; until then, this is the ear.
        """
        info = parse_server_line(line)
        self.dispatch("on_info", info)
        if info.is_user:
            self.dispatch("on_user_info", info)
        return info

    def dispatch_custom(self, event, args):
        for entry in list(self.loaded.values()):
            handler = getattr(entry.module, "on_event", None)
            if handler:
                self._call(entry, "on_event", handler, entry.interface,
                           event, args)

    # ------------------------------------------------- what the interface asks

    def send_to_server(self, command):
        if self.server is None:
            self.log("[w4ve] would run: %s" % command)
            return False
        ok, _ = self.server.send(command)
        return ok

    def tellraw(self, target, text):
        payload = RTextBase.from_any(text).to_json_str()
        return self.send_to_server("tellraw %s %s" % (target, payload))

    def is_server_running(self):
        return bool(self.server and self.server.state in ("starting", "running"))

    def is_server_ready(self):
        return bool(self.server and self.server.ready)

    def is_rcon_running(self):
        if self.server is None:
            return False
        console, _ = self.server.rcon()
        if console is None:
            return False
        console.close()
        return True

    def rcon_query(self, command):
        if self.server is None:
            return None
        console, why = self.server.rcon()
        if console is None:
            return None
        try:
            return console.command(command)
        finally:
            console.close()

    def start_server(self):
        return self.server.start()[0] if self.server else False

    def stop_server(self):
        return self.server.stop()[0] if self.server else False

    def restart_server(self):
        if not self.server:
            return False
        self.server.stop()
        return self.server.start()[0]

    def wait_until_stop(self):
        if self.server:
            self.server._wait_for_exit(3600)

    def wait_for_start(self):
        return self.is_server_ready()

    def server_pid(self):
        return self.server.pid if self.server else None

    def server_information(self):
        return ServerInformation(self)

    # ------------------------------------------------------------- bookkeeping

    def register_help(self, plugin_id, prefix, message, permission=0):
        self._help.append({"plugin": plugin_id, "prefix": prefix,
                           "message": message, "permission": permission})

    @property
    def help_messages(self):
        return list(self._help)

    def register_command(self, plugin_id, root_node):
        entry = self.loaded.get(plugin_id)
        if entry is not None:
            entry.commands.append(root_node)
        # Dispatching these needs the command tree, which is the next piece.
        self.problems.append("%s registered a command, and this host cannot "
                             "route commands yet" % plugin_id)

    def register_listener(self, plugin_id, event, callback, priority=None):
        self.problems.append("%s registered a listener for %r, which this host "
                             "does not dispatch yet" % (plugin_id, event))

    def unregister_listener(self, plugin_id, event, callback):
        pass

    def register_translation(self, language, mapping):
        self.translations.setdefault(language, {}).update(mapping or {})

    def has_translation(self, key, language=None):
        return key in self.translations.get(language or self.language, {})

    def translate(self, key, *args, **kwargs):
        text = self.translations.get(self.language, {}).get(key)
        if text is None:
            return key
        try:
            return text.format(*args, **kwargs)
        except (IndexError, KeyError):
            return text

    def plugin_instance(self, plugin_id):
        entry = self.loaded.get(plugin_id)
        return entry.module if entry else None

    def plugin_ids(self):
        return sorted(self.loaded)

    def plugin_metadata(self, plugin_id):
        entry = self.loaded.get(plugin_id)
        return self.metadata_object(entry.plugin) if entry else None

    def metadata_object(self, plugin):
        return Metadata(plugin)

    def mcdr_config(self):
        return {"language": self.language, "working_directory": "server"}

    def is_on_executor_thread(self):
        return threading.current_thread() is self._executor

    def schedule_task(self, callback, block=False, timeout=None):
        if block:
            callback()
            return None
        thread = threading.Thread(target=callback, daemon=True)
        thread.start()
        return thread

    def permission_of(self, obj):
        return PermissionLevel.USER

    def plugin_command_source(self):
        return ConsoleCommandSource(self)


class ServerInformation:
    """What `get_server_information()` returns: attributes, not a dict.

    MinecraftDataAPI reads `.version` off it and compares it with `Version`, so
    a plain mapping would break it.
    """

    def __init__(self, host):
        self._host = host

    @property
    def version(self):
        return getattr(self._host.server, "mc_version", None)

    @property
    def ip(self):
        return None

    @property
    def port(self):
        return None


class Metadata:
    """What `get_self_metadata()` returns: the manifest, as attributes."""

    def __init__(self, plugin):
        self.id = plugin.id
        self.version = plugin.version
        self.name = plugin.name
        self.description = plugin.description
        self.author = plugin.meta.get("author")
        self.link = plugin.meta.get("link")
        self.dependencies = plugin.meta.get("dependencies") or {}

    def __repr__(self):
        return "<Metadata %s %s>" % (self.id, self.version)


class CommandSource:
    """Whoever a command came from, and where its reply goes."""

    def __init__(self, host):
        self._host = host

    @property
    def is_player(self):
        return False

    @property
    def is_console(self):
        return False

    def reply(self, text, **kwargs):
        raise NotImplementedError

    def get_permission_level(self):
        return PermissionLevel.USER

    def has_permission(self, level):
        return self.get_permission_level() >= level


class ConsoleCommandSource(CommandSource):

    @property
    def is_console(self):
        return True

    def reply(self, text, **kwargs):
        self._host.log(RTextBase.from_any(text).to_plain_text())

    def get_permission_level(self):
        return PermissionLevel.OWNER


class PlayerCommandSource(CommandSource):

    def __init__(self, host, player):
        CommandSource.__init__(self, host)
        self.player = player

    @property
    def is_player(self):
        return True

    def reply(self, text, **kwargs):
        self._host.tellraw(self.player, text)
