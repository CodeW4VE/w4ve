#!/usr/bin/env python3
"""Smoke-test a complete archive in a temporary prefix, without server access."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


def check(archive):
    version, _, _ = install.read_archive(archive)
    with tempfile.TemporaryDirectory(prefix="w4ve-dist-check-") as scratch:
        root = Path(scratch)
        prefix, bin_dir = root / "runtime", root / "bin"
        launcher = install.install(archive, prefix, bin_dir)
        result = subprocess.run([sys.executable, "-I", str(launcher), "--help"],
                                cwd=root, capture_output=True, text=True, timeout=20)
        if result.returncode or "install" not in result.stdout:
            raise AssertionError("installed launcher failed: " + result.stderr)
        release = prefix / "releases" / version
        imports = (
            "import sys; sys.path[:0] = " + repr([str(release), str(release / "sdk/python")])
            + "; import runtime, package, worker, api, piecesettings, discover, rcon, pluginhost;"
            + " import mcdr, w4ve_plugin; print('installed modules imported')"
        )
        result = subprocess.run([sys.executable, "-I", "-c", imports], cwd=root,
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AssertionError("installed imports failed: " + result.stderr)
        install.install(archive, prefix, bin_dir)
        install.verified_release(release, version)
        install.activate(release, bin_dir)
        old = launcher.read_bytes()
        original = release / "runtime.py"
        copy = root / "runtime-copy.py"
        copy.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(copy)
        try:
            install.activate(release, bin_dir)
        except ValueError:
            pass
        else:
            raise AssertionError("symlink accepted during activation")
        if launcher.read_bytes() != old:
            raise AssertionError("refused activation changed launcher")
    return {"version": version, "launcher": "passed", "installed_imports": "passed",
            "idempotent_install": "passed", "reactivation": "passed",
            "symlink_refusal": "passed", "server_operations": "not tested"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.archive), sort_keys=True))
