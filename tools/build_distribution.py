#!/usr/bin/env python3
"""Build the complete, reproducible W4VE runtime using the standard library."""
import argparse
import ast
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("w4ve.py", "runtime.py", "package.py", "piecesettings.py",
           "discover.py", "worker.py", "api.py", "rcon.py", "pluginhost.py", "identity.py", "pairing.py")
TREES = ("mcdr", "compat", "sdk", "docs")


def build(source, output):
    source, output = Path(source), Path(output)
    tree = ast.parse((source / "w4ve.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "VERSION"
                           for t in node.targets))
    paths = [source / name for name in MODULES + ("install.py", "README.md", "LICENSE")]
    for name in TREES:
        paths.extend(p for p in (source / name).rglob("*") if p.is_file()
                     and not any(part in ("__pycache__", ".venv", ".git")
                                 for part in p.relative_to(source).parts)
                     and p.suffix not in (".pyc", ".pyo"))
    payload = {}
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("distribution may not contain symlinks: %s" % path)
        relative = path.relative_to(source).as_posix()
        content = path.read_bytes()
        if path.suffix in (".py", ".sh", ".md"):
            content = content.replace(b"\r\n", b"\n")
        payload[relative] = content
    manifest = {"schema": 1, "version": version,
                "files": {name: hashlib.sha256(data).hexdigest()
                          for name, data in payload.items()}}
    payload["distribution.json"] = (json.dumps(manifest, sort_keys=True, indent=2)
                                     + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / ("w4ve-%s.tar.gz" % version)
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, data in sorted(payload.items()):
                    item = tarfile.TarInfo("w4ve-%s/%s" % (version, name))
                    item.size = len(data)
                    item.mode = 0o755 if name.endswith(".sh") else 0o644
                    archive.addfile(item, io.BytesIO(data))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(
        "%s  %s\n" % (digest, target.name), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "distribution")
    args = parser.parse_args()
    print(build(ROOT, args.out))
