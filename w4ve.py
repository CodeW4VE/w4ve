#!/usr/bin/env python3
"""w4ve, the command line for the W4VE catalog.

    w4ve init                 write a w4ve.toml for this server
    w4ve adopt                write down what the server already has
    w4ve list                 what the catalog has
    w4ve search <text>        find a piece
    w4ve info <id>            everything known about one piece
    w4ve install <id...>      download it and put it where it goes
    w4ve update [id...]       move to the newest version and write it down
    w4ve remove <id...>       take it out
    w4ve sync                 leave the server exactly as w4ve.toml says
    w4ve status               versions, pending restart, health at a glance
    w4ve doctor               find what is wrong before the server does
    w4ve profiles             curated sets of pieces
    w4ve pack <folder>        build a .w4ve out of a folder
    w4ve plugins              what MCDR plugins this server has
    w4ve install <file.w4ve>  install a package, second half included

And the runtime side, RFC-0001:

    w4ve run                  own the server: start it and watch it
    w4ve start | stop         with a guardian already up
    w4ve console              attach to the live console, detach without harm
    w4ve logs                 console and journal, with --follow

Standard library only, on purpose: it has to run on a server without installing
anything first, and be editable by someone who is not a programmer. Same rule
as the registry's build.py. The catalog lives here in one file; `runtime.py`
and `package.py` sit next to it and are imported only if they are there, so a
bare `w4ve.py` still installs mods.

Two files hold the state of a server:

    w4ve.toml         you edit this. It says what the server should have.
    w4ve/state.json   generated. It says what the server actually has.

If they disagree, `w4ve sync` makes the second one match the first.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.3.0"
USER_AGENT = f"w4ve/{VERSION} (+https://w4ve.xyz)"

# The published catalog. Override with --index or W4VE_INDEX, which is how you
# work against a local registry checkout while editing pieces.toml.
DEFAULT_INDEX = ("https://raw.githubusercontent.com/CodeW4VE/registry/"
                 "main/index.json")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME",
                                Path.home() / ".cache")) / "w4ve"
CACHE_TTL = 6 * 3600

# Where each kind of piece goes when the catalog does not say. The catalog's
# install_dir wins; this is the fallback so a new piece type does not silently
# land in the wrong folder.
DEFAULT_DIRS = {
    "fabric-mod": "mods",
    "mcdr-plugin": "plugins",
    "datapack": "world/datapacks",
    "resourcepack": "resourcepacks",
}

# Types the CLI refuses to install: they are infrastructure or code you run
# elsewhere, not something you drop into a server folder.
NOT_INSTALLABLE = {
    "discord-bot": "a bot you host yourself, not a server file",
    # `service` used to mean "infrastructure, not a product". A piece whose
    # second half is a process is now installable like anything else, so the
    # word it needed is `w4ve-package`, below.
    "service": "infrastructure, not a product",
    "runtime": "install it with pip, not into a server folder",
    "library": "a dependency for building mods, not for running one",
    "tool": "you are looking at it",
}

# Release assets that are never what you want installed.
JUNK_SUFFIXES = ("-sources.jar", "-dev.jar", "-javadoc.jar", "-api.jar",
                 ".asc", ".sig", ".txt", ".md")

VERSION_TOKEN = re.compile(r"\d+\.\d+(?:\.\d+)?")


# --------------------------------------------------------------------- output

class Out:
    """Colour when a human is watching, plain text when something else is."""

    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def paint(cls, text, code):
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def ok(cls, text):
        return cls.paint(text, "32")

    @classmethod
    def warn(cls, text):
        return cls.paint(text, "33")

    @classmethod
    def bad(cls, text):
        return cls.paint(text, "31")

    @classmethod
    def dim(cls, text):
        return cls.paint(text, "2")

    @classmethod
    def bold(cls, text):
        return cls.paint(text, "1")


def say(text=""):
    print(text)


def problem(text):
    # Flush stdout first: stderr is unbuffered and stdout is not, so without
    # this the errors of a command arrive before the output they are about.
    sys.stdout.flush()
    print(f"{Out.bad('error')} {text}", file=sys.stderr)
    sys.stderr.flush()


def note(text):
    print(f"{Out.warn('note')}  {text}")


def ask(question, default=False):
    """Yes/no. Anything non interactive answers the default, so this is safe
    to call from a script or a cron without hanging forever."""
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    if not answer:
        return default
    return answer in ("s", "si", "sí", "y", "yes")


# ------------------------------------------------------------------- versions

def vtuple(version):
    """Comparable tuple out of a version string.

    Only the first real version number matters: `1.13.1-w4ve.1` sorts next to
    `1.13.1`, which is what we want for our fork tags. The suffix breaks ties
    so the fork counts as newer than the upstream release it is built on.
    """
    version = str(version or "").lstrip("vV")
    # Skip the Minecraft version some projects put first: Lithium ships
    # `mc1.21-0.13.1`, where the number that orders releases is the second one.
    chosen = None
    for match in VERSION_TOKEN.finditer(version):
        before = version[:match.start()].lower()
        if before.endswith("mc") or before.endswith("mc-"):
            continue
        chosen = match
        break
    chosen = chosen or VERSION_TOKEN.search(version)
    numbers = tuple(int(n) for n in chosen.group(0).split(".")) if chosen else (0,)
    numbers = numbers + (0,) * (3 - len(numbers))
    tail = version[chosen.end():] if chosen else version
    # `shapeboard-1.7.0+26.2` is not newer than `1.7.0`: the `+` half says
    # which Minecraft it was built for, not which release it is. A `-w4ve.1`
    # suffix does count, because that one really is a build of ours on top.
    tail = tail.split("+", 1)[0]
    return numbers, tail


def newer(a, b):
    return vtuple(a) > vtuple(b)


def mc_matches(spec, version, exclude=()):
    """Does `version` fall inside a range like '>=1.21 <1.22'?

    Ranges, not lists, because listing every 1.21.x by hand is how Beaconator
    ended up with four build pipelines. An empty spec means "we do not know",
    which is not the same as "no": callers decide what to do with that.
    """
    if version in (exclude or ()):
        return False
    if not spec:
        return None
    target = vtuple(version)[0]
    for token in str(spec).split():
        token = token.strip(",")
        if token in ("*", "any"):
            continue
        match = re.match(r"(>=|<=|==|=|>|<|\^|~)?\s*(\d[\d.]*)", token)
        if not match:
            continue
        op, bound = match.group(1) or "==", vtuple(match.group(2))[0]
        # A bare '1.21' bound means the 1.21 line, so 1.21.4 is inside it.
        loose = len(match.group(2).split(".")) < len(str(version).split("."))
        if op in (">=",) and not target >= bound:
            return False
        if op == ">" and not target > bound:
            return False
        if op in ("<=",) and not target <= bound:
            return False
        if op == "<" and not target < bound:
            return False
        if op in ("==", "="):
            if loose:
                if target[:len(bound)] != bound[:len(bound)]:
                    return False
            elif target != bound:
                return False
        if op in ("^", "~") and target[0] != bound[0]:
            return False
    return True


def mod_declares_mc(spec, mc):
    """Whether a mod's own `depends.minecraft` covers this version.

    A list means alternatives, so any one of them matching is enough. The `~`
    and `^` of Fabric are near enough to `>=` for the only question we ask
    here, which is whether to warn a human.
    """
    if not spec:
        return None
    options = spec if isinstance(spec, list) else [spec]
    results = [mc_matches(str(option).replace("~", ">=").replace("^", ">="), mc)
               for option in options]
    if any(result is True for result in results):
        return True
    return False if any(result is False for result in results) else None


def looks_like_mc(token):
    """Whether a version number could be a Minecraft version at all.

    `ripdeepslate-1.0.0.jar` carries one version number and it is the mod's
    own. Reading it as a Minecraft version made the CLI announce there was no
    build for 1.21, about a jar that has only ever been built for 1.21.
    """
    numbers = vtuple(token)[0]
    return (numbers[0] == 1 and numbers[1] >= 14) or numbers[0] >= 25


def mc_versions_in(filename):
    return [t for t in VERSION_TOKEN.findall(filename) if looks_like_mc(t)]


def name_mentions_mc(filename, mc):
    """True when a file name carries exactly this Minecraft version.

    Exact on purpose: `shapeboard-1.6.0+1.21.jar` is not a build for 1.21.4,
    and a substring check would happily install it there.
    """
    return mc in mc_versions_in(filename)


# --------------------------------------------------------------------- config

def read_toml(path):
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        return _toml_lite(Path(path).read_text(encoding="utf-8"))


def toml_value(value):
    """One value, written as TOML rather than as JSON.

    They agree on strings, numbers, booleans and lists, and they disagree on
    exactly one thing that matters here: a table. JSON says `{"A": "b"}` and
    TOML says `{A = "b"}`, and writing the JSON one into w4ve.toml does not
    break that line, it breaks **the whole file** for every reader after it.
    Found by a test the day `env` became something a package could declare.
    """
    if isinstance(value, dict):
        inside = ", ".join("%s = %s" % (key, toml_value(item))
                           for key, item in value.items())
        return "{%s}" % inside
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(toml_value(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


def _inline_table(text):
    """`{A = "b", C = 1}` -> dict, for Python 3.9 which has no tomllib."""
    out, depth, current = {}, 0, ""
    parts = []
    for char in text[1:-1]:
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current)
    for part in parts:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        out[key.strip().strip('"')] = raw.strip().strip('"')
    return out


def _toml_lite(text):
    """Enough TOML for w4ve.toml, for Python older than 3.11.

    Sections, strings, numbers, booleans and flat lists. If someone writes
    something fancier than that in a w4ve.toml, the file is too clever.
    """
    data, section = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = data
            for part in line[1:-1].split("."):
                section = section.setdefault(part.strip().strip('"'), {})
            continue
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.strip('"')
        if value.startswith("{") and value.endswith("}"):
            # An inline table: `env = {A = "b"}`. This is what lets a service
            # carry an environment on Python 3.9, which is what the mirror runs.
            (section if section is not None else data)[key] = _inline_table(value)
            continue
        if value.startswith("[") and value.endswith("]"):
            parsed = [v.strip().strip('"\'') for v in value[1:-1].split(",") if v.strip()]
        elif value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        elif value.startswith(('"', "'")):
            parsed = value.strip("\"'")
        else:
            try:
                parsed = int(value)
            except ValueError:
                parsed = value
        (section if section is not None else data)[key] = parsed
    return data


TEMPLATE = """# {name}
# What this server has. You edit this file; `w4ve sync` makes the server obey.

[server]
kind     = "{kind}"          # server or client
mc       = "{mc}"
java     = "{java}"          # what the SERVER runs, not this machine
game_dir = "{game_dir}"          # subfolder Minecraft runs in, "server" under MCDR
# start = "java -Xms8G -Xmx8G -jar fabric-server-launch.jar nogui"
# restart = "docker restart -t 60 <container>"

[pieces]
# id = "version", or "*" for whatever is newest.
"""


def _realign(lines, start, end):
    """Line up the `=` of a section after adding an entry to it.

    A file that a person is meant to read should not drift out of shape every
    time a command writes a line into it. Comments and blank lines are left
    exactly where they were.
    """
    simple = [i for i in range(start, min(end, len(lines)))
              if "=" in lines[i] and not lines[i].strip().startswith("#")
              and lines[i].split("=")[0].strip()]
    if not simple:
        return lines
    width = max(len(lines[i].split("=")[0].strip()) for i in simple)
    for i in simple:
        key, _, value = lines[i].partition("=")
        lines[i] = f"{key.strip().ljust(width)} ={value.rstrip()}"
    return lines


class Server:
    """A folder with a w4ve.toml in it."""

    def __init__(self, root):
        self.root = Path(root)
        self.config_path = self.root / "w4ve.toml"
        self.state_path = self.root / "w4ve" / "state.json"
        self.config = read_toml(self.config_path) if self.config_path.exists() else {}
        self.state = self._load_state()

    @classmethod
    def find(cls, start=None):
        """Walk up looking for w4ve.toml, like git does with .git."""
        here = Path(start or os.environ.get("W4VE_SERVER") or Path.cwd()).resolve()
        for folder in [here, *here.parents]:
            if (folder / "w4ve.toml").exists():
                return cls(folder)
        return None

    # ------------------------------------------------------------- properties

    @property
    def kind(self):
        return self.config.get("server", {}).get("kind", "server")

    @property
    def mc(self):
        return self.config.get("server", {}).get("mc")

    @property
    def game_dir(self):
        """Subfolder Minecraft runs in. `server` under MCDR, `.` on its own."""
        return self.config.get("server", {}).get("game_dir", ".")

    @property
    def java(self):
        return str(self.config.get("server", {}).get("java") or "")

    @property
    def pins(self):
        return dict(self.config.get("pieces", {}))

    # ------------------------------------------------------------------ state

    def _load_state(self):
        """Read the state, and fall back to the last good copy if it is torn.

        Losing this file means w4ve no longer knows what it installed, so a
        half written one is not a reason to start from scratch: the previous
        copy is almost always right, and saying which one is being used beats
        silently forgetting a server.
        """
        for path, what in ((self.state_path, "w4ve/state.json"),
                           (self.state_path.with_suffix(".json.bak"),
                            "the previous copy of w4ve/state.json")):
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                note(f"{what} is unreadable")
        return {"schema": 1, "pieces": {}}

    def save_state(self):
        """Write it whole or not at all, keeping the copy it replaces.

        A power cut in the middle of `write_text` leaves a truncated file, and
        this is the file that says what the server has. Write to a temporary
        name, flush it to the disk itself, then rename: a rename is the one
        operation the filesystem promises is all or nothing.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            shutil.copy2(self.state_path, self.state_path.with_suffix(".json.bak"))
        tmp = self.state_path.with_suffix(".json.writing")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.state, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def installed(self, pid):
        return self.state["pieces"].get(pid)

    @property
    def pending(self):
        return [pid for pid, entry in self.state["pieces"].items()
                if entry.get("pending_restart")]

    def clear_pending(self):
        for entry in self.state["pieces"].values():
            entry.pop("pending_restart", None)
        self.save_state()

    # ------------------------------------------------------- editing the toml

    def set_pin(self, pid, version):
        """Write `id = "version"` into [pieces], keeping comments and layout.

        Text surgery instead of a TOML writer because this file is meant to be
        read and edited by a person: rewriting it from a parsed dict would eat
        every comment they left themselves.
        """
        if not self.config_path.exists():
            return
        lines = self.config_path.read_text(encoding="utf-8").splitlines()
        start, end = None, len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "[pieces]":
                start = i
            elif start is not None and stripped.startswith("[") and i > start:
                end = i
                break
        entry_re = re.compile(rf"^(\s*){re.escape(pid)}(\s*)=")
        if start is not None:
            for i in range(start + 1, end):
                if entry_re.match(lines[i]):
                    indent, pad = entry_re.match(lines[i]).groups()
                    comment = lines[i].split("#", 1)
                    tail = f"  #{comment[1]}" if len(comment) > 1 else ""
                    lines[i] = f'{indent}{pid}{pad}= "{version}"{tail}'
                    self._write_lines(lines)
                    return
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, f'{pid} = "{version}"')
            lines = _realign(lines, start + 1, end + 1)
        else:
            lines += ["", "[pieces]", f'{pid} = "{version}"']
        self._write_lines(lines)

    def set_service(self, name, fields, owner=""):
        """Write a `[services.<name>]` block a person can read and edit.

        Services live in w4ve.toml and not in generated state on purpose: the
        second half of a piece is something an operator has to be able to see,
        comment and change by hand, in the same file as everything else about
        this server.
        """
        self._set_block("services", name, fields, owner)

    def set_watch(self, name, fields, owner=""):
        """Same idea for `[watch.<name>]`: a line to look for and what to do."""
        self._set_block("watch", name, fields, owner)

    def set_http(self, name, fields, owner=""):
        """`[http.<name>]`: a path on the public side, served or proxied."""
        self._set_block("http", name, fields, owner)

    def set_storage(self, name, fields, owner=""):
        """`[storage.<name>]`: a folder with a ceiling and a way to make room."""
        self._set_block("storage", name, fields, owner)

    def set_timer(self, name, fields, owner=""):
        """`[timer.<name>]`: something to run again, watched instead of cron'd."""
        self._set_block("timer", name, fields, owner)

    def remove_block(self, section, name):
        """Take out any one block, whatever section it is in."""
        self._remove_block(section, name)

    # ---------------------------------------------------------------- settings

    def settings_module(self):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import piecesettings
            return piecesettings
        except ImportError:
            return None

    def settings_of(self, pid):
        """What has already been answered for this piece, secrets included.

        Secrets come back too because the caller is usually about to decide
        whether anything is still missing, and a token that is set is not
        missing. Nothing here prints them.
        """
        values = dict((self.config.get("settings") or {}).get(pid) or {})
        module = self.settings_module()
        if module:
            values.update(module.Secrets(self.root).all_for(pid))
        return values

    def plain_settings(self, pid):
        """Only what is in the toml: safe to print, safe to log."""
        return dict((self.config.get("settings") or {}).get(pid) or {})

    def secrets_of(self, pid):
        module = self.settings_module()
        return module.Secrets(self.root).all_for(pid) if module else {}

    def set_setting(self, pid, name, value, declarations=(), owner=""):
        """Write one answer where it belongs, and nowhere else."""
        module = self.settings_module()
        declaration = next((d for d in declarations if d.name == name), None)
        if declaration is not None and declaration.secret:
            module.Secrets(self.root).set(pid, name, value)
            return
        values = self.plain_settings(pid)
        values[name] = value
        self.write_settings(pid, values, declarations, owner)

    def write_settings(self, pid, values, declarations=(), owner=""):
        """Rewrite `[settings.<piece>]` with the help text as comments.

        Comments and layout are not preserved here, unlike everywhere else in
        this file, because this block **is** generated from the declarations:
        the comments are the piece's own help, and regenerating them is how a
        new version of a piece explains a new option to somebody who installed
        the old one.
        """
        module = self.settings_module()
        if module is None or not self.config_path.exists():
            return
        declarations = list(declarations)
        # Anything with a default is written out with it, even if nobody was
        # asked. A value you cannot see is a value you cannot change, and the
        # whole point of this file is that somebody opens it and edits it.
        values = dict(values)
        for declaration in declarations:
            if (not declaration.secret and declaration.name not in values
                    and declaration.default is not None):
                values[declaration.name] = declaration.default
        known = {d.name for d in declarations}
        # Anything the operator added by hand that the piece does not declare
        # is kept: guessing it is rubbish is how a tool eats somebody's work.
        extra = {k: v for k, v in values.items() if k not in known}
        block = module.toml_block(pid, declarations, values, owner)
        if extra:
            block += "\n\n# Kept as you left it (not declared by this piece):\n"
            block += "\n".join("%s = %s" % (k, toml_value(v))
                               for k, v in sorted(extra.items()))
        self._replace_section("settings", pid, block)

    def forget_secrets(self, pid):
        module = self.settings_module()
        if module:
            module.Secrets(self.root).forget(pid)

    def _replace_section(self, section, name, block):
        self._remove_block(section, name)
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text.rstrip("\n") + "\n\n" + block + "\n",
                                    encoding="utf-8")
        self.config = read_toml(self.config_path)

    def _set_block(self, section, name, fields, owner=""):
        if not self.config_path.exists():
            return
        text = self.config_path.read_text(encoding="utf-8")
        if re.search(rf"^\[{section}\.{re.escape(name)}\]", text, re.M):
            self._remove_block(section, name)
            text = self.config_path.read_text(encoding="utf-8")
        header = f"[{section}.{name}]"
        if owner:
            header += f"  # installed by {owner}"
        lines = [header]
        width = max(len(key) for key in fields)
        for key, value in fields.items():
            lines.append(f"{key.ljust(width)} = {toml_value(value)}")
        block = "\n".join(lines)
        text = text.rstrip("\n") + "\n\n" + block + "\n"
        self.config_path.write_text(text, encoding="utf-8")
        self.config = read_toml(self.config_path)

    def remove_service(self, name):
        self._remove_block("services", name)

    def remove_watch(self, name):
        self._remove_block("watch", name)

    def _remove_block(self, section, name):
        """Take the block out, and only that block."""
        if not self.config_path.exists():
            return
        lines = self.config_path.read_text(encoding="utf-8").splitlines()
        start = None
        for i, line in enumerate(lines):
            if re.match(rf"^\[{section}\.{re.escape(name)}\]", line.strip()):
                start = i
                break
        if start is None:
            return
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip().startswith("["):
                end = i
                break
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]
        self.config_path.write_text("\n".join(lines).rstrip("\n") + "\n",
                                    encoding="utf-8")
        self.config = read_toml(self.config_path)

    def remove_pin(self, pid):
        if not self.config_path.exists():
            return
        entry_re = re.compile(rf"^\s*{re.escape(pid)}\s*=")
        lines = [line for line in
                 self.config_path.read_text(encoding="utf-8").splitlines()
                 if not entry_re.match(line)]
        self._write_lines(lines)

    def _write_lines(self, lines):
        self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.config = read_toml(self.config_path)


