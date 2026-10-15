"""The `.w4ve` package: a piece that arrives whole.

A `.w4ve` is a zip with a `w4ve.json` in it. Everything else is optional, and
the point of the format is the case a jar cannot cover: pieces whose second
half is not a file but a process. WaveChat is a mod plus an upload service plus
a generated pack; ShapeBoard is a mod plus a scan that runs on a timer;
RegionCast needed an MCDR plugin of its own just to restart itself. Today each
of those is installed by hand, in several places, by the one person who knows
how. That is the thing being fixed here.

The spec lives in `notas/ecosistema/W4VE-DISENO.md`, section 2 and 3. This
module adds one component kind to it, `service`, because the guardian of
RFC-0001 can already supervise processes and there is no reason for the second
half to keep living in a cron nobody looks at.

Standard library only, Python 3.9, same rules as the rest.
"""

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import piecesettings

SCHEMA = 1

# What a component can be. `plugin` is declared by the format and refused by
# this implementation on purpose: the native plugin platform does not exist
# yet, and accepting one would mean installing code nothing can run.
KINDS = {
    "mod": "mods",
    "config": "config",
    "datapack": "world/datapacks",
    "resourcepack": "resourcepacks",
    "asset": "w4ve/assets",
    "client": "w4ve/client",  # a jar for the players, served rather than loaded
    # A native plugin (RFC-0003). Its own folder under w4ve/plugins, because
    # a plugin is a folder with a manifest in it and not a single file.
    "plugin": "w4ve/plugins",
    "service": None,          # a process, not a file
    "watch": None,            # a line to look for, and what to do about it
    "http": None,             # a path on the public side, served or proxied
    "storage": None,          # a folder this piece writes to, with a ceiling
    "timer": None,            # something to run again every so often
}

# Kinds that are declarations rather than files, and so land in w4ve.toml.
DECLARED = ("service", "watch", "http", "storage", "timer")

# Nothing is refused outright any more. `plugin` was, until there was a
# platform to run one on.
NOT_YET = {}

RELOADS = ("hot", "restart", "never")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class PackageError(Exception):
    """Something about the package is wrong, and the message says what."""


# -------------------------------------------------------------------- reading

