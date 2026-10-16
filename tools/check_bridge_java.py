"""Compile and probe the pure Java source client against owned local fixtures."""
import argparse
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def check(args):
    runtime_root = (args.runtime_root or ROOT).resolve()
    sys.path[:0] = [str(runtime_root), str(runtime_root / "sdk/client")]
    import api
    import gameevents
    from gameevents import SourceRuntime
    for module in (api, gameevents):
        if not Path(module.__file__).resolve().is_relative_to(runtime_root):
            raise AssertionError("bridge fixture imported a different runtime")
    with tempfile.TemporaryDirectory(prefix="w4ve-bridge-java-") as scratch:
        root = Path(scratch)
        manager = SourceRuntime(root, {"enabled": True}, lambda *args: False)
        authority = api.Api(root, address="127.0.0.1:0", game_events=manager.journal)
        if not authority.start()[0]:
            raise AssertionError("local fixture API did not start")
        servers, threads = [], []
        try:
            manager.prepare(authority)
            plan = {"positive": str(manager.path), "negative": []}
            template = json.loads(manager.path.read_text())
            cases = {
                "redirect": b"HTTP/1.1 302 Found\r\nLocation: https://example.invalid/\r\nContent-Length: 0\r\n\r\n",
                "duplicate-length": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
                "chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                "oversized": b"HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\n",
                "missing-length": b"HTTP/1.1 200 OK\r\n\r\n{}",
                "bad-json": b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\nPRIVATE_FIXTURE!",
                "wrong-ok-type": b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{"ok":"true"}',
                "trailing": b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}x',
                "invalid-utf8": b'HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\n{"ok":true,"x":"\xff"}',
                "deadline": b"HTTP/1.1 200 OK\r\nContent-Length: 32\r\n\r\n",
            }
            class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
            class Response(socketserver.BaseRequestHandler):
                def handle(self):
                    self.request.settimeout(4)
                    try:
                        self.request.recv(20000)
                        self.request.sendall(self.server.payload)
                        if self.server.label == "deadline":
                            for _ in range(20):
                                self.request.sendall(b" ")
                                time.sleep(.2)
                    except OSError:
                        pass
            for name, payload in cases.items():
                server = Server(("127.0.0.1", 0), Response)
                server.payload, server.label = payload, name
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append(server); threads.append(thread)
                descriptor = root / (name + ".json")
                descriptor.write_text(json.dumps({**template, "api_address": "127.0.0.1:%d" % server.server_address[1]}))
                os.chmod(descriptor, 0o600)
                plan["negative"].append({"name": name, "descriptor": str(descriptor)})
            forbidden = root / "non-loopback.json"
            forbidden.write_text(json.dumps({**template, "api_address": "192.0.2.1:9"}))
            os.chmod(forbidden, 0o600)
            plan["negative"].append({"name": "non-loopback", "descriptor": str(forbidden)})
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan))
            classes = root / "classes"
            classes.mkdir()
            compilation = subprocess.run([str(args.java_home / "bin/javac"), "-cp", str(args.gson), "-d", str(classes),
                str(ROOT / "bridge/src/main/java/xyz/w4ve/bridge/LocalSource.java"), str(ROOT / "tools/BridgeSourceCheck.java")],
                capture_output=True, text=True, timeout=30)
            if compilation.returncode:
                raise AssertionError("pure Java IPC harness did not compile: " + compilation.stderr)
            result = subprocess.run([str(args.java_home / "bin/java"), "-cp", str(classes) + os.pathsep + str(args.gson),
                "BridgeSourceCheck", str(plan_path)], capture_output=True, text=True, timeout=25)
            if result.returncode:
                # Java stack lines locate the bug without printing fixture content.
                lines = [line for line in result.stderr.splitlines() if line.startswith("\tat ")]
                raise AssertionError("Java source IPC checks failed\n" + "\n".join(lines))
            return {"java_local_ipc": "passed", "bounded_response_and_deadline": "passed", "unsafe_responses": len(plan["negative"]),
                    "source_replay_and_conflict": "passed", "minecraft_and_players": "not tested"}
        finally:
            for server in servers:
                server.shutdown(); server.server_close()
            for thread in threads:
                thread.join(timeout=2)
            authority.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--gson", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, help="installed release to test instead of checkout modules")
    print(json.dumps(check(parser.parse_args()), sort_keys=True))
