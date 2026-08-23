"""The commands the host answers itself: `!!w4ve` and `!!help`.

**There is no `!!MCDR`.** It was there for a while as an alias, and the user
decided against it: W4VE runs MCDR *plugins*, it does not pretend to be MCDR.
Keeping the old command would have meant a server that answers to the name of
the thing it replaced, forever.

What is left is one line of courtesy. Typing `!!MCDR` says where the command
went and does nothing else: no status, no reload, no plugin management. That
is not compatibility, it is a signpost, and it exists only because the
alternative is Minecraft answering "Unknown command" to somebody's muscle
memory.

⚠️ Every clickable line here uses `suggest_command`, never `run_command`. A
click that runs a command whose text does not start with `/` has done nothing
since MC 1.20.5, silently: the same trap that broke `!!here`'s waypoint
buttons. Suggesting puts it in the chat box and the player presses enter,
which works on every version.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.
"""

from mcdr.command import (GreedyText, Literal, QuotableText,
                          RequirementNotMet, Text)
from mcdr.interface import PermissionLevel
from mcdr.permission import LEVELS, NAMES
from mcdr.text import RAction, RColor, RStyle, RText, RTextList

# What it takes to manage the host. Same level MCDR asks for, so a staff list
# carried over from `permission.yml` keeps meaning what it meant.
MANAGE_LEVEL = PermissionLevel.ADMIN

TITLE = RColor.gold
KEY = RColor.aqua
DIM = RColor.gray
BAD = RColor.red
GOOD = RColor.green


def _button(label, command, hint=None, color=KEY):
    """A word you can click to get the command typed out for you."""
    text = RText(label, color)
    text.set_click_event(RAction.suggest_command, command)
    text.set_hover_text(hint or command)
    return text


def _line(source, *parts):
    source.reply(RTextList(*parts))


def _denied(source):
    _line(source, RText("You need to be admin for that.", BAD))


