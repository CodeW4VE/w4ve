"""Host driver for disposable, network-none Podman bridge integration checks.

Requires cached python:3.12-slim and node:22-slim images, an exported Java21
runtime and a locked Mineflayer install. Copies only explicit QA inputs. Leaves
logs/world for inspection and never mounts a source checkout or production path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

TOOLS = Path(__file__).resolve().parent


def run(args):
    root = args.root.resolve()
    if root.parent != Path("/tmp") or not root.name.startswith("w4ve-bridge-qa-") or root.exists():
        raise ValueError("provide a fresh /tmp/w4ve-bridge-qa-* scratch directory")
    root.mkdir(mode=0o700)
    inputs = root / "inputs"
    inputs.mkdir()
    files = {"install.py": args.installer, "w4ve-0.5.0.tar.gz": args.runtime,
             "game.zip": args.game, "fabric-api.jar": args.fabric_api,
             "bridge.jar": args.bridge, "wavechat.jar": args.companion,
             "check_bridge_game.py": TOOLS / "check_bridge_game.py",
             "bridge_game_actors.cjs": TOOLS / "bridge_game_actors.cjs"}
    for name, path in files.items():
        shutil.copyfile(path.resolve(), inputs / name)
    manifest = {"inputs": {name: hashlib.sha256((inputs / name).read_bytes()).hexdigest() for name in files},
                "chat_opt_in": not args.chat_off}
    for image in ("python:3.12-slim", "node:22-slim"):
        result = subprocess.run(["podman", "image", "inspect", "docker.io/library/" + image,
                                 "--format", "{{.Id}}"], capture_output=True, text=True, check=True, timeout=20)
        manifest[image] = result.stdout.strip()
    manifest["node_lock_sha256"] = hashlib.sha256((args.node_dir / "package-lock.json").read_bytes()).hexdigest()
    (root / "inputs.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    name = "w4ve-bridge-qa-" + uuid.uuid4().hex[:12]
    common = ["podman", "run", "--rm", "--userns", "keep-id", "--user", "%d:%d" % (os.getuid(), os.getgid()),
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
              "--tmpfs", "/tmp:rw,exec,nosuid,size=256m", "-v", str(root) + ":/qa:z",
              "-v", str(args.java_home.resolve()) + ":/qa/jre:ro,z",
              "-v", str(args.node_dir.resolve()) + ":/qa/node:ro,z", "-w", "/qa"]
    command = common + ["--name", name, "--network", "none", "--memory", "2g", "--cpus", "2",
                        "--pids-limit", "192", "-e", "QA_CHAT_ENABLED=" + ("false" if args.chat_off else "true"),
                        "docker.io/library/python:3.12-slim", "python", "/qa/inputs/check_bridge_game.py"]
    with (root / "game-driver.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 180
            while not (root / "work/ready.signal").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("QA game did not become ready; inspect " + str(root))
                time.sleep(.25)
            with (root / "actors.log").open("w") as actors:
                actor = subprocess.run(common + ["--network", "container:" + name, "--memory", "512m",
                    "--pids-limit", "64", "docker.io/library/node:22-slim", "node", "/qa/inputs/bridge_game_actors.cjs"],
                    stdout=actors, stderr=actors, timeout=100)
            if actor.returncode:
                raise AssertionError("QA actors failed; inspect " + str(root))
            if process.wait(timeout=60):
                raise AssertionError("QA guardian driver failed; inspect " + str(root))
            result = json.loads((root / "work/result.json").read_text())
            result["scratch"] = str(root)
            return result
        finally:
            if process.poll() is None:
                subprocess.run(["podman", "stop", "--time", "20", name], capture_output=True, timeout=30)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("root", "runtime", "installer", "game", "fabric-api", "bridge", "companion", "java-home", "node-dir"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--chat-off", action="store_true", help="verify real chat is excluded unless explicitly enabled")
    print(json.dumps(run(parser.parse_args()), sort_keys=True))
