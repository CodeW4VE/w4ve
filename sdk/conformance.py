#!/usr/bin/env python3
"""Does this thing really speak the protocol? Ask it, do not assume.

An SDK is called official when it passes this, and not before. It runs against
**any** plugin in any language: it starts the process, speaks RFC-0003 at it
and checks what comes back. Nothing here knows about Python.

    sdk/conformance.py sdk/python/example
    sdk/conformance.py /path/to/a/plugin --verbose

What it checks is the behaviour that a plugin gets wrong when nobody is
checking: saying hello before anything else, answering a command it registered,
saying it does not know one it did not, not dying on nonsense, and leaving when
asked. Those are the five that turn into support threads.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import worker as worker_module  # noqa: E402

PROTOCOL = worker_module.PROTOCOL

failures = []
checks = 0


def check(what, ok, detail=""):
    global checks
    checks += 1
    print(("  ok    " if ok else "  FAIL  ") + what
          + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(what)


class Conversation:
    """A plugin process, and the lines going each way."""

    def __init__(self, folder, verbose=False):
        self.manifest = worker_module.read_manifest(folder)
        self.folder = Path(folder)
        self.verbose = verbose
        self.data = Path(os.environ.get("TMPDIR", "/tmp")) / "w4ve-conformance"
        self.data.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["W4VE_PLUGIN"] = self.manifest["id"]
        environment["W4VE_DATA_DIR"] = str(self.data)
        # The same PYTHONPATH the core gives a worker. Without it the kit
        # fails every Python plugin that does not carry its own copy of the
        # SDK, which is all of them once they are installed from a package.
        sdk = worker_module.sdk_path()
        if sdk:
            existing = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (str(sdk) + os.pathsep + existing
                                         if existing else str(sdk))
        self.proc = subprocess.Popen(
            self.manifest["command"], cwd=str(self.folder), env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        # ⚠️ Read on a thread, not with `readline()` and a timeout, because
        # `readline()` has no timeout: it waits for a line that may never come,
        # and the deadline in the caller never gets a chance to run. Which is
        # the same reason the core reads its workers on threads.
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    # ⚠️ The kit has to answer what a plugin asks it, or it fails every plugin
    # that uses the API and passes only the ones that do not. Found by running
    # it against the plugin `w4ve dev new` generates, which calls
    # `server_status()` in its first command: the kit stayed silent, the plugin
    # waited out its own timeout, and everything after that was measured
    # against a plugin that was busy waiting.
    #
    # The answers are obviously fake and say so. This is a test harness, not a
    # server: what is being checked is that the plugin asks properly and
    # carries on with what it is told.
    CORE_ANSWERS = {
        "server_status": {"running": True, "ready": True, "fake": True},
        "execute": True,
        "plugins": [],
    }

    def _answer_call(self, message):
        method = message.get("method", "")
        reply = {"type": "result", "id": message.get("id")}
        if method in self.CORE_ANSWERS:
            reply["ok"] = True
            reply["value"] = self.CORE_ANSWERS[method]
        else:
            reply["ok"] = False
            reply["error"] = "conformance: no such method %r" % method
        self.send(reply)

    def send(self, message):
        if self.verbose:
            print("    -> %s" % json.dumps(message))
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout=10):
        """One message, or None if it never comes."""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            if self.verbose:
                print("    <- %s" % line)
            try:
                message = json.loads(line)
            except ValueError:
                # Not JSON on stdout is a failure of the contract, and the
                # caller is the one who should say so.
                return {"type": "__not_json__", "raw": line}
            if message.get("type") == "call":
                # Answered here rather than handed up: a plugin waiting for
                # this is a plugin that cannot answer the thing being tested.
                self._answer_call(message)
                continue
            return message

    def read_until(self, kind, timeout=10, collect=None):
        """Read past anything else, looking for one kind of message."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.read(timeout=max(0.5, deadline - time.time()))
            if message is None:
                return None
            if collect is not None and message.get("type") == collect[0]:
                collect[1].append(message)
            if message.get("type") == kind:
                return message
        return None

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run(folder, verbose=False):
    print("conformance: %s" % folder)
    print()

    print("-- the manifest --")
    try:
        manifest = worker_module.read_manifest(folder)
        check("w4ve.plugin.toml reads", True, None)
        check("it has an id, a version and a command",
              manifest["id"] and manifest["version"] and manifest["command"],
              manifest)
        check("it asks for a protocol this core speaks",
              manifest["protocol"] <= PROTOCOL, manifest["protocol"])
    except worker_module.WorkerError as exc:
        check("w4ve.plugin.toml reads", False, exc)
        return 1

    print("-- saying hello --")
    talk = Conversation(folder, verbose)
    try:
        first = talk.read(timeout=worker_module.HANDSHAKE_TIMEOUT)
        check("it says something before being spoken to", first is not None,
              "a worker that waits to be greeted deadlocks the handshake")
        if first is None:
            return 1
        check("and the first thing is hello", first.get("type") == "hello",
              first)
        check("hello names the plugin", bool(first.get("plugin")), first)
        check("hello carries a version", bool(first.get("version")), first)
        check("hello states the protocol",
              int(first.get("protocol", 0)) == PROTOCOL, first)

        print("-- what it registers --")
        commands = []
        talk.send({"type": "welcome", "protocol": PROTOCOL,
                   "server": str(folder), "config": manifest["config"],
                   "data_dir": str(talk.data)})
        # Commands arrive after the welcome; give it a moment and collect.
        deadline = time.time() + 3
        while time.time() < deadline:
            message = talk.read(timeout=0.6)
            if message is None:
                break
            if message.get("type") == "command":
                commands.append(message)
        check("it registers at least one command", bool(commands),
              "a plugin nobody can talk to is hard to tell from a broken one")
        if commands:
            check("every command has a name",
                  all(c.get("name") for c in commands), commands)
            check("every command has help text",
                  all(c.get("help") for c in commands),
                  "help is what `!!help` shows; without it the command is a rumour")

        print("-- answering --")
        if commands:
            name = commands[0]["name"]
            talk.send({"type": "invoke", "id": 900, "command": name,
                       "args": [], "source": "console"})
            answer = talk.read_until("result", timeout=10)
            check("a command it registered gets an answer",
                  answer is not None and answer.get("id") == 900, answer)
            check("and the answer says whether it went well",
                  answer is not None and "ok" in answer, answer)

        talk.send({"type": "invoke", "id": 901, "command": "!!definitely-not-real",
                   "args": [], "source": "console"})
        answer = talk.read_until("result", timeout=10)
        check("a command it never registered is refused, not ignored",
              answer is not None and answer.get("id") == 901
              and answer.get("ok") is False, answer)

        print("-- nonsense --")
        # The point: a plugin that dies on a message it does not understand
        # is a plugin that dies the first time the core learns a new one.
        talk.send({"type": "something-from-the-future", "id": 902})
        talk.send({"type": "event", "event": "unknown_event_type",
                   "payload": {"weird": True}})
        time.sleep(0.5)
        check("it survives a message it does not understand",
              talk.proc.poll() is None,
              "exit %s" % talk.proc.poll())
        if commands:
            talk.send({"type": "invoke", "id": 903,
                       "command": commands[0]["name"], "args": [],
                       "source": "console"})
            answer = talk.read_until("result", timeout=10)
            check("and still answers afterwards",
                  answer is not None and answer.get("id") == 903, answer)

        print("-- leaving --")
        talk.send({"type": "shutdown", "reason": "conformance"})
        gone = False
        for _ in range(50):
            if talk.proc.poll() is not None:
                gone = True
                break
            time.sleep(0.1)
        check("it leaves when asked, without being signalled", gone,
              "still running 5s after shutdown")
    finally:
        talk.close()

    print()
    if failures:
        print("%d of %d checks failed" % (len(failures), checks))
        for what in failures:
            print("  - %s" % what)
        return 1
    print("all %d checks passed" % checks)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="a plugin folder with w4ve.plugin.toml")
    parser.add_argument("--verbose", action="store_true",
                        help="print every line of the conversation")
    args = parser.parse_args()
    return run(args.folder, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
