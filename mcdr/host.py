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

import contextlib
import importlib.util
import sys
import threading
import traceback
import types
import zipfile
from pathlib import Path, PurePosixPath

from mcdr import loader
from mcdr.command import (AbstractNode, CommandError, CommandContext,  # noqa: F401
                          UnknownArgument, UnknownCommand, UnknownRootArgument)
from mcdr.event import (LiteralEvent, MCDREvent, MCDRPluginEvents,
                        PluginEvent, name_of as event_name)
from mcdr.info import Info, parse_server_line, player_event
from mcdr.interface import PermissionLevel, PluginServerInterface, ServerInterface
from mcdr.misc import FunctionThread, Version, VersionRequirement, new_thread
from mcdr.permission import PermissionFile
from mcdr.rcon_api import RconConnection
from mcdr.serializer import Serializable
from mcdr.serializer import deserialize as _module_deserialize
from mcdr.serializer import serialize as _module_serialize
from mcdr.text import (RAction, RColor, RStyle, RText, RTextBase, RTextList,
                       RTextMCDRTranslation, RTextTranslation)

# The events this host dispatches. Measured, not guessed: these are the nine
# the plugins MineWave runs actually implement.
EVENTS = ("on_load", "on_unload", "on_info", "on_user_info",
          "on_player_joined", "on_player_left",
          "on_server_start", "on_server_startup", "on_server_stop",
          # The wrapper's own lifetime, which under MCDR is MCDR starting and
          # stopping and here is the guardian doing it. A plugin uses these to
          # tell "the server restarted" apart from "everything restarted".
          "on_mcdr_start", "on_mcdr_stop")

# Everything MCDR names as an event, including the ones this host does not
# raise. Used to tell "a corner of MCDR is missing" apart from "this is a
# plugin's own event", which look identical until you have this list.
MCDR_EVENT_NAMES = frozenset(
    event.default_method_name for event in vars(MCDRPluginEvents).values()
    if isinstance(event, MCDREvent))

# API compatibility is frozen at MCDR 2.x on purpose, per W4VE-DISENO section
# 5: a moving target cannot be a compatibility promise.
CLAIMED_MCDR_VERSION = "2.15.5"


