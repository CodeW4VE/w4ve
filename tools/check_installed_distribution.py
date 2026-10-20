#!/usr/bin/env python3
"""Smoke-test a complete archive in a temporary prefix, without server access."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


def check(archive, tests_dir=None):
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
            + "; import runtime, package, worker, api, piecesettings, discover, rcon, pluginhost, serviceprep, sourceprep;"
            + " import identity, gameevents, mcdr, w4ve_plugin; print('installed modules imported')"
        )
        result = subprocess.run([sys.executable, "-I", "-c", imports], cwd=root,
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise AssertionError("installed imports failed: " + result.stderr)
        install.install(archive, prefix, bin_dir)
        install.verified_release(release, version)
        install.activate(release, bin_dir)
        if tests_dir is not None:
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join([str(release), str(release / "sdk/client"), str(tests_dir)])
            for pattern in ("test_exec_service.py", "test_service_diagnostics.py", "test_serviceprep.py", "test_sourceprep.py", "test_gameevents*.py", "test_identity*.py", "test_api.py", "test_package.py", "test_pairing.py", "test_settings.py",
                            "test_runtime.py", "test_handover.py"):
                result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir),
                                         "-p", pattern, "-v"], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=90)
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
                if result.returncode:
                    raise AssertionError("installed API/identity tests failed")
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
            "symlink_refusal": "passed", "identity_api_tests": "passed" if tests_dir is not None else "not tested",
            "server_operations": "not tested"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--tests-dir", type=Path, help="trusted tests to run against installed runtime")
    args = parser.parse_args()
    print(json.dumps(check(args.archive, args.tests_dir), sort_keys=True))