# -------------------------------------------------------------------- catalog

def fetch(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def load_index(source=None, refresh=False):
    """The catalog, from a local path or over HTTP, cached for a few hours."""
    source = source or os.environ.get("W4VE_INDEX") or DEFAULT_INDEX
    local = Path(source).expanduser()
    if not str(source).startswith(("http://", "https://")):
        if not local.exists():
            problem(f"no index at {local}")
            sys.exit(2)
        return json.loads(local.read_text())

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / "index.json"
    fresh = cached.exists() and time.time() - cached.stat().st_mtime < CACHE_TTL
    if fresh and not refresh:
        return json.loads(cached.read_text())
    try:
        raw = fetch(source)
        cached.write_bytes(raw)
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        if cached.exists():
            note(f"catalog unreachable ({err}), using the cached copy")
            return json.loads(cached.read_text())
        problem(f"cannot reach the catalog: {err}")
        sys.exit(2)


def get_piece(index, pid):
    piece = index["pieces"].get(pid)
    if piece:
        return piece
    # A typo should not send you back to the docs.
    close = [k for k in index["pieces"] if pid.lower() in k.lower()]
    close += [k for k in difflib.get_close_matches(pid, index["pieces"], 3, 0.6)
              if k not in close]
    problem(f"unknown piece '{pid}'"
            + (f". Did you mean: {', '.join(close[:5])}?" if close else ""))
    return None


# ------------------------------------------------------------------ selection

def release_supports_mc(release, mc):
    """True, False, or None when nothing in the release says either way."""
    if not mc:
        return None
    versions = release.get("minecraft")
    if versions:
        return mc in versions
    named = [f for f in release.get("files", []) if mc_versions_in(f["name"])]
    if not named:
        return None  # nothing says either way, which is not a no
    return any(name_mentions_mc(f["name"], mc) for f in named)


def published_for(piece, mc):
    """Whether some release of this piece explicitly ships that Minecraft.

    The `mc` range in the catalog is a promise somebody typed; a release that
    names the version is a fact. When they disagree the fact wins, because the
    alternative is refusing to install a jar that exists: ShapeBoard said
    `>=1.21 <1.22` long after it started shipping a 26.2 build."""
    if not mc:
        return False
    return any(mc in (r.get("minecraft") or ())
               for r in piece.get("releases") or [])


def pick_release(piece, want=None, mc=None):
    """Which release to install, and why not, when there is none."""
    releases = piece.get("releases") or []
    if not releases:
        return None, "no downloadable release yet"
    if want and want != "*":
        for release in releases:
            if release["version"].lstrip("v") == want.lstrip("v"):
                return release, None
        # Same numbers, different dress: a mod says it is `1.64.0` inside its
        # own jar while its release is tagged `v1.64.0-mc1.21.1`. Both are the
        # same build, and refusing to see that breaks every adopted server.
        for release in releases:
            if vtuple(release["version"])[0] == vtuple(want)[0]:
                return release, None
        return None, (f"version {want} is not in the catalog "
                      f"(installed by hand? leaving it alone)")

    stable = [r for r in releases if not r.get("prerelease")] or releases
    if mc:
        exact = [r for r in stable if release_supports_mc(r, mc) is True]
        unknown = [r for r in stable if release_supports_mc(r, mc) is None]
        if exact:
            return exact[0], None
        if unknown:
            return unknown[0], None
        return None, f"no release for Minecraft {mc}"
    return stable[0], None


def pick_file(piece, release, mc=None, explicit=None):
    """Which asset of that release. Ambiguity is reported, never guessed."""
    files = [f for f in release.get("files", [])
             if not f["name"].lower().endswith(JUNK_SUFFIXES)]
    if not files:
        return None, "the release has no installable file"
    if explicit:
        files = [f for f in files if explicit in f["name"]]
        if not files:
            return None, f"no file matching '{explicit}'"
    if len(files) == 1:
        return files[0], None
    if mc:
        tagged = [f for f in files if name_mentions_mc(f["name"], mc)]
        if tagged:
            files = tagged
    if len(files) > 1:
        primary = [f for f in files if f.get("primary")]
        if primary:
            files = primary
    if len(files) == 1:
        return files[0], None
    names = ", ".join(f["name"] for f in files[:6])
    return None, f"several files match, pick one with --file: {names}"


def read_file_meta(path):
    """What a jar or plugin says about itself, from inside the file.

    File names lie: `ferritecore.jar`, `Carpet Extra.jar` and
    `ServerReplay-1.1.2+mc1.21 (1).jar` are all real files on a real server of
    ours. The manifest inside does not lie, and every Fabric mod and MCDR
    plugin carries one.
    """
    try:
        import zipfile
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "fabric.mod.json" in names:
                # Some mods ship trailing commas or raw newlines in strings.
                raw = archive.read("fabric.mod.json").decode("utf-8", "replace")
                data = json.loads(re.sub(r",(\s*[}\]])", r"\1",
                                         raw.replace("\n", " ")))
                # `depends.minecraft` is a string in most mods and a list of
                # alternatives in others. Both are valid and both are in mods/.
                return {"id": data.get("id"), "version": data.get("version"),
                        "name": data.get("name"),
                        "mc": data.get("depends", {}).get("minecraft")}
            for candidate in ("mcdreforged.plugin.json", "plugin.json"):
                if candidate in names:
                    data = json.loads(archive.read(candidate))
                    return {"id": data.get("id"), "version": data.get("version"),
                            "name": data.get("name"), "mc": None}
    except Exception:
        # A file we cannot read is a file we say nothing about, never a crash.
        pass
    return None


def match_piece(index, meta, filename):
    """Which catalog piece a file on disk is, and which release, if known.

    The file name is checked first because when it matches, it also tells us
    the version the catalog calls this build, which is not always the version
    the mod calls itself.
    """
    for pid, piece in index["pieces"].items():
        for release in piece.get("releases", []):
            if any(f["name"] == filename for f in release.get("files", [])):
                return pid, release["version"]
    if meta and meta.get("id"):
        wanted = meta["id"].lower().replace("_", "-")
        for pid, piece in index["pieces"].items():
            candidates = {pid, (piece.get("modrinth") or "").lower(),
                          (piece.get("repo") or "").split("/")[-1].lower(),
                          (piece.get("name") or "").lower().replace(" ", "-")}
            if wanted in candidates - {""}:
                return pid, None
    return None, None


def mc_sensitive(piece):
    """Does this piece care which Minecraft the server runs?

    A jar does. A Python plugin does not: PrimeBackup 1.13.1 is the same file
    on every version, and filtering it by the numbers in its own file name is
    how it got dropped from a 1.21 server that could run it fine.
    """
    # A `.w4ve` carries every version at once and picks at install time, so
    # filtering its release by the server's Minecraft would hide the one file
    # that was going to work.
    return piece.get("type") in ("fabric-mod", "library", "datapack",
                                 "resourcepack")


def conflicts_of(piece):
    """Normalise `conflicts` into entries with versions.

    A bare id in the list means every version with every version, which is
    almost never true and is why the mirror got told its own working setup was
    broken. Entries with `mine` and `theirs` say what actually clashes.
    """
    entries = []
    for item in piece.get("conflicts", []) or []:
        if isinstance(item, str):
            entries.append({"with": item, "mine": None, "theirs": None,
                            "why": None})
        elif item.get("with"):
            entries.append({"with": item["with"], "mine": item.get("mine"),
                            "theirs": item.get("theirs"),
                            "why": item.get("why")})
    return entries


def clashes(entry, my_version, their_version):
    """True only when both versions fall inside the declared ranges."""
    for spec, version in ((entry["mine"], my_version),
                          (entry["theirs"], their_version)):
        if spec and version and mc_matches(spec, version) is False:
            return False
    return True


def newest_for(piece, server):
    """The newest version installable on THIS server, not in the world.

    `latest` in the catalog is Carpet 26.2. Offering that to a 1.21 server is
    offering to break it, which is worse than saying nothing.
    """
    release, _ = pick_release(piece, None,
                              server.mc if mc_sensitive(piece) else None)
    return release["version"] if release else None


def entry_files(server, entry):
    """Every file a piece owns, whichever way it got installed.

    A catalog piece is one file and knows its path. A package can be several
    files in several folders, and the ones under `w4ve/` belong to the runtime
    rather than to the game. Everything that walks over installed files goes
    through here so neither shape is a special case twice.
    """
    if entry.get("source") == "package":
        out = []
        for relative, digest in (entry.get("files") or {}).items():
            base = (server.root if relative.startswith("w4ve/")
                    else server.root / server.game_dir)
            out.append((relative, digest, base / relative))
        return out
    path = entry.get("path")
    if not path:
        return []
    return [(path, entry.get("sha256"), server.root / path)]


def install_dir_for(piece, server=None):
    """Where a piece goes, relative to the folder with the w4ve.toml.

    Under MCDR the game lives in a subfolder and the plugins do not: mods go
    to `server/mods` while plugins go to `plugins`. That is the layout of
    every server we run, so it is the layout the CLI has to understand.
    """
    folder = piece.get("install_dir") or DEFAULT_DIRS.get(piece.get("type")) \
        or "mods"
    game = (server.game_dir if server else ".")
    if game != "." and piece.get("type") != "mcdr-plugin":
        return f"{game}/{folder}"
    return folder


def applies_hot(piece):
    """Whether landing this file changes anything without a restart.

    Until pieces ship a w4ve.json declaring it per component, the type is the
    honest approximation: a jar always waits for the next boot, a Python
    plugin can be reloaded in place.
    """
    return piece.get("type") == "mcdr-plugin"


# ----------------------------------------------------------------- installing

def _umask():
    """Read the umask without leaving it changed. There is no way to ask."""
    current = os.umask(0o022)
    os.umask(current)
    return current


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path, hashes):
    """Check whatever hash the source published. Modrinth gives sha1 and
    sha512, PyPI gives sha256, GitHub gives nothing."""
    for algorithm in ("sha512", "sha256", "sha1"):
        expected = (hashes or {}).get(algorithm)
        if not expected:
            continue
        digest = hashlib.new(algorithm)
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest() == expected, algorithm
    return None, None


