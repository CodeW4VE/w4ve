"""Info: one line off the server console, parsed into something usable.

Every plugin that reacts to what happens in game gets one of these. It carries
the raw line, what the server said, and, when a player said it, who. The
distinction between `is_player` and `is_user` is the one plugins actually
branch on, and Here branches on it twice in nine lines.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.

The parsing here is the part W4VE intends to outgrow. Reading chat by matching
the shape of a log line is what MCDR has to do, and since Minecraft 26.1 ships
unobfuscated a companion mod can hand over typed events instead. Until that
exists (RFC-0003), this is how a plugin hears anything, so it has to be right.
"""

import re

# `[13:24:11] [Server thread/INFO]: <TVTvirus> hello`
VANILLA = re.compile(
    r"^\[(?P<hour>\d+):(?P<min>\d+):(?P<sec>\d+)\] "
    r"\[(?P<thread>[^/]+)/(?P<level>[A-Z]+)\][^:]*: (?P<content>.*)$")

# Paper folds the thread into the prefix: `[13:24:11 INFO]: something`
BUKKIT = re.compile(
    r"^\[(?P<hour>\d+):(?P<min>\d+):(?P<sec>\d+) (?P<level>[A-Z]+)\]: "
    r"(?P<content>.*)$")

# `<TVTvirus> hello`, which is what a player saying something looks like.
CHAT = re.compile(r"^<(?P<player>[^>]+)> (?P<message>.*)$")

# `TVTvirus joined the game`, and the same with a login address in front.
# Only ever matched against a line the *server* wrote: a player typing "X
# joined the game" in chat arrives with `player` already set, so it cannot
# forge somebody else's join.
JOINED = re.compile(r"^(?P<player>[^ <\[]+)(\[[^\]]*\])? joined the game$")
LEFT = re.compile(r"^(?P<player>[^ <\[]+)(\[[^\]]*\])? left the game$")


def player_event(info):
    """`("joined", name)`, `("left", name)`, or None for anything else."""
    if info.player is not None or not info.content:
        return None
    match = JOINED.match(info.content)
    if match:
        return "joined", match.group("player")
    match = LEFT.match(info.content)
    if match:
        return "left", match.group("player")
    return None


CONSOLE_SOURCE = 0
SERVER_SOURCE = 1


class Info:

    def __init__(self, raw="", content="", player=None, source=SERVER_SOURCE,
                 hour=None, minute=None, second=None, logging_level=None):
        self.raw_content = raw
        self.content = content
        self.player = player
        self.source = source
        self.hour = hour
        self.min = minute
        self.sec = second
        self.logging_level = logging_level
        # Set when a plugin claims the line so nothing else acts on it.
        self._cancelled = False

    # -------------------------------------------------------------- what it is

    @property
    def is_from_server(self):
        return self.source == SERVER_SOURCE

    @property
    def is_from_console(self):
        return self.source == CONSOLE_SOURCE

    @property
    def is_player(self):
        """A player said this in chat."""
        return self.player is not None

    @property
    def is_user(self):
        """A person typed this: a player in chat, or whoever has the console."""
        return self.is_player or self.is_from_console

    def get_server(self):
        return None

    def cancel_send_to_server(self):
        self._cancelled = True

    def is_send_to_server_cancelled(self):
        return self._cancelled

    def __repr__(self):
        return "<Info %s%r>" % ("from %s " % self.player if self.player else "",
                                self.content)


def parse_console(text):
    """A line somebody typed into the console."""
    return Info(raw=text, content=text, source=CONSOLE_SOURCE)


def parse_server_line(line):
    """One line of server output, whatever flavour wrote it."""
    match = VANILLA.match(line) or BUKKIT.match(line)
    if not match:
        # Output that does not look like a log line is still output: give the
        # plugin the whole thing rather than nothing.
        return Info(raw=line, content=line)
    groups = match.groupdict()
    content = groups["content"]
    player = None
    said = CHAT.match(content)
    if said:
        player = said.group("player")
        content = said.group("message")
    return Info(raw=line, content=content, player=player,
                hour=int(groups["hour"]), minute=int(groups["min"]),
                second=int(groups["sec"]), logging_level=groups["level"])