class Logger:
    """What `server.logger` is. Named, so an operator knows who said it."""

    def __init__(self, host, plugin_id=None):
        self.host = host
        self.plugin_id = plugin_id
        self.file = None

    def _say(self, level, message):
        who = self.plugin_id or "mcdr"
        line = "[%s] [%s] %s" % (who, level, message)
        self.host.log(line)
        if getattr(self, "file", None):
            try:
                self.file.write(line + "\n")
            except (OSError, ValueError):
                self.file = None

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

    def set_file(self, path):
        """Also write to a file of the plugin's choosing.

        Task asks for this the moment it loads (`inject_set_file_method`), so
        a logger without it is a plugin that will not import. Failing to open
        the file is not worth refusing to load over: the plugin keeps its
        console logging and is told why the file is not there.
        """
        try:
            folder = Path(path).parent
            if str(folder):
                folder.mkdir(parents=True, exist_ok=True)
            self.file = open(str(path), "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self.file = None
            self._say("WARN", "could not open the log file %s (%s)" % (path, exc))
        return self

    def close_file(self):
        if getattr(self, "file", None):
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None


class LoadedPlugin:
    """A plugin that is in memory, with everything it put into the host.

    Everything it registered is kept here rather than only in the host's own
    lists, because that is what makes unloading one plugin possible: its
    commands, its help lines, its listeners and its translations can be taken
    back out without disturbing anybody else's.
    """

    def __init__(self, plugin, module, interface):
        self.plugin = plugin
        self.module = module
        self.interface = interface
        self.help_messages = []
        self.commands = []
        self.listeners = {}          # event -> [callback]
        self.translations = {}       # language -> {key: text}
        self.stamp = file_stamp(plugin.path)

    @property
    def id(self):
        return self.plugin.id

    def handler(self, event):
        return getattr(self.module, event, None)


def file_stamp(path):
    """A cheap fingerprint of a plugin on disk: what changed since last time.

    A folder plugin is more than one file, so the newest mtime under it is the
    honest answer for a folder. Content hashing would be exact and would also
    mean reading every plugin on every check; a plugin that is edited without
    its mtime moving is not a case worth paying for on every tick.
    """
    path = Path(path)
    try:
        if path.is_dir():
            newest, count = 0.0, 0
            for child in path.rglob("*"):
                if child.is_file() and "__pycache__" not in child.parts:
                    newest = max(newest, child.stat().st_mtime)
                    count += 1
            return (newest, count)
        stat = path.stat()
        return (stat.st_mtime, stat.st_size)
    except OSError:
        return None


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
        # Read from the same `permission.yml` MCDR used, in the same place, so
        # a cutover inherits the staff list instead of asking for it again.
        self.permissions = PermissionFile(
            self.server_root / "permission.yml",
            on_problem=lambda line: self.log("[w4ve] permission.yml: " + line))
        # The host's own commands: `!!w4ve`, `!!MCDR` and `!!help`. Built here
        # so they exist before any plugin loads, which is what makes them
        # usable to fix a server where the plugins are what is broken.
        from mcdr.builtin import Builtins

        self.builtins = Builtins(self)
        self._executor = threading.current_thread()
        self._loading = None
        self._pending_translations = {}
        self._help = []
        self.commands = []               # (plugin id, root node)

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
            "serialize": _module_serialize, "deserialize": _module_deserialize,
            # the server
            "ServerInterface": ServerInterface,
            "PluginServerInterface": PluginServerInterface,
            "PermissionLevel": PermissionLevel,
            "Info": Info,
            # Found by loading real plugins: DigAll needs the decorator,
            # MinecraftDataAPI needs to compare versions.
            "new_thread": new_thread,
            # Plugin to plugin events. QuickBackupM and TimedQBM will not even
            # import without these.
            "PluginEvent": PluginEvent, "LiteralEvent": LiteralEvent,
            "MCDREvent": MCDREvent, "MCDRPluginEvents": MCDRPluginEvents,
            # Task type hints on the logger class it is given.
            "MCDReforgedLogger": Logger,
            # MultiRconAPI subclasses this one.
            "RconConnection": RconConnection,
            "FunctionThread": FunctionThread,
            "Version": Version,
            "VersionRequirement": VersionRequirement,
        }
        # The command tree, which is the half of the API plugins build their
        # commands out of. Imported here rather than at the top so the module
        # list stays in one readable place.
        from mcdr import command as command_module

        for name in dir(command_module):
            value = getattr(command_module, name)
            if name[0].isupper() and isinstance(value, type):
                names.setdefault(name, value)

        # The command sources live here rather than in the tree, because they
        # need the host to answer with. Plugins annotate their callbacks with
        # them (`def cmd_status(source: CommandSource)`), so they have to be
        # importable even though nothing constructs one from a plugin.
        names.update({
            "CommandSource": CommandSource,
            "ConsoleCommandSource": ConsoleCommandSource,
            "PlayerCommandSource": PlayerCommandSource,
            "PluginCommandSource": ConsoleCommandSource,
        })

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
        from mcdr.serializer import deserialize as _deserialize
        from mcdr.serializer import serialize as _serialize

        serializer = types.ModuleType("mcdreforged.utils.serializer")
        serializer.Serializable = Serializable
        serializer.serialize = _serialize
        serializer.deserialize = _deserialize
        utils = types.ModuleType("mcdreforged.utils")
        utils.__path__ = []
        utils.serializer = serializer
        utils.Serializable = Serializable
        utils.serialize = _serialize
        utils.deserialize = _deserialize
        command = types.ModuleType("mcdreforged.command")
        command.__path__ = []
        command_source = types.ModuleType("mcdreforged.command.command_source")
        # Filled in, not left empty: Task imports CommandSource straight from
        # this path rather than from `api.all`, and an empty module is an
        # ImportError with a confusing message.
        for name in ("CommandSource", "ConsoleCommandSource",
                     "PlayerCommandSource", "PluginCommandSource"):
            setattr(command_source, name, names[name])
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

    def load_translations(self, plugin):
        """Read a plugin's `lang/` files, so its messages are words.

        Without this every message a plugin sends comes out as the key it was
        looked up by, which is technically working and useless to read: real
        DigAll answers `!!dig-all` with `dig_all.command.sidebar_set` instead
        of a sentence. The files are YAML nested by key; the subset used is
        flat enough to read without a YAML library, which W4VE does not have
        and will not add for this.

        Both shapes are read. A plugin shipped as a folder keeps `lang/` on
        disk; a `.mcdr` or `.pyz` keeps it inside the zip, and that is how
        every plugin actually installed on MineWave ships.
        """
        loaded = 0
        for language, suffix, read in self._language_files(plugin):
            if suffix in (".yml", ".yaml"):
                mapping = _read_simple_yaml_text(read())
            elif suffix == ".json":
                import json as _json

                try:
                    mapping = _flatten(_json.loads(read()))
                except ValueError:
                    continue
            else:
                continue
            if mapping:
                self.register_translation(language, mapping, plugin.id)
                loaded += len(mapping)
        return loaded

    def _language_files(self, plugin):
        """(language, suffix, read) for every language file a plugin carries."""
        path = Path(plugin.path)
        if plugin.shape == "folder":
            folder = path / "lang"
            if not folder.is_dir():
                return
            for child in sorted(folder.iterdir()):
                if child.is_file():
                    yield (child.stem, child.suffix,
                           lambda c=child: c.read_text("utf-8"))
            return
        if plugin.shape != "archive":
            return
        try:
            archive = zipfile.ZipFile(str(path))
        except (OSError, zipfile.BadZipFile):
            return
        with archive:
            for name in sorted(archive.namelist()):
                inside = PurePosixPath(name)
                # `lang/en_us.yml` at the root, and the same folder nested
                # under the package directory: both are shipped in the wild.
                if inside.parent.name != "lang" or not inside.suffix:
                    continue
                try:
                    data = archive.read(name)
                except KeyError:
                    continue
                yield (inside.stem, inside.suffix,
                       lambda d=data: d.decode("utf-8", "replace"))

    def load(self, plugin):
        """Load one plugin, or record why it did not load.

        A plugin that blows up on import must not take the rest with it: the
        point of the whole exercise is knowing which ones work before a
        cutover, and one exception would hide the other twenty answers.
        """
        # Set for the duration of the import: a plugin that asks
        # `ServerInterface.si().as_plugin_server_interface()` at module level,
        # the way Task does, has to be answered with its own interface.
        self._loading = PluginServerInterface(self, plugin)
        # ⚠️ Before the import, not after. A plugin is allowed to translate at
        # module level, and QuickBackupM does: its help message is built while
        # the module is executing. Loading its language files afterwards means
        # that message is the raw key forever, and no amount of reloading
        # fixes it because the damage is done at import.
        self._pending_translations = {}
        self.load_translations(plugin)
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
            self._drop_pending_translations()
            return None
        except Exception as exc:
            self.problems.append("%s did not load: %s: %s"
                                 % (plugin.id, type(exc).__name__, exc))
            self._drop_pending_translations()
            return None

        interface = self._loading
        self._loading = None
        entry = LoadedPlugin(plugin, module, interface)
        self.loaded[plugin.id] = entry
        # The language files were read before the import; hand them to the
        # entry now, so unloading this plugin takes them back out again.
        entry.translations = self._pending_translations
        self._pending_translations = {}

        # Which events this plugin implements that this host never raises.
        # This used to look them up in `loader.MCDR_EVENTS`, which does not
        # exist, so the list was always empty and the check always passed:
        # exactly the kind of silence a compatibility report must not have.
        unsupported = [name for name in dir(module)
                       if name.startswith("on_") and name not in EVENTS
                       and callable(getattr(module, name, None))
                       and name in MCDR_EVENT_NAMES]
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

        The cache is cleared for that name first. Two plugins with the same id
        in different folders is not hypothetical, it is what a mirror and a
        production server look like from one machine, and without this the
        second one silently gets the first one's code. It is also what a hot
        reload will need.
        """
        root = str(Path(root).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        self._forget(plugin.entrypoint)
        return importlib.import_module(plugin.entrypoint)

    @staticmethod
    def _forget(name):
        """Drop a module and everything under it out of the import cache."""
        head = name.split(".")[0]
        for cached in [n for n in sys.modules
                       if n == head or n.startswith(head + ".")]:
            sys.modules.pop(cached, None)

    def _import_archive(self, plugin):
        """A `.mcdr` or `.pyz` is a zip, and a zip can go straight on the path."""
        path = str(Path(plugin.path).resolve())
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        if path not in sys.path:
            sys.path.insert(0, path)
        entry = plugin.entrypoint
        if entry and any(n.startswith(entry.split(".")[0] + "/") for n in names):
            self._forget(entry)
            return importlib.import_module(entry)
        self._forget(plugin.id)
        return importlib.import_module(plugin.id)

    def unload_all(self):
        for plugin_id in list(self.loaded):
            self.unload(plugin_id)
        self.loaded.clear()
        self.commands = []
        self._help = []
        self.translations = {}

    # ------------------------------------------------------------- hot reload

    def unload(self, plugin_id):
        """Take one plugin out, leaving everybody else exactly as they were.

        This is the half of a reload that is easy to get wrong. A plugin is
        not only its module: it is the commands it registered, the help lines
        it added, the listeners it handed over and the translations it
        brought. All four have to come back out, or the next load ends up with
        a server answering `!!dig-all` twice and a help screen listing a
        plugin that is gone.
        """
        entry = self.loaded.get(plugin_id)
        if entry is None:
            return False
        handler = entry.handler("on_unload")
        if handler:
            self._call(entry, "on_unload", handler, entry.interface)
        self.commands = [(pid, node) for pid, node in self.commands
                         if pid != plugin_id]
        self._help = [item for item in self._help
                      if item.get("plugin") != plugin_id]
        del self.loaded[plugin_id]
        self._rebuild_translations()
        # Out of the import cache, or loading it again gets the code it had
        # before, and out of `sys.path` too: a zip that stays on the path is
        # still the old zip even after the file is replaced.
        self._forget(entry.plugin.entrypoint or plugin_id)
        self._forget_path(entry.plugin)
        return True

    def _forget_path(self, plugin):
        """Undo what importing this plugin did to `sys.path` and the caches."""
        path = str(Path(plugin.path).resolve())
        root = str(Path(plugin.path).resolve()) if plugin.shape == "folder" else path
        for candidate in {path, root}:
            while candidate in sys.path:
                sys.path.remove(candidate)
        # zipimport keeps its own directory listing per archive path, so a
        # rebuilt `.mcdr` at the same name would import its old contents.
        try:
            import zipimport

            zipimport._zip_directory_cache.pop(path, None)
        except Exception:
            pass
        importlib.invalidate_caches()

    def dependents(self, plugin_id):
        """Loaded plugins that declare a dependency on this one.

        Reloading a plugin leaves anybody holding a reference to its old
        module (`get_plugin_instance` hands the module out) pointing at code
        that is no longer loaded. Reloading them too is the only way that
        stays honest.
        """
        out = []
        for entry in self.loaded.values():
            if plugin_id in (entry.plugin.dependencies or {}):
                out.append(entry.id)
        return sorted(out)

    def _find_on_disk(self, plugin_id=None, path=None):
        """Re-read the plugins folder and pick one out of it.

        Deliberately re-read rather than remembered: a reload has to pick up a
        plugin whose metadata changed, including its version and its
        dependencies, and the copy in memory is by definition the old one.
        """
        for plugin in loader.discover(self.plugins_dir):
            if path is not None and Path(plugin.path) == Path(path):
                return plugin
            if plugin_id is not None and plugin.id == plugin_id:
                return plugin
        return None

    def reload(self, plugin_id, cascade=True):
        """Unload a plugin and load whatever is on disk under that id now.

        Returns (ok, problems), where problems are the ones this reload added.
        If it fails to come back, it stays unloaded and says so: a half loaded
        plugin that answers some of its commands is worse than one that is
        plainly gone.
        """
        before = len(self.problems)
        entry = self.loaded.get(plugin_id)
        followers = self.dependents(plugin_id) if (entry and cascade) else []
        if entry is None and self._find_on_disk(plugin_id=plugin_id) is None:
            self.problems.append("no plugin called %s is loaded or in %s"
                                 % (plugin_id, self.plugins_dir))
            return False, self.problems[before:]

        for follower in followers:
            self.unload(follower)
        self.unload(plugin_id)

        plugin = self._find_on_disk(plugin_id=plugin_id)
        if plugin is None:
            self.problems.append("%s is no longer in %s, so it stays unloaded"
                                 % (plugin_id, self.plugins_dir))
            return False, self.problems[before:]
        ok = self.load(plugin) is not None
        for follower in followers:
            found = self._find_on_disk(plugin_id=follower)
            if found is not None:
                self.load(found)
        return ok, self.problems[before:]

    def load_new(self, path):
        """Load a plugin file that was not loaded before."""
        before = len(self.problems)
        plugin = self._find_on_disk(path=path)
        if plugin is None:
            self.problems.append("%s is not a plugin this host can read"
                                 % Path(path).name)
            return False, self.problems[before:]
        if plugin.id in self.loaded:
            self.problems.append("%s is already loaded; reload it instead"
                                 % plugin.id)
            return False, self.problems[before:]
        ok = self.load(plugin) is not None
        return ok, self.problems[before:]

    def changed_on_disk(self):
        """What moved since the last look: (new, changed, gone).

        The three cases an operator actually has: they dropped a jar in, they
        edited one, or they deleted one. Answering all three from one scan is
        what makes `refresh` a single command instead of a decision.
        """
        found = {}
        for plugin in loader.discover(self.plugins_dir):
            if plugin.id:
                found.setdefault(plugin.id, plugin)
        new = [plugin for pid, plugin in sorted(found.items())
               if pid not in self.loaded]
        changed, gone = [], []
        for plugin_id, entry in sorted(self.loaded.items()):
            plugin = found.get(plugin_id)
            if plugin is None:
                gone.append(plugin_id)
            elif file_stamp(plugin.path) != entry.stamp:
                changed.append(plugin_id)
        return new, changed, gone

    def refresh_changed_plugins(self):
        """Load what appeared, reload what changed, drop what was deleted."""
        before = len(self.problems)
        new, changed, gone = self.changed_on_disk()
        for plugin_id in gone:
            self.unload(plugin_id)
        for plugin_id in changed:
            self.reload(plugin_id, cascade=False)
        for plugin in new:
            self.load(plugin)
        return {"loaded": [p.id for p in new], "reloaded": changed,
                "unloaded": gone, "problems": self.problems[before:]}

    def refresh_all_plugins(self):
        """Unload everything and load the folder again, from scratch."""
        before = len(self.problems)
        self.unload_all()
        self.load_all()
        return {"loaded": sorted(self.loaded), "problems": self.problems[before:]}

    def disable_plugin(self, plugin_id):
        """Unload it and rename it so it does not come back on next start.

        Same convention MCDR uses, and the reason it is a rename rather than a
        list somewhere: the state is visible in the folder, which is where
        somebody looks when a plugin is missing.
        """
        entry = self.loaded.get(plugin_id)
        plugin = entry.plugin if entry else self._find_on_disk(plugin_id=plugin_id)
        if plugin is None:
            return False, "no plugin called %s" % plugin_id
        path = Path(plugin.path)
        self.unload(plugin_id)
        target = path.with_name(path.name + ".disabled")
        if target.exists():
            return False, "%s already exists" % target.name
        try:
            path.rename(target)
        except OSError as exc:
            return False, "could not rename %s (%s)" % (path.name, exc)
        return True, target.name

    def disabled_plugins(self):
        """The `.disabled` files sitting in the folder, ready to come back."""
        if not self.plugins_dir.is_dir():
            return []
        return sorted(entry.name for entry in self.plugins_dir.iterdir()
                      if entry.name.endswith(".disabled"))

    def enable_plugin(self, name):
        """Put a disabled plugin back and load it."""
        path = self.plugins_dir / name
        if not path.name.endswith(".disabled"):
            path = path.with_name(path.name + ".disabled")
        if not path.exists():
            return False, "%s is not in %s" % (path.name, self.plugins_dir)
        target = path.with_name(path.name[:-len(".disabled")])
        if target.exists():
            return False, "%s is already there" % target.name
        try:
            path.rename(target)
        except OSError as exc:
            return False, "could not rename %s (%s)" % (path.name, exc)
        ok, problems = self.load_new(target)
        return ok, (problems[0] if problems else target.name)

    # ---------------------------------------------------------------- dispatch

    def _call(self, entry, event, handler, *args):
        """Call one plugin's handler, and never let it take the host down."""
        try:
            handler(*args)
        except NameError as exc:
            # A name the fake `mcdreforged` module does not carry yet. Saying
            # which one turns "it does not work" into a one line fix.
            wanted = str(exc).split("'")[1] if "'" in str(exc) else "?"
            self.problems.append("%s uses %s, which this host does not provide "
                                 "yet" % (entry.id, wanted))
        except Exception as exc:
            self.problems.append("%s failed in %s: %s: %s"
                                 % (entry.id, event, type(exc).__name__, exc))
            entry.interface.logger.exception("error in %s" % event)

    def dispatch(self, event, *args):
        """Send an event to every plugin that implements it.

        Both ways of asking for an event get it here: a function named after
        it in the plugin module, and a callback handed to
        `register_event_listener`.
        """
        for entry in list(self.loaded.values()):
            handler = entry.handler(event)
            if handler:
                self._call(entry, event, handler, entry.interface, *args)
            for callback in list(entry.listeners.get(event) or []):
                self._call(entry, event, callback, entry.interface, *args)

    def feed_line(self, line):
        """One line of server output, turned into the events plugins expect.

        This is the whole reason MCDR exists and the thing W4VE means to
        outgrow: the only way to know a player said something is to recognise
        the shape of a log line. RFC-0003 replaces it with typed events from a
        companion mod; until then, this is the ear.
        """
        info = parse_server_line(line)
        self.dispatch("on_info", info)
        moved = player_event(info)
        if moved:
            what, player = moved
            if what == "joined":
                self.dispatch("on_player_joined", player, info)
            else:
                self.dispatch("on_player_left", player)
        if info.is_user:
            self.dispatch("on_user_info", info)
            # A registered command is offered the line after the events, the
            # way MCDR does it: a plugin listening on on_info sees everything,
            # and a command only fires on what it claims.
            self.run_command(self.source_for(info), info.content)
        return info

    def source_for(self, info):
        """Who is speaking, as something a plugin can reply to."""
        if info.is_player:
            return PlayerCommandSource(self, info.player)
        return ConsoleCommandSource(self)

    def dispatch_custom(self, event, args):
        """`server.dispatch_event(SOME_EVENT, (a, b))`: one plugin telling
        another something happened.

        This is how QuickBackupM tells TimedQBM a backup finished. The
        listeners are called as `callback(server, *args)`, which is the shape
        MCDR documents and the shape those plugins are written against.
        """
        name = event_name(event)
        args = tuple(args or ())
        for entry in list(self.loaded.values()):
            for callback in list(entry.listeners.get(name) or []):
                self._call(entry, name, callback, entry.interface, *args)
            # A plugin may also just name the function after the event, the
            # same way it does for the built-in ones.
            handler = getattr(entry.module, name, None) if name.startswith("on_") \
                else None
            if handler:
                self._call(entry, name, handler, entry.interface, *args)

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
        item = {"plugin": plugin_id, "prefix": prefix,
                "message": message, "permission": permission}
        self._help.append(item)
        entry = self.loaded.get(plugin_id)
        if entry is not None:
            entry.help_messages.append(item)

    @property
    def help_messages(self):
        return list(self._help)

    def help_for(self, source):
        """The help lines this source is allowed to see, in order.

        Filtering by permission is not decoration: half the lines in `!!help`
        on a real server are admin commands, and showing a player commands
        they cannot run is how a help screen stops being read.
        """
        level = source.get_permission_level() if source else 0
        return [item for item in self._help
                if level >= (item.get("permission") or 0)]

    def register_command(self, plugin_id, root_node):
        entry = self.loaded.get(plugin_id)
        if entry is None:
            return
        entry.commands.append(root_node)
        self.commands.append((plugin_id, root_node))

    def run_command(self, source, line):
        """Give a command line to whichever plugin claims its first word.

        Returns True if a plugin ran something. A tree that raises an error it
        did not handle is reported against the plugin that owns it, and the
        others still get their turn, because one plugin's bad branch must not
        eat somebody else's command.
        """
        line = (line or "").strip()
        if not line:
            return False
        # The host's own commands go first. A plugin that registered `!!w4ve`
        # would otherwise be able to take over plugin management, and the one
        # command that has to keep working is the one that unloads a plugin
        # that is misbehaving.
        if self.builtins.run(source, line):
            return True
        ran = False
        for plugin_id, root in list(self.commands):
            if not root._could_match(line):
                continue
            try:
                if root.execute(source, line):
                    ran = True
            except CommandError as error:
                if not error.is_handled():
                    self.logger_for(plugin_id).warning(
                        "%s: %s" % (type(error).__name__,
                                    error.to_mcdr_string()))
                ran = True
            except Exception:
                self.logger_for(plugin_id).exception(
                    "error running %r" % line)
                ran = True
        return ran

    def register_listener(self, plugin_id, event, callback, priority=None):
        """A listener added at runtime instead of by naming a function.

        Takes what MCDR takes: an event object, its id, or the `on_...` name.
        All three land in the same bucket, so a listener registered as
        `MCDRPluginEvents.PLAYER_JOINED` fires for the same thing a function
        called `on_player_joined` does.

        An event this host does not raise is accepted and reported rather than
        refused. Two different things live in that bucket: a corner of MCDR
        that is missing, and a plugin's own event that another plugin will
        raise, which is not a hole at all. Only the first is a problem.
        """
        entry = self.loaded.get(plugin_id)
        if entry is None:
            return
        name = event_name(event)
        entry.listeners.setdefault(name, []).append(callback)
        if name not in EVENTS and name in MCDR_EVENT_NAMES:
            self.problems.append("%s listens for %r, which this host does not "
                                 "dispatch yet" % (plugin_id, name))

    def unregister_listener(self, plugin_id, event, callback):
        entry = self.loaded.get(plugin_id)
        if entry is None:
            return
        listeners = entry.listeners.get(event_name(event)) or []
        if callback in listeners:
            listeners.remove(callback)

    def register_translation(self, language, mapping, plugin_id=None):
        self.translations.setdefault(language, {}).update(mapping or {})
        entry = self.loaded.get(plugin_id)
        if entry is not None:
            entry.translations.setdefault(language, {}).update(mapping or {})
        elif plugin_id is not None:
            # Registered before the plugin finished loading, which is the
            # normal case for its own language files.
            self._pending_translations.setdefault(language, {}).update(
                mapping or {})

    def _drop_pending_translations(self):
        """Forget the language files of a plugin that did not load."""
        self._pending_translations = {}
        self._rebuild_translations()

    def _rebuild_translations(self):
        """Recompute the table from the plugins that are still loaded.

        Cheaper to think about than removing keys one at a time: two plugins
        can register the same key, and subtracting one of them has to leave
        the other one's value in place.
        """
        self.translations = {}
        for entry in self.loaded.values():
            for language, mapping in entry.translations.items():
                self.translations.setdefault(language, {}).update(mapping)

    def has_translation(self, key, language=None):
        return key in self.translations.get(language or self.language, {})

    def translate(self, key, *args, **kwargs):
        table = self.translations.get(self.language) or {}
        text = table.get(key)
        if text is None and self.language != "en_us":
            text = (self.translations.get("en_us") or {}).get(key)
        if text is None:
            # Saying the key is the honest failure: it is searchable, and it
            # tells whoever reads it which language file is missing a line.
            return key
        try:
            return text.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
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
        """The level of whoever this is: a name, a source, or an Info.

        MCDR takes any of the three and plugins pass all three, so guessing
        from the shape is the compatible answer. The console is owner without
        asking the file: it is not a player and has no name to look up.
        """
        if obj is None:
            return self.permissions.default_level
        if isinstance(obj, CommandSource):
            return obj.get_permission_level()
        if isinstance(obj, Info):
            if obj.is_player:
                return self.permissions.level_of(obj.player)
            return PermissionLevel.OWNER
        if isinstance(obj, int):
            return max(0, min(4, obj))
        return self.permissions.level_of(obj)

    def set_permission(self, player, level):
        """Move a player to a level and write it down. Returns the level."""
        from mcdr.permission import level_from

        level = level_from(level)
        self.permissions.set_level(player, level)
        return level

    def reload_permissions(self):
        self.permissions.load()
        return True

    def plugin_command_source(self):
        return ConsoleCommandSource(self)

    def current_plugin_interface(self):
        """The interface of the plugin being loaded right now, if any."""
        return getattr(self, "_loading", None)