def download(url, dest, expected_hashes=None, quiet=False):
    """Download to a temporary file, verify, then move into place.

    Never writes a partial file where a server would load it: a half
    downloaded jar in mods/ is a server that does not boot.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    os.close(handle)
    temp = Path(temp)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(temp, "wb") as fh:
                while True:
                    block = response.read(1 << 16)
                    if not block:
                        break
                    fh.write(block)
                    done += len(block)
                    if not quiet and total and sys.stdout.isatty():
                        percent = done * 100 // total
                        print(f"\r  {dest.name}  {percent:3d}%", end="")
        if not quiet and sys.stdout.isatty():
            print("\r" + " " * 60 + "\r", end="")
        good, algorithm = verify(temp, expected_hashes)
        if good is False:
            temp.unlink(missing_ok=True)
            return None, f"{algorithm} does not match, refusing to install"
        digest = sha256_of(temp)
        # mkstemp makes the file 0600. A jar the server process cannot read is
        # a server that boots without the mod, and in a container the game
        # runs as a different user than whoever ran this command.
        os.chmod(temp, 0o644 & ~_umask())
        shutil.move(str(temp), str(dest))
        return digest, None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        temp.unlink(missing_ok=True)
        return None, str(err)


def install_catalog_package(server, pid, release, asset, dry_run, quiet):
    """Download a `.w4ve` from the catalog and install it as a package.

    Downloaded to a temporary file and never kept: the package is a delivery
    format, and leaving a copy of it in the server folder would be one more
    thing nobody knows whether they can delete.
    """
    current = server.installed(pid)
    if current and current.get("version") == release["version"]:
        return False, None
    if dry_run:
        version_from = current["version"] if current else "-"
        return True, f"{pid}  {version_from} -> {release['version']}  (package)"

    pkg = package_module()
    if pkg is None:
        return False, f"{pid}: this w4ve cannot read packages (package.py is missing)"

    folder = Path(tempfile.mkdtemp(prefix="w4ve-pkg-"))
    try:
        target = folder / asset["name"]
        _digest, err = download(asset["url"], target, asset.get("hashes"),
                                quiet=quiet)
        if err:
            return False, f"{pid}: {err}"
        # Straight through the same path as a package on disk, so a piece from
        # the catalog and a piece from a file cannot behave differently.
        args = argparse.Namespace(dry_run=False, force=True, yes=True,
                                  dir=str(server.root))
        code = install_packages(server, [target], args, report=False)
        if code:
            return False, f"{pid}: the package did not install"
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return True, None


def install_one(server, index, pid, want=None, explicit_file=None,
                dry_run=False, quiet=False):
    """Install or move a single piece. Returns (changed, message)."""
    piece = get_piece(index, pid)
    if not piece:
        return False, None

    kind = piece.get("type")
    if kind in NOT_INSTALLABLE:
        return False, f"{pid}: {NOT_INSTALLABLE[kind]}"

    side = piece.get("side", "both")
    if side != "both" and side != server.kind:
        return False, (f"{pid} is {side}-side and this is a {server.kind}. "
                       f"Install it in the other one.")

    supported = mc_matches(piece.get("mc"), server.mc, piece.get("mc_exclude", []))
    if supported is False and not published_for(piece, server.mc):
        return False, (f"{pid} supports Minecraft {piece['mc']}, "
                       f"this server is {server.mc}")

    mc = server.mc if mc_sensitive(piece) else None
    release, why = pick_release(piece, want, mc)
    if not release:
        return False, f"{pid}: {why}"
    asset, why = pick_file(piece, release, mc, explicit_file)
    if not asset:
        return False, f"{pid}: {why}"

    # A piece whose release is a `.w4ve` is a whole piece, not a file, and it
    # is what makes `w4ve install wavechat-server` mean the same as installing
    # the package by hand. Without this the catalog can only offer the half of
    # a piece that happens to be a jar, which is the gap this whole format
    # exists to close.
    if asset["name"].endswith(".w4ve"):
        return install_catalog_package(server, pid, release, asset, dry_run, quiet)

    current = server.installed(pid)
    if current and current.get("source") == "package":
        return False, (f"{pid} was installed from a .w4ve package; "
                       f"reinstall it with the package, not from the catalog")
    if current and (server.root / current["path"]).exists() \
            and vtuple(current["version"])[0] == vtuple(release["version"])[0]:
        # Right version, possibly under a different file name: an adopted
        # server has `Carpet Extra.jar`, the catalog calls it
        # `carpet-extra-1.21-1.4.148.jar`. Downloading the second one would
        # leave two copies of the same mod, which is a server that does not
        # boot. Having the version is what matters, not the file name.
        return False, None

    destination = server.root / install_dir_for(piece, server) / asset["name"]
    if dry_run:
        version_from = current["version"] if current else "-"
        return True, f"{pid}  {version_from} -> {release['version']}"

    digest, err = download(asset["url"], destination,
                           asset.get("hashes"), quiet=quiet)
    if err:
        return False, f"{pid}: {err}"

    # Remove the file the previous version left behind, but only if it is not
    # the one we just wrote (same name, new content, happens on reinstalls).
    if current:
        stale = server.root / current["path"]
        if stale.exists() and stale != destination:
            stale.unlink()

    server.state["pieces"][pid] = {
        "version": release["version"],
        "file": asset["name"],
        "path": str(destination.relative_to(server.root)),
        "sha256": digest,
        "source": release.get("source"),
        "url": asset["url"],
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not applies_hot(piece):
        server.state["pieces"][pid]["pending_restart"] = True
    server.save_state()
    server.set_pin(pid, release["version"])

    how = "reload it in place" if applies_hot(piece) else "waits for restart"
    version_from = f"{current['version']} -> " if current else ""
    return True, f"{Out.ok('ok')}  {pid}  {version_from}{release['version']}  " \
                 f"{Out.dim('(' + how + ')')}"


def resolve_deps(index, pids, server):
    """Requirements the server does not have yet. One level, on purpose: the
    day this needs backtracking, the catalog is the thing that is wrong."""
    missing, seen = [], set(pids)
    queue = list(pids)
    while queue:
        piece = index["pieces"].get(queue.pop())
        if not piece:
            continue
        for dep in piece.get("requires", []):
            if dep in seen or server.installed(dep) or dep in server.pins:
                continue
            if index["pieces"].get(dep, {}).get("type") in NOT_INSTALLABLE:
                continue
            seen.add(dep)
            missing.append(dep)
            queue.append(dep)
    return missing


def check_conflicts(index, wanted, server):
    """Clashes between what is about to be installed and what is already here.

    `wanted` maps id to the version being installed, or None for "the newest
    one". Versions matter: the answer for carpet-tis 1.64 is not the answer
    for 1.82.
    """
    found = []
    for pid, version in wanted.items():
        piece = index["pieces"].get(pid, {})
        mine = version or newest_for(piece, server)
        for entry in conflicts_of(piece):
            other = entry["with"]
            installed = server.installed(other)
            if other in wanted:
                theirs = wanted[other] or newest_for(
                    index["pieces"].get(other, {}), server)
            elif installed:
                theirs = installed["version"]
            else:
                continue
            if clashes(entry, mine, theirs):
                found.append((pid, other, entry.get("why")))
    return found


# ------------------------------------------------------------------- commands

def check_interrupted(server):
    """Clean up after an install that was killed, before doing anything else.

    Anything that touches this server has to run this first: a half applied
    piece is not a state anyone should be allowed to build on top of.
    """
    pkg = package_module()
    if pkg is None:
        return
    found = pkg.interrupted(server)
    if not found:
        return
    problem(f"an install of {found['piece']} {found['version']} was interrupted")
    result = pkg.roll_back_interrupted(server)
    if not result:
        return
    say(f"{Out.ok('ok')}  rolled it back: "
        f"{len(result['undone'])} file{'s' if len(result['undone']) != 1 else ''} "
        f"removed, {len(result['restored'])} put back")
    for path in result["kept"]:
        note(f"could not undo {path}, look at it by hand")


def need_server(args):
    server = Server.find(getattr(args, "dir", None))
    if not server:
        problem("no w4ve.toml here or above. Run `w4ve init` first.")
        sys.exit(2)
    return server


def cmd_init(args, index):
    root = Path(args.dir or ".").resolve()
    path = root / "w4ve.toml"
    if path.exists() and not args.force:
        problem(f"{path} already exists (use --force to overwrite)")
        return 1
    # Deliberately not detected: on a host running containers, the `java` on
    # PATH is not the one the server runs, and a wrong value here makes
    # `doctor` cry wolf forever.
    java = ""
    # Where the game runs. Getting this wrong is the quiet kind of wrong: jars
    # land where Minecraft never reads, and a plugin writing a relative path
    # writes it into the wrong folder and says nothing.
    #
    # ⚠️ MCDR's own config is asked FIRST, and it is the only one of these that
    # is a statement rather than a guess. Looking for `server/mods` was the
    # only test for a while, and it says "." for a vanilla server under MCDR,
    # which is every one that runs no mods.
    mcdr = read_mcdr_config(root)
    start = (mcdr or {}).get("start_command") or ""
    if mcdr and mcdr.get("working_directory"):
        game_dir = str(mcdr["working_directory"]).strip("/") or "."
        found = "MCDR's config.yml says so"
    elif (root / "server" / "mods").is_dir():
        game_dir, found = "server", "there is a server/mods here"
    elif (root / "server").is_dir() and any(
            (root / "server").glob("*.jar")):
        game_dir, found = "server", "the jar is in server/"
    else:
        game_dir, found = ".", ""
    body = TEMPLATE.format(
        name=args.name or root.name,
        kind="client" if args.client else "server",
        mc=args.mc or "1.21",
        java=java or "21",
        game_dir=game_dir)
    if start:
        # Copied, never rewritten: the flags on it are somebody's tuning, and
        # a start command that is nearly right is a different server.
        body = body.replace(
            '# start = "java -Xms8G -Xmx8G -jar fabric-server-launch.jar nogui"',
            'start    = %s' % json.dumps(start))
    path.write_text(body)
    say(f"{Out.ok('ok')}  wrote {path}")
    if found:
        say(Out.dim(f"    game_dir = \"{game_dir}\", because {found}"))
    if start:
        say(Out.dim("    start command copied from MCDR's config.yml"))
    say(Out.dim("    `w4ve adopt` writes down what is already installed"))
    return 0


def cmd_adopt(args, index):
    """Write down what a server already has, so w4ve can take over from here.

    Every server we run existed long before this tool did. Without this, the
    first thing the CLI says about a real server is 33 lines of "I did not
    install this", which is true and useless.
    """
    server = need_server(args)
    folders = [server.root / install_dir_for({"type": "fabric-mod"}, server),
               server.root / "plugins"]
    found, unknown, unmanageable, updated = [], [], [], 0
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.is_dir() or path.suffix.lower() not in \
                    (".jar", ".mcdr", ".pyz"):
                continue
            meta = read_file_meta(path)
            pid, catalog_version = match_piece(index, meta, path.name)
            # Adopting something the CLI would then refuse to install leaves
            # w4ve.toml asking for a file no command can ever produce.
            if pid and index["pieces"].get(pid, {}).get("type") in NOT_INSTALLABLE:
                unmanageable.append((path.relative_to(server.root), pid))
                continue
            if not pid:
                unknown.append((path.relative_to(server.root), meta))
                continue
            version = catalog_version or (meta or {}).get("version") or "*"
            found.append((pid, version, path))

    for pid, version, path in found:
        say(f"  {Out.ok(pid.ljust(22))} {version:<24} {path.name}")
        if args.dry_run:
            continue
        server.state["pieces"][pid] = {
            "version": version,
            "file": path.name,
            "path": str(path.relative_to(server.root)),
            "sha256": sha256_of(path),
            "source": "adopted",
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        server.set_pin(pid, version)
        updated += 1

    if unmanageable:
        say()
        say(f"{len(unmanageable)} in the catalog but not installable by w4ve:")
        for path, pid in unmanageable:
            say(Out.dim(f"  {path}  ({pid}, "
                        f"{NOT_INSTALLABLE[index['pieces'][pid]['type']]})"))

    if unknown:
        say()
        say(f"{len(unknown)} not in the catalog, left alone:")
        for path, meta in unknown:
            label = (meta or {}).get("id") or "?"
            say(Out.dim(f"  {path}  ({label})"))

    if not args.dry_run:
        server.save_state()
        say()
        say(f"{Out.ok('ok')}  adopted {updated} pieces into {server.config_path.name}")
    else:
        say(Out.dim("\ndry run, nothing was written"))
    return 0


def cmd_list(args, index):
    pieces = index["pieces"]
    server = Server.find(getattr(args, "dir", None))
    if args.profile:
        profile = index["profiles"].get(args.profile)
        if not profile:
            problem(f"unknown profile '{args.profile}'")
            return 2
        wanted = profile["pieces"]
        pieces = {k: v for k, v in pieces.items() if k in wanted}
    rows = []
    for pid, piece in sorted(pieces.items()):
        if args.type and piece.get("type") != args.type:
            continue
        if args.side and piece.get("side") not in (args.side, "both"):
            continue
        if args.installed and not (server and server.installed(pid)):
            continue
        mark = ""
        if server and server.installed(pid):
            have = server.installed(pid)["version"]
            latest = newest_for(piece, server)
            mark = Out.warn(f"update -> {latest}") \
                if latest and newer(latest, have) else Out.ok("installed")
        rows.append((pid, piece.get("latest") or "-", piece.get("type", ""),
                     piece.get("summary", ""), mark))
    if not rows:
        say("nothing matches")
        return 0
    width = max(len(r[0]) for r in rows)
    for pid, latest, kind, summary, mark in rows:
        line = f"{Out.bold(pid.ljust(width))}  {latest:<12} {kind:<12} {summary[:60]}"
        say(line + (f"  {mark}" if mark else ""))
    say(Out.dim(f"\n{len(rows)} pieces"))
    return 0


def cmd_search(args, index):
    needle = args.text.lower()
    hits = []
    for pid, piece in sorted(index["pieces"].items()):
        haystack = " ".join([pid, piece.get("name", ""), piece.get("summary", ""),
                             " ".join(piece.get("tags", []))]).lower()
        if needle in haystack:
            hits.append((pid, piece))
    for pid, piece in hits:
        say(f"{Out.bold(pid)}  {piece.get('latest') or '-'}")
        say(f"    {piece.get('summary', '')}")
    if not hits:
        say(f"nothing matches '{args.text}'")
    return 0


def cmd_info(args, index):
    piece = get_piece(index, args.id)
    if not piece:
        return 2
    say(f"{Out.bold(piece.get('name', args.id))}  {piece.get('latest') or '-'}")
    say(f"  {piece.get('summary', '')}")
    say()
    facts = [
        ("id", piece["id"]),
        ("type", f"{piece.get('type')} ({piece.get('side')}-side)"),
        ("status", piece.get("status")),
        ("minecraft", piece.get("mc") or "any"),
        ("origin", piece.get("origin")),
        ("repo", piece.get("repo") and f"https://github.com/{piece['repo']}"),
        ("modrinth", piece.get("modrinth")
         and f"https://modrinth.com/mod/{piece['modrinth']}"),
        ("requires", ", ".join(piece.get("requires", [])) or None),
        ("conflicts", ", ".join(piece.get("conflicts", [])) or None),
        ("installs to", install_dir_for(piece)),
    ]
    for label, value in facts:
        if value:
            say(f"  {label:<12} {value}")
    info = piece.get("modrinth_info") or {}
    if info.get("status") and info["status"] != "approved":
        say(f"  {Out.warn('modrinth')}     {info['status']}, not downloadable there yet")
    releases = piece.get("releases") or []
    if releases:
        say(f"\n  {Out.bold('releases')}")
        for release in releases[:5]:
            files = len(release.get("files", []))
            say(f"    {release['version']:<20} {release.get('source', ''):<9} "
                f"{files} file{'s' if files != 1 else ''}"
                + ("  (prerelease)" if release.get("prerelease") else ""))
    else:
        say(f"\n  {Out.warn('no downloadable release')}")
    return 0


def cmd_profiles(args, index):
    for name, profile in index["profiles"].items():
        say(f"{Out.bold(name)}  {profile.get('name', '')}")
        say(f"  {profile.get('description', '')}")
        say(Out.dim(f"  {len(profile['pieces'])} pieces: "
                    f"{', '.join(profile['pieces'])}"))
        say()
    say(Out.dim("install one with: w4ve install --profile <name>"))
    return 0


def cmd_install(args, index):
    server = need_server(args)
    packages = [t for t in args.ids if t.endswith(".w4ve")]
    if packages:
        if len(packages) != len(args.ids) or args.profile:
            problem("install a .w4ve package on its own, not mixed with catalog ids")
            return 2
        return install_packages(server, packages, args)
    targets = list(args.ids)
    if args.profile:
        profile = index["profiles"].get(args.profile)
        if not profile:
            problem(f"unknown profile '{args.profile}'")
            return 2
        targets += [p for p in profile["pieces"] if p not in targets]
    if not targets:
        problem("nothing to install")
        return 2

    wanted = {}
    for target in targets:
        pid, _, version = target.partition("@")
        wanted[pid] = version or server.pins.get(pid) or None

    conflicts = check_conflicts(index, wanted, server)
    for pid, bad, why in conflicts:
        problem(f"{pid} breaks with the {bad} you have"
                + (f": {why}" if why else ""))
    if conflicts and not args.force:
        say(Out.dim("use --force if you know what you are doing"))
        return 1

    missing = resolve_deps(index, list(wanted), server)
    if missing:
        say(f"also needs: {', '.join(missing)}")
        if args.yes or ask("install them too?", default=True):
            for dep in missing:
                wanted.setdefault(dep, None)

    changed = 0
    for pid, version in wanted.items():
        did, message = install_one(server, index, pid, version,
                                   args.file if len(wanted) == 1 else None,
                                   dry_run=args.dry_run)
        if message:
            say(message if did else f"{Out.warn('skip')}  {message}")
        changed += bool(did)
    return report_pending(server, changed, args)


def cmd_update(args, index):
    server = need_server(args)
    targets = args.ids or list(server.state["pieces"])
    if not targets:
        say("nothing installed yet")
        return 0
    changed = 0
    for pid in targets:
        piece = index["pieces"].get(pid)
        current = server.installed(pid)
        if not piece or not current:
            note(f"{pid} is not installed")
            continue
        release, why = pick_release(piece, None,
                                    server.mc if mc_sensitive(piece) else None)
        if not release:
            note(f"{pid}: {why}")
            continue
        if not newer(release["version"], current["version"]):
            if args.verbose:
                say(Out.dim(f"    {pid} {current['version']} is the newest"))
            continue
        # The newest version of one piece can be the one that breaks another.
        # Carpet TIS 1.82 and Lithium 0.13 is not a hypothetical: it stopped
        # the mirror from booting in August.
        clash = check_conflicts(index, {pid: release["version"]}, server)
        if clash and not args.force:
            for _, other, why in clash:
                note(f"holding {pid} at {current['version']}: "
                     f"{release['version']} breaks with the {other} you have"
                     + (f" ({why})" if why else ""))
            continue
        did, message = install_one(server, index, pid, release["version"],
                                   dry_run=args.dry_run)
        if message:
            say(message)
        changed += bool(did)
    if not changed:
        say("everything is up to date")
    return report_pending(server, changed, args)


def cmd_remove(args, index):
    server = need_server(args)
    removed = 0
    for pid in args.ids:
        entry = server.installed(pid)
        if not entry:
            note(f"{pid} is not installed")
            continue
        if entry.get("source") == "package":
            removed += bool(remove_package(server, pid, args))
            continue
        path = server.root / entry["path"]
        if path.exists():
            if args.dry_run:
                say(f"would delete {entry['path']}")
            else:
                path.unlink()
        if not args.dry_run:
            server.state["pieces"].pop(pid, None)
            server.save_state()
            server.remove_pin(pid)
        say(f"{Out.ok('ok')}  removed {pid}")
        removed += 1
    if removed and not args.dry_run:
        # A jar needs a restart; a plugin does not. Saying "mod" for both is
        # how somebody restarts a server full of people for no reason.
        note("anything that was a jar needs a restart to take effect")
    return 0


def cmd_sync(args, index):
    """Make the server match w4ve.toml. Extra pieces are reported, not deleted:
    guessing that a file is garbage and deleting it is how you lose a world."""
    server = need_server(args)
    pins = server.pins
    if not pins:
        say("w4ve.toml has no [pieces], nothing to sync")
        return 0

    changed = 0
    for pid, version in pins.items():
        did, message = install_one(server, index, pid,
                                   None if version in ("*", "latest") else version,
                                   dry_run=args.dry_run)
        if message:
            say(message if did else "    " + message)
        changed += bool(did)

    extra = [pid for pid in server.state["pieces"] if pid not in pins]
    for pid in extra:
        note(f"{pid} is installed but not in w4ve.toml "
             f"(remove it with `w4ve remove {pid}`)")
    if not changed and not extra:
        say(f"{Out.ok('ok')}  the server already matches w4ve.toml")
    return report_pending(server, changed, args)


def report_pending(server, changed, args):
    """Offer the restart, never take it.

    Only after something actually changed: a command that installed nothing
    has no business asking to restart a server with people on it.
    """
    if args.dry_run:
        say(Out.dim("dry run, nothing was written"))
        return 0
    pending = server.pending
    if not pending or not changed:
        return 0
    say()
    say(f"{len(pending)} piece{'s' if len(pending) > 1 else ''} "
        f"waiting for a restart: {', '.join(pending)}")
    # If we own the server, restarting it is our job and not a shell command
    # somebody has to remember to write down.
    rt = runtime_module()
    if rt is not None:
        client = rt.Client(server.root)
        if client.connected():
            return restart_with_guardian(server, client, args)

    restart = server.config.get("server", {}).get("restart")
    if not restart:
        say(Out.dim("    no restart command in w4ve.toml, and no guardian "
                    "running, so this one is yours"))
        return 0
    if args.yes or ask(f"run `{restart}` now?", default=False):
        result = subprocess.run(restart, shell=True)
        if result.returncode == 0:
            server.clear_pending()
            say(f"{Out.ok('ok')}  restarted")
        else:
            problem(f"restart command exited {result.returncode}")
    else:
        say(Out.dim("    it stays pending, `w4ve status` will remind you"))
    return 0


def restart_with_guardian(server, client, args):
    """Stop and start through the guardian, which knows how to wait."""
    if not (args.yes or ask("restart the server now?", default=False)):
        say(Out.dim("    it stays pending, `w4ve status` will remind you"))
        return 0
    stopped = client.call({"cmd": "stop"}, timeout=180)
    if not stopped.get("ok"):
        problem(stopped.get("error") or stopped.get("message"))
        return 1
    say(f"{Out.ok('ok')}  {stopped['message']}")
    started = client.call({"cmd": "start"}, timeout=60)
    if not started.get("ok"):
        problem(started.get("error") or started.get("message"))
        return 1
    say(f"{Out.ok('ok')}  {started['message']}")
    server.clear_pending()
    say(Out.dim("    `w4ve status` says when it is ready again"))
    return 0


def cmd_status(args, index):
    server = need_server(args)
    config = server.config.get("server", {})
    say(f"{Out.bold(server.root.name)}  "
        f"{config.get('kind', 'server')}  Minecraft {server.mc or '?'}  "
        f"Java {server.java or '?'}")
    say(Out.dim(f"  {server.config_path}"))
    say()
    rt = runtime_module()
    if rt is not None:
        report_runtime(server, rt)
    entries = server.state["pieces"]
    if not entries:
        say("nothing installed yet. `w4ve sync` installs what w4ve.toml says.")
        return 0
    width = max(len(pid) for pid in entries)
    for pid in sorted(entries):
        entry = entries[pid]
        piece = index["pieces"].get(pid, {})
        latest = newest_for(piece, server) if piece else None
        flag = ""
        if latest and newer(latest, entry["version"]):
            flag = Out.warn(f"update -> {latest}")
        if entry.get("pending_restart"):
            flag = (flag + "  " if flag else "") + Out.warn("waits for restart")
        if missing_settings(server, pid):
            flag = (flag + "  " if flag else "") + Out.warn("not configured")
        say(f"  {pid.ljust(width)}  {entry['version']:<20} {flag}")
    pending = server.pending
    if pending:
        say()
        say(Out.warn(f"{len(pending)} waiting for a restart"))
    unconfigured = [pid for pid in entries if missing_settings(server, pid)]
    if unconfigured:
        say()
        for pid in unconfigured:
            say(Out.warn(f"`w4ve configure {pid}` before it can run"))
    return 0


# Asked of the interpreter the service will really use. One process, both
# spellings, and no import: a distribution that is installed answers here even
# when its import name is nothing like it.
DISTRIBUTION_PROBE = """
import importlib.metadata as meta, sys
for name in sys.argv[1:]:
    try:
        meta.distribution(name)
        sys.exit(0)
    except Exception:
        pass
