#!/usr/bin/env python3
"""The reference plugin: everything a real one needs, once.

The manifesto asks that this not be a useless hello world, so it is not. It
greets players when they join, counts how often each of them has, remembers
that across restarts, and answers a command about it. Small, and it exercises
config, an event, a command, state on disk, a call back into the core, and
saving on the way out.

Run it by hand to see the protocol:

    echo '{"type":"welcome","protocol":1,"config":{},"data_dir":"/tmp"}' | python3 main.py
"""

import json
import sys
from pathlib import Path

# W4VE puts its SDK on the path when it starts a plugin, so nothing is needed
# here. This line is only so the example also runs straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from w4ve_plugin import Plugin  # noqa: E402

plugin = Plugin("greeter", "1.0.0", name="Greeter")

seen = {}


def _file():
    """Where this plugin keeps its own things.

    `data_dir` is given by the core and is this plugin's alone. Writing beside
    the code would put state inside something a package replaces on update.
    """
    return Path(plugin.data_dir or ".") / "seen.json"


@plugin.on_start
def load():
    """Called once the core has told us our config, not before."""
    global seen
    try:
        seen = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No file yet, or a torn one. Starting the count again is a better
        # answer than refusing to run.
        seen = {}
    plugin.log("greeter ready, %d players remembered" % len(seen))


@plugin.on_stop
def save():
    """Called when the core asks us to leave. This is where state survives."""
    try:
        _file().write_text(json.dumps(seen, indent=1), encoding="utf-8")
    except OSError as exc:
        plugin.log("could not save: %s" % exc)


@plugin.on("player_joined")
def greet(payload):
    name = payload.get("player")
    if not name:
        return
    seen[name] = seen.get(name, 0) + 1
    vips = plugin.config.get("vips") or []
    greeting = plugin.config.get("greeting", "welcome")
    if seen[name] == 1:
        plugin.say("%s is here for the first time" % name)
    elif not vips or name in vips:
        # Only to them: everybody else does not need to know how many times
        # somebody has logged in today.
        plugin.say("%s (%d visits)" % (greeting, seen[name]), player=name)


@plugin.command("!!seen", help="how many times somebody has joined")
def seen_command(source, args):
    if not args:
        if not seen:
            return "nobody has joined yet"
        top = sorted(seen.items(), key=lambda pair: -pair[1])[:5]
        return ["most seen:"] + ["  %s  %d" % (name, count) for name, count in top]
    name = args[0]
    count = seen.get(name)
    return ("%s has joined %d times" % (name, count) if count
            else "%s has never joined" % name)


@plugin.command("!!greeter", help="what this plugin thinks is going on")
def diagnostics(source, args):
    """A diagnostic command, which every plugin should have and few do.

    It answers the question somebody actually has at three in the morning:
    is this thing working, and what does it think the world looks like.
    """
    status = plugin.server_status()
    return [
        "greeter %s" % plugin.version,
        "  server: %s%s" % ("running" if status.get("running") else "stopped",
                            ", ready" if status.get("ready") else ""),
        "  players remembered: %d" % len(seen),
        "  greeting: %r" % plugin.config.get("greeting"),
        "  data: %s" % _file(),
    ]


if __name__ == "__main__":
    plugin.run()
