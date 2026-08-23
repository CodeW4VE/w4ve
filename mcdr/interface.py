"""ServerInterface: the object a plugin is handed, and everything it can ask.

This is the surface `docs/mcdr-compat-surface.md` measured. Twelve of its
methods are asked for by more than one of the plugins MineWave runs, and three
by all of them: `load_config_simple`, `register_command` and
`register_help_message`.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.

Underneath, every one of these calls lands on the W4VE guardian of RFC-0001.
That is the whole trick of the compatibility layer: a plugin thinks it is
talking to MCDR, and what actually happens is that W4VE owns the process,
writes the console and holds the state. The plugin does not have to know, and
one day it will not have to be there.
"""

import json
from pathlib import Path

from mcdr.serializer import Serializable
from mcdr.text import RTextBase


class PermissionLevel:
    GUEST = 0
    USER = 1
    HELPER = 2
    ADMIN = 3
    OWNER = 4

    NAMES = {0: "guest", 1: "user", 2: "helper", 3: "admin", 4: "owner"}

    @classmethod
    def from_value(cls, value):
        if isinstance(value, int):
            return max(0, min(4, value))
        for level, name in cls.NAMES.items():
            if name == str(value).lower():
                return level
        return cls.USER


class ServerInterface:
    """What a plugin holds. One per host, shared by every plugin."""

    # Set by the host when it installs the module. Plugins reach for it from
    # code that was never handed an interface: DigAll's `tr()` helper does it
    # at module level, and MinecraftDataAPI asks the server its version that
    # way.
    _instance = None

    def __init__(self, host):
        self._host = host

    @classmethod
    def si(cls):
        return cls._instance

    @classmethod
    def get_instance(cls):
        return cls._instance

    @classmethod
    def si_opt(cls):
        return cls._instance

    @classmethod
    def psi(cls):
        return cls._instance

    @classmethod
    def psi_opt(cls):
        return cls._instance

    # ------------------------------------------------------------ the server

    def execute(self, command, encoding=None):
        """Send a command to the server console."""
        self._host.send_to_server(command)

    def say(self, text):
        """Say something to everyone, as the server."""
        self._host.tellraw("@a", text)

    def tell(self, player, text):
        self._host.tellraw(player, text)

    def broadcast(self, text, encoding=None):
        """Everyone in game and whoever is watching the console."""
        self._host.tellraw("@a", text)
        self._host.log(RTextBase.from_any(text).to_plain_text())

    def reply(self, info, text, encoding=None):
        if getattr(info, "is_player", False):
            self.tell(info.player, text)
        else:
            self._host.log(RTextBase.from_any(text).to_plain_text())

    def start(self):
        return self._host.start_server()

    def stop(self):
        return self._host.stop_server()

    def stop_exit(self):
        return self._host.stop_server()

    def restart(self):
        return self._host.restart_server()

    def wait_until_stop(self):
        return self._host.wait_until_stop()

    def wait_for_start(self):
        return self._host.wait_for_start()

    def is_server_running(self):
        return self._host.is_server_running()

    def is_server_startup(self):
        return self._host.is_server_ready()

    def is_rcon_running(self):
        return self._host.is_rcon_running()

    def rcon_query(self, command):
        """Run a command over RCON and return what came back, or None.

        Here uses this to read a player's position without touching the
        console, which is the difference between a clean answer and parsing it
        back out of the log.
        """
        return self._host.rcon_query(command)

    def get_server_pid(self):
        return self._host.server_pid()

    def get_server_information(self):
        return self._host.server_information()

    # ----------------------------------------------------------- the plugins

    def get_plugin_instance(self, plugin_id):
        return self._host.plugin_instance(plugin_id)

    def get_plugin_list(self):
        return self._host.plugin_ids()

    def get_plugin_metadata(self, plugin_id):
        return self._host.plugin_metadata(plugin_id)

    def dispatch_event(self, event, args):
        self._host.dispatch_custom(event, args)

    # ------------------------------------------------------------ hot reload

    def reload_plugin(self, plugin_id):
        """Reload a plugin without stopping the server.

        ⚠️ A plugin that reloads *itself* from inside one of its own commands
        is asking to be unloaded while its code is running. It works, because
        Python keeps the running function alive, but anything the old module
        held is gone when it returns. MCDR has the same edge and so does this.
        """
        ok, _ = self._host.reload(plugin_id)
        return ok

    def load_plugin(self, path):
        ok, _ = self._host.load_new(path)
        return ok

    def unload_plugin(self, plugin_id):
        return self._host.unload(plugin_id)

    def enable_plugin(self, name):
        ok, _ = self._host.enable_plugin(name)
        return ok

    def disable_plugin(self, plugin_id):
        ok, _ = self._host.disable_plugin(plugin_id)
        return ok

    def refresh_changed_plugins(self):
        return self._host.refresh_changed_plugins()

    def refresh_all_plugins(self):
        return self._host.refresh_all_plugins()

    # -------------------------------------------------------------- the host

    def logger_for(self, plugin_id):
        return self._host.logger_for(plugin_id)

    @property
    def logger(self):
        return self._host.logger_for(None)

    def get_mcdr_config(self):
        return self._host.mcdr_config()

    def get_mcdr_language(self):
        return self._host.language

    def is_on_executor_thread(self):
        """Are we on the thread that dispatches events?

        Plugins ask before doing something that has to be ordered against
        other events, so answering honestly matters more than answering yes.
        """
        return self._host.is_on_executor_thread()

    def has_translation(self, key, language=None):
        return self._host.has_translation(key, language)

    def tr(self, key, *args, **kwargs):
        return self._host.translate(key, *args, **kwargs)

    def rtr(self, key, *args, **kwargs):
        from mcdr.text import RTextMCDRTranslation

        text = RTextMCDRTranslation(key, *args, **kwargs)
        return text.set_translator(self._host.translate)

    def schedule_task(self, callback, block=False, timeout=None):
        return self._host.schedule_task(callback, block=block, timeout=timeout)

    def get_permission_level(self, obj):
        return self._host.permission_of(obj)

    def set_permission_level(self, player, value):
        """Give a player a level, written to `permission.yml` right away.

        MCDR takes a number or one of the five names here, and so does this.
        """
        return self._host.set_permission(player, value)

    def reload_permission_file(self):
        return self._host.reload_permissions()

    def get_permission_players(self, level=None):
        return self._host.permissions.players(level)

    def get_plugin_command_source(self):
        return self._host.plugin_command_source()

    def as_basic_server_interface(self):
        return self

    def as_plugin_server_interface(self):
        """The plugin-flavoured interface, if this call is coming from one.

        Task reaches for it at import time through the singleton, which is a
        plain ServerInterface, and MCDR answers with the interface of whatever
        plugin is being loaded right now. Answering None instead would be
        technically true and would crash it.
        """
        return self._host.current_plugin_interface() or self


