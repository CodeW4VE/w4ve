"""What a piece needs told to it, and where those answers live.

A `.w4ve` could always copy files and start processes. What it could not do was
**ask for a value**, and that is the whole reason installing something like
WaveChat still meant editing four files by hand: a public domain, a Discord
token, a channel, a couple of role ids.

Two places, on purpose:

- **Ordinary answers go in `w4ve.toml`**, under `[settings.<piece>]`, written
  with the help text above each line as a comment. It is the file the operator
  already reads, they can edit it with the server running, and a value they can
  see is a value they can fix at three in the morning.
- **Secrets go in `w4ve/secrets.json`**, mode 600, and never anywhere else.
  Not in the toml, not in `state.json`, not in the journal, not on the console.
  A piece that leaks a bot token in a log is a piece nobody should install.

Both are read back into a service's environment as `W4VE_<NAME>`, which is why
a package almost never needs a config template: the process just reads its own
environment.

Standard library only, Python 3.9, same rules as the rest.
"""

import json
import os
import re
import stat
from pathlib import Path

# What a setting can be. Kept small deliberately: every type here has an
# unambiguous answer to "is this value valid", and a type that cannot answer
# that is a type that pushes the error to the service's first minute of life.
TYPES = ("text", "url", "int", "bool", "path", "choice", "list", "secret")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REFERENCE_RE = re.compile(r"\$\{(setting|secret):([a-z][a-z0-9_]*)\}")

TRUE = ("true", "yes", "on", "1", "y", "si", "sí")
FALSE = ("false", "no", "off", "0", "n")

# Sizes and durations are written the way an operator writes them, because the
# alternative is a config full of 604800 and nobody remembering what it was.
SIZE_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class SettingError(Exception):
    """A value that does not fit its declaration, said in words."""


# ------------------------------------------------------------------- parsing

def parse_size(text, what="size"):
    """`2G`, `500m`, `1024` -> bytes."""
    raw = str(text).strip().lower().replace("b", "")
    if not raw:
        raise SettingError("%s is empty" % what)
    unit = SIZE_UNITS.get(raw[-1])
    number = raw[:-1] if unit else raw
    try:
        value = float(number)
    except ValueError:
        raise SettingError("%s %r is not a number followed by K, M, G or T"
                           % (what, text))
    return int(value * (unit or 1))


def parse_time(text, what="duration"):
    """`7d`, `30s`, `12h` -> seconds."""
    raw = str(text).strip().lower()
    if not raw:
        raise SettingError("%s is empty" % what)
    unit = TIME_UNITS.get(raw[-1])
    number = raw[:-1] if unit else raw
    try:
        value = float(number)
    except ValueError:
        raise SettingError("%s %r is not a number followed by s, m, h, d or w"
                           % (what, text))
    return int(value * (unit or 1))


def human_size(count):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return "%.0f %s" % (count, unit) if unit == "B" else "%.1f %s" % (count, unit)
        count /= 1024.0
    return "%.1f TB" % count


# --------------------------------------------------------------- declarations

class Declaration:
    """One thing a piece needs told to it."""

    def __init__(self, data):
        self.data = data or {}

    @property
    def name(self):
        return self.data.get("name", "")

    @property
    def type(self):
        return self.data.get("type", "text")

    @property
    def secret(self):
        return self.type == "secret"

    @property
    def required(self):
        # A setting with a default is answered already; anything else is
        # required unless it says otherwise.
        if "required" in self.data:
            return bool(self.data["required"])
        return "default" not in self.data

    @property
    def default(self):
        return self.data.get("default")

    @property
    def title(self):
        return self.data.get("title") or self.name.replace("_", " ")

    @property
    def help(self):
        return self.data.get("help", "")

    @property
    def example(self):
        return self.data.get("example", "")

    @property
    def options(self):
        return list(self.data.get("options") or [])

    @property
    def discover(self):
        """Where the CLI can go and find the possible answers by itself.

        The point of the whole module: an id is never asked for, it is
        discovered. `discord.channel` means "log in with the token we already
        have, list the channels, let them pick by name".
        """
        return self.data.get("discover", "")

    @property
    def when(self):
        """Only ask this if another setting is on (`when = "music"`)."""
        return self.data.get("when", "")

    def problems(self):
        out = []
        if not NAME_RE.match(self.name or ""):
            out.append("setting name %r is not lowercase letters, digits and "
                       "underscores" % self.name)
        if self.type not in TYPES:
            out.append("setting %s: type %r is not one of %s"
                       % (self.name, self.type, ", ".join(TYPES)))
        if self.type == "choice" and not self.options:
            out.append("setting %s: a choice needs options" % self.name)
        if self.secret and "default" in self.data:
            out.append("setting %s: a secret cannot have a default in the "
                       "manifest" % self.name)
        if not self.help:
            out.append("setting %s: no help, and the help is what ends up as "
                       "the comment in w4ve.toml" % self.name)
        if "default" in self.data:
            try:
                self.coerce(self.data["default"])
            except SettingError as exc:
                out.append("setting %s: the default does not fit: %s"
                           % (self.name, exc))
        return out

    # ----------------------------------------------------------------- values

    def coerce(self, value):
        """Turn what somebody typed into the value this setting means."""
        if value is None:
            raise SettingError("no value")
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in TRUE:
                return True
            if text in FALSE:
                return False
            raise SettingError("%r is not yes or no" % value)
        if self.type == "int":
            try:
                return int(str(value).strip())
            except ValueError:
                raise SettingError("%r is not a whole number" % value)
        if self.type == "list":
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if str(v).strip()]
            return [part.strip() for part in str(value).split(",") if part.strip()]
        if self.type == "choice":
            text = str(value).strip()
            if text not in self.options:
                raise SettingError("%r is not one of %s"
                                   % (text, ", ".join(self.options)))
            return text
        text = str(value).strip()
        if not text:
            raise SettingError("empty")
        if self.type == "url":
            if not text.startswith(("http://", "https://")):
                raise SettingError("%r is not an http or https address" % text)
            # A trailing slash is the difference between working and a 404, and
            # nobody should have to know that. Add it and stop thinking about it.
            if not text.endswith("/") and "?" not in text and text.count("/") <= 3:
                text += "/"
        return text


