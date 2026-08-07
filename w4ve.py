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

Standard library only, single file, on purpose: it has to run on a server
without installing anything first, and be editable by someone who is not a
programmer. Same rule as the registry's build.py.

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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
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
    print(f"{Out.bad('error')} {text}", file=sys.stderr)


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
    return numbers, version[chosen.end():] if chosen else version


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
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                note("w4ve/state.json is corrupt, starting a new one")
        return {"schema": 1, "pieces": {}}

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

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
    if supported is False:
        return False, (f"{pid} supports Minecraft {piece['mc']}, "
                       f"this server is {server.mc}")

    mc = server.mc if mc_sensitive(piece) else None
    release, why = pick_release(piece, want, mc)
    if not release:
        return False, f"{pid}: {why}"
    asset, why = pick_file(piece, release, mc, explicit_file)
    if not asset:
        return False, f"{pid}: {why}"

    current = server.installed(pid)
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
    # An MCDR install has the game in `server/`. Guessing this right matters:
    # get it wrong and every jar lands in a folder Minecraft never reads.
    game_dir = "server" if (root / "server" / "mods").is_dir() else "."
    path.write_text(TEMPLATE.format(
        name=args.name or root.name,
        kind="client" if args.client else "server",
        mc=args.mc or "1.21",
        java=java or "21",
        game_dir=game_dir))
    say(f"{Out.ok('ok')}  wrote {path}")
    if game_dir != ".":
        say(Out.dim(f"    found an MCDR layout, game_dir = \"{game_dir}\""))
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
        note("removing a mod also needs a restart to take effect")
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
    restart = server.config.get("server", {}).get("restart")
    if not restart:
        say(Out.dim("    no restart command in w4ve.toml, do it your way"))
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


def cmd_status(args, index):
    server = need_server(args)
    config = server.config.get("server", {})
    say(f"{Out.bold(server.root.name)}  "
        f"{config.get('kind', 'server')}  Minecraft {server.mc or '?'}  "
        f"Java {server.java or '?'}")
    say(Out.dim(f"  {server.config_path}"))
    say()
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
        say(f"  {pid.ljust(width)}  {entry['version']:<20} {flag}")
    pending = server.pending
    if pending:
        say()
        say(Out.warn(f"{len(pending)} waiting for a restart"))
    return 0


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
        path = server.root / entry["path"]
        if not path.exists():
            problems.append(f"{pid}: {entry['path']} is gone")
        elif entry.get("sha256") and sha256_of(path) != entry["sha256"]:
            warnings.append(f"{pid}: {entry['path']} changed since it was "
                            f"installed (replaced by hand?)")

    # 3. version and side sanity
    seen_pairs = set()
    for pid, entry in entries.items():
        piece = index["pieces"].get(pid)
        if not piece:
            warnings.append(f"{pid} is installed but no longer in the catalog")
            continue
        if server.mc and mc_matches(piece.get("mc"), server.mc,
                                    piece.get("mc_exclude", [])) is False:
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
    managed = {(server.root / e["path"]).resolve() for e in entries.values()}
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
    init.add_argument("--name")
    init.add_argument("--mc")
    init.add_argument("--client", action="store_true",
                      help="this folder is a game instance, not a server")
    init.add_argument("--force", action="store_true")

    add("adopt", cmd_adopt, "write down what is already installed here")

    listing = add("list", cmd_list, "what the catalog has")
    listing.add_argument("--type")
    listing.add_argument("--side", choices=["server", "client"])
    listing.add_argument("--profile")
    listing.add_argument("--installed", action="store_true")

    search = add("search", cmd_search, "find a piece")
    search.add_argument("text")

    info = add("info", cmd_info, "everything known about one piece")
    info.add_argument("id")

    add("profiles", cmd_profiles, "curated sets of pieces")

    install = add("install", cmd_install, "download and place a piece")
    install.add_argument("ids", nargs="*", metavar="id[@version]")
    install.add_argument("--profile")
    install.add_argument("--file", help="pick a specific asset by name")
    install.add_argument("--force", action="store_true")

    update = add("update", cmd_update, "move to the newest version")
    update.add_argument("ids", nargs="*")
    update.add_argument("-v", "--verbose", action="store_true")
    update.add_argument("--force", action="store_true",
                        help="update even into a known conflict")

    remove = add("remove", cmd_remove, "take a piece out")
    remove.add_argument("ids", nargs="+")

    add("sync", cmd_sync, "leave the server as w4ve.toml says")
    add("status", cmd_status, "versions and pending restarts")
    add("doctor", cmd_doctor, "find what is wrong")

    for sub in subparsers.choices.values():
        sub.add_argument("-n", "--dry-run", action="store_true",
                         help="say what would happen, change nothing")
        sub.add_argument("-y", "--yes", action="store_true",
                         help="answer yes to every question")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    index = load_index(args.index, args.refresh)
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