class Manifest:

    def __init__(self, data, source=""):
        self.data = data or {}
        self.source = source

    # -------------------------------------------------------------- accessors

    @property
    def id(self):
        return self.data.get("id", "")

    @property
    def version(self):
        return str(self.data.get("version", ""))

    @property
    def name(self):
        return self.data.get("name") or self.id

    @property
    def side(self):
        return self.data.get("side", "server")

    @property
    def mc(self):
        return self.data.get("mc", "")

    @property
    def components(self):
        return self.data.get("components") or []

    @property
    def requires(self):
        return self.data.get("requires") or {}

    @property
    def conflicts(self):
        return self.data.get("conflicts") or []

    @property
    def settings(self):
        """What this piece needs told to it before it can run."""
        return piecesettings.declarations_of(self.data)

    @property
    def guide(self):
        """A short install guide the piece carries, printed after installing.

        Three or four lines, in the manifest and not in a web page, because the
        moment somebody needs them is the moment the command finishes.
        """
        value = self.data.get("guide") or []
        return [value] if isinstance(value, str) else list(value)

    # ------------------------------------------------------------- validation

    def problems(self):
        """Everything wrong with this manifest, in one pass.

        One pass and not the first error, because whoever is building a package
        would rather see the four things to fix than run the command four
        times.
        """
        out = []
        schema = self.data.get("schema")
        if schema != SCHEMA:
            out.append("schema is %r, this w4ve understands %d" % (schema, SCHEMA))
        if not ID_RE.match(self.id or ""):
            out.append("id %r is not lowercase letters, digits and dashes" % self.id)
        for field in ("version", "summary"):
            if not self.data.get(field):
                out.append("%s is missing" % field)
        if self.side not in ("server", "client", "both"):
            out.append("side is %r, not server, client or both" % self.side)
        if not self.components:
            out.append("no components: the package would install nothing")

        seen = set()
        if self.data.get("pairing") is not None:
            try:
                from pairing import declaration
                spec = declaration(self.data["pairing"])
                declared_settings = {item.name: item.type for item in self.settings}
                if (declared_settings.get(spec["enabled_setting"]) != "bool"
                        or declared_settings.get(spec["url_setting"]) != "url"):
                    out.append("pairing requires declared bool enable and url settings")
            except (ValueError, TypeError, KeyError):
                out.append("invalid pairing declaration")
        for declaration in self.settings:
            out.extend(declaration.problems())
            if declaration.name in seen:
                out.append("setting %s is declared twice" % declaration.name)
            seen.add(declaration.name)
        for declaration in self.settings:
            if declaration.when and declaration.when not in seen:
                out.append("setting %s waits on %r, which is not a setting here"
                           % (declaration.name, declaration.when))

        for index, component in enumerate(self.components):
            where = "component %d" % (index + 1)
            kind = component.get("kind")
            if kind not in KINDS:
                out.append("%s: unknown kind %r" % (where, kind))
                continue
            if kind in NOT_YET:
                out.append("%s: %s" % (where, NOT_YET[kind]))
            reload_rule = component.get("reload", "restart")
            if kind == "mod" and "required" in component and type(component["required"]) is not bool:
                out.append("%s: required must be a boolean" % where)
            if kind == "mod" and component.get("required") is True and not component.get("mc"):
                out.append("%s: a required server mod needs a Minecraft compatibility range" % where)
            if reload_rule not in RELOADS:
                out.append("%s: reload is %r, not one of %s"
                           % (where, reload_rule, ", ".join(RELOADS)))
            if kind == "service":
                out.extend("%s: %s" % (where, p)
                           for p in service_problems(component))
            elif kind == "watch":
                out.extend("%s: %s" % (where, p)
                           for p in watch_problems(component))
            elif kind == "http":
                out.extend("%s: %s" % (where, p)
                           for p in http_problems(component))
            elif kind == "storage":
                out.extend("%s: %s" % (where, p)
                           for p in storage_problems(component))
            elif kind == "timer":
                out.extend("%s: %s" % (where, p)
                           for p in timer_problems(component))
            elif kind == "plugin" and not component.get("install_dir"):
                if not component.get("name"):
                    out.append("%s: a plugin needs a `name`, which is the "
                               "folder it lands in" % where)
                elif not ID_RE.match(component["name"]):
                    out.append("%s: plugin name %r is not lowercase letters, "
                               "digits and dashes" % (where, component["name"]))
                if not component.get("file"):
                    out.append("%s: no file" % where)
            elif not component.get("file"):
                out.append("%s: no file" % where)
            elif component["file"].startswith("/") or ".." in component["file"]:
                # A member path is not a place on this machine to write to.
                out.append("%s: file %r escapes the package"
                           % (where, component["file"]))
        return out

    # --------------------------------------------------------------- choosing

    def mods_for(self, mc):
        """Every `mod` component that fits this Minecraft version.

        More than one means the package is wrong, and it is better to say so
        while installing than to let Fabric find two copies at boot.
        """
        import w4ve  # for mc_matches, which already knows about ranges

        chosen = []
        for component in self.components:
            if component.get("kind") != "mod":
                continue
            spec = component.get("mc")
            if not spec or w4ve.mc_matches(spec, mc) is not False:
                chosen.append(component)
        return chosen

    def install_plan(self, mc, side="server"):
        """What this package would put where, in order.

        Returns (actions, problems). Never touches the disk: `w4ve install -n`
        prints exactly this, and the apply step below consumes it.
        """
        problems = list(self.problems())
        actions = []
        if self.side != "both" and side != self.side:
            problems.append("this package is for the %s side, not the %s side"
                            % (self.side, side))
        mods = self.mods_for(mc)
        if not mods and any(c.get("kind") == "mod" and c.get("required") is True for c in self.components):
            problems.append("this package requires a server mod, but none of its jars supports Minecraft %s" % mc)
        if len(mods) > 1:
            problems.append(
                "%d of its jars claim Minecraft %s (%s): the package has to "
                "pick one" % (len(mods), mc,
                              ", ".join(Path(m["file"]).name for m in mods)))
        for component in self.components:
            kind = component.get("kind")
            if kind == "mod" and component not in mods:
                continue
            if kind in DECLARED:
                actions.append({
                    "kind": kind,
                    "name": component["name"],
                    "component": component,
                    "reload": component.get("reload", "hot"),
                })
                continue
            target = component.get("install_dir") or KINDS.get(kind) or "."
            if kind == "plugin" and not component.get("install_dir"):
                # A native plugin is a folder with a manifest in it, not a
                # loose file, so every file of one lands under its own name.
                # Without this they would all pile into w4ve/plugins/ and no
                # plugin would have a folder to be found in.
                target = "%s/%s" % (target, component.get("name") or "")
            actions.append({
                "kind": kind,
                "member": component["file"],
                "dest": "%s/%s" % (target.rstrip("/"), Path(component["file"]).name),
                "reload": component.get("reload", "restart"),
                "keep": bool(component.get("keep_on_update")),
                "component": component,
            })
        return actions, problems


