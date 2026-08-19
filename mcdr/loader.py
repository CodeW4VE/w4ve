"""Finding MCDR plugins and reading what they say about themselves.

This is the first half of compatibility, and it deliberately runs no plugin
code: it finds what is installed, works out each plugin's identity and what it
depends on, and puts them in an order that can be loaded. Anything wrong with a
plugin is reported as a sentence rather than raised, because an operator
looking at a folder of thirty plugins needs the list, not the first exception.

The four shapes a plugin comes in, all of them present in MineWave today:

    dig.py                      a single file, metadata in PLUGIN_METADATA
    Here/                       a folder with mcdreforged.plugin.json in it
    DigAll-v4.2.1.mcdr          a zip of the above
    PrimeBackup-v1.13.1.pyz     the same, with an entry point declared

Metadata is read with `ast` for single files and with `json` for the rest, so
nothing is imported and a plugin that cannot run can still be inspected. That
matters for `w4ve doctor`: telling someone their plugin declares a dependency
they do not have should never require running it first.
"""

import ast
import json
import re
import zipfile
from pathlib import Path

MANIFEST = "mcdreforged.plugin.json"

# What the file is called decides how it is read, and the suffix is the only
# honest signal: `PrimeBackup-v1.13.1.pyz` has no `.mcdr` in it and is one.
ARCHIVE_SUFFIXES = (".mcdr", ".pyz")

# Folders in `plugins/` that hold history rather than plugins. MineWave has
# both, and treating them as plugins is how you get four PrimeBackups.
NOT_PLUGINS = {"olds", "plugin_bak", "__pycache__"}

# A file that is a copy of a plugin rather than a plugin.
BACKUP_MARKS = (".bak", ".disabled", ".pre-", ".old", ".backup")

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Plugin:
    """One plugin found on disk, and everything known without running it."""

    def __init__(self, path, meta, shape, problems=()):
        self.path = Path(path)
        self.meta = meta or {}
        self.shape = shape              # file | folder | archive
        self.problems = list(problems)

    # ----------------------------------------------------------- identity

    @property
    def id(self):
        return self.meta.get("id", "")

    @property
    def version(self):
        return str(self.meta.get("version", ""))

    @property
    def name(self):
        return self.meta.get("name") or self.id or self.path.name

    @property
    def entrypoint(self):
        """The module MCDR imports. Defaults to the plugin's own id."""
        return self.meta.get("entrypoint") or self.id

    @property
    def dependencies(self):
        """id -> requirement, with mcdreforged itself kept separate."""
        deps = dict(self.meta.get("dependencies") or {})
        deps.pop("mcdreforged", None)
        return deps

    @property
    def requires_mcdr(self):
        return (self.meta.get("dependencies") or {}).get("mcdreforged", "")

    @property
    def description(self):
        text = self.meta.get("description")
        if isinstance(text, dict):
            # Plugins ship en_us and zh_cn; take English and fall back to
            # whatever is there rather than printing a dict at somebody.
            return text.get("en_us") or next(iter(text.values()), "")
        return text or ""

    def __repr__(self):
        return "<Plugin %s %s (%s)>" % (self.id or "?", self.version, self.shape)


# --------------------------------------------------------------- reading one

def read_single_file(path):
    """A one file plugin keeps its metadata in a PLUGIN_METADATA literal.

    Read with `ast`, so a plugin that imports something we do not have can
    still be identified. If the dict is built at runtime rather than written
    out, we say so instead of guessing.
    """
    problems = []
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return Plugin(path, {}, "file",
                      ["will not parse as Python: %s" % exc.msg])
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "PLUGIN_METADATA" not in names:
            continue
        try:
            return Plugin(path, ast.literal_eval(node.value), "file")
        except ValueError:
            problems.append("PLUGIN_METADATA is built at runtime, so its id "
                            "and version cannot be read without running it")
            break
    else:
        problems.append("no PLUGIN_METADATA in it")
    # A nameless plugin still exists, and calling it by its filename beats
    # dropping it off the list entirely.
    return Plugin(path, {"id": Path(path).stem}, "file", problems)


def read_folder(path):
    manifest = Path(path) / MANIFEST
    if not manifest.exists():
        return Plugin(path, {"id": Path(path).name}, "folder",
                      ["no %s in it" % MANIFEST])
    try:
        return Plugin(path, json.loads(manifest.read_text(encoding="utf-8")),
                      "folder")
    except ValueError as exc:
        return Plugin(path, {"id": Path(path).name}, "folder",
                      ["%s is not valid JSON: %s" % (MANIFEST, exc)])