class Builtins:
    """The host's own command tree, built once and asked to run a line.

    Kept out of `host.py` because it is a different kind of thing: the host is
    the machinery, this is the interface a person types at.
    """

    def __init__(self, host):
        self.host = host
        self.roots = [self._build("!!w4ve"), self._moved_tree(),
                      self._help_tree()]

    # ------------------------------------------------------------------ entry

    def run(self, source, line):
        """Try the line against the built-in commands. True if one claimed it."""
        line = (line or "").strip()
        if not line:
            return False
        for root in self.roots:
            if not root._could_match(line):
                continue
            try:
                if root.execute(source, line):
                    return True
            except Exception:
                self.host.logger_for("w4ve").exception(
                    "error running %r" % line)
                return True
        return False

    # ------------------------------------------------------------- the tree

    def _may_manage(self, source):
        return source.has_permission(MANAGE_LEVEL)

    def _moved_tree(self):
        """`!!MCDR` is not a command here. This says so and stops.

        Deliberately outside the permission check: somebody who cannot manage
        plugins still deserves to be told the command does not exist, rather
        than being told they lack permission for something that is not there.
        """
        root = Literal("!!MCDR").runs(self.moved)
        # And the same for anything after it: `!!MCDR reload plugin x` has to
        # land here too, not fall through to Minecraft.
        root.then(GreedyText("rest").runs(lambda src, ctx: self.moved(src)))
        return root

    def moved(self, source):
        _line(source, RText("There is no !!MCDR here. This is W4VE: ", DIM),
              _button("!!w4ve", "!!w4ve", "the plugin manager"),
              RText(" (it still runs MCDR plugins).", DIM))
        return True

    def _build(self, prefix):
        root = (Literal(prefix)
                .requires(self._may_manage, lambda: "admin only")
                .runs(lambda src: self.overview(src, prefix)))
        # A player without the level gets told, once, instead of the tree
        # raising past the host and landing in the log as an error nobody in
        # game will ever see.
        root.on_error(RequirementNotMet,
                      lambda src, err, ctx: _denied(src), handled=True)
        root.on_child_error(RequirementNotMet,
                            lambda src, err, ctx: _denied(src), handled=True)

        plugin = Literal("plugin")
        plugin.runs(lambda src: self.plugin_list(src, prefix))
        plugin.then(Literal("list").runs(lambda src: self.plugin_list(src, prefix)))
        plugin.then(Literal("load").then(
            GreedyText("path").runs(
                lambda src, ctx: self.plugin_load(src, ctx["path"]))))
        plugin.then(Literal("unload").then(
            QuotableText("id").runs(
                lambda src, ctx: self.plugin_unload(src, ctx["id"]))))
        plugin.then(Literal("reload").then(
            QuotableText("id").runs(
                lambda src, ctx: self.plugin_reload(src, ctx["id"]))))
        plugin.then(Literal("enable").then(
            GreedyText("name").runs(
                lambda src, ctx: self.plugin_enable(src, ctx["name"]))))
        plugin.then(Literal("disable").then(
            QuotableText("id").runs(
                lambda src, ctx: self.plugin_disable(src, ctx["id"]))))
        root.then(plugin)
        root.then(Literal("plg").redirects(plugin))

        reload_node = Literal("reload")
        reload_node.runs(lambda src: self.usage_reload(src, prefix))
        reload_node.then(Literal("all").runs(lambda src: self.reload_all(src)))
        reload_node.then(Literal("plugins").runs(lambda src: self.refresh(src)))
        reload_node.then(Literal("permission").runs(
            lambda src: self.reload_permission(src)))
        reload_node.then(Literal("plugin").then(
            QuotableText("id").runs(
                lambda src, ctx: self.plugin_reload(src, ctx["id"]))))
        root.then(reload_node)
        root.then(Literal("r").redirects(reload_node))

        root.then(Literal("status").runs(lambda src: self.status(src, prefix)))
        root.then(Literal("refresh").runs(lambda src: self.refresh(src)))

        permission = Literal("permission")
        permission.runs(lambda src: self.permission_list(src, prefix))
        permission.then(Literal("list").runs(
            lambda src: self.permission_list(src, prefix)))
        permission.then(Literal("set").then(
            QuotableText("player").then(
                Text("level").runs(
                    lambda src, ctx: self.permission_set(
                        src, ctx["player"], ctx["level"])))))
        permission.then(Literal("remove").then(
            QuotableText("player").runs(
                lambda src, ctx: self.permission_remove(src, ctx["player"]))))
        permission.then(Literal("default").then(
            Text("level").runs(
                lambda src, ctx: self.permission_default(src, ctx["level"]))))
        root.then(permission)
        root.then(Literal("perm").redirects(permission))
        return root

    def _help_tree(self):
        # No permission requirement: everybody gets a help screen, and what is
        # in it is already filtered by what they are allowed to run.
        return Literal("!!help").runs(self.help)

    # --------------------------------------------------------------- answers

    def overview(self, source, prefix):
        _line(source, RText("W4VE plugins", TITLE, [RStyle.bold]))
        rows = [
            ("status", "what is running right now"),
            ("plugin list", "the plugins that are loaded"),
            ("plugin reload <id>", "load one again from disk"),
            ("refresh", "load, reload and drop whatever changed on disk"),
            ("reload all", "throw everything away and load the folder again"),
            ("permission list", "who is what"),
            ("permission set <player> <level>", "make somebody helper, admin…"),
        ]
        for command, what in rows:
            _line(source,
                  _button(prefix + " " + command, prefix + " " + command),
                  RText("  " + what, DIM))

    def usage_reload(self, source, prefix):
        _line(source, RText("Say what to reload: ", DIM),
              _button("plugin <id>", prefix + " reload plugin "),
              RText(" · ", DIM), _button("plugins", prefix + " reload plugins"),
              RText(" · ", DIM), _button("permission", prefix + " reload permission"),
              RText(" · ", DIM), _button("all", prefix + " reload all"))

    def status(self, source, prefix):
        host = self.host
        server = host.server
        _line(source, RText("W4VE", TITLE, [RStyle.bold]),
              RText("  server: ", DIM),
              RText(getattr(server, "state", "not attached"),
                    GOOD if host.is_server_running() else BAD),
              RText("  ready: ", DIM),
              RText("yes" if host.is_server_ready() else "no",
                    GOOD if host.is_server_ready() else DIM))
        _line(source, RText("  plugins: ", DIM),
              RText(str(len(host.loaded)), KEY),
              RText(" loaded", DIM),
              RText(("  (%d disabled)" % len(host.disabled_plugins()))
                    if host.disabled_plugins() else "", DIM))
        problems = host.problems
        if problems:
            _line(source, RText("  problems: ", DIM),
                  RText(str(len(problems)), BAD),
                  RText("  ", DIM),
                  _button("[see them]", prefix + " plugin list"))
        pid = host.server_pid()
        if pid:
            _line(source, RText("  pid: ", DIM), RText(str(pid), KEY))

    def plugin_list(self, source, prefix):
        host = self.host
        if not host.loaded:
            _line(source, RText("No plugins loaded.", DIM))
        for plugin_id in sorted(host.loaded):
            entry = host.loaded[plugin_id]
            _line(source,
                  RText("  " + plugin_id, KEY),
                  RText(" " + (entry.plugin.version or "?"), DIM),
                  RText("  "),
                  _button("[reload]", "%s plugin reload %s" % (prefix, plugin_id)),
                  RText(" "),
                  _button("[unload]", "%s plugin unload %s" % (prefix, plugin_id),
                          color=DIM))
        for name in host.disabled_plugins():
            _line(source, RText("  " + name, DIM), RText("  disabled  ", DIM),
                  _button("[enable]", "%s plugin enable %s" % (prefix, name),
                          color=GOOD))
        for problem in host.problems:
            _line(source, RText("  ! ", BAD), RText(problem, DIM))

    # ------------------------------------------------------------ the actions

    def plugin_reload(self, source, plugin_id):
        ok, problems = self.host.reload(plugin_id)
        if ok:
            _line(source, RText("reloaded ", GOOD), RText(plugin_id, KEY))
        else:
            _line(source, RText("could not reload ", BAD), RText(plugin_id, KEY))
        for line in problems:
            _line(source, RText("  " + line, DIM))
        return ok

    def plugin_unload(self, source, plugin_id):
        if self.host.unload(plugin_id):
            _line(source, RText("unloaded ", GOOD), RText(plugin_id, KEY))
            return True
        _line(source, RText("%s is not loaded" % plugin_id, BAD))
        return False

    def plugin_load(self, source, path):
        ok, problems = self.host.load_new(path.strip())
        _line(source, RText("loaded" if ok else "did not load",
                            GOOD if ok else BAD),
              RText(" " + path.strip(), KEY))
        for line in problems:
            _line(source, RText("  " + line, DIM))
        return ok

    def plugin_enable(self, source, name):
        ok, detail = self.host.enable_plugin(name.strip())
        _line(source, RText("enabled " if ok else "could not enable ",
                            GOOD if ok else BAD), RText(str(detail), KEY))
        return ok

    def plugin_disable(self, source, plugin_id):
        ok, detail = self.host.disable_plugin(plugin_id)
        _line(source, RText("disabled " if ok else "could not disable ",
                            GOOD if ok else BAD), RText(str(detail), KEY))
        return ok

    def refresh(self, source):
        result = self.host.refresh_changed_plugins()
        moved = False
        for label, key, color in (("loaded", "loaded", GOOD),
                                  ("reloaded", "reloaded", KEY),
                                  ("dropped", "unloaded", DIM)):
            if result[key]:
                moved = True
                _line(source, RText(label + ": ", DIM),
                      RText(", ".join(result[key]), color))
        if not moved:
            _line(source, RText("nothing changed on disk", DIM))
        for line in result["problems"]:
            _line(source, RText("  ! ", BAD), RText(line, DIM))
        return True

    def reload_all(self, source):
        result = self.host.refresh_all_plugins()
        _line(source, RText("loaded ", GOOD),
              RText(str(len(result["loaded"])), KEY),
              RText(" plugins: ", DIM),
              RText(", ".join(result["loaded"]) or "none", DIM))
        for line in result["problems"]:
            _line(source, RText("  ! ", BAD), RText(line, DIM))
        return True

    def reload_permission(self, source):
        self.host.reload_permissions()
        _line(source, RText("permission.yml read again", GOOD))
        return True

    # ----------------------------------------------------------- permissions

    def permission_list(self, source, prefix):
        perms = self.host.permissions
        _line(source, RText("permission.yml", TITLE, [RStyle.bold]),
              RText("  default: ", DIM),
              RText(NAMES[perms.default_level], KEY))
        listed = perms.players()
        if not listed:
            _line(source, RText("  nobody is listed; everybody is ", DIM),
                  RText(NAMES[perms.default_level], KEY))
        for level in (4, 3, 2, 1, 0):
            names = perms.players(level)
            if names:
                _line(source, RText("  %-7s" % NAMES[level], KEY),
                      RText(", ".join(names), DIM))

    def permission_set(self, source, player, level):
        if str(level).strip().lower() not in LEVELS and not str(level).isdigit():
            _line(source, RText("%r is not a level. Try: " % level, BAD),
                  RText(" ".join(sorted(LEVELS, key=LEVELS.get)), KEY))
            return False
        # Nobody hands out a level above their own. Without this check,
        # `!!MCDR permission set me owner` is a promotion anybody with admin
        # can give themselves.
        from mcdr.permission import level_from

        wanted = level_from(level)
        if wanted > source.get_permission_level():
            _line(source, RText("you cannot hand out a level above your own",
                                BAD))
            return False
        self.host.set_permission(player, wanted)
        _line(source, RText(player, KEY), RText(" is now ", DIM),
              RText(NAMES[wanted], GOOD))
        return True

    def permission_remove(self, source, player):
        if self.host.permissions.remove(player):
            _line(source, RText(player, KEY), RText(" is off the list, so ", DIM),
                  RText(NAMES[self.host.permissions.default_level], KEY),
                  RText(" now", DIM))
            return True
        _line(source, RText("%s was not listed" % player, DIM))
        return False

    def permission_default(self, source, level):
        from mcdr.permission import level_from

        wanted = level_from(level)
        if wanted > source.get_permission_level():
            _line(source, RText("you cannot set a default above your own level",
                                BAD))
            return False
        self.host.permissions.set_default_level(wanted)
        _line(source, RText("anybody not listed is now ", DIM),
              RText(NAMES[wanted], GOOD))
        return True

    # ----------------------------------------------------------------- !!help

    def help(self, source):
        """Every help line the plugins registered, that this source may run."""
        items = self.host.help_for(source)
        _line(source, RText("Commands", TITLE, [RStyle.bold]))
        if not items:
            _line(source, RText("  nothing registered a help message yet", DIM))
        for item in items:
            prefix = str(item.get("prefix") or "")
            message = item.get("message")
            _line(source, RText("  "), _button(prefix, prefix.split()[0] + " "
                                               if prefix else ""),
                  RText("  "), message if message is not None else RText(""))
        if source.has_permission(MANAGE_LEVEL):
            _line(source, RText("  "), _button("!!w4ve", "!!w4ve"),
                  RText("  the plugins themselves", DIM))
        return True
