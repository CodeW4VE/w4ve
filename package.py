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
    "service": None,          # a process, not a file
    "plugin": "plugins",
}

NOT_YET = {"plugin": "the native plugin platform does not exist yet"}

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

        for index, component in enumerate(self.components):
            where = "component %d" % (index + 1)
            kind = component.get("kind")
            if kind not in KINDS:
                out.append("%s: unknown kind %r" % (where, kind))
                continue
            if kind in NOT_YET:
                out.append("%s: %s" % (where, NOT_YET[kind]))
            reload_rule = component.get("reload", "restart")
            if reload_rule not in RELOADS:
                out.append("%s: reload is %r, not one of %s"
                           % (where, reload_rule, ", ".join(RELOADS)))
            if kind == "service":
                out.extend("%s: %s" % (where, p)
                           for p in service_problems(component))
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
        if len(mods) > 1:
            problems.append(
                "%d of its jars claim Minecraft %s (%s): the package has to "
                "pick one" % (len(mods), mc,
                              ", ".join(Path(m["file"]).name for m in mods)))
        for component in self.components:
            kind = component.get("kind")
            if kind == "mod" and component not in mods:
                continue
            if kind == "service":
                actions.append({
                    "kind": "service",
                    "name": component["name"],
                    "component": component,
                    "reload": component.get("reload", "hot"),
                })
                continue
            target = component.get("install_dir") or KINDS.get(kind) or "."
            actions.append({
                "kind": kind,
                "member": component["file"],
                "dest": "%s/%s" % (target.rstrip("/"), Path(component["file"]).name),
                "reload": component.get("reload", "restart"),
                "keep": bool(component.get("keep_on_update")),
                "component": component,
            })
        return actions, problems


def service_problems(component):
    out = []
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


class Install:
    """One installation, applied whole or not at all.

    Every file written is remembered with its hash, so removing a piece takes
    out exactly what it put in, and `doctor` can tell a file somebody edited
    from a file that drifted.
    """

    def __init__(self, package, server):
        self.package = Path(package)
        self.server = server
        self.manifest = read_manifest(package)
        self.written = []       # (destination, backup or None)
        self.services = []

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

    def apply(self, actions):
        record = {"version": self.manifest.version, "source": "package",
                  "files": {}, "editable": [], "services": [],
                  "package": self.package.name}
        pending = False
        try:
            with zipfile.ZipFile(self.package) as archive:
                for action in actions:
                    if action["kind"] == "service":
                        self._add_service(action, record)
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
        self.server.state["pieces"][self.manifest.id] = record
        self.server.save_state()
        return record

    def _write(self, archive, action, destination, record):
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if destination.exists():
            if action["keep"]:
                # A config the operator edited is theirs, not ours: leave it
                # and put the new default next to it for `doctor` to mention.
                fresh = destination.with_suffix(destination.suffix + ".new")
                self._extract(archive, action["member"], fresh)
                self.written.append((fresh, None))
                record["files"][action["dest"] + ".new"] = sha256_of(fresh)
                return
            backup = destination.with_suffix(destination.suffix + ".w4ve-old")
            shutil.move(str(destination), str(backup))
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
        self.server.set_service(component["name"], fields,
                                owner=self.manifest.id)
        record["services"].append(component["name"])
        self.services.append(component["name"])

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
        self.written, self.services = [], []

    def commit(self):
        """Drop the backups once the install stuck."""
        for _, backup in self.written:
            if backup and backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass


def remove(server, piece_id):
    """Take out exactly what a package put in, and say what it did not.

    A file that changed since we wrote it is left alone: somebody meant that,
    and a remove command is not a licence to throw away work.
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
    for name in entry.get("services") or []:
        server.remove_service(name)
    del server.state["pieces"][piece_id]
    server.save_state()
    return {"removed": gone, "changed": changed, "missing": missing,
            "services": entry.get("services") or []}
