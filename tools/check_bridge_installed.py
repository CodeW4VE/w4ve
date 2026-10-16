"""Run trusted Java bridge fixtures against an installed runtime in scratch space."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check(args):
    spec = importlib.util.spec_from_file_location("bridge_qa_installer", args.installer)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    with tempfile.TemporaryDirectory(prefix="w4ve-bridge-installed-") as temporary:
        root = Path(temporary)
        version, _, _ = installer.read_archive(args.runtime)
        installer.install(args.runtime, root / "runtime", root / "bin")
        release = root / "runtime/releases" / version
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join([str(release), str(release / "sdk/client")])
        checks = []
        for name in ("check_bridge_java.py", "check_bridge_spool.py"):
            script = Path(__file__).resolve().parent / name
            result = subprocess.run([sys.executable, str(script), "--java-home", str(args.java_home),
                                     "--gson", str(args.gson), "--runtime-root", str(release)], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise AssertionError("installed bridge check failed: " + name + "\n" + result.stderr)
            checks.append(json.loads(result.stdout))
        return {"installed_runtime": version, "checks": checks, "production_or_players": "not used"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "installer", "java-home", "gson"):
        parser.add_argument("--" + name, type=Path, required=True)
    print(json.dumps(check(parser.parse_args()), sort_keys=True))