sys.exit(1)
"""


def cmd_doctor(args, index):
    """Everything that is wrong, before the server finds out at 3am."""
    server = need_server(args)
    problems, warnings = [], []
    entries = server.state["pieces"]
    pins = server.pins

    if not server.mc:
        warnings.append("w4ve.toml does not say which Minecraft version this is, "
                        "so version checks are skipped")

    # 1. what the file says versus what is on disk
    for pid, version in pins.items():
        entry = entries.get(pid)
        if not entry:
            problems.append(f"{pid} is in w4ve.toml but not installed "
                            f"(`w4ve sync`)")
        elif version not in ("*", "latest") and entry["version"] != version:
            problems.append(f"{pid}: w4ve.toml says {version}, "
                            f"installed is {entry['version']}")
    for pid in entries:
        if pid not in pins:
            warnings.append(f"{pid} is installed but not written in w4ve.toml")

    # 2. files that vanished or changed behind our back
    for pid, entry in entries.items():
        for relative, digest, path in entry_files(server, entry):
            if not path.exists():
                problems.append(f"{pid}: {relative} is gone")
            elif relative in (entry.get("editable") or []):
                continue        # a config the operator owns, edits expected
            elif digest and sha256_of(path) != digest:
                warnings.append(f"{pid}: {relative} changed since it was "
                                f"installed (replaced by hand?)")

    # 3. version and side sanity
    seen_pairs = set()
    for pid, entry in entries.items():
        piece = index["pieces"].get(pid)
        if not piece:
            warnings.append(f"{pid} is installed but no longer in the catalog")
            continue
        if (server.mc
                and mc_matches(piece.get("mc"), server.mc,
                               piece.get("mc_exclude", [])) is False
                and not published_for(piece, server.mc)):
            problems.append(f"{pid} supports {piece['mc']}, "
                            f"this server is {server.mc}")
        side = piece.get("side", "both")
        if side != "both" and side != server.kind:
            problems.append(f"{pid} is {side}-side on a {server.kind}")
        for dep in piece.get("requires", []):
            if dep not in entries and index["pieces"].get(dep, {}).get("type") \
                    not in NOT_INSTALLABLE:
                problems.append(f"{pid} needs {dep}, which is not installed")
        for conflict in conflicts_of(piece):
            other = conflict["with"]
            if other not in entries or (other, pid) in seen_pairs:
                continue
            if clashes(conflict, entry["version"], entries[other]["version"]):
                seen_pairs.add((pid, other))
                problems.append(
                    f"{pid} {entry['version']} breaks with {other} "
                    f"{entries[other]['version']}"
                    + (f": {conflict['why']}" if conflict.get("why") else ""))

    # 4. what is actually sitting in mods/, according to the jars themselves
    managed = {path.resolve()
               for entry in entries.values()
               for _, _, path in entry_files(server, entry)}
    mods_dir = server.root / install_dir_for({"type": "fabric-mod"}, server)
    if mods_dir.is_dir():
        by_id, unmanaged = {}, 0
        for jar in sorted(mods_dir.glob("*.jar")):
            meta = read_file_meta(jar) or {}
            by_id.setdefault(meta.get("id") or jar.name, []).append((jar.name, meta))
            if jar.resolve() not in managed:
                unmanaged += 1
            # A mod that declares a Minecraft it does not have is the reason a
            # server boots into "Incompatible mods found" at the worst moment.
            if server.mc and mod_declares_mc(meta.get("mc"), server.mc) is False:
                warnings.append(f"{jar.name} declares Minecraft {meta['mc']}, "
                                f"this server is {server.mc}")
        for mod_id, jars in by_id.items():
            if len(jars) > 1:
                problems.append(f"two jars of '{mod_id}': "
                                f"{', '.join(name for name, _ in jars)}")
        if unmanaged:
            warnings.append(f"{unmanaged} of {sum(len(v) for v in by_id.values())} "
                            f"jars were not installed by w4ve "
                            f"(`w4ve adopt` writes down the ones it knows)")

    # 5. the Java the server actually runs
    if server.java:
        try:
            out = subprocess.run(["java", "-version"], capture_output=True,
                                 text=True, timeout=10)
            found = re.search(r'"(\d+)', out.stderr or out.stdout)
            if found and found.group(1) != server.java:
                warnings.append(f"w4ve.toml says Java {server.java}, "
                                f"`java` on this machine is {found.group(1)}")
        except (OSError, subprocess.SubprocessError):
            warnings.append("no `java` on PATH to check against w4ve.toml")

    # 6. an install that never finished
    pkg = package_module()
    found = pkg.interrupted(server) if pkg else None
    if found:
        problems.append(f"an install of {found['piece']} {found['version']} was "
                        f"interrupted and never rolled back: the next `w4ve "
                        f"install` undoes it, or `w4ve repair` right now")

    # 7. the services a piece brought with it
    rt = runtime_module()
    for service in (rt.services_from(server.config) if rt else []):
        # Expanded first, or every check below is run against the literal
        # `${setting:python_bin}` rather than against the program that will
        # really be started.
        owner = owner_of(server, "services", service["name"])
        service, _answers = with_answers(server, owner, service)
        pending_answers = settings_module().unresolved(service["command"]) \
            if settings_module() else []
        if pending_answers:
            continue        # already reported as "not configured yet"
        where = server.root / service["cwd"]
        if not where.is_dir():
            problems.append(f"service {service['name']}: {service['cwd']} "
                            f"is not there")
            continue
        program = service["command"][0]
        if "/" in program and not Path(program).exists():
            problems.append(f"service {service['name']}: {program} does not exist")
        elif "/" not in program and not shutil.which(program):
            problems.append(f"service {service['name']}: no {program} on PATH")
        # A service that declared what it imports gets checked before it is
        # ever started, which beats reading a traceback at three in the
        # morning.
        entry = (server.config.get("services") or {}).get(service["name"]) or {}
        # Ask the interpreter the service will actually use, not the one
        # running this command: a service pointed at a venv is the normal case.
        interpreter = program if "python" in Path(program).name else sys.executable
        for module in entry.get("python_requires") or []:
            # Asked as a **distribution**, not as an importable module: the pip
            # name and the import name disagree often enough that checking the
            # import reports things as missing that are installed (`pillow` is
            # imported as `PIL`, `python-multipart` as `multipart`).
            name = module.split("[")[0].split("=")[0].split(">")[0].strip()
            probe = subprocess.run([interpreter, "-c", DISTRIBUTION_PROBE,
                                    name, name.replace("_", "-")],
                                   capture_output=True)
            if probe.returncode != 0:
                problems.append(f"service {service['name']} needs the Python "
                                f"package {module}, which is not installed "
                                f"for {Path(interpreter).name}")

    # 8. pieces that were installed and never answered. This is the most
    # common way a piece is "broken": it was never told what it needed.
    module = settings_module()
    for pid in entries:
        for declaration in missing_settings(server, pid):
            problems.append(f"{pid} has no {declaration.name} yet "
                            f"(`w4ve configure {pid}`)")
    if module and module.Secrets(server.root).insecure():
        problems.append("w4ve/secrets.json is readable by other users on this "
                        "machine: chmod 600 it")

    # 9. the disk, which fills up over days and is nobody's job until it is
    for area in storage_areas(server):
        folder = server.root / area["dir"]
        if not folder.is_dir():
            warnings.append(f"storage {area['name']}: {area['dir']} is not there")
            continue
        if not area.get("quota") or not module or not rt:
            continue
        used, _count = rt.folder_usage(folder)
        quota = module.parse_size(area["quota"])
        if quota and used > quota:
            problems.append(f"storage {area['name']} is over its ceiling "
                            f"({module.human_size(used)} of {area['quota']}): "
                            f"`w4ve storage --purge`")
        elif quota and used > quota * 0.9:
            warnings.append(f"storage {area['name']} is nearly full "
                            f"({module.human_size(used)} of {area['quota']})")

    # 10. routes that exist in w4ve.toml and in nobody's web server
    routes = http_routes(server)
    if routes:
        applied = server.state.get("http_applied") or {}
        current = {route["name"]: route["path"] for route in routes}
        if applied != current:
            warnings.append(f"{len(routes)} public route"
                            f"{'s' if len(routes) != 1 else ''} declared here "
                            f"are not in any web server yet (`w4ve http`)")

    # 11. Running as root, which nothing here refuses and everything here
    # should mention. Refusing outright would break every container that runs
    # as root today, which is most of them; saying it is the honest middle,
    # and a mod installed as root is a file the server user cannot replace.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        warnings.append("this is running as root: anything it writes will be "
                        "owned by root, and the server usually is not")

    if server.pending:
        warnings.append(f"waiting for a restart: {', '.join(server.pending)}")

    for line in problems:
        say(f"{Out.bad('problem')}  {line}")
    for line in warnings:
        say(f"{Out.warn('warning')}  {line}")
    if not problems and not warnings:
        say(f"{Out.ok('ok')}  nothing wrong with {server.root.name}")
    else:
        say()
        say(f"{len(problems)} problem{'s' if len(problems) != 1 else ''}, "
            f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    return 1 if problems else 0


# ---------------------------------------------------------------------- api

def api_module():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import api
        return api
    except ImportError:
        return None


def cmd_api(args, index):
    """The local API: tokens, and asking it things from the command line."""
    module = api_module()
    if module is None:
        problem("this w4ve has no API next to it (api.py is missing)")
        return 2
    server = need_server(args)
    if server is None:
        return 2
    tokens = module.Tokens(server.root)

    if args.action == "token":
        if not args.name:
            problem("give the token a name: w4ve api token dashboard --scope read")
            return 2
        try:
            token = tokens.issue(args.name, args.scope)
        except ValueError as exc:
            problem(str(exc))
            return 1
        say(f"{Out.ok('ok')}  {args.name}  ({args.scope})")
        say()
        say(token)
        say()
        # Said once, here, because it is not stored anywhere it can be read
        # back from: what is on disk is the token, and this is the only moment
        # it is on a screen next to its name.
        say(Out.dim("  Copy it now. `w4ve api tokens` lists names, never this."))
        return 0

    if args.action == "tokens":
        listing = tokens.listing()
        if not listing:
            say("no tokens here")
            return 0
        for entry in listing:
            say(f"  {entry['name']:<20} {entry['scope']:<8} "
                f"{Out.dim(entry.get('issued') or '')}")
        if tokens.insecure():
            problem("w4ve/api-tokens.json is readable by other users: chmod 600 it")
            return 1
        return 0

    if args.action == "revoke":
        if not args.name:
            problem("which one? w4ve api revoke dashboard")
            return 2
        gone = tokens.revoke(args.name)
        say(f"{Out.ok('ok')}  {gone} revoked" if gone
            else f"{Out.warn('none')}  nothing called {args.name!r}")
        return 0 if gone else 1

    if args.action == "get":
        # Asking the API from here is how somebody checks it works without
        # writing a client first.
        instance = module.Api(server.root)
        path = args.name or "/v1/status"
        if not path.startswith("/"):
            path = "/v1/" + path
        route = ("GET", path.rstrip("/"))
        if route not in module.ROUTES:
            problem(f"no such thing: GET {path}")
            say(Out.dim("  " + ", ".join(sorted(p for m, p in module.ROUTES
                                                if m == "GET"))))
            return 1
        code, payload = instance.run(route, {})
        say(json.dumps(payload, indent=2))
        return 0 if code < 400 else 1

    problem("api takes token, tokens, revoke or get")
    return 2


# ---------------------------------------------------------------------- dev

PLUGIN_TEMPLATE = """# {name}, a W4VE plugin.
# Everything the core needs to know about it lives here.

[plugin]
id       = "{pid}"
name     = "{name}"
version  = "0.1.0"
protocol = 1

[run]
# The interpreter is part of the command on purpose: a plugin with its own
# virtualenv points at that python here and nothing else has to know.
command = ["python3", "main.py"]

[config]
# Whatever is here arrives as `plugin.config`. Edit it, reload the plugin.
greeting = "hello"
"""

PLUGIN_MAIN = '''"""{name}: a W4VE plugin.

Run the conformance kit against this folder before shipping it:

    w4ve dev check .
