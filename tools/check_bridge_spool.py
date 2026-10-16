"""Pure Java spool QA. Does not attach a player or claim real chat delivery."""
import argparse
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def check(args):
    runtime_root = (args.runtime_root or ROOT).resolve()
    sys.path.insert(0, str(runtime_root))
    import gameevents
    from gameevents import SourceRuntime
    if not Path(gameevents.__file__).resolve().is_relative_to(runtime_root):
        raise AssertionError("spool fixture imported a different runtime")
    with tempfile.TemporaryDirectory(prefix="w4ve-spool-java-") as temporary:
        root = Path(temporary)
        descriptors = []
        for name in ("recovery", "count", "bytes"):
            work = root / name
            manager = SourceRuntime(work, {"enabled": True, "chat": True}, lambda *args: False)
            manager.prepare(SimpleNamespace(address=None, socket_path=work / "w4ve/run/api.sock"))
            descriptors.append(str(manager.path))
        classes = root / "classes"
        classes.mkdir()
        source = ROOT / "bridge/src/main/java/xyz/w4ve/bridge"
        result = subprocess.run([str(args.java_home / "bin/javac"), "-cp", str(args.gson), "-d", str(classes),
            str(source / "LocalSource.java"), str(source / "CaptureSpool.java"), str(ROOT / "tools/BridgeSpoolCheck.java")],
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError("spool harness did not compile: " + result.stderr)
        result = subprocess.run([str(args.java_home / "bin/java"), "-cp", str(classes) + os.pathsep + str(args.gson),
            "BridgeSpoolCheck", *descriptors], capture_output=True, text=True, timeout=30)
        if result.returncode:
            lines = [line for line in result.stderr.splitlines() if line.startswith("\tat ")]
            raise AssertionError("Java spool checks failed\n" + "\n".join(lines))
        return {"durable_reopen_and_staging": "passed", "receipt_boundary_and_sequence": "passed", "integrity_and_limits": "passed",
                "expiry_without_presence_claim": "passed", "minecraft_players_and_power_loss": "not tested"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--gson", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, help="installed release to test instead of checkout modules")
    print(json.dumps(check(parser.parse_args()), sort_keys=True))
