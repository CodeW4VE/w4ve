#!/usr/bin/env python3
"""What an MCDR plugin actually asks of MCDR, counted instead of guessed.

W4VE has to run the plugins MineWave depends on, and the honest way to size
that job is to read them: which names they import from `mcdreforged`, which
methods they call on the server interface, and which events they register for.
The answer is the list of things the compatibility host has to implement, in
the order that matters, with the plugins that need each one named.

    python3 tools/mcdr_api_survey.py ~/plugins/*            # one line per API
    python3 tools/mcdr_api_survey.py --by-plugin ./DigAll   # grouped the other way
    python3 tools/mcdr_api_survey.py --json ./PrimeBackup

It reads source with `ast`, so nothing is executed and a plugin does not have
to be installable to be surveyed. `.mcdr` and `.pyz` archives are read from the
inside; a plain `.py` plugin is read as it is.

Static reading has a floor: a call made through a variable
(`api = server.get_plugin_instance(...)`) is not attributed to the interface,
and `getattr` is invisible. What comes out is a lower bound, and a lower bound
is still a plan.
"""

import argparse
import ast
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

# Names that appear in `from mcdreforged.api.all import *` and are worth
# counting separately from method calls, because each one is a type or a
# decorator the host has to provide rather than a behaviour.
INTERESTING_ROOTS = ("mcdreforged",)

# The events MCDR 2.x actually calls. Any other `on_*` function in a plugin is
# the plugin's own callback, and counting those as API inflates the job: the
# first survey said 28 events where there are nine.
MCDR_EVENTS = {
    "on_load", "on_unload", "on_info", "on_user_info",
    "on_player_joined", "on_player_left",
    "on_server_start", "on_server_startup", "on_server_stop",
    "on_mcdr_start", "on_mcdr_stop",
}

SOURCE_SUFFIXES = (".py",)
ARCHIVE_SUFFIXES = (".mcdr", ".pyz", ".zip")