def declarations_of(manifest_data):
    return [Declaration(item) for item in (manifest_data.get("settings") or [])]


# --------------------------------------------------------------------- store

class Secrets:
    """`w4ve/secrets.json`, mode 600, and nothing else knows about it."""

    def __init__(self, root):
        self.path = Path(root) / "w4ve" / "secrets.json"

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def all_for(self, piece):
        return dict(self._read().get(piece) or {})

    def get(self, piece, name, default=None):
        return self.all_for(piece).get(name, default)

    def set(self, piece, name, value):
        data = self._read()
        data.setdefault(piece, {})[name] = value
        self._write(data)

    def forget(self, piece, name=None):
        data = self._read()
        if piece not in data:
            return
        if name is None:
            data.pop(piece)
        else:
            data[piece].pop(name, None)
            if not data[piece]:
                data.pop(piece)
        self._write(data)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create it closed before anything goes in it: writing first and
        # chmod'ing after leaves a window where the token is world readable.
        fd = os.open(str(self.path) + ".writing",
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(self.path) + ".writing", self.path)
        os.chmod(self.path, 0o600)

    def insecure(self):
        """True if somebody widened the permissions, which doctor should say."""
        if not self.path.exists():
            return False
        mode = stat.S_IMODE(self.path.stat().st_mode)
        return bool(mode & 0o077)


# ------------------------------------------------------------------ expansion

def expand(value, settings, secrets):
    """Replace `${setting:x}` and `${secret:y}` inside strings, lists and dicts.

    Anything unknown is left exactly as it was rather than blanked: a service
    started with an empty token fails in a way nobody can read, and a service
    started with the literal `${secret:token}` fails saying the word `secret`.
    """
    if isinstance(value, str):
        def swap(match):
            kind, name = match.group(1), match.group(2)
            source = settings if kind == "setting" else secrets
            if name not in source:
                return match.group(0)
            found = source[name]
            return found if isinstance(found, str) else json.dumps(found)
        return REFERENCE_RE.sub(swap, value)
    if isinstance(value, list):
        return [expand(item, settings, secrets) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, settings, secrets) for key, item in value.items()}
    return value


def unresolved(value):
    """Every `${...}` still standing, so the caller can refuse to start."""
    out = []
    if isinstance(value, str):
        out += [match.group(0) for match in REFERENCE_RE.finditer(value)]
    elif isinstance(value, list):
        for item in value:
            out += unresolved(item)
    elif isinstance(value, dict):
        for item in value.values():
            out += unresolved(item)
    return out


def environment_for(piece, settings, secrets):
    """`W4VE_<NAME>` for every answer, which is how a service reads them.

    Uppercased with the piece stripped off, because a service already knows
    which piece it is: WaveChat's process wants `W4VE_PUBLIC_URL`, not
    `W4VE_WAVECHAT_SERVER_PUBLIC_URL`.
    """
    out = {}
    for name, value in dict(settings or {}).items():
        out["W4VE_" + name.upper()] = _as_env(value)
    for name, value in dict(secrets or {}).items():
        out["W4VE_" + name.upper()] = _as_env(value)
    out["W4VE_PIECE"] = piece
    return out


def _as_env(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


# ------------------------------------------------------- writing the toml bit

def toml_value(value):
    """One value written as TOML, which differs from JSON for a table only."""
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s = %s" % (key, toml_value(item))
                                  for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(toml_value(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


def toml_block(piece, declarations, values, owner=""):
    """The `[settings.<piece>]` block, with the help text as comments.

    This is the file MCDR taught us to write: a person opens it, reads what
    each line is for in their own file rather than in a web page, changes one
    value and restarts. Everything here exists to make that possible.
    """
    lines = ["[settings.%s]" % piece]
    if owner:
        lines[0] += "  # installed by %s" % owner
    first = True
    for declaration in declarations:
        if declaration.secret:
            continue
        name = declaration.name
        if name not in values:
            continue
        if not first:
            lines.append("")
        first = False
        lines.append("# %s" % (declaration.title or name))
        for chunk in _wrap(declaration.help, 74):
            lines.append("# %s" % chunk)
        if declaration.type == "choice":
            lines.append("# one of: %s" % ", ".join(declaration.options))
        if declaration.example:
            lines.append("# for example: %s" % declaration.example)
        lines.append("%s = %s" % (name, toml_value(values[name])))
    secrets_declared = [d.name for d in declarations if d.secret]
    if secrets_declared:
        lines.append("")
        lines.append("# Not here on purpose, they live in w4ve/secrets.json "
                     "(mode 600): %s" % ", ".join(secrets_declared))
        lines.append("# Change one with:  w4ve configure %s" % piece)
    return "\n".join(lines)


def _wrap(text, width):
    out, line = [], ""
    for word in str(text).split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out