def _flatten(data, prefix=""):
    """`{'a': {'b': 'x'}}` becomes `{'a.b': 'x'}`, which is how keys are used."""
    out = {}
    for key, value in (data or {}).items():
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _read_simple_yaml(path):
    return _read_simple_yaml_text(Path(path).read_text(encoding="utf-8"))


def _read_simple_yaml_text(text):
    """Enough YAML for a language file, which is all nesting and strings.

    Not a YAML parser and not pretending to be one: keys, two space nesting,
    quoted or bare scalars, and block scalars. A language file that needs more
    than this is a language file doing something a language file should not.

    Takes text rather than a path because half the language files it reads are
    inside a zip and never touch the disk.

    ⚠️ Block scalars (`key: |`) are not an optional nicety. Every MCDR plugin
    of any size keeps its help screen in one, so a reader without them answers
    `!!stats` with a single `|` character, which is what real StatsHelper did
    here before this understood them.
    """
    out, stack = {}, []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if value[:1] in ("|", ">"):
            block, index = _read_block(lines, index, indent)
            folded = value[:1] == ">"
            chomp = value[1:2]
            text_value = " ".join(block) if folded else "\n".join(block)
            if chomp != "-" and not folded:
                text_value += "\n"
            out[".".join([k for _, k in stack] + [key])] = text_value
            continue
        if not value:
            stack.append((indent, key))
            continue
        if value[:1] in ('"', "'") and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
            # The files escape quotes and newlines the way JSON does.
            value = value.replace('\\"', '"').replace("\\n", "\n")
        path_key = ".".join([k for _, k in stack] + [key])
        out[path_key] = value
    return out


