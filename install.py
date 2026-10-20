#!/usr/bin/env python3
"""Install a complete W4VE archive; activating it does not restart servers."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

REQUIRED = {"w4ve.py", "runtime.py", "package.py", "worker.py", "api.py",
            "piecesettings.py", "discover.py", "rcon.py", "pluginhost.py", "identity.py",
            "pairing.py", "gameevents.py", "serviceprep.py", "sdk/conformance.py", "sdk/python/w4ve_plugin/__init__.py",
            "mcdr/__init__.py"}
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("unsafe distribution path")
    parts = PurePosixPath(name).parts
    if (PurePosixPath(name).is_absolute() or "/".join(parts) != name
            or any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                   or part.endswith(".") for part in parts)):
        raise ValueError("unsafe distribution path: %s" % name)
    return name


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest["schema"] != 1 or not isinstance(manifest.get("version"), str)
            or not VERSION_RE.fullmatch(manifest["version"])
            or not isinstance(manifest.get("files"), dict)):
        raise ValueError("unsupported distribution manifest")
    names = manifest["files"]
    for name, digest in names.items():
        safe_name(name)
        if name == "distribution.json" or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid distribution hash: %s" % name)
    if not REQUIRED <= names.keys():
        raise ValueError("incomplete runtime distribution")
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents
           if str(parent) != "."):
        raise ValueError("file and directory paths conflict")
    return manifest["version"]


def verified_release(release, expected_version=None):
    release = Path(release)
    if release.is_symlink() or release.parent.is_symlink():
        raise ValueError("release directories may not be symlinks")
    manifest_path = release / "distribution.json"
    if manifest_path.is_symlink():
        raise ValueError("release manifest may not be a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = validate_manifest(manifest)
    if expected_version is not None and version != expected_version:
        raise ValueError("installed version disagrees with manifest")
    for name, digest in manifest["files"].items():
        candidate = release / name
        if any(part.is_symlink() for part in (candidate, *candidate.parents)
               if part != release.parent):
            raise ValueError("installed release contains a symlink: %s" % name)
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            raise ValueError("installed release was modified: %s" % name)
    return manifest


def read_archive(path):
    files = {}
    total = 0
    root = None
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            safe_name(item.name)
            parts = PurePosixPath(item.name).parts
            if not item.isfile() or len(parts) < 2 or item.size < 0:
                raise ValueError("unsafe archive entry: %s" % item.name)
            if root is None:
                root = parts[0]
            elif root != parts[0]:
                raise ValueError("archive contains multiple roots")
            name = "/".join(parts[1:])
            if name in files:
                raise ValueError("duplicate archive entry: %s" % name)
            total += item.size
            if item.size > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
                raise ValueError("runtime archive exceeds size limit")
            files[name] = archive.extractfile(item).read()
    manifest = json.loads(files.pop("distribution.json"))
    version = validate_manifest(manifest)
    if root != "w4ve-" + version:
        raise ValueError("archive root disagrees with manifest")
    if set(files) != set(manifest["files"]) or not REQUIRED <= set(files):
        raise ValueError("incomplete runtime distribution")
    for name, data in files.items():
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
            raise ValueError("hash mismatch: %s" % name)
    return version, files, manifest


def activate(release, bin_dir):
    # A normal directory, rather than zipimport: workers execute the SDK and
    # conformance scripts as real files in their own Python processes.
    verified_release(release)
    release, bin_dir = Path(release).resolve(), Path(bin_dir).resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = ("#!%s\nimport sys\nsys.path.insert(0, %r)\n"
                "from w4ve import main\nraise SystemExit(main())\n"
                % (sys.executable, str(release)))
    fd, temporary = tempfile.mkstemp(prefix=".w4ve-", dir=bin_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(launcher)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, bin_dir / "w4ve")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return bin_dir / "w4ve"


def install(archive, prefix, bin_dir):
    version, files, manifest = read_archive(archive)
    prefix = Path(prefix).resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    release = prefix / "releases" / version
    if release.parent.is_symlink() or release.is_symlink():
        raise ValueError("release directories may not be symlinks")
    release.parent.mkdir(parents=True, exist_ok=True)
    if release.exists():
        if verified_release(release, version) != manifest:
            raise ValueError("version already installed with different contents; use a new version")
    else:
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=release.parent))
        try:
            for name, data in files.items():
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                destination.chmod(0o755 if name.endswith(".sh") else 0o644)
            (staging / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
            os.replace(staging, release)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return activate(release, bin_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--archive", type=Path)
    choice.add_argument("--rollback", help="activate an already installed version")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/w4ve")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    try:
        if args.archive:
            command = install(args.archive, args.prefix, args.bin_dir)
        else:
            if not VERSION_RE.fullmatch(args.rollback):
                raise ValueError("invalid rollback version")
            release = args.prefix / "releases" / args.rollback
            verified_release(release, args.rollback)
            command = activate(release, args.bin_dir)
        print("W4VE installed: %s\nExisting server processes were not restarted." % command)
        return 0
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print("W4VE installation refused: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