WATCH_ACTIONS = ("restart", "note")


def watch_problems(component):
    """A watch is a line to look for on the console, and what to do about it.

    This is what lets RegionCast stop needing an MCDR plugin of its own. The
    mod stops the server to copy region files and prints one line on the way
    out asking to come back; under MCDR that costs a plugin, because MCDR shuts
    down with the server. The guardian already reads the console and already
    knows how to start a server, so the piece can just say so.
    """
    out = []
    if not component.get("name"):
        out.append("a watch needs a name")
    elif not ID_RE.match(component["name"]):
        out.append("watch name %r is not lowercase letters, digits and dashes"
                   % component["name"])
    if not component.get("on"):
        out.append("a watch needs `on`, the line to look for")
    else:
        try:
            re.compile(component["on"])
        except re.error as exc:
            out.append("`on` is not a valid pattern: %s" % exc)
    if component.get("do") not in WATCH_ACTIONS:
        out.append("`do` is %r, not one of %s"
                   % (component.get("do"), ", ".join(WATCH_ACTIONS)))
    return out


CACHE_RULES = ("immutable", "short", "none")


def http_problems(component):
    """A path on the public side: either a folder to serve or a service to ask.

    This is the piece of WaveChat that costs the most by hand today, and the
    one that has nothing to do with Minecraft: a bind mount, a `location` that
    has to come before another one, a body size limit, a cache header that has
    to be repeated or the browser downloads the image instead of showing it.
    None of that is knowledge a person installing a chat mod should need.
    """
    out = []
    if not component.get("name"):
        out.append("an http route needs a name")
    elif not ID_RE.match(component["name"]):
        out.append("http name %r is not lowercase letters, digits and dashes"
                   % component["name"])
    path = component.get("path")
    if not path:
        out.append("an http route needs `path`, the URL it answers on")
    elif not path.startswith("/"):
        out.append("path %r has to start with /" % path)
    serve, proxy = component.get("serve"), component.get("proxy")
    if bool(serve) == bool(proxy):
        out.append("an http route is either `serve` (a folder) or `proxy` "
                   "(a service), one of the two")
    if serve and (str(serve).startswith("/") or ".." in str(serve)):
        out.append("serve %r escapes the server folder" % serve)
    if proxy and not re.match(r"^[\w.\-]+:\d+$", str(proxy)):
        out.append("proxy %r is not host:port" % proxy)
    cache = component.get("cache", "none")
    if cache not in CACHE_RULES:
        out.append("cache is %r, not one of %s" % (cache, ", ".join(CACHE_RULES)))
    if component.get("max_body"):
        try:
            piecesettings.parse_size(component["max_body"], "max_body")
        except piecesettings.SettingError as exc:
            out.append(str(exc))
    return out