class PluginServerInterface(ServerInterface):
    """The same interface, plus everything that is about *this* plugin.

    A plugin gets one of these, so calls like `get_data_folder` can answer for
    it without the plugin having to say which one it is.
    """

    def __init__(self, host, plugin):
        ServerInterface.__init__(self, host)
        self._plugin = plugin

    # --------------------------------------------------------------- itself

    def get_self_metadata(self):
        return self._host.metadata_object(self._plugin)

    def get_plugin_file_path(self):
        return str(self._plugin.path)

    def get_data_folder(self):
        """`config/<plugin id>/`, created on demand, like MCDR does."""
        folder = Path(self._host.config_dir) / self._plugin.id
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    @property
    def logger(self):
        return self._host.logger_for(self._plugin.id)

    # ---------------------------------------------------------- registration

    def register_help_message(self, prefix, message, permission=0):
        self._host.register_help(self._plugin.id, prefix, message, permission)

    def register_command(self, root_node, allow_duplicates=False):
        self._host.register_command(self._plugin.id, root_node)

    def register_event_listener(self, event, callback, priority=None):
        self._host.register_listener(self._plugin.id, event, callback, priority)

    def register_translation(self, language, mapping):
        self._host.register_translation(language, mapping, self._plugin.id)

    def unregister_event_listener(self, event, callback):
        self._host.unregister_listener(self._plugin.id, event, callback)

    # --------------------------------------------------------------- config

    def load_config_simple(self, file_name=None, default_config=None,
                           in_data_folder=True, echo_in_console=True,
                           source_to_reply=None, target_class=None,
                           encoding="utf-8", failure_policy=None):
        """Read a plugin's config, filling in whatever the file leaves out.

        The three arguments that matter in practice, and that every measured
        plugin uses: a file name, a `target_class` deriving from Serializable,
        and `in_data_folder` deciding whether the path is relative to the
        plugin's own folder or to the server root. Here passes
        `in_data_folder=False` with `config/here.json`, so both paths are real.
        """
        path = self._config_path(file_name, in_data_folder)
        data, problems = {}, []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding=encoding))
            except ValueError as exc:
                problems.append("%s is not valid JSON (%s), using defaults"
                                % (path.name, exc))
                data = {}
        else:
            problems.append("%s did not exist, writing the defaults" % path.name)

        if target_class is not None and issubclass(target_class, Serializable):
            config, issues = target_class.deserialize_reporting(data)
            problems.extend(issues)
        elif target_class is not None:
            config = target_class()
            for key, value in (data or {}).items():
                setattr(config, key, value)
        else:
            config = dict(default_config or {})
            config.update(data or {})

        logger = self.logger
        for line in problems:
            logger.warning(line)
        if not path.exists():
            # Writing the defaults back is what makes a config discoverable:
            # the file is how an operator finds out what can be set.
            self.save_config_simple(config, file_name, in_data_folder,
                                    echo_in_console=False)
        return config

    def save_config_simple(self, config, file_name=None, in_data_folder=True,
                           echo_in_console=True, encoding="utf-8"):
        path = self._config_path(file_name, in_data_folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(config, Serializable):
            text = config.to_json_str()
        elif isinstance(config, dict):
            text = json.dumps(config, indent=4, ensure_ascii=False)
        else:
            text = json.dumps(getattr(config, "__dict__", {}), indent=4,
                              ensure_ascii=False)
        # Same rule as everywhere else in W4VE: written whole or not at all.
        tmp = path.with_suffix(path.suffix + ".writing")
        tmp.write_text(text + "\n", encoding=encoding)
        tmp.replace(path)
        if echo_in_console:
            self.logger.info("saved %s" % path.name)

    def _config_path(self, file_name, in_data_folder):
        name = file_name or "config.json"
        if in_data_folder:
            return Path(self.get_data_folder()) / name
        return Path(self._host.server_root) / name
