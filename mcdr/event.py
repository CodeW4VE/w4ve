"""Plugin events as objects, which is how plugins talk to each other.

A plugin does not only react to the server. It reacts to other plugins:
QuickBackupM announces `quick_backup_multi.backup_done` when a backup lands,
and TimedQBM listens for it instead of guessing. That handshake is an event
with an id, dispatched by name, and it is why both of those plugins refuse to
even import without `LiteralEvent`.

Two kinds, and the difference matters:

  * the **built-in** events, which are the ones the host raises itself. Each
    one has a canonical id (`mcdr.player_joined`) and a callback name
    (`on_player_joined`), and a plugin may use either to say the same thing.
  * **literal** events, invented by a plugin, which the host knows nothing
    about and only carries between plugins.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.
"""


class PluginEvent:
    """An event with an id. Two events are the same event if the ids match."""

    def __init__(self, event_id):
        self.id = str(event_id)

    def __eq__(self, other):
        return isinstance(other, PluginEvent) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        return "<%s %s>" % (type(self).__name__, self.id)


class MCDREvent(PluginEvent):
    """One the host raises, so it also has a `on_...` name."""

    def __init__(self, event_id, default_method_name):
        PluginEvent.__init__(self, event_id)
        self.default_method_name = default_method_name


class LiteralEvent(PluginEvent):
    """One a plugin invented, identified only by the string it chose."""


class MCDRPluginEvents:
    GENERAL_INFO = MCDREvent("mcdr.general_info", "on_info")
    USER_INFO = MCDREvent("mcdr.user_info", "on_user_info")
    SERVER_START = MCDREvent("mcdr.server_start", "on_server_start")
    SERVER_STARTUP = MCDREvent("mcdr.server_startup", "on_server_startup")
    SERVER_STOP = MCDREvent("mcdr.server_stop", "on_server_stop")
    PLAYER_JOINED = MCDREvent("mcdr.player_joined", "on_player_joined")
    PLAYER_LEFT = MCDREvent("mcdr.player_left", "on_player_left")
    PLUGIN_LOADED = MCDREvent("mcdr.plugin_loaded", "on_load")
    PLUGIN_UNLOADED = MCDREvent("mcdr.plugin_unloaded", "on_unload")
    # Raised when the wrapper itself starts and stops. W4VE raises these when
    # the guardian does, which is the closest true statement.
    MCDR_START = MCDREvent("mcdr.mcdr_start", "on_mcdr_start")
    MCDR_STOP = MCDREvent("mcdr.mcdr_stop", "on_mcdr_stop")


BUILTIN = [value for value in vars(MCDRPluginEvents).values()
           if isinstance(value, MCDREvent)]
BY_ID = {event.id: event for event in BUILTIN}
BY_METHOD = {event.default_method_name: event for event in BUILTIN}


def name_of(event):
    """The callback name an event dispatches under, whatever it was given as.

    A plugin can ask for the same event three ways: the object, its id, or the
    `on_...` name. All three have to end up in the same bucket, or a listener
    registered one way never fires for an event raised another way.
    """
    if isinstance(event, PluginEvent):
        if isinstance(event, MCDREvent):
            return event.default_method_name
        known = BY_ID.get(event.id)
        return known.default_method_name if known else event.id
    text = str(event)
    if text in BY_METHOD:
        return text
    known = BY_ID.get(text)
    return known.default_method_name if known else text


def is_builtin(event):
    return name_of(event) in BY_METHOD