"""
import sys
from pathlib import Path

# Where the SDK is. Written out by `w4ve dev new` so this file runs from
# anywhere; change it if you move either of them, or install the SDK and
# delete these two lines.
sys.path.insert(0, r"{sdk}")

from w4ve_plugin import Plugin

plugin = Plugin("{pid}", "0.1.0", name="{name}")


@plugin.on_start
def started():
    plugin.log("{pid} is up")


@plugin.command("!!{pid}", help="what this plugin is doing")
def status(source, args):
    """Whatever this returns is shown to whoever ran the command."""
    state = plugin.server_status()
    return [
        "{name} 0.1.0",
        "  server: %s" % ("running" if state.get("running") else "stopped"),
        "  greeting: %r" % plugin.config.get("greeting"),
    ]


@plugin.on("player_joined")
def joined(payload):
    plugin.say("%s, %s" % (plugin.config.get("greeting", "hello"),
                           payload.get("player", "somebody")))


if __name__ == "__main__":
    plugin.run()
'''


def worker_module():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import worker
        return worker
    except ImportError:
        return None


def plugin_env_path(folder):
    """Where a plugin's own virtualenv lives, whether or not it exists."""
    return Path(folder) / ".venv"


def plugin_interpreter(folder):
    """The Python of a plugin's own environment, if it has one."""
    candidate = plugin_env_path(folder) / "bin" / "python"
    return candidate if candidate.exists() else None


def dev_env(args):
    """Build a plugin its own environment, because W4VE will not share hers.

    The manifesto is blunt about this: W4VE does not install a plugin's
    dependencies into its own environment. Two plugins wanting different
    versions of the same library is not a conflict to resolve, it is two
    environments; and a plugin that can add packages to the process watching
    Minecraft is a plugin that can break it from a requirements file.

    ⚠️ **This installs, and it is the only thing here that does.** That is not
    a hole in the rule, it is the shape of it: nothing installs *by itself* or
    as a side effect of starting a server. This is a person typing a command
    about a plugin they chose, being told exactly what will be fetched, and
    saying yes.
    """
    module = worker_module()
    if module is None:
        problem("this w4ve cannot read native plugins (worker.py is missing)")
        return 2
    folder = Path(args.folder or ".").resolve()
    try:
        manifest = module.read_manifest(folder)
    except module.WorkerError as exc:
        problem(str(exc))
        return 1

    wanted = list(manifest.get("requires") or [])
    lock = folder / "requirements.txt"
    if lock.exists():
        # A lockfile wins over the manifest list: it is the one that can carry
        # exact versions and hashes, which is the difference between "install
        # something called requests" and "install this exact file".
        say(Out.dim(f"  using {lock.name}"))
    elif not wanted:
        say(f"{manifest['id']} declares no Python packages, so there is "
            f"nothing to build an environment for")
        return 0

    home = plugin_env_path(folder)
    if home.exists() and not args.force:
        problem(f"{home} already exists (use --force to build it again)")
        return 1

    say(Out.bold(f"{manifest['id']} {manifest['version']}"))
    say(f"  environment: {home}")
    if lock.exists():
        hashed = "--hash=" in lock.read_text(encoding="utf-8")
        say(f"  from: {lock}" + ("  (with hashes)" if hashed
                                 else "  " + Out.warn("(no hashes: a name is "
                                                      "not an identity)")))
    else:
        say(f"  packages: {', '.join(wanted)}")
        say(Out.warn("  no requirements.txt, so these are names and not "
                     "versions: what arrives today may not be what arrives "
                     "tomorrow"))
    say()
    say(Out.dim("  This downloads and runs somebody else's code. A hash "
                "proves identity, never good intentions."))
    if args.dry_run:
        say(Out.dim("dry run, nothing was fetched"))
        return 0
    if not (args.yes or ask("build it?", default=False)):
        say("nothing done")
        return 0

    if home.exists():
        shutil.rmtree(home)
    result = subprocess.run([sys.executable, "-m", "venv", str(home)])
    if result.returncode != 0:
        problem("could not create the environment")
        return 1
    pip = home / "bin" / "pip"
    command = [str(pip), "install"]
    if lock.exists():
        command += ["-r", str(lock)]
        if "--hash=" in lock.read_text(encoding="utf-8"):
            # Refuses anything whose hash is not pinned, including transitive
            # dependencies. Half a locked file is not a locked file.
            command.append("--require-hashes")
    else:
        command += wanted
    result = subprocess.run(command)
    if result.returncode != 0:
        problem("the packages did not install; the environment is left in "
                "place so you can see why")
        return 1

    absent = module.missing_requirements(dict(manifest,
                                              command=[str(home / "bin" / "python")]))
    if absent:
        problem("built it, and it still does not have: " + ", ".join(absent))
        return 1
    say(f"{Out.ok('ok')}  {home}")
    say(Out.dim(f"    w4ve dev run {folder}   uses it from now on"))
    return 0


def dev_lock(args):
    """Freeze a plugin's environment into a requirements.txt with hashes.

    What makes an install reproducible is not a list of names, it is a list of
    files. `pip freeze` gives versions; `--hash` is what makes a second machine
    get the same bytes.
    """
    folder = Path(args.folder or ".").resolve()
    interpreter = plugin_interpreter(folder)
    if interpreter is None:
        problem(f"{folder} has no environment yet (w4ve dev env {folder})")
        return 1
    pip = folder / ".venv" / "bin" / "pip"
    result = subprocess.run([str(pip), "freeze"], capture_output=True, text=True)
    if result.returncode != 0:
        problem("could not read the environment")
        return 1
    frozen = [line for line in result.stdout.splitlines() if line.strip()
              and not line.startswith("-e ")]
    if not frozen:
        say("that environment has nothing in it")
        return 0
    lock = folder / "requirements.txt"
    if args.dry_run:
        say(Out.dim(f"would write {len(frozen)} lines to {lock}"))
        return 0
    lock.write_text(
        "# Written by `w4ve dev lock`. Exact versions, so a second machine\n"
        "# gets what this one has.\n"
        "#\n"
        "# ⚠️ Without hashes this pins versions and not files. To pin files:\n"
        "#     pip hash <wheel>   and add --hash= to each line, then\n"
        "#     w4ve dev env . --force   installs with --require-hashes.\n"
        + "\n".join(frozen) + "\n", encoding="utf-8")
    say(f"{Out.ok('ok')}  {lock}  ({len(frozen)} package"
        f"{'s' if len(frozen) != 1 else ''})")
    return 0


def cmd_dev(args, index):
    """Everything somebody writing a plugin needs, and nothing else."""
    # One positional for all three: `new` reads it as a name, the others as a
    # folder. Three subcommands with three different nouns would be tidier and
    # would also mean remembering which is which.
    args.folder = args.name or "."
    if args.action == "new":
        return dev_new(args)
    if args.action == "check":
        return dev_check(args)
    if args.action == "run":
        return dev_run(args)
    if args.action == "env":
        return dev_env(args)
    if args.action == "lock":
        return dev_lock(args)
    problem("dev takes new, check, run, env or lock")
    return 2


def dev_new(args):
    """Scaffold a plugin that already passes the conformance kit.

    Starting from something that works beats starting from something that
    merely compiles: the first thing a new plugin author should see is a green
    run, not their own first bug.
    """
    name = args.name or ""
    pid = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    if not pid:
        problem("give it a name: w4ve dev new my-plugin")
        return 2
    folder = Path(args.dir or ".") / pid
    if folder.exists():
        problem(f"{folder} already exists")
        return 1
    folder.mkdir(parents=True)
    pretty = name if name != pid else pid.replace("-", " ").title()
    (folder / "w4ve.plugin.toml").write_text(
        PLUGIN_TEMPLATE.format(pid=pid, name=pretty), encoding="utf-8")
    sdk = Path(__file__).resolve().parent / "sdk" / "python"
    (folder / "main.py").write_text(
        PLUGIN_MAIN.format(pid=pid, name=pretty, sdk=sdk), encoding="utf-8")
    say(f"{Out.ok('ok')}  {folder}")
    say(Out.dim(f"    w4ve dev check {folder}     does it speak the protocol"))
    say(Out.dim(f"    w4ve dev run {folder}       talk to it by hand"))
    return 0


def dev_check(args):
    """Run the conformance kit. This is what "official SDK" means."""
    kit = Path(__file__).resolve().parent / "sdk" / "conformance.py"
    if not kit.exists():
        problem("the conformance kit is not next to this w4ve")
        return 2
    folder = Path(args.folder or ".").resolve()
    command = [sys.executable, str(kit), str(folder)]
    if args.verbose:
        command.append("--verbose")
    return subprocess.call(command)


def dev_run(args):
    """Start one plugin and talk to it, with no server and no guardian.

    Being able to try a plugin without a Minecraft server is most of what
    writing one involves.
    """
    module = worker_module()
    if module is None:
        problem("this w4ve cannot run native plugins (worker.py is missing)")
        return 2
    folder = Path(args.folder or ".").resolve()
    try:
        manifest = module.read_manifest(folder)
    except module.WorkerError as exc:
        problem(str(exc))
        return 1
    root = Path(tempfile.mkdtemp(prefix="w4ve-dev-"))
    host = module.WorkerHost(root)
    one = module.Worker(manifest, root, host=host)
    host.workers[one.id] = one
    ok, message = one.start()
    say(("%s  %s" % (Out.ok("ok"), message)) if ok
        else ("%s  %s" % (Out.bad("error"), message)))
    if not ok:
        return 1
    say(Out.dim("  commands: " + (", ".join(sorted(one.commands)) or "none")))
    say(Out.dim("  type a command, `!event <name> {json}` to send one, Ctrl-D to leave"))
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line.startswith("!event "):
                parts = line.split(None, 2)
                payload = {}
                if len(parts) > 2:
                    try:
                        payload = json.loads(parts[2])
                    except ValueError:
                        payload = {"value": parts[2]}
                one.deliver(parts[1], payload)
                say(Out.dim("  sent"))
                continue
            claimed, lines = host.command(line)
            if not claimed:
                say(Out.warn("  no plugin owns that command"))
            for answer in lines:
                say("  " + str(answer))
    except KeyboardInterrupt:
        pass
    finally:
        one.stop()
    return 0


# ------------------------------------------------------------------ configure

def settings_module():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import piecesettings
        return piecesettings
    except ImportError:
        return None


def discover_module():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import discover
        return discover
    except ImportError:
        return None


def declarations_for(server, pid):
    """What this piece needs told to it, from the state written at install."""
    module = settings_module()
    entry = server.state["pieces"].get(pid) or {}
    if module is None:
        return []
    return module.declarations_of({"settings": entry.get("settings") or []})


def missing_settings(server, pid):
    """Required answers nobody has given yet, in declaration order."""
    values = server.settings_of(pid)
    out = []
    for declaration in declarations_for(server, pid):
        if declaration.when and not values.get(declaration.when):
            continue
        if declaration.required and declaration.name not in values:
            out.append(declaration)
    return out


