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
            "piecesettings.py", "discover.py", "rcon.py", "pluginhost.py",
            "sdk/conformance.py", "sdk/python/w4ve_plugin/__init__.py",
            "mcdr/__init__.py"}


def read_archive(path):
    files = {}
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            parts = PurePosixPath(item.name).parts
            if (not item.isfile() or len(parts) < 2 or ".." in parts
                    or PurePosixPath(item.name).is_absolute()
                    or "\\" in item.name or ":" in item.name):
                raise ValueError("unsafe archive entry: %s" % item.name)
            name = "/".join(parts[1:])
            if name in files:
                raise ValueError("duplicate archive entry: %s" % name)
            total += item.size
            if item.size > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
                raise ValueError("runtime archive exceeds size limit")
            files[name] = archive.extractfile(item).read()
    manifest = json.loads(files.pop("distribution.json"))
    version = manifest.get("version", "")
    if manifest.get("schema") != 1 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("unsupported distribution manifest")
    if set(files) != set(manifest["files"]) or not REQUIRED <= set(files):
        raise ValueError("incomplete runtime distribution")
    for name, data in files.items():
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
            raise ValueError("hash mismatch: %s" % name)
    return version, files, manifest


def activate(release, bin_dir):
    # A normal directory, rather than zipimport: workers execute the SDK and
    # conformance scripts as real files in their own Python processes.
    release, bin_dir = Path(release).resolve(), Path(bin_dir).resolve()
    if not (release / "distribution.json").is_file():
        raise ValueError("not an installed W4VE release: %s" % release)
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
    release.parent.mkdir(parents=True, exist_ok=True)
    if release.exists():
        if json.loads((release / "distribution.json").read_text()) != manifest:
            raise ValueError("version already installed with different contents; use a new version")
        for name, digest in manifest["files"].items():
            if hashlib.sha256((release / name).read_bytes()).hexdigest() != digest:
                raise ValueError("installed release was modified: %s" % name)
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
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", args.rollback):
                raise ValueError("invalid rollback version")
            release = args.prefix / "releases" / args.rollback
            manifest = json.loads((release / "distribution.json").read_text())
            for name, digest in manifest["files"].items():
                if hashlib.sha256((release / name).read_bytes()).hexdigest() != digest:
                    raise ValueError("installed release was modified: %s" % name)
            command = activate(release, args.bin_dir)
        print("W4VE installed: %s\nExisting server processes were not restarted." % command)
        return 0
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print("W4VE installation refused: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