def storage_problems(component):
    """A folder this piece writes to, with a ceiling and a way to make room.

    Declared because a piece that writes without a ceiling is how a disk fills
    up quietly: the music cache of WaveChat is eighty times bigger than
    everything else it stores, and nothing anywhere said so.
    """
    out = []
    if not component.get("name"):
        out.append("a storage needs a name")
    elif not ID_RE.match(component["name"]):
        out.append("storage name %r is not lowercase letters, digits and dashes"
                   % component["name"])
    folder = component.get("dir")
    if not folder:
        out.append("a storage needs `dir`")
    elif str(folder).startswith("/") or ".." in str(folder):
        out.append("dir %r escapes the server folder" % folder)
    if component.get("quota"):
        try:
            piecesettings.parse_size(component["quota"], "quota")
        except piecesettings.SettingError as exc:
            out.append(str(exc))
    if component.get("keep"):
        try:
            piecesettings.parse_time(component["keep"], "keep")
        except piecesettings.SettingError as exc:
            out.append(str(exc))
    if not component.get("quota") and not component.get("keep"):
        out.append("a storage needs a `quota`, a `keep`, or both: without one "
                   "of them it is just a folder")
    if component.get("by", "mtime") not in ("mtime", "atime"):
        out.append("by is %r, not mtime or atime" % component.get("by"))
    return out


def timer_problems(component):
    """Something to run again every so often, watched instead of forgotten.

    A cron nobody looks at is the failure mode this exists to remove: the
    ShapeBoard scan was dead for three days and the only symptom was a board
    that stopped changing.
    """
    out = []
    if not component.get("name"):
        out.append("a timer needs a name")
    elif not ID_RE.match(component["name"]):
        out.append("timer name %r is not lowercase letters, digits and dashes"
                   % component["name"])
    command = component.get("command")
    if not command or not isinstance(command, list):
        out.append("a timer needs a command, as a list of arguments")
    if not component.get("every"):
        out.append("a timer needs `every`, how often to run it")
    else:
        try:
            seconds = piecesettings.parse_time(component["every"], "every")
            if seconds < 5:
                out.append("every %r is faster than five seconds, which is a "
                           "loop and not a timer" % component["every"])
        except piecesettings.SettingError as exc:
            out.append(str(exc))
    return out


def service_problems(component):
    out = []
    if component.get("w4ve_api") not in (None, "identity"):
        out.append("w4ve_api must be identity; services cannot request operator scopes")
    python = component.get("python") or {}
    if python and not isinstance(python.get("requires", []), list):
        out.append("python.requires has to be a list of module names")
    if not component.get("name"):
        out.append("a service needs a name")
    elif not ID_RE.match(component["name"]):
        out.append("service name %r is not lowercase letters, digits and dashes"
                   % component["name"])
    command = component.get("command")
    if not command or not isinstance(command, list):
        out.append("a service needs a command, as a list of arguments")
    restart = component.get("restart", "on-failure")
    if restart not in ("never", "on-failure", "always"):
        out.append("restart is %r, not never, on-failure or always" % restart)
    return out


def read_manifest(path):
    """Read the manifest out of a `.w4ve`, or out of a folder that will be one."""
    path = Path(path)
    if path.is_dir():
        manifest = path / "w4ve.json"
        if not manifest.exists():
            raise PackageError("%s has no w4ve.json in it" % path)
        return Manifest(json.loads(manifest.read_text(encoding="utf-8")), str(path))
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open("w4ve.json") as fh:
                return Manifest(json.loads(fh.read().decode("utf-8")), str(path))
    except KeyError:
        raise PackageError("%s has no w4ve.json in it, so it is not a package"
                           % path.name)
    except zipfile.BadZipFile:
        raise PackageError("%s is not a zip, so it is not a package" % path.name)