def _prompt(text, default=None):
    """One line of input, or the default when nobody is at the keyboard."""
    if not sys.stdin.isatty():
        return default
    shown = f" [{default}]" if default not in (None, "") else ""
    try:
        answer = input(f"{text}{shown}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        raise SystemExit(1)
    return answer or default


def _pick(options, title, current=None):
    """Choose from a numbered list of names. Ids never get typed."""
    say()
    say(Out.bold(title))
    for number, (value, label) in enumerate(options, 1):
        mark = "  <- set now" if current and value == current else ""
        say(f"  {number:>2}. {label}{Out.dim(mark)}")
    say()
    while True:
        answer = _prompt("number", "1" if not current else None)
        if answer is None:
            return current
        try:
            chosen = options[int(answer) - 1]
        except (ValueError, IndexError):
            problem("pick one of the numbers above")
            continue
        return chosen[0]


def _ask_one(declaration, current, context, server, pid, args):
    """Ask for one setting, going and looking first when it can."""
    module = settings_module()
    say()
    say(Out.bold(declaration.title))
    for line in module._wrap(declaration.help, 74):
        say(f"  {line}")
    if declaration.example:
        say(Out.dim(f"  for example: {declaration.example}"))

    if declaration.discover:
        found = _discovered(declaration, context, server, pid)
        if found is not None:
            if declaration.type == "list":
                return _pick_many(found, "pick the ones that apply", current)
            return _pick(found, "pick one", current)

    if declaration.type == "bool":
        return ask("  yes or no?", default=bool(current if current is not None
                                                else declaration.default))
    if declaration.type == "choice":
        return _pick([(o, o) for o in declaration.options], "pick one", current)
    while True:
        shown = "(set)" if declaration.secret and current else current
        answer = _prompt("  value", shown if shown != "(set)" else None)
        if answer is None or (declaration.secret and answer == ""):
            return current if current is not None else declaration.default
        try:
            return declaration.coerce(answer)
        except module.SettingError as exc:
            problem(f"  {exc}")


def _pick_many(options, title, current):
    say()
    say(Out.bold(title))
    chosen = set(current or [])
    for number, (value, label) in enumerate(options, 1):
        mark = " *" if value in chosen else ""
        say(f"  {number:>2}. {label}{Out.bold(mark)}")
    say()
    answer = _prompt("  numbers, separated by commas", "")
    if not answer:
        return list(current or [])
    out = []
    for part in answer.split(","):
        try:
            out.append(options[int(part.strip()) - 1][0])
        except (ValueError, IndexError):
            problem(f"  ignoring {part.strip()!r}, it is not one of the numbers")
    return out


def _discovered(declaration, context, server, pid):
    """Go and ask Discord (or whoever) for the possible answers."""
    module = discover_module()
    if module is None:
        return None
    token_name = module.token_setting_for(declaration.discover)
    token = context.get(token_name) if token_name else None
    try:
        return module.options_for(declaration.discover, token, context)
    except module.DiscoveryError as exc:
        note(f"  {exc}")
        note("  falling back to typing it by hand")
        return None


def cmd_configure(args, index):
    """Answer what a piece needs, without anybody copying an id by hand.

    This is the command that decides whether a big piece is installable by
    somebody who is not its author. Everything it does is in service of one
    rule: **an id is discovered, never asked for**.
    """
    server = need_server(args)
    if server is None:
        return 2
    module = settings_module()
    if module is None:
        problem("this w4ve cannot handle settings (piecesettings.py is missing)")
        return 2

    pid = args.id
    entry = server.state["pieces"].get(pid)
    if entry is None:
        problem(f"{pid} is not installed here")
        return 1
    declarations = declarations_for(server, pid)
    if not declarations:
        say(f"{pid} has nothing to configure")
        return 0

    values = server.settings_of(pid)
    if args.show:
        return _show_settings(server, pid, declarations, values)

    if args.set:
        return _set_settings(server, pid, declarations, values, args)

    # An application id in the manifest means the piece can print its own
    # invite link, which is the step people get wrong when they do it by hand.
    _print_bot_hint(server, pid, declarations, values)

    asked = 0
    for declaration in declarations:
        if declaration.when and not values.get(declaration.when):
            continue
        known = declaration.name in values
        if known and not args.all:
            continue
        if not declaration.required and not args.all and not sys.stdin.isatty():
            continue
        answer = _ask_one(declaration, values.get(declaration.name), values,
                          server, pid, args)
        if answer is None:
            continue
        values[declaration.name] = answer
        asked += 1
        if declaration.secret:
            server.set_setting(pid, declaration.name, answer, declarations, pid)
        if declaration.name == "discord_token" and answer:
            _print_invite(answer)
        # Written after each answer, not at the end: a session interrupted
        # halfway keeps what was already answered instead of starting over.
        server.write_settings(pid, {k: v for k, v in values.items()
                                    if k not in {d.name for d in declarations
                                                 if d.secret}},
                              declarations, pid)

    say()
    if asked:
        say(f"{Out.ok('saved')}  {pid}: {asked} answered")
    still = missing_settings(server, pid)
    if still:
        problem("still missing: " + ", ".join(d.name for d in still))
        say(Out.dim(f"  run `w4ve configure {pid}` again when you have them"))
        return 1
    say(Out.dim(f"  values in w4ve.toml under [settings.{pid}]"))
    if any(d.secret for d in declarations):
        say(Out.dim("  secrets in w4ve/secrets.json (mode 600)"))
    for line in (entry.get("guide") or []):
        say(f"  {line}")
    return 0


def _print_bot_hint(server, pid, declarations, values):
    """If a piece needs a Discord token, say exactly how to get one."""
    if not any(d.name == "discord_token" for d in declarations):
        return
    if values.get("discord_token"):
        return
    say()
    say(Out.bold("Getting the Discord token (two minutes, once)"))
    say("  1. https://discord.com/developers/applications  ->  New Application")
    say("  2. Bot tab  ->  Reset Token  ->  copy it")
    say("  3. Bot tab  ->  Message Content Intent  ->  ON")
    say(Out.dim("     (without that last one the bridge sees empty messages)"))
    say("  4. paste it here when asked")
    module = discover_module()
    if module:
        say(Out.dim("     w4ve will print the invite link with the right "
                    "permissions once it has the token"))


def _print_invite(token):
    """Say who the token belongs to, and hand over the invite link.

    Two things at once on purpose: the first call also proves the token works,
    so a typo is caught here rather than when the service refuses to start.
    """
    module = discover_module()
    if module is None:
        return
    try:
        me = module.discord_me(token)
    except module.DiscoveryError as exc:
        problem(f"  {exc}")
        return
    say(f"  {Out.ok('token ok')}  the bot is {me.get('username', '?')}")
    try:
        guilds = module.discord_guilds(token)
    except module.DiscoveryError:
        guilds = []
    if not guilds:
        say()
        say("  It is not in any Discord server yet. Invite it with this link, "
            "then keep going:")
        say(f"  {module.invite_url(me.get('id'))}")
        _prompt("  press enter when it is in", "")


def _show_settings(server, pid, declarations, values):
    say(Out.bold(f"settings for {pid}"))
    for declaration in declarations:
        name = declaration.name
        if declaration.secret:
            shown = Out.ok("set") if values.get(name) else Out.warn("not set")
        elif name in values:
            shown = json.dumps(values[name])
        else:
            shown = Out.warn("not set") if declaration.required else Out.dim("default")
        say(f"  {name:<20} {shown}")
    return 0


def _set_settings(server, pid, declarations, values, args):
    """`--set name=value`, for scripts and for anybody who hates prompts."""
    module = settings_module()
    changed = 0
    for pair in args.set:
        if "=" not in pair:
            problem(f"{pair!r} is not name=value")
            return 2
        name, raw = pair.split("=", 1)
        name = name.strip()
        declaration = next((d for d in declarations if d.name == name), None)
        if declaration is None:
            problem(f"{pid} has no setting called {name!r}")
            return 1
        try:
            value = declaration.coerce(raw)
        except module.SettingError as exc:
            problem(f"{name}: {exc}")
            return 1
        if args.dry_run:
            say(f"would set {name}")
            continue
        server.set_setting(pid, name, value, declarations, pid)
        if not declaration.secret:
            values[name] = value
        changed += 1
    if changed and not args.dry_run:
        server.write_settings(pid, {k: v for k, v in values.items()
                                    if k not in {d.name for d in declarations
                                                 if d.secret}},
                              declarations, pid)
        say(f"{Out.ok('saved')}  {changed} changed")
    return 0


# ----------------------------------------------------------------------- http

CACHE_HEADERS = {
    "immutable": 'add_header Cache-Control "public, max-age=604800, immutable";',
    "short": 'add_header Cache-Control "public, max-age=60";',
    "none": 'add_header Cache-Control "no-store";',
}


def http_routes(server):
    """`[http.<name>]` blocks, longest path first.

    Sorted here rather than left to the operator because nginx picks the
    longest matching prefix but an exact `location =` beats every prefix, and
    getting that order wrong is what made `/v1/upload` get eaten by `/v1` on a
    real machine.
    """
    out = []
    for name, entry in (server.config.get("http") or {}).items():
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        out.append({
            "name": name,
            "path": entry["path"],
            "serve": entry.get("serve"),
            "proxy": entry.get("proxy"),
            "cache": entry.get("cache", "none"),
            "max_body": entry.get("max_body"),
            "exact": bool(entry.get("exact")),
            "why": entry.get("why", ""),
        })
    return sorted(out, key=lambda r: (not r["exact"], -len(r["path"])))


def render_nginx(server, routes):
    """A block to paste into an nginx server, or to include from one.

    Generated rather than documented: the four things that go wrong here (the
    order, the body size, the cache header, the trailing slash of an alias) are
    all things a program can get right every time and a person gets right most
    times.
    """
    root = Path(server.root).resolve()
    lines = [
        "# Generated by w4ve. Paste inside the `server { }` that serves your",
        "# public domain, or save it and `include` it from there.",
        "#",
        "# The order matters: exact matches (`location =`) first, then the",
        "# longest prefixes. w4ve already sorted them for you.",
        "",
    ]
    for route in routes:
        if route["why"]:
            lines.append(f"# {route['why']}")
        marker = "= " if route["exact"] else ""
        lines.append(f"location {marker}{route['path']} {{")
        if route["max_body"]:
            lines.append(f"    client_max_body_size {route['max_body']};")
        if route["serve"]:
            folder = (root / route["serve"]).as_posix().rstrip("/") + "/"
            lines.append(f"    alias {folder};")
            lines.append("    autoindex off;")
            # Its own header, always: without it this location inherits
            # whatever the parent one says, and a Content-Disposition from a
            # neighbour turns every image into a download.
            lines.append(f"    {CACHE_HEADERS.get(route['cache'], CACHE_HEADERS['none'])}")
        else:
            lines.append(f"    proxy_pass http://{route['proxy']}{route['path']};")
            lines.append("    proxy_set_header Host $host;")
            lines.append("    proxy_set_header X-Forwarded-For $remote_addr;")
            lines.append("    proxy_read_timeout 120s;")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def render_caddy(server, routes):
    """The same thing for Caddy, which is what a first timer should be using.

    Caddy gets its own renderer and not a footnote because it does the part
    that stops people cold (a certificate) by itself.
    """
    root = Path(server.root).resolve()
    lines = ["# Generated by w4ve. Put this in your Caddyfile, under the site",
             "# block for your domain. Caddy gets the certificate by itself.",
             ""]
    for route in routes:
        if route["why"]:
            lines.append(f"# {route['why']}")
        matcher = route["path"]
        if route["serve"]:
            folder = (root / route["serve"]).as_posix()
            lines.append(f"handle_path {matcher.rstrip('/')}/* {{")
            lines.append(f"    root * {folder}")
            if route["cache"] == "immutable":
                lines.append('    header Cache-Control "public, max-age=604800, immutable"')
            lines.append("    file_server")
            lines.append("}")
        else:
            lines.append(f"handle {matcher} {{")
            if route["max_body"]:
                lines.append(f"    request_body {{ max_size {route['max_body']} }}")
            lines.append(f"    reverse_proxy {route['proxy']}")
            lines.append("}")
        lines.append("")
    return "\n".join(lines)


def cmd_http(args, index):
    """Print the web server configuration the installed pieces need."""
    server = need_server(args)
    if server is None:
        return 2
    routes = http_routes(server)
    if not routes:
        say("no piece here asks for anything on the public side")
        return 0
    text = (render_caddy if args.flavour == "caddy" else render_nginx)(server, routes)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        say(f"{Out.ok('written')}  {args.out}")
        if args.flavour == "nginx":
            say(Out.dim("  then: nginx -t && systemctl reload nginx"))
        # Written down so `doctor` stops nagging about routes that are in
        # place, and starts again the day a piece adds a new one.
        server.state["http_applied"] = {r["name"]: r["path"] for r in routes}
        server.save_state()
        return 0
    say(text)
    return 0


# -------------------------------------------------------------------- storage

def storage_areas(server):
    out = []
    for name, entry in (server.config.get("storage") or {}).items():
        if not isinstance(entry, dict) or not entry.get("dir"):
            continue
        out.append({"name": name, "dir": entry["dir"],
                    "quota": entry.get("quota"), "keep": entry.get("keep"),
                    "by": entry.get("by", "mtime"), "why": entry.get("why", "")})
    return out


def cmd_storage(args, index):
    """What the pieces are keeping on this disk, and whether it fits."""
    server = need_server(args)
    if server is None:
        return 2
    module = settings_module()
    rt = runtime_module()
    areas = storage_areas(server)
    if not areas:
        say("no piece here declares any storage")
        return 0
    trouble = 0
    for area in areas:
        folder = Path(server.root) / area["dir"]
        used, files = (rt.folder_usage(folder) if rt
                       else _usage_fallback(folder))
        quota = module.parse_size(area["quota"]) if area["quota"] else None
        label = module.human_size(used)
        if quota:
            share = used * 100 // quota if quota else 0
            label += f" of {module.human_size(quota)} ({share}%)"
            if used > quota:
                trouble += 1
        head = Out.warn(area["name"]) if quota and used > quota else Out.ok(area["name"])
        say(f"{head}  {label}  {files} files  {Out.dim(area['dir'])}")
        if area["why"]:
            say(Out.dim(f"   {area['why']}"))
        if args.purge and rt:
            if args.dry_run:
                say(Out.dim("   would make room now"))
                continue
            freed, gone = rt.purge_area(folder, quota,
                                        module.parse_time(area["keep"]) if area["keep"] else None,
                                        area["by"])
            if gone:
                say(f"   {Out.ok('freed')} {module.human_size(freed)} in {gone} files")
    if trouble:
        problem(f"{trouble} over its ceiling: `w4ve storage --purge` makes room")
        return 1
    return 0


def _usage_fallback(folder):
    total, count = 0, 0
    for path in Path(folder).rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            count += 1
    return total, count


# ------------------------------------------------------------------- packages

def package_module():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import package
        return package
    except ImportError:
        return None


def install_packages(server, paths, args, report=True):
    """Install `.w4ve` files: a piece that arrives whole, second half included.

    `report=False` is for the catalog path, which installs a package on its way
    through and reports once at the end for everything it installed. Without
    it the pending-restart notice comes out twice.
    """
    pkg = package_module()
    if pkg is None:
        problem("this w4ve cannot read packages (package.py is missing)")
        return 2
    if not args.dry_run:
        check_interrupted(server)
    changed = 0
    for path in paths:
        path = Path(path)
        if not path.exists():
            problem(f"{path} does not exist")
            return 1
        try:
            install = pkg.Install(path, server)
            actions, problems = install.plan()
        except pkg.PackageError as exc:
            problem(str(exc))
            return 1
        manifest = install.manifest
        say(f"{Out.bold(manifest.name)}  {manifest.version}")
        for issue in problems:
            problem(issue)
        if problems:
            return 1

        already = server.installed(manifest.id)
        for action in actions:
            if action["kind"] == "service":
                say(f"  service   {action['name']}  "
                    + Out.dim(" ".join(action["component"]["command"])))
            elif action["kind"] == "watch":
                component = action["component"]
                say(f"  watch     {action['name']}  "
                    + Out.dim(f"on {component['on']} -> {component['do']}"))
            elif action["kind"] == "http":
                component = action["component"]
                where = component.get("serve") or component.get("proxy")
                say(f"  http      {component['path']}  "
                    + Out.dim(f"-> {where}"))
            elif action["kind"] == "storage":
                component = action["component"]
                say(f"  storage   {action['name']}  "
                    + Out.dim(f"{component['dir']}"
                              f"{', up to ' + component['quota'] if component.get('quota') else ''}"
                              f"{', kept ' + component['keep'] if component.get('keep') else ''}"))
            elif action["kind"] == "timer":
                component = action["component"]
                say(f"  timer     {action['name']}  "
                    + Out.dim(f"every {component['every']}"))
            else:
                say(f"  {action['kind'].ljust(9)} {action['dest']}  "
                    + Out.dim(action["reload"]))
        if args.dry_run:
            say(Out.dim("dry run, nothing was written"))
            continue
        if already and already.get("version") == manifest.version and not args.force:
            note(f"{manifest.id} {manifest.version} is already installed")
            continue
        if already:
            # An update, not a removal: what was answered stays.
            remove_package(server, manifest.id, args, quiet=True,
                           keep_settings=True)
        try:
            record = install.apply(actions)
            install.commit()
        except pkg.PackageError as exc:
            problem(str(exc))
            return 1
        except OSError as exc:
            problem(f"{manifest.id} was not installed, and nothing changed: {exc}")
            return 1
        changed += 1
        server.set_pin(manifest.id, manifest.version)
        say(f"{Out.ok('ok')}  {manifest.id} {manifest.version}")
        if record.get("services"):
            say(Out.dim(f"    services in w4ve.toml: "
                        f"{', '.join(record['services'])}"))
            say(Out.dim("    `w4ve run` watches them like it watches the server"))
        for name in record.get("watches") or []:
            entry = (server.config.get("watch") or {}).get(name) or {}
            say(Out.dim(f"    watch {name}: {entry.get('why') or entry.get('on')}"))
        for name in record.get("timers") or []:
            entry = (server.config.get("timer") or {}).get(name) or {}
            say(Out.dim(f"    timer {name} every {entry.get('every')}"
                        f"{': ' + entry['why'] if entry.get('why') else ''}"))
        if record.get("storage"):
            say(Out.dim(f"    storage: {', '.join(record['storage'])}  "
                        f"(`w4ve storage` says how full)"))
        if record.get("http"):
            say(Out.dim(f"    public routes: {', '.join(record['http'])}  "
                        f"(`w4ve http` writes the web server config)"))
        finish_install(server, manifest.id, args)
    return report_pending(server, changed, args) if report else 0


def finish_install(server, pid, args):
    """The last stretch: answer what it needs, then say what is left to do.

    A piece that installs and then sits there unconfigured is a piece that
    looks broken. Asking here, while the person is still watching the command
    they just ran, is the difference between five minutes and a support thread.
    """
    entry = server.state["pieces"].get(pid) or {}
    missing = missing_settings(server, pid)
    if missing and sys.stdin.isatty() and not args.dry_run:
        say()
        say(Out.bold(f"{pid} needs {len(missing)} thing"
                     f"{'s' if len(missing) != 1 else ''} told to it."))
        if ask("answer them now?", default=True):
            configure_args = argparse.Namespace(
                id=pid, all=False, show=False, set=None,
                dir=getattr(args, "dir", None), dry_run=False, yes=True)
            cmd_configure(configure_args, None)
            return
    if missing:
        say(Out.warn(f"    not configured yet: `w4ve configure {pid}`"))
    for line in entry.get("guide") or []:
        say(f"    {line}")


def remove_package(server, pid, args, quiet=False, keep_settings=False):
    pkg = package_module()
    if pkg is None:
        problem("this w4ve cannot read packages (package.py is missing)")
        return 0
    if args.dry_run:
        entry = server.installed(pid) or {}
        for relative in (entry.get("files") or {}):
            say(f"would delete {relative}")
        return 0
    result = pkg.remove(server, pid, keep_settings=keep_settings)
    if result is None:
        return 0
    server.remove_pin(pid)
    if not quiet:
        say(f"{Out.ok('ok')}  removed {pid}"
            + (f" ({len(result['removed'])} files)" if result["removed"] else ""))
        for relative in result["changed"]:
            note(f"left {relative} alone: it changed since we wrote it")
        if result["services"]:
            say(Out.dim(f"    services taken out of w4ve.toml: "
                        f"{', '.join(result['services'])}"))
    return 1


def cmd_repair(args, index):
    """Undo an install that was killed halfway, on purpose rather than by luck."""
    pkg = package_module()
    if pkg is None:
        problem("this w4ve cannot read packages (package.py is missing)")
        return 2
    server = need_server(args)
    found = pkg.interrupted(server)
    if not found:
        say(f"{Out.ok('ok')}  nothing to repair")
        return 0
    say(f"an install of {Out.bold(found['piece'])} {found['version']} "
        f"never finished")
    for step in found["steps"]:
        if step.get("op") == "write":
            say(Out.dim(f"    would undo {step['path']}"))
    if args.dry_run:
        return 0
    if not (args.yes or ask("roll it back?", default=True)):
        return 0
    result = pkg.roll_back_interrupted(server)
    say(f"{Out.ok('ok')}  {len(result['undone'])} removed, "
        f"{len(result['restored'])} put back")
    for path in result["kept"]:
        note(f"could not undo {path}, look at it by hand")
    return 0


def cmd_pack(args, index):
    """Build a `.w4ve` out of a folder, so a piece can ship whole."""
    pkg = package_module()
    if pkg is None:
        problem("this w4ve cannot build packages (package.py is missing)")
        return 2
    try:
        target, manifest = pkg.pack(args.folder, args.out)
    except pkg.PackageError as exc:
        for line in str(exc).splitlines():
            problem(line)
        return 1
    size = target.stat().st_size
    say(f"{Out.ok('ok')}  {target}  ({size // 1024} KB)")
    for component in manifest.components:
        kind = component.get("kind", "?")
        if kind == "watch":
            say(Out.dim(f"    watch    {component['name']}  "
                        f"-> {component.get('do')}"))
        elif kind == "http":
            where = component.get("serve") or component.get("proxy")
            say(Out.dim(f"    http     {component['path']}  -> {where}"))
        elif kind in ("service", "storage", "timer"):
            say(Out.dim(f"    {kind.ljust(8)} {component['name']}"))
        else:
            say(Out.dim(f"    {kind.ljust(8)} {component.get('file', '?')}"))
    return 0


# -------------------------------------------------------------------- runtime

def read_mcdr_config(root):
    """The handful of keys MCDR's `config.yml` holds that a cutover needs.

    Read by hand rather than with a YAML library, for the same reason the rest
    of this program has none: it must run on a server where nothing was
    installed first. The keys wanted are flat strings at the top level, which
    is the easy half of YAML.
    """
    path = Path(root) / "config.yml"
    if not path.is_file():
        return None
    wanted = ("working_directory", "start_command", "handler", "encoding",
              "decoding", "language")
    found = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        if raw[:1].isspace() or raw.lstrip().startswith("#"):
            continue                     # nested, or a comment
        key, sep, value = raw.partition(":")
        if not sep or key.strip() not in wanted:
            continue
        value = value.split(" #", 1)[0].strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            found[key.strip()] = value
    return found or None


def rcon_of(game_dir):
    """(enabled, port) from a server.properties, or (None, None)."""
    path = Path(game_dir) / "server.properties"
    if not path.is_file():
        return None, None
    enabled, port = False, None
    try:
        for line in path.read_text(errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key.strip() == "enable-rcon":
                enabled = value.strip().lower() == "true"
            elif key.strip() == "rcon.port":
                port = value.strip()
    except OSError:
        return None, None
    return enabled, port


def cmd_preflight(args, index):
    """Can this server move to W4VE today? One command, one answer.

    Everything else in this program answers a piece of that question: which
    plugins load, what pip is missing, whether the layout is what it looks
    like. Asked separately they are five commands and a document; asked here
    they are one run and a verdict, which is what somebody standing in front of
    a live server at eleven at night actually needs.

    ⚠️ It works with no `w4ve.toml`, on purpose. The honest moment to ask "can
    I move?" is while MCDR is still the thing running, before anything has been
    written, and a check that demands you convert first is a check that arrives
    too late to be an answer.
    """
    root = Path(args.dir or ".").resolve()
    server = Server.find(str(root))
    if server and server.root != root:
        server = None                    # a w4ve.toml further up is not ours
    blockers, warnings, notes = [], [], []

    say(Out.bold(f"preflight  {root}"))
    say()

    # 1. What is here. Under MCDR the shape is unmistakable, and knowing it is
    # what makes every later answer specific rather than generic.
    mcdr_config = read_mcdr_config(root)
    plugins_dir = root / "plugins"
    permission = root / "permission.yml"
    if mcdr_config:
        say(f"{Out.ok('mcdr')}  config.yml found: this server runs MCDR today")
    elif plugins_dir.is_dir():
        say(f"{Out.warn('mcdr')}  plugins/ is here but there is no config.yml")
        notes.append("without MCDR's config.yml the start command has to be "
                     "written by hand")
    else:
        say(f"{Out.dim('mcdr')}  no MCDR here, so there is nothing to move from")

    # 2. Where Minecraft actually runs. Getting this wrong is the quiet kind of
    # wrong: plugins write to paths relative to it, so a backup lands in an
    # empty folder and reports success.
    declared = (mcdr_config or {}).get("working_directory") or "server"
    game_dir = root / declared
    if game_dir.is_dir():
        say(f"{Out.ok('layout')}  Minecraft runs in {declared}/")
    else:
        say(f"{Out.bad('layout')}  {declared}/ is not here")
        blockers.append(f"MCDR says the server lives in {declared}/, which does "
                        f"not exist")
    if server:
        if server.game_dir_name != declared:
            blockers.append(f"w4ve.toml says game_dir = \"{server.game_dir_name}\" "
                            f"and MCDR says {declared!r}: plugins write to paths "
                            f"relative to it, so this is the difference between "
                            f"a backup and an empty folder")
        else:
            notes.append(f"w4ve.toml already agrees: game_dir = {declared!r}")

    # 3. The start command. Copied, never guessed: the flags on it are somebody's
    # tuning and a rewritten line is a different server.
    start = (mcdr_config or {}).get("start_command")
    if start:
        say(f"{Out.ok('start')}  {Out.dim(start)}")
        if server and server.config.get("server", {}).get("start"):
            if server.config["server"]["start"].strip() != start.strip():
                warnings.append("w4ve.toml has a different start command than "
                                "MCDR's config.yml")
        elif server:
            blockers.append("w4ve.toml has no `start` line yet; copy MCDR's "
                            "start_command into it")
    elif mcdr_config:
        warnings.append("MCDR's config.yml has no start_command in it")

    # 4. The plugins, read rather than run.
    mcdr = mcdr_module()
    plugins = []
    if mcdr is None:
        blockers.append("this copy of w4ve cannot read MCDR plugins (the mcdr "
                        "package is missing next to w4ve.py)")
    elif not plugins_dir.is_dir():
        say(f"{Out.dim('plugins')}  no plugins/ folder")
    else:
        found = mcdr.report(plugins_dir)
        plugins = found["plugins"]
        broken = [p for p in plugins if p.problems]
        say(f"{Out.ok('plugins') if not broken else Out.warn('plugins')}  "
            f"{len(plugins)} found"
            + (f", {len(broken)} that do not even parse" if broken else ""))
        for plugin in broken:
            blockers.append(f"{plugin.path.name}: {plugin.problems[0]}")

    # 5. What pip is missing. Not installed here, ever: this says the line and
    # the operator decides. A plugin can also load fine and still be missing
    # something it only imports when somebody uses it.
    missing = []
    if plugins:
        try:
            from mcdr import requirements
        except ImportError:
            requirements = None
        if requirements:
            wanted = {}
            for plugin in plugins:
                for name in requirements.missing(plugin):
                    wanted.setdefault(name, []).append(plugin.id or plugin.path.name)
            missing = sorted(wanted)
            if missing:
                say(f"{Out.warn('python')}  {len(missing)} package"
                    f"{'s' if len(missing) != 1 else ''} some plugin asked for "
                    f"and this machine does not have")
                say(f"          {Out.dim(requirements.install_line(wanted))}")
                blockers.append("install those packages before the cutover: a "
                                "plugin missing one either will not load or "
                                "will die the first time somebody uses it")
            else:
                say(f"{Out.ok('python')}  every package the plugins declare is here")

    # 6. The staff list, which is the thing nobody notices is gone until
    # somebody who should be admin is not.
    if permission.is_file():
        try:
            from mcdr.permission import PermissionFile
        except ImportError:
            PermissionFile = None
        if PermissionFile is None:
            warnings.append("permission.yml is here but this w4ve cannot read it")
        else:
            complaints = []
            perms = PermissionFile(permission, on_problem=complaints.append)
            perms.load()
            counted = {level: len(perms.names_at(level))
                       for level in ("owner", "admin", "helper")}
            people = sum(counted.values())
            say(f"{Out.ok('permissions')}  {people} named "
                f"({counted['owner']} owner, {counted['admin']} admin, "
                f"{counted['helper']} helper)")
            for line in complaints:
                warnings.append(f"permission.yml: {line}")
    elif mcdr_config:
        warnings.append("no permission.yml: everybody would start as the "
                        "default level")

    # 7. RCON, which is the fallback channel for a server W4VE did not start.
    enabled, port = rcon_of(game_dir)
    if enabled is None:
        warnings.append(f"no server.properties in {declared}/ to check RCON")
    elif enabled:
        say(f"{Out.ok('rcon')}  on, port {port}")
    else:
        say(f"{Out.warn('rcon')}  off")
        notes.append("RCON off is workable but it is the fallback channel for "
                     "a server the guardian did not start")

    # 8. Java, because the version the server needs is not always the one on
    # PATH, and finding out at boot is a bad time.
    try:
        out = subprocess.run(["java", "-version"], capture_output=True,
                             text=True, timeout=10)
        found_java = re.search(r'"(\d+)', out.stderr or out.stdout)
        if found_java:
            say(f"{Out.ok('java')}  {found_java.group(1)} on PATH")
            if server and server.java and found_java.group(1) != server.java:
                warnings.append(f"w4ve.toml says Java {server.java}, PATH has "
                                f"{found_java.group(1)}")
    except (OSError, subprocess.SubprocessError):
        warnings.append("no `java` on PATH here (normal on a container host)")

    # The rehearsal. Off by default because it imports every plugin, which is
    # somebody else's code running on this machine, and that should be asked
    # for rather than assumed.
    if getattr(args, "load", False) and plugins:
        say()
        say(Out.dim("loading them for real, against nothing:"))
        load_plugins(server or _rootlike(root), plugins_dir)

    say()
    for line in blockers:
        say(f"{Out.bad('blocker')}  {line}")
    for line in warnings:
        say(f"{Out.warn('warning')}  {line}")
    for line in notes:
        say(f"{Out.dim('note')}     {line}")
    say()
    if blockers:
        say(f"{Out.bad('not ready')}  {len(blockers)} thing"
            f"{'s' if len(blockers) != 1 else ''} to fix first")
        return 1
    say(f"{Out.ok('ready')}  nothing is in the way of a cutover")
    note("`w4ve preflight --load` loads the plugins for real, which is the "
         "rehearsal")
    note("the steps themselves are in docs/cutover-del-mirror.md")
    return 0


class _rootlike:
    """Just enough of a server for the rehearsal, when there is no w4ve.toml.

    The whole point of preflight is answering before anything is written, and
    the rehearsal only needs to know where the folder is.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.config = {}
        self.mc = None


def mcdr_module():
    """The MCDR plugin reader, if this copy of w4ve ships it."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mcdr import loader
        return loader
    except ImportError:
        return None


# What `w4ve plugins <action>` sends to a running guardian. Kept as a table
# rather than an if-chain because the command names are the same ones typed in
# game, and one list is how they stay that way.
LIVE_PLUGIN_ACTIONS = {
    "reload": ("!!w4ve plugin reload %s", "which plugin"),
    "unload": ("!!w4ve plugin unload %s", "which plugin"),
    "load": ("!!w4ve plugin load %s", "which file"),
    "enable": ("!!w4ve plugin enable %s", "which file"),
    "disable": ("!!w4ve plugin disable %s", "which plugin"),
    "refresh": ("!!w4ve refresh", None),
    "list": ("!!w4ve plugin list", None),
    "status": ("!!w4ve status", None),
}


def guess_interpreter(root):
    """A virtualenv sitting in the server folder, if there is one.

    Looked for rather than demanded: somebody migrating from MCDR almost
    certainly has one, because that is how MCDR is installed, and making them
    pass a flag to be told the truth is a good way to be told a lie.
    """
    for name in ("venv", ".venv", "env"):
        candidate = Path(root) / name / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return None


def cmd_plugins(args, index):
    """What MCDR plugins this server has, and what is wrong with them.

    With no action, nothing here runs a plugin: it reads what each one
    declares. That is the point, because the answer to "why is this plugin not
    loading" should not require loading it.

    With an action, it is the opposite: it talks to the guardian that is
    already running them, so `w4ve plugins reload here` is the same command as
    typing `!!w4ve plugin reload here` in game, and answers in the terminal.
    """
    if getattr(args, "action", None):
        return live_plugins(args)
    mcdr = mcdr_module()
    if mcdr is None:
        problem("this w4ve cannot read MCDR plugins (the mcdr package is missing)")
        return 2
    # ⚠️ Works with no w4ve.toml, on purpose, and `preflight` works the same
    # way for the same reason. The honest moment to ask "will my plugins run
    # under this?" is while MCDR is still the thing running, before anything
    # has been converted. A check that demands you convert first is a check
    # that arrives after the decision it was supposed to inform.
    server = Server.find(args.dir) or _rootlike(Path(args.dir or ".").resolve())
    folder = Path(server.root) / (args.folder or "plugins")
    # Which Python the plugins will really run under. A server with its own
    # virtualenv is the normal case, and asking this one instead reports as
    # missing whatever is installed over there. Guessed from the folder when
    # nobody says, because that guess is right nearly every time.
    chosen = getattr(args, "python", None) or guess_interpreter(server.root)
    if chosen:
        try:
            from mcdr import requirements as _requirements
            _requirements.use_interpreter(chosen)
            if chosen != sys.executable:
                say(Out.dim(f"asking {chosen} what is installed"))
        except ImportError:
            pass
    if not folder.is_dir():
        problem(f"{folder} is not there")
        note("MCDR keeps plugins in `plugins/` at the root of the server")
        return 1

    if args.load:
        return load_plugins(server, folder)

    found = mcdr.report(folder)
    plugins = found["plugins"]
    if not plugins:
        say(f"no MCDR plugins in {folder}")
        return 0

    width = max(len(p.id or p.path.name) for p in plugins)
    for plugin in sorted(plugins, key=lambda p: (p.id or p.path.name)):
        label = (plugin.id or Out.dim("?")).ljust(width)
        bits = [plugin.version or Out.dim("no version")]
        if plugin.problems:
            bits.append(Out.bad(plugin.problems[0]))
        else:
            # Only what is actually missing. Printing every dependency made a
            # plugin whose dependency was sitting right there in the same list
            # read as broken, which is the opposite of what this command is for.
            here = {p.id for p in plugins if p.id}
            absent = sorted(d for d in plugin.dependencies if d not in here)
            if absent:
                bits.append(Out.bad("needs " + ", ".join(absent)))
        say(f"  {label}  {'  '.join(bits)}  {Out.dim(plugin.path.name)}")

    say()
    report_requirements(plugins)
    if found["order"]:
        say(Out.dim("load order: " + " ".join(p.id for p in found["order"])))
    real = [p for p in found["problems"]
            if not any(p.startswith(x.path.name + ":") for x in plugins)]
    for line in real:
        problem(line)
    say()
    say(f"{len(plugins)} plugin{'s' if len(plugins) != 1 else ''}, "
        f"{len(found['problems'])} problem{'s' if len(found['problems']) != 1 else ''}")
    return 1 if found["problems"] else 0


def report_requirements(plugins):
    """What these plugins need from pip that is not installed here.

    Declared and checked, never installed: W4VE does not run pip at anybody.
    It is here because the alternative is finding out one crash at a time on
    the day of the cutover, and because a plugin can load fine and still be
    missing something it only imports when somebody uses it. DayCountNBT does
    exactly that: it loads without `nbtlib` and dies the first time a player
    types `!!day`.
    """
    try:
        from mcdr import requirements
    except ImportError:
        return []

    wanted = {}
    for plugin in plugins:
        for name in requirements.missing(plugin):
            wanted.setdefault(name, []).append(plugin.id or plugin.path.name)
    if not wanted:
        return []
    say(Out.dim("not installed, and some plugin asked for it:"))
    width = max(len(name) for name in wanted)
    for name in sorted(wanted, key=str.lower):
        say(f"  {name.ljust(width)}  {Out.dim(', '.join(sorted(wanted[name])))}")
    say()
    say(Out.dim("  " + requirements.install_line(wanted)))
    say()
    return sorted(wanted)


def live_plugins(args):
    """Act on the plugins of a server that is running right now."""
    action = args.action
    if action not in LIVE_PLUGIN_ACTIONS:
        problem(f"no such action {action!r}")
        note("try: " + ", ".join(sorted(LIVE_PLUGIN_ACTIONS)))
        return 2
    template, wants = LIVE_PLUGIN_ACTIONS[action]
    if wants and not args.target:
        problem(f"say {wants}: w4ve plugins {action} <name>")
        return 2

    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    client = rt.Client(server.root)
    if not client.connected():
        problem("no guardian is running this server, so nothing is loaded")
        note("`w4ve plugins --load` rehearses the same thing without a server")
        return 1

    command = template % args.target if wants else template
    reply = client.call({"cmd": "send", "process": "server", "text": command})
    for line in reply.get("lines") or []:
        say("  " + line)
    if not reply.get("ok"):
        problem(reply.get("message") or reply.get("error") or "no answer")
        return 1
    if not reply.get("lines"):
        # The guardian took it but the plugins said nothing, which usually
        # means this w4ve has no compatibility layer loaded over there.
        note("the guardian sent it to the server instead: this server's "
             "plugins are not loaded by w4ve")
    return 0


def load_plugins(server, folder):
    """Actually load them, which is the only honest answer to "does it work".

    Reading a plugin says what it declares; loading it says whether it runs.
    This is the rehearsal for a cutover: it happens with the server untouched,
    so a plugin that will not work under W4VE is known before anything moves.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mcdr.host import Host
    except ImportError as exc:
        problem(f"this w4ve cannot run MCDR plugins ({exc})")
        return 2

    lines = []
    host = Host(server.root, plugins_dir=folder, server=None,
                on_output=lines.append)
    host.load_all()

    if host.loaded:
        width = max(len(pid) for pid in host.loaded)
        for pid in sorted(host.loaded):
            entry = host.loaded[pid]
            events = [e for e in ("on_info", "on_player_joined", "on_player_left",
                                  "on_server_startup", "on_server_stop")
                      if entry.handler(e)]
            say(f"  {Out.ok('loaded')}  {pid.ljust(width)}  "
                f"{entry.plugin.version}  "
                + Out.dim(", ".join(events) if events else "no events"))
    for line in lines:
        if "ERROR" in line or "WARN" in line:
            say(Out.dim("    " + line))
    say()
    for line in host.problems:
        problem(line)
    say()
    try:
        from mcdr import loader as _loader

        report_requirements(_loader.discover(folder))
    except ImportError:
        pass
    say(f"{len(host.loaded)} loaded, {len(host.problems)} "
        f"problem{'s' if len(host.problems) != 1 else ''}")
    note("this was a rehearsal: nothing was connected to the server")
    return 1 if host.problems else 0


def runtime_module():
    """The guardian, if this copy of w4ve ships it (RFC-0001).

    The catalog side of w4ve has to keep working on a machine where only
    `w4ve.py` was downloaded, so the runtime is an optional import instead of
    a hard dependency.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import runtime
        return runtime
    except ImportError:
        return None


def need_runtime():
    module = runtime_module()
    if module is None:
        problem("this w4ve has no runtime next to it (runtime.py is missing)")
    return module


def owner_of(server, section, name):
    """Which piece put this block in w4ve.toml, from what install wrote down."""
    key = {"services": "services", "timer": "timers"}.get(section, section)
    for pid, entry in (server.state.get("pieces") or {}).items():
        if name in (entry.get(key) or []):
            return pid
    return ""


def answers_for(server, pid):
    """(settings, secrets) for a piece, or two empty dicts."""
    if not pid:
        return {}, {}
    return server.plain_settings(pid), server.secrets_of(pid)


def with_answers(server, pid, fields):
    """Expand `${setting:x}` and `${secret:y}`, and hand the rest over as env.

    The environment is why a package almost never needs a config template: a
    service reads `W4VE_PUBLIC_URL` and that is the end of it. The expansion is
    for the cases that cannot be an environment variable, like a port inside a
    command line.
    """
    module = settings_module()
    if module is None or not pid:
        return fields, {}
    settings, secrets = answers_for(server, pid)
    expanded = module.expand(fields, settings, secrets)
    return expanded, module.environment_for(pid, settings, secrets)


def runtime_pieces(server, rt):
    """Turn w4ve.toml into a guardian: one server, its services, its upkeep."""
    game_dir = server.root / server.game_dir
    command = rt.server_command(server.config, server.root, server.game_dir)
    guardian = rt.Guardian(server.root, server.config)
    section = server.config.get("server", {})
    module = settings_module()
    if command:
        guardian.add_server(
            command, game_dir,
            flavor=section.get("flavor"),
            stop_grace=int(section.get("stop_grace") or rt.STOP_GRACE),
            restart=section.get("restart_policy") or "never",
            watches=rt.watches_from(server.config))
    for service in rt.services_from(server.config):
        pid = owner_of(server, "services", service["name"])
        service, answers = with_answers(server, pid, service)
        environment = dict(answers)
        environment.update(service.get("env") or {})
        if module:
            still = module.unresolved([service["command"], environment])
            if still:
                # Refusing to start beats starting with an empty token: the
                # first failure says the word `secret`, the second says
                # nothing anybody can read.
                problem(f"{service['name']} is not configured: "
                        f"{', '.join(sorted(set(still)))}")
                note(f"  run `w4ve configure {pid}`" if pid else "")
                continue
        guardian.add_service(service["name"], service["command"],
                             server.root / service["cwd"], service["restart"],
                             health=service.get("health"),
                             health_every=service.get("health_every", 30),
                             env=environment,
                             mcdr=service.get("mcdr", False),
                             stop_grace=service.get("stop_grace",
                                                    rt.SERVICE_GRACE),
                             after=service.get("after") or ())
    for timer in rt.timers_from(server.config):
        pid = owner_of(server, "timer", timer["name"])
        timer, answers = with_answers(server, pid, timer)
        environment = dict(answers)
        environment.update(timer.get("env") or {})
        guardian.add_timer(timer["name"], timer["command"], timer["every"],
                           cwd=timer["cwd"], env=environment,
                           why=timer.get("why", ""))
    for area in rt.storage_from(server.config):
        guardian.add_storage(area["name"], area["dir"], quota=area.get("quota"),
                             keep=area.get("keep"), by=area.get("by", "mtime"),
                             why=area.get("why", ""))
    return guardian, command


def cmd_run(args, index):
    """Own the server in the foreground, which is what a container wants."""
    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    guardian, command = runtime_pieces(server, rt)
    if not command:
        # A folder with no Minecraft in it is a real case, and it is the one a
        # shared piece needs: a hub that serves a whole network of servers runs
        # on a machine of its own, with services and timers and no world.
        if guardian.watched or guardian.timers:
            note("no Minecraft server here: running the services and timers "
                 "on their own")
        else:
            problem("w4ve.toml does not say how to start this server")
            note('add `start = "java -Xms4G -Xmx4G -jar fabric-server-launch.jar nogui"` '
                 "under [server]")
            return 2
    guardian.echo = not args.quiet
    say(Out.dim(f"guardian for {server.root.name}, Ctrl-C stops the server"))

    def console():
        # Whatever the operator types goes to the server, exactly like the
        # console they are used to.
        for line in sys.stdin:
            proc = guardian.watched.get("server")
            if proc:
                proc.send(line)

    threading.Thread(target=console, daemon=True).start()
    code, message = guardian.run(start_now=not args.no_start)
    say(message)
    return code


def cmd_start(args, index):
    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    client = rt.Client(server.root)
    reply = client.call({"cmd": "start", "process": args.process})
    if not reply.get("ok"):
        problem(reply.get("error") or reply.get("message"))
        if not client.connected():
            note("`w4ve run` starts the guardian")
        return 1
    say(f"{Out.ok('ok')}  {reply['message']}")
    return 0


def cmd_stop(args, index):
    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    client = rt.Client(server.root)
    if client.connected():
        reply = client.call({"cmd": "stop", "process": args.process,
                             "grace": args.grace}, timeout=args.grace + 60)
        if not reply.get("ok"):
            problem(reply.get("error") or reply.get("message"))
            return 1
        say(f"{Out.ok('ok')}  {reply['message']}")
        return 0

    # No guardian, but there may well be a server: it survives us on purpose.
    offline = client.offline_status() or {"processes": []}
    running = [p for p in offline["processes"]
               if p["name"] == args.process and not p["stale"]]
    if not running:
        say("nothing running here")
        return 0
    pid = running[0]["pid"]
    if not args.force:
        problem(f"no guardian, but {args.process} is alive (pid {pid})")
        note("start the guardian with `w4ve run` and stop it from there, "
             "or use --force to signal it directly")
        return 1
    note(f"no guardian: signalling {args.process} (pid {pid}) directly")
    os.kill(pid, signal.SIGTERM)
    say(f"{Out.ok('ok')}  sent SIGTERM to {pid}")
    return 0


def cmd_console(args, index):
    """Watch the console and type into it, without owning the process."""
    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    client = rt.Client(server.root)
    if not client.connected():
        problem("no guardian here, so there is no console to attach to")
        note("`w4ve logs` reads what was already written")
        return 1

    def typing():
        for line in sys.stdin:
            reply = client.call({"cmd": "send", "process": args.process,
                                 "text": line})
            # ⚠️ The answer is NOT printed here. A plugin's reply reaches this
            # terminal twice: once in `lines`, and once through the console
            # stream this command is already attached to. Printing both is how
            # `!!help` came out with every line of it duplicated, the two
            # copies interleaved so the title landed in the middle.
            # `w4ve plugins reload <id>` is the other caller, and that one is
            # not attached to anything, so it still prints `lines` itself.
            if not reply.get("ok"):
                problem(reply.get("message") or reply.get("error"))

    threading.Thread(target=typing, daemon=True).start()
    say(Out.dim("attached, Ctrl-C detaches (the server keeps running)"))
    try:
        client.tail(lambda name, line: say(line if name == "server"
                                           else f"[{name}] {line}"))
    except KeyboardInterrupt:
        say("\ndetached")
    return 0


def cmd_logs(args, index):
    rt = need_runtime()
    if rt is None:
        return 2
    server = need_server(args)
    run_dir = server.root / "w4ve" / "run"
    path = (run_dir / "journal.log" if args.journal
            else run_dir / f"{args.process}.console.log")
    if not path.exists():
        problem(f"{path} does not exist yet")
        return 1
    with open(path) as fh:
        if not args.follow:
            lines = fh.readlines()
            for line in lines[-args.lines:]:
                say(line.rstrip("\n"))
            return 0
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if line:
                    say(line.rstrip("\n"))
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return 0


def report_runtime(server, rt):
    """The runtime half of `w4ve status`, which is what people ask first."""
    client = rt.Client(server.root)
    live = client.call({"cmd": "status"}) if client.connected() else None
    if live and live.get("ok"):
        state = live
        guardian = True
    else:
        state = client.offline_status()
        guardian = False
    if not state or not state["processes"]:
        return
    width = max(len(entry["name"]) for entry in state["processes"])
    for entry in state["processes"]:
        colour = {"running": Out.ok, "starting": Out.warn, "stopping": Out.warn,
                  "crashed": Out.bad, "killed": Out.bad}.get(entry["state"], Out.dim)
        bits = [colour(entry["state"])]
        if entry.get("ready") is True:
            bits.append("ready")
        elif entry["state"] == "starting":
            bits.append(Out.dim("not ready yet"))
        elif entry.get("ready") is None and entry["state"] == "running":
            bits.append(Out.dim("readiness unknown"))
        if entry.get("pid"):
            bits.append(Out.dim(f"pid {entry['pid']}"))
        if entry.get("uptime"):
            bits.append(Out.dim(f"up {entry['uptime'] // 60}m"))
        if entry.get("pid") and not entry.get("owned"):
            bits.append(Out.warn("adopted, over RCON" if entry.get("rcon")
                                 else "adopted, no console"))
        if entry.get("give_up"):
            bits.append(Out.bad("gave up restarting it"))
        say(f"  {entry['name'].ljust(width)} {'  '.join(bits)}")
        caps = entry.get("capabilities")
        if caps and caps.get("note"):
            say(Out.dim(f"    {caps['flavor']}: {caps['note']}"))
    if not guardian:
        say(Out.dim("  no guardian running, so this is what the last one left "
                    "written down, checked against /proc"))
    say()


# ----------------------------------------------------------------------- main

def build_parser():
    parser = argparse.ArgumentParser(
        prog="w4ve", description="Install and update W4VE pieces.")
    parser.add_argument("--index", help="catalog path or URL")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cached catalog")
    parser.add_argument("--dir", help="server folder (default: look upwards)")
    parser.add_argument("--version", action="version", version=f"w4ve {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, function, help_text):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(run=function)
        return sub

    init = add("init", cmd_init, "write a w4ve.toml here")
    init.add_argument("--name", help="what to call this server (default: the folder)")
    init.add_argument("--mc", help="Minecraft version, if it cannot be worked out")
    init.add_argument("--client", action="store_true",
                      help="this folder is a game instance, not a server")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing w4ve.toml")

    add("adopt", cmd_adopt, "write down what is already installed here")

    listing = add("list", cmd_list, "what the catalog has")
    listing.add_argument("--type", help="only this kind (fabric-mod, mcdr-plugin...)")
    listing.add_argument("--side", choices=["server", "client"],
                         help="only what goes on that side")
    listing.add_argument("--profile", help="only what this profile contains")
    listing.add_argument("--installed", action="store_true",
                         help="only what is installed here")

    search = add("search", cmd_search, "find a piece")
    search.add_argument("text", help="words to look for in names, summaries and tags")

    info = add("info", cmd_info, "everything known about one piece")
    info.add_argument("id", help="the piece to explain")

    add("profiles", cmd_profiles, "curated sets of pieces")

    install = add("install", cmd_install, "download and place a piece")
    install.add_argument("ids", nargs="*", metavar="id[@version]",
                         help="pieces to install; @version pins one; "
                              "or the path of a .w4ve package")
    install.add_argument("--profile", help="install a whole profile at once")
    install.add_argument("--file", help="pick a specific asset by name")
    install.add_argument("--force", action="store_true",
                         help="install over a known conflict or an unsupported version")

    update = add("update", cmd_update, "move to the newest version")
    update.add_argument("ids", nargs="*", help="pieces to update (default: all of them)")
    update.add_argument("-v", "--verbose", action="store_true",
                        help="also say what is already up to date")
    update.add_argument("--force", action="store_true",
                        help="update even into a known conflict")

    remove = add("remove", cmd_remove, "take a piece out")
    remove.add_argument("ids", nargs="+", help="pieces to take out")

    add("repair", cmd_repair, "undo an install that was killed halfway")

    listing_plugins = add("plugins", cmd_plugins,
                          "what MCDR plugins this server has")
    listing_plugins.add_argument("--folder",
                                 help="where they live (default: plugins)")
    listing_plugins.add_argument("--python",
                                 help="the interpreter the plugins will run "
                                      "under (default: a venv in the server "
                                      "folder, or this one)")
    listing_plugins.add_argument("--load", action="store_true",
                                 help="actually load them, to see which ones run")
    listing_plugins.add_argument("action", nargs="?",
                                 help="reload, unload, load, enable, disable, "
                                      "refresh, list, status (on the running "
                                      "server)")
    listing_plugins.add_argument("target", nargs="?",
                                 help="which plugin the action is about")

    configure = add("configure", cmd_configure,
                    "answer what a piece needs told to it")
    configure.add_argument("id", help="the piece to configure")
    configure.add_argument("--all", action="store_true",
                           help="go through every setting, not only the missing ones")
    configure.add_argument("--show", action="store_true",
                           help="what is set and what is not (never prints secrets)")
    configure.add_argument("--set", action="append", metavar="name=value",
                           help="set one without prompting (repeatable)")

    http_cmd = add("http", cmd_http, "the web server config this server needs")
    http_cmd.add_argument("flavour", nargs="?", default="nginx",
                          choices=["nginx", "caddy"],
                          help="which web server to write it for (default: nginx)")
    http_cmd.add_argument("--out", help="write it to a file instead of printing")

    storage = add("storage", cmd_storage, "what pieces are storing, and its ceiling")
    storage.add_argument("--purge", action="store_true",
                         help="make room now, following what each piece declared")

    api_cmd = add("api", cmd_api, "the local API: tokens, and asking it things")
    api_cmd.add_argument("action", choices=["token", "tokens", "revoke", "get"],
                         help="token: issue one. tokens: list them. "
                              "revoke: take one away. get: ask a route")
    api_cmd.add_argument("name", nargs="?",
                         help="the token's name, or the route for `get`")
    api_cmd.add_argument("--scope", default="read", choices=["read", "write", "admin"],
                         help="what the token may do (default: read)")

    dev = add("dev", cmd_dev, "write a plugin: scaffold it, check it, try it")
    dev.add_argument("action",
                     choices=["new", "check", "run", "env", "lock"],
                     help="new: scaffold one. check: conformance. run: try it. "
                          "env: build its own virtualenv. lock: freeze that "
                          "environment into a requirements.txt")
    dev.add_argument("--force", action="store_true",
                     help="for `env`, build it again over the existing one")
    dev.add_argument("name", nargs="?",
                     help="for `new`, what to call it; for the rest, the folder")
    dev.add_argument("--verbose", action="store_true",
                     help="for `check`, print every line of the conversation")

    packing = add("pack", cmd_pack, "build a .w4ve out of a folder")
    packing.add_argument("folder", help="folder with a w4ve.json in it")
    packing.add_argument("--out", help="where to leave the package")

    add("sync", cmd_sync, "leave the server as w4ve.toml says")
    add("status", cmd_status, "versions, pending restarts and what is running")
    add("doctor", cmd_doctor, "find what is wrong")

    # The runtime half. None of it needs the catalog, and that matters: a
    # server has to be startable when GitHub is down.
    run = add("run", cmd_run, "own this server and watch it, in the foreground")
    run.add_argument("--no-start", action="store_true",
                     help="hold the server: start the guardian only")
    run.add_argument("--quiet", action="store_true",
                     help="do not echo the server console here")

    start = add("start", cmd_start, "start the server with the guardian already up")
    start.add_argument("process", nargs="?", default="server",
                       help="which process (default: the server)")

    stop = add("stop", cmd_stop, "stop the server, saving first")
    stop.add_argument("process", nargs="?", default="server",
                      help="which process (default: the server)")
    stop.add_argument("--grace", type=int, default=90,
                      help="seconds to wait before escalating (default: 90)")
    stop.add_argument("--force", action="store_true",
                      help="signal the process directly when no guardian is up")

    console = add("console", cmd_console, "attach to the live console")
    console.add_argument("process", nargs="?", default="server",
                         help="which process (default: the server)")

    logs = add("logs", cmd_logs, "read the console or the journal")
    logs.add_argument("process", nargs="?", default="server",
                      help="whose console (default: the server)")
    logs.add_argument("-f", "--follow", action="store_true",
                      help="keep printing new lines")
    logs.add_argument("-n", "--lines", type=int, default=40,
                      help="how many lines to show (default: 40)")
    logs.add_argument("--journal", action="store_true",
                      help="the guardian journal instead of the console")

    for name in ("run", "start", "stop", "console", "logs", "pack", "repair",
                 "plugins", "configure", "http", "storage", "dev", "api"):
        subparsers.choices[name].set_defaults(needs_index=False)

    for name, sub in subparsers.choices.items():
        # `logs -n` is a line count, so it does not get the global short flag.
        if name != "logs":
            sub.add_argument("-n", "--dry-run", action="store_true",
                             help="say what would happen, change nothing")
        else:
            sub.set_defaults(dry_run=False)
        sub.add_argument("-y", "--yes", action="store_true",
                         help="answer yes to every question")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    index = ({"schema": 1, "pieces": {}, "profiles": {}}
             if not getattr(args, "needs_index", True)
             else load_index(args.index, args.refresh))
    if index.get("schema", 1) > 1:
        problem(f"this catalog is schema {index['schema']}, "
                f"this w4ve only understands 1. Update it.")
        return 2
    try:
        return args.run(args, index)
    except KeyboardInterrupt:
        say("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