def imports_mcdr(tree):
    """Does this file import anything from mcdreforged at all?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in INTERESTING_ROOTS:
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in INTERESTING_ROOTS:
                    return True
    return False


class Survey:

    def __init__(self):
        self.imports = defaultdict(set)      # name -> plugins using it
        self.methods = defaultdict(set)      # server.<method> -> plugins
        self.events = defaultdict(set)       # real MCDR events -> plugins
        self.own_hooks = defaultdict(set)    # the plugin's own on_* callbacks
        self.files = defaultdict(int)        # plugin -> files read
        self.mcdr_files = defaultdict(int)   # plugin -> files that touch MCDR
        self.skipped = defaultdict(int)      # plugin -> files with no MCDR in them
        self.failed = defaultdict(list)      # plugin -> files that would not parse

    # ---------------------------------------------------------------- reading

    def add_path(self, path):
        path = Path(path)
        name = path.stem if path.is_file() else path.name
        if path.is_dir():
            for source in sorted(path.rglob("*.py")):
                if any(part in ("test", "tests", "__pycache__", "build")
                       for part in source.parts):
                    continue
                self._add_source(name, str(source.relative_to(path)),
                                 source.read_text(encoding="utf-8", errors="replace"))
        elif path.suffix in ARCHIVE_SUFFIXES:
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if not member.endswith(".py"):
                        continue
                    with archive.open(member) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8",
                                                errors="replace").read()
                    self._add_source(name, member, text)
        elif path.suffix in SOURCE_SUFFIXES:
            self._add_source(name, path.name,
                             path.read_text(encoding="utf-8", errors="replace"))
        else:
            raise SystemExit("%s is not a plugin I can read" % path)

    def _add_source(self, plugin, where, text):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Python 2 leftovers and template files live in real plugins.
            self.failed[plugin].append(where)
            return
        self.files[plugin] += 1
        # Only files that import mcdreforged are read for API use. ChatBridge
        # has a server class of its own, and counting `server.tell(...)` there
        # as MCDR API turned a 20 method surface into a 28 method one.
        if not imports_mcdr(tree):
            self.skipped[plugin] += 1
            return
        self.mcdr_files[plugin] += 1
        self._walk(plugin, tree)

    # ---------------------------------------------------------------- walking

    def _walk(self, plugin, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in INTERESTING_ROOTS:
                    for alias in node.names:
                        label = ("%s.*" % node.module if alias.name == "*"
                                 else "%s.%s" % (node.module, alias.name))
                        self.imports[label].add(plugin)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in INTERESTING_ROOTS:
                        self.imports[alias.name].add(plugin)
            elif isinstance(node, ast.FunctionDef) and node.name.startswith("on_"):
                # MCDR calls its own events by name. Everything else called
                # `on_something` belongs to the plugin and is not our problem.
                if node.name in MCDR_EVENTS:
                    self.events[node.name].add(plugin)
                else:
                    self.own_hooks[node.name].add(plugin)
            elif isinstance(node, ast.Call):
                target = self._interface_call(node)
                if target:
                    self.methods[target].add(plugin)

    @staticmethod
    def _interface_call(node):
        """`server.something(...)` where `server` looks like the interface."""
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in ("server", "psi", "si",
                                                        "interface"):
            return func.attr
        if isinstance(owner, ast.Attribute) and owner.attr in ("server", "psi"):
            return func.attr
        return None

    # --------------------------------------------------------------- printing

    def report(self, by_plugin=False):
        plugins = sorted(self.files)
        out = []
        out.append("%d plugin%s, %d files read, %d of them touching MCDR"
                   % (len(plugins), "" if len(plugins) == 1 else "s",
                      sum(self.files.values()), sum(self.mcdr_files.values())))
        for plugin in plugins:
            if self.failed[plugin]:
                out.append("  %s: %d file(s) would not parse"
                           % (plugin, len(self.failed[plugin])))
        out.append("")

        if by_plugin:
            for plugin in plugins:
                out.append(plugin)
                for label, table in (("imports", self.imports),
                                     ("calls", self.methods),
                                     ("events", self.events)):
                    used = sorted(k for k, v in table.items() if plugin in v)
                    if used:
                        out.append("  %-8s %s" % (label, ", ".join(used)))
                out.append("")
            return "\n".join(out)

        for title, table in (("Imported from mcdreforged", self.imports),
                             ("Called on the server interface", self.methods),
                             ("MCDR events implemented", self.events)):
            out.append(title)
            out.append("-" * len(title))
            rows = sorted(table.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            width = max([len(k) for k, _ in rows] or [1])
            for name, users in rows:
                out.append("  %s  %d  %s" % (name.ljust(width), len(users),
                                             ", ".join(sorted(users))))
            out.append("")
        if self.own_hooks:
            out.append("Not API: %d other `on_*` functions belong to the plugins "
                       "themselves" % len(self.own_hooks))
            out.append("")
        out.append("The compatibility host has to provide: %d names from "
                   "mcdreforged, %d interface methods, %d events."
                   % (len(self.imports), len(self.methods), len(self.events)))
        return "\n".join(out)

    def as_json(self):
        return {
            "plugins": sorted(self.files),
            "imports": {k: sorted(v) for k, v in self.imports.items()},
            "methods": {k: sorted(v) for k, v in self.methods.items()},
            "events": {k: sorted(v) for k, v in self.events.items()},
            "own_hooks": {k: sorted(v) for k, v in self.own_hooks.items()},
            "unparsed": {k: v for k, v in self.failed.items() if v},
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+",
                    help="plugin folders, .mcdr/.pyz archives or .py files")
    ap.add_argument("--by-plugin", action="store_true",
                    help="group by plugin instead of by API")
    ap.add_argument("--json", action="store_true", help="machine readable")
    args = ap.parse_args(argv)

    survey = Survey()
    for path in args.paths:
        survey.add_path(path)
    if args.json:
        print(json.dumps(survey.as_json(), indent=2, sort_keys=True))
    else:
        print(survey.report(by_plugin=args.by_plugin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