def _read_block(lines, index, key_indent):
    """The indented body of a `key: |` block, and where it ends."""
    body, block_indent = [], None
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            body.append("")
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= key_indent:
            break
        if block_indent is None:
            block_indent = indent
        body.append(raw[block_indent:])
        index += 1
    while body and not body[-1]:
        body.pop()
    return body, index


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
        # Flattened for anybody reading `.description`, which is a string
        # under MCDR too; the raw value is kept so `get_description(language)`
        # can still answer per language.
        self.description = plugin.description
        self._descriptions = plugin.meta.get("description")
        self.author = plugin.meta.get("author")
        self.link = plugin.meta.get("link")
        self.dependencies = plugin.meta.get("dependencies") or {}

    def get_description(self, language=None):
        """The description, in a language if the manifest offers a choice.

        A manifest may write `description` as a plain string or as a mapping
        of language to string. TimedQBM asks for it with a language on load,
        so both shapes have to answer.
        """
        raw = self._descriptions
        if isinstance(raw, dict):
            if language and language in raw:
                return raw[language]
            for fallback in ("en_us", "zh_cn"):
                if fallback in raw:
                    return raw[fallback]
            return next(iter(raw.values()), "")
        return self.description or ""

    def get_name(self):
        return self.name or self.id

    def get_description_rtext(self, language=None):
        return RText(self.get_description(language))

    def __repr__(self):
        return "<Metadata %s %s>" % (self.id, self.version)