# -------------------------------------------------------------------- packing

def pack(folder, out_dir=None):
    """Build `<id>-<version>.w4ve` out of a folder with a w4ve.json in it."""
    folder = Path(folder)
    manifest = read_manifest(folder)
    problems = manifest.problems()
    missing = [c["file"] for c in manifest.components
               if c.get("kind") != "service" and c.get("file")
               and not (folder / c["file"]).exists()]
    problems += ["%s is in the manifest and not in the folder" % m for m in missing]
    if problems:
        raise PackageError("\n".join(problems))

    out_dir = Path(out_dir or folder.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("%s-%s.w4ve" % (manifest.id, manifest.version))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            archive.write(path, path.relative_to(folder).as_posix())
    return target, manifest


# ----------------------------------------------------------------- installing

def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def client_version(component, filename, fallback):
    """What version a client jar is, from the manifest or from its name.

    `wavechat-4.7.0+1.21.jar` -> `4.7.0`. Declared wins over guessed, because a
    file name is a convention and a manifest is a statement.
    """
    declared = component.get("version")
    if declared:
        return str(declared)
    stem = Path(filename).stem
    if "+" in stem:
        stem = stem.split("+")[0]
    if "-" in stem:
        tail = stem.rsplit("-", 1)[1]
        if tail and tail[0].isdigit():
            return tail
    return fallback


class Install:
    """One installation, applied whole or not at all.

    Every file written is remembered with its hash, so removing a piece takes
    out exactly what it put in, and `doctor` can tell a file somebody edited
    from a file that drifted.

    Rolling back in memory only helps while this process is alive. A power cut
    or a kill in the middle would leave half a piece on disk and nothing that
    knows it, so every step is also written to a journal on disk before it
    happens, and the next command to run finds it and undoes the mess.
    """

    def __init__(self, package, server):
        self.package = Path(package)
        self.server = server
        self.manifest = read_manifest(package)
        self.written = []       # (destination, backup or None)
        self.services = []
        self.watches = []
        self.declared = []      # (section, name), for anything else in the toml
        self.journal_path = (Path(server.root) / "w4ve" / "run" /
                             "install.journal")

    def plan(self, mc=None, side=None):
        return self.manifest.install_plan(mc or self.server.mc,
                                          side or self.server.kind)

    def base_for(self, relative):
        """Where a component lands: the game folder, or the runtime folder.

        Anything under `w4ve/` belongs to the runtime and not to Minecraft, so
        it hangs off the server root even when the game lives in `server/`.
        That is what keeps a service and its files together no matter which
        layout this server uses.
        """
        root = Path(self.server.root)
        if str(relative).startswith("w4ve/"):
            return root
        return root / self.server.game_dir

    def _journal(self, entry):
        """Write down what is about to happen, before it happens."""
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def apply(self, actions):
        record = {"version": self.manifest.version, "source": "package",
                  "files": {}, "editable": [], "services": [], "watches": [],
                  "http": [], "storage": [], "timers": [],
                  # Kept so `w4ve configure` works months later, with the
                  # package file long gone: the declarations are what turn a
                  # value into a question with help text.
                  "settings": self.manifest.data.get("settings") or [],
                  "guide": self.manifest.guide,
                  "package": self.package.name}
        if self.manifest.data.get("pairing") is not None:
            record["pairing"] = self.manifest.data["pairing"]
        pending = False
        self._journal({"op": "begin", "piece": self.manifest.id,
                       "version": self.manifest.version,
                       "package": self.package.name})
        try:
            with zipfile.ZipFile(self.package) as archive:
                for action in actions:
                    if action["kind"] == "service":
                        self._add_service(action, record)
                        continue
                    if action["kind"] == "watch":
                        self._add_watch(action, record)
                        continue
                    if action["kind"] == "http":
                        self._add_http(action, record)
                        continue
                    if action["kind"] == "storage":
                        self._add_storage(action, record)
                        continue
                    if action["kind"] == "timer":
                        self._add_timer(action, record)
                        continue
                    destination = self.base_for(action["dest"]) / action["dest"]
                    self._write(archive, action, destination, record)
                    if action["reload"] == "restart":
                        pending = True
        except Exception:
            self.rollback()
            raise
        if pending:
            record["pending_restart"] = True
        self._write_client_index(record)
        self.server.state["pieces"][self.manifest.id] = record
        self.server.save_state()
        return record

    def _write_client_index(self, record):
        """Say which client jar goes with this version of the server half.

        The two halves of a piece are published together or they lie to each
        other. WaveChat's client asks a JSON which version to run, and today
        that JSON is uploaded by hand, after the jars, in an order that if
        reversed points everybody at a 404. Written here it cannot get out of
        step: it is written from what was just installed.
        """
        jars = {k: v for k, v in record["files"].items()
                if k.startswith("w4ve/client/")}
        if not jars:
            return
        root = Path(self.server.root)
        index = {"piece": self.manifest.id, "version": self.manifest.version,
                 "minecraft": {}}
        for component in self.manifest.components:
            if component.get("kind") != "client":
                continue
            name = Path(component["file"]).name
            relative = "w4ve/client/" + name
            if relative not in jars:
                continue
            index["minecraft"][str(component.get("mc") or "*")] = {
                # ⚠️ The version of the JAR, not of the package. They are
                # different numbers on purpose: the piece is at 1.0.0 while the
                # mod is at 4.7.0. A client compares this against its own
                # version, so writing the package's here tells everybody they
                # are running something newer than what is published.
                "version": client_version(component, name, self.manifest.version),
                "file": name,
                "sha256": jars[relative],
            }
        path = root / "w4ve" / "client" / "latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        record["files"]["w4ve/client/latest.json"] = sha256_of(path)

    def _write(self, archive, action, destination, record):
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if destination.exists():
            if action["keep"]:
                # A config the operator edited is theirs, not ours: leave it
                # and put the new default next to it for `doctor` to mention.
                fresh = destination.with_suffix(destination.suffix + ".new")
                self._journal({"op": "write", "path": str(fresh), "backup": None})
                self._extract(archive, action["member"], fresh)
                self.written.append((fresh, None))
                record["files"][action["dest"] + ".new"] = sha256_of(fresh)
                return
            backup = destination.with_suffix(destination.suffix + ".w4ve-old")
            shutil.move(str(destination), str(backup))
        self._journal({"op": "write", "path": str(destination),
                       "backup": str(backup) if backup else None})
        self._extract(archive, action["member"], destination)
        self.written.append((destination, backup))
        record["files"][action["dest"]] = sha256_of(destination)
        if action["kind"] == "config" or action["component"].get("keep_on_update"):
            # The operator is supposed to edit this one, so a changed hash is
            # the tool working, not a problem to report.
            record["editable"].append(action["dest"])

    def _extract(self, archive, member, destination):
        with archive.open(member) as source, open(destination, "wb") as fh:
            shutil.copyfileobj(source, fh)
        # Readable by the user the server runs as, which in a container is not
        # the user running this command.
        os.chmod(destination, 0o644)

    def _add_service(self, action, record):
        component = action["component"]
        self._journal({"op": "service", "name": component["name"]})
        fields = {
            "command": component["command"],
            "cwd": component.get("cwd", "."),
            "restart": component.get("restart", "on-failure"),
        }
        # Declared, not installed. Fetching packages from pip is a different
        # problem with a different blast radius; what a piece owes the operator
        # today is to say what it needs, so `doctor` can check it and nobody
        # finds out from a traceback at boot.
        requires = (component.get("python") or {}).get("requires")
        if requires:
            fields["python_requires"] = list(requires)
        health = component.get("health") or {}
        if health.get("http"):
            fields["health_url"] = health["http"]
            fields["health_every"] = int(health.get("every", 30))
        elif health.get("tcp"):
            # A daemon speaking its own protocol could never answer a URL, and
            # without this its readiness stays unknown forever.
            fields["health_tcp"] = health["tcp"]
        # Everything the runtime already understood but the packager could not
        # write down, which meant a package could not express ChatBridge.
        if component.get("after"):
            fields["after"] = component["after"]
        if component.get("env"):
            fields["env"] = dict(component["env"])
        if component.get("mcdr"):
            fields["mcdr"] = True
        if component.get("w4ve_api"):
            fields["w4ve_api"] = component["w4ve_api"]
        if component.get("stop_grace"):
            fields["stop_grace"] = int(component["stop_grace"])
        self.server.set_service(component["name"], fields,
                                owner=self.manifest.id)
        record["services"].append(component["name"])
        self.services.append(component["name"])

    def _add_http(self, action, record):
        component = action["component"]
        self._journal({"op": "http", "name": component["name"]})
        fields = {"path": component["path"]}
        for key in ("serve", "proxy", "cache", "max_body", "why"):
            if component.get(key):
                fields[key] = component[key]
        if component.get("exact"):
            fields["exact"] = True
        self.server.set_http(component["name"], fields, owner=self.manifest.id)
        record["http"].append(component["name"])
        self.declared.append(("http", component["name"]))

    def _add_storage(self, action, record):
        component = action["component"]
        self._journal({"op": "storage", "name": component["name"]})
        fields = {"dir": component["dir"]}
        for key in ("quota", "keep", "by", "why"):
            if component.get(key):
                fields[key] = component[key]
        self.server.set_storage(component["name"], fields, owner=self.manifest.id)
        # The folder is made now rather than by the service on its first write,
        # so a permissions problem is found by `install` and not at 3am.
        folder = Path(self.server.root) / component["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        record["storage"].append(component["name"])
        self.declared.append(("storage", component["name"]))

    def _add_timer(self, action, record):
        component = action["component"]
        self._journal({"op": "timer", "name": component["name"]})
        fields = {"command": list(component["command"]),
                  "every": component["every"]}
        for key in ("cwd", "why"):
            if component.get(key):
                fields[key] = component[key]
        if component.get("env"):
            fields["env"] = dict(component["env"])
        self.server.set_timer(component["name"], fields, owner=self.manifest.id)
        record["timers"].append(component["name"])
        self.declared.append(("timer", component["name"]))

    def _add_watch(self, action, record):
        component = action["component"]
        self._journal({"op": "watch", "name": component["name"]})
        self.server.set_watch(component["name"], {
            "on": component["on"],
            "do": component["do"],
            "why": component.get("why", ""),
        }, owner=self.manifest.id)
        record.setdefault("watches", []).append(component["name"])
        self.watches.append(component["name"])

    def rollback(self):
        """Put the folder back the way it was, then complain upwards."""
        for destination, backup in reversed(self.written):
            try:
                if destination.exists():
                    destination.unlink()
                if backup and backup.exists():
                    shutil.move(str(backup), str(destination))
            except OSError:
                pass
        for name in self.services:
            self.server.remove_service(name)
        for name in self.watches:
            self.server.remove_watch(name)
        for section, name in self.declared:
            self.server.remove_block(section, name)
        self.written, self.services, self.watches = [], [], []
        self.declared = []
        try:
            self.journal_path.unlink()
        except OSError:
            pass

    def commit(self):
        """Drop the backups and close the journal once the install stuck."""
        for _, backup in self.written:
            if backup and backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass
        try:
            self.journal_path.unlink()
        except OSError:
            pass


def interrupted(server):
    """Was an install killed halfway through? Return what it had got to."""
    path = Path(server.root) / "w4ve" / "run" / "install.journal"
    if not path.exists():
        return None
    steps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            steps.append(json.loads(line))
        except ValueError:
            continue
    if not steps:
        return None
    begin = steps[0] if steps[0].get("op") == "begin" else {}
    return {"piece": begin.get("piece", "something"),
            "version": begin.get("version", ""),
            "package": begin.get("package", ""),
            "steps": steps, "path": path}


def roll_back_interrupted(server):
    """Undo what a killed install had already done, and say what it undid.

    This runs from the next command, not from the process that died, so the
    only thing it can trust is the journal: which files were about to be
    written, and what was moved out of the way to make room.
    """
    found = interrupted(server)
    if not found:
        return None
    undone, restored, kept = [], [], []
    for step in reversed(found["steps"]):
        if step.get("op") == "write":
            path = Path(step["path"])
            backup = Path(step["backup"]) if step.get("backup") else None
            if path.exists():
                try:
                    path.unlink()
                    undone.append(str(path))
                except OSError:
                    kept.append(str(path))
            if backup and backup.exists():
                try:
                    shutil.move(str(backup), str(path))
                    restored.append(str(path))
                except OSError:
                    kept.append(str(backup))
        elif step.get("op") == "service":
            server.remove_service(step["name"])
        elif step.get("op") == "watch":
            server.remove_watch(step["name"])
        elif step.get("op") in ("http", "storage", "timer"):
            server.remove_block(step["op"], step["name"])
    server.state["pieces"].pop(found["piece"], None)
    server.save_state()
    try:
        found["path"].unlink()
    except OSError:
        pass
    return {"piece": found["piece"], "version": found["version"],
            "undone": undone, "restored": restored, "kept": kept}


def remove(server, piece_id, keep_settings=False):
    """Take out exactly what a package put in, and say what it did not.

    A file that changed since we wrote it is left alone: somebody meant that,
    and a remove command is not a licence to throw away work.

    `keep_settings` is for the update path, which removes the old version
    before writing the new one. **Updating a piece must never lose what
    somebody answered**: a token typed once and forgotten is a token nobody
    can type again.
    """
    entry = server.state["pieces"].get(piece_id)
    if not entry:
        return None
    root = Path(server.root)
    gone, changed, missing = [], [], []
    for relative, digest in (entry.get("files") or {}).items():
        base = root if relative.startswith("w4ve/") else root / server.game_dir
        path = base / relative
        if not path.exists():
            missing.append(relative)
            continue
        if digest and sha256_of(path) != digest:
            changed.append(relative)
            continue
        if relative in (entry.get("editable") or []) and digest is None:
            changed.append(relative)
            continue
        path.unlink()
        gone.append(relative)
    # Read where the data lives before the blocks that say so are taken out.
    kept_dirs = [(server.config.get("storage") or {}).get(name, {}).get("dir")
                 for name in entry.get("storage") or []]
    for name in entry.get("services") or []:
        server.remove_service(name)
    for name in entry.get("watches") or []:
        server.remove_watch(name)
    for section, key in (("http", "http"), ("storage", "storage"),
                         ("timer", "timers")):
        for name in entry.get(key) or []:
            server.remove_block(section, name)
    # The settings block goes with it: leaving `[settings.<piece>]` behind
    # after a remove is how a reinstall silently inherits an old answer.
    if not keep_settings:
        server.remove_block("settings", piece_id)
        server.forget_secrets(piece_id)
    del server.state["pieces"][piece_id]
    server.save_state()
    return {"removed": gone, "changed": changed, "missing": missing,
            "services": entry.get("services") or [],
            "watches": entry.get("watches") or [],
            "http": entry.get("http") or [],
            "timers": entry.get("timers") or [],
            # Data is never deleted by a remove. Say where it is instead.
            "kept_data": [d for d in kept_dirs if d]}
