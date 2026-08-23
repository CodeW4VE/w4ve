"""Who is allowed to do what, kept in a file a person can read and edit.

MCDR keeps this in `permission.yml` next to the server, and so does W4VE: the
same file, the same five levels, the same names. A cutover must not ask
anybody to retype their staff list.

Two rules from W4VE-DISENO shape everything here:

  * **Flat files, visible and commented, editable by hand and while running.**
    So the file is written with its own explanation at the top, and it is
    re-read when it changes on disk. Nobody has to restart anything, and
    nobody has to run a command to hand somebody else admin: editing the file
    is a supported way to do it.
  * **Written whole or not at all.** A half written permission file is a
    server where staff silently lost their commands, so every write goes to a
    temporary file and gets renamed over the real one.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.
"""

from pathlib import Path

# Least to most. The names are MCDR's, because they are what is already
# written in the file this reads.
LEVELS = {"guest": 0, "user": 1, "helper": 2, "admin": 3, "owner": 4}
NAMES = {value: key for key, value in LEVELS.items()}

GUEST, USER, HELPER, ADMIN, OWNER = 0, 1, 2, 3, 4

DEFAULT_TEXT = """\
# Who can do what. Levels, lowest to highest:
#
#   guest(0)  user(1)  helper(2)  admin(3)  owner(4)
#
# Put a player under the level they should have, one `- Name` per line. A
# player who is not listed anywhere gets `default_level`.
#
# Edit this by hand whenever you like, including while the server is running:
# W4VE notices the file changed and re-reads it. The console is always owner,
# because whoever holds the console already holds the server.

default_level: user

owner:
admin:
helper:
user:
guest:
"""


def level_from(value, fallback=USER):
    """A level from whatever was written: a number, or one of the five names."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return max(GUEST, min(OWNER, value))
    text = str(value).strip().lower()
    if text in LEVELS:
        return LEVELS[text]
    try:
        return max(GUEST, min(OWNER, int(text)))
    except ValueError:
        return fallback


class PermissionFile:
    """`permission.yml`, read on demand and edited in place.

    Held as the lines it was read as, not as a parsed structure written back
    out. That is what keeps somebody's comments and ordering intact when a
    command adds one name: a rewrite from a dict would quietly eat the file
    they wrote by hand, which is the opposite of the promise.
    """

    def __init__(self, path, on_problem=None):
        self.path = Path(path)
        self._on_problem = on_problem or (lambda line: None)
        self._lines = []
        self._stamp = None
        self._default = USER
        self._levels = {}          # player -> level
        self.load()

    # ----------------------------------------------------------------- reading

    def _disk_stamp(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def load(self):
        """Read the file, writing the default one if there is none yet."""
        if not self.path.exists():
            self._write(DEFAULT_TEXT.splitlines())
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            self._on_problem("could not read %s (%s), everybody is %s"
                             % (self.path.name, exc, NAMES[self._default]))
            self._lines = []
            self._stamp = None
            return
        self._lines = text.splitlines()
        self._stamp = self._disk_stamp()
        self._parse()

    def reload_if_changed(self):
        """Re-read if somebody edited the file. Returns True if it was re-read.

        One `stat` per call, which is cheap enough to do on every permission
        check and is what makes hand editing work while the server runs.
        """
        if self._disk_stamp() != self._stamp:
            self.load()
            return True
        return False

    def _parse(self):
        """The subset of YAML this file is: one mapping of lists of names."""
        self._default = USER
        self._levels = {}
        section = None
        seen = {}
        for raw in self._lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                if section is None:
                    continue
                player = line[1:].strip().strip('"').strip("'")
                if not player:
                    continue
                if player in seen and seen[player] != section:
                    # Listed twice at different levels. Taking the higher one
                    # is the safe reading: the alternative is silently
                    # demoting somebody because of where a line happened to
                    # be.
                    self._on_problem(
                        "%s is listed under both %s and %s in %s; using %s"
                        % (player, NAMES[seen[player]], NAMES[section],
                           self.path.name,
                           NAMES[max(seen[player], section)]))
                    highest = max(seen[player], section)
                    seen[player] = highest
                    self._levels[player] = highest
                    continue
                seen[player] = section
                self._levels[player] = section
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "default_level":
                self._default = level_from(value)
                section = None
            elif key in LEVELS:
                section = LEVELS[key]
                # `owner: [Steve, Alex]` is legal YAML and somebody will write
                # it, so it is read too.
                if value.startswith("[") and value.endswith("]"):
                    for player in value[1:-1].split(","):
                        player = player.strip().strip('"').strip("'")
                        if player:
                            self._levels[player] = section
                            seen[player] = section
                    section = None
            else:
                section = None

    # ----------------------------------------------------------------- asking

    @property
    def default_level(self):
        self.reload_if_changed()
        return self._default

    def level_of(self, player):
        """The level of a player by name. Unknown names get the default."""
        self.reload_if_changed()
        if player is None:
            return self._default
        return self._levels.get(str(player), self._default)

    def players(self, level=None):
        """Everybody listed, or everybody listed at one level."""
        self.reload_if_changed()
        if level is None:
            return dict(self._levels)
        return sorted(name for name, value in self._levels.items()
                      if value == level)

    # ---------------------------------------------------------------- writing

    def set_level(self, player, level):
        """Move a player to a level, keeping the rest of the file as it was."""
        player = str(player).strip()
        if not player:
            return False
        level = level_from(level)
        self.reload_if_changed()
        if not self._lines:
            self._lines = DEFAULT_TEXT.splitlines()
        lines = [line for line in self._lines
                 if not _is_entry_for(line, player)]
        lines = _insert_under(lines, NAMES[level], player)
        self._write(lines)
        self._parse()
        return True

    def remove(self, player):
        """Take a player off the file, so they fall back to the default."""
        player = str(player).strip()
        self.reload_if_changed()
        lines = [line for line in self._lines
                 if not _is_entry_for(line, player)]
        if len(lines) == len(self._lines):
            return False
        self._write(lines)
        self._parse()
        return True

    def set_default_level(self, level):
        level = level_from(level)
        self.reload_if_changed()
        lines, done = [], False
        for raw in self._lines:
            if raw.strip().lower().startswith("default_level:"):
                lines.append("default_level: %s" % NAMES[level])
                done = True
            else:
                lines.append(raw)
        if not done:
            lines.append("default_level: %s" % NAMES[level])
        self._write(lines)
        self._parse()
        return True

    def _write(self, lines):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(lines).rstrip("\n") + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".writing")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        self._lines = text.splitlines()
        self._stamp = self._disk_stamp()


def _is_entry_for(line, player):
    """Is this line the list entry for this player?"""
    stripped = line.strip()
    if not stripped.startswith("-"):
        return False
    name = stripped[1:].strip().strip('"').strip("'")
    return name == player


def _insert_under(lines, section, player):
    """Put `- player` under `section:`, adding the section if it is missing."""
    out = []
    placed = False
    for index, raw in enumerate(lines):
        out.append(raw)
        if placed:
            continue
        key = raw.strip().lower()
        if key == section + ":" or key.startswith(section + ": "):
            out.append("- %s" % player)
            placed = True
    if not placed:
        if out and out[-1].strip():
            out.append("")
        out.append("%s:" % section)
        out.append("- %s" % player)
    return out