class Preference:
    """`source.get_preference()`: what MCDR knows about somebody's taste."""

    def __init__(self, language):
        self.language = language

    def __repr__(self):
        return "<Preference language=%r>" % self.language


@contextlib.contextmanager
def _language_context(host, language):
    previous, host.language = host.language, language or host.language
    try:
        yield
    finally:
        host.language = previous


class CommandSource:
    """Whoever a command came from, and where its reply goes."""

    def __init__(self, host):
        self._host = host

    def get_server(self):
        """The interface, so a callback can reach the server from a source.

        DigAll does `source.get_server()` inside a command rather than closing
        over the interface it was handed in on_load, which is common enough
        that a source without this is a source that does not work.
        """
        return ServerInterface._instance

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

    def get_preference(self):
        """What this source prefers. Today that is only a language."""
        return Preference(self._host.language)

    def preferred_language_context(self):
        """`with source.preferred_language_context():` around translations.

        Found by running real StatsHelper: it wraps its whole help screen in
        this, so a source without it does not print a broken help screen, it
        raises AttributeError and prints nothing.

        W4VE has one language, the host's, so this sets nothing and restores
        nothing. It is still a real context manager rather than a stub that
        returns None, because the plugin uses it in a `with` statement and
        None is not a context manager.
        """
        return _language_context(self._host, self.get_preference().language)


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

    def get_permission_level(self):
        return self._host.permissions.level_of(self.player)

    def __repr__(self):
        return "<player %s>" % self.player