def read_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if MANIFEST in names:
                inside = MANIFEST
            else:
                # A `.pyz` built by the official packer puts everything under a
                # folder named after the plugin.
                candidates = [n for n in names if n.endswith("/" + MANIFEST)]
                if not candidates:
                    return Plugin(path, {"id": Path(path).stem}, "archive",
                                  ["no %s inside" % MANIFEST])
                inside = sorted(candidates, key=len)[0]
            with archive.open(inside) as fh:
                return Plugin(path, json.loads(fh.read().decode("utf-8")),
                              "archive")
    except zipfile.BadZipFile:
        return Plugin(path, {"id": Path(path).stem}, "archive",
                      ["not a valid zip"])
    except ValueError as exc:
        return Plugin(path, {"id": Path(path).stem}, "archive",
                      ["%s is not valid JSON: %s" % (MANIFEST, exc)])


def read(path):
    """Read whatever kind of plugin this path is."""
    path = Path(path)
    if path.is_dir():
        return read_folder(path)
    if path.suffix in ARCHIVE_SUFFIXES:
        return read_archive(path)
    if path.suffix == ".py":
        return read_single_file(path)
    return None


# -------------------------------------------------------------- reading many

def looks_like_a_backup(name):
    lowered = name.lower()
    return any(mark in lowered for mark in BACKUP_MARKS)


def discover(folder):
    """Every plugin in a folder, with the copies and the archive left out."""
    folder = Path(folder)
    found = []
    if not folder.is_dir():
        return found
    for entry in sorted(folder.iterdir()):
        if entry.name.startswith(".") or looks_like_a_backup(entry.name):
            continue
        if entry.is_dir():
            if entry.name in NOT_PLUGINS:
                continue
            # A folder without a manifest is somebody's data directory, not a
            # broken plugin: only complain if it looks like it meant to be one.
            if not (entry / MANIFEST).exists():
                continue
            found.append(read_folder(entry))
        elif entry.suffix in ARCHIVE_SUFFIXES or entry.suffix == ".py":
            plugin = read(entry)
            if plugin is not None:
                found.append(plugin)
    return found


# ------------------------------------------------------------------ ordering

def duplicates(plugins):
    """Plugins claiming the same id, which is a real thing in MineWave.

    The `plugins` server has two MultiRconAPI and two Online. Loading either
    twice is worse than loading neither, so this is a fact the caller has to
    be told rather than a tie to break quietly.
    """
    by_id = {}
    for plugin in plugins:
        if plugin.id:
            by_id.setdefault(plugin.id, []).append(plugin)
    return {pid: found for pid, found in by_id.items() if len(found) > 1}


def load_order(plugins):
    """Order the plugins so nothing loads before what it depends on.

    Returns (order, problems). A cycle or a missing dependency does not stop
    the rest: the ones that can load are ordered, and the ones that cannot are
    named with the reason, which is what an operator can act on.
    """
    problems = []
    by_id = {}
    for plugin in plugins:
        if not plugin.id:
            problems.append("%s has no id" % plugin.path.name)
            continue
        if not ID_RE.match(plugin.id):
            problems.append("%s: id %r is not a valid plugin id"
                            % (plugin.path.name, plugin.id))
        by_id.setdefault(plugin.id, plugin)

    for pid, found in duplicates(plugins).items():
        problems.append("%d copies of '%s': %s"
                        % (len(found), pid,
                           ", ".join(p.path.name for p in found)))

    order, state = [], {}       # unseen | visiting | done

    def visit(pid, trail):
        if state.get(pid) == "done":
            return
        if state.get(pid) == "visiting":
            problems.append("dependency cycle: %s"
                            % " -> ".join(trail + [pid]))
            return
        state[pid] = "visiting"
        plugin = by_id.get(pid)
        if plugin is not None:
            for dep in sorted(plugin.dependencies):
                if dep not in by_id:
                    problems.append("%s needs %s, which is not installed"
                                    % (pid, dep))
                    continue
                visit(dep, trail + [pid])
            order.append(plugin)
        state[pid] = "done"

    for pid in sorted(by_id):
        visit(pid, [])
    return order, problems


def report(folder):
    """Everything worth saying about a plugins folder, without running any."""
    plugins = discover(folder)
    order, problems = load_order(plugins)
    for plugin in plugins:
        for problem in plugin.problems:
            problems.append("%s: %s" % (plugin.path.name, problem))
    return {"plugins": plugins, "order": order, "problems": problems}
