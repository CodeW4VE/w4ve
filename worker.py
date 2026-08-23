"""Plugins that are processes, and the protocol they speak.

This is RFC-0003. The one sentence that explains why it exists: **a plugin that
hangs, crashes or floods must not be able to take the guardian with it**, and
that is not a promise anybody can make about code running inside their own
process. So a native plugin is a process, and what crosses between it and the
core is a line of JSON.

Everything here is about that boundary being real:

- every worker has its own reader thread, so one that stops reading stops only
  itself;
- every request has a deadline, so a worker that never answers produces an
  error rather than a hang;
- every queue has a ceiling, so a worker that cannot keep up loses events
  rather than growing memory that belongs to the process watching Minecraft;
- a worker that dies takes down its own process and nothing else.

The MCDR compatibility host (`mcdr/`) keeps working exactly as it did. It runs
plugins in-process because that is what MCDR plugins expect, and it is honest
about the weaker guarantees that come with it. The two live side by side.

Standard library only, Python 3.9, same rules as the rest.
"""

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

PROTOCOL = 1

# How long a worker has to say `hello` before we decide it is not really alive.
# Long enough for an interpreter to start and import; short enough that a boot
# is not held hostage by something broken.
HANDSHAKE_TIMEOUT = 20

# How long the core waits for an answer to a command it sent. Anything slower
# than this is not a slow answer, it is a worker that is not going to answer.
CALL_TIMEOUT = 15

# Events waiting for one worker. Past this it drops them and says so once: a
# worker that cannot keep up is its own problem, which is the whole point of it
# not being inside the guardian.
QUEUE_LIMIT = 1000

# Three deaths this close together and it is left alone. Same reasoning as the
# server in RFC-0001: retrying against something broken is how an incident
# becomes a loop.
CRASH_WINDOW = 300
CRASH_LIMIT = 3

# How long a worker gets to leave on its own before it is signalled.
STOP_GRACE = 5


class WorkerError(Exception):
    """Something the operator has to know about a worker."""


def sdk_path():
    """Where the Python SDK lives, or None if this w4ve does not carry it."""
    folder = Path(__file__).resolve().parent / "sdk" / "python"
    return folder if (folder / "w4ve_plugin" / "__init__.py").exists() else None


def read_manifest(folder):
    """Read `w4ve.plugin.toml`. Returns a dict, or raises with the reason."""
    folder = Path(folder)
    path = folder / "w4ve.plugin.toml"
    if not path.is_file():
        raise WorkerError("%s has no w4ve.plugin.toml" % folder.name)
    try:
        import tomllib
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except ModuleNotFoundError:
        import w4ve
        data = w4ve._toml_lite(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkerError("%s: %s" % (path, exc))

    plugin = data.get("plugin") or {}
    run = data.get("run") or {}
    problems = []
    if not plugin.get("id"):
        problems.append("no id")
    if not plugin.get("version"):
        problems.append("no version")
    command = run.get("command")
    if isinstance(command, str):
        command = command.split()
    if not command:
        problems.append("no run.command, so there is nothing to start")
    wanted = int(plugin.get("protocol") or PROTOCOL)
    if wanted > PROTOCOL:
        problems.append("wants protocol %d and this w4ve speaks %d"
                        % (wanted, PROTOCOL))
    if problems:
        raise WorkerError("%s: %s" % (folder.name, "; ".join(problems)))
    python = data.get("python") or {}
    requires = python.get("requires") or []
    if isinstance(requires, str):
        requires = [requires]
    return {
        "id": plugin["id"],
        "name": plugin.get("name") or plugin["id"],
        "version": str(plugin["version"]),
        "protocol": wanted,
        "command": list(command),
        "config": dict(data.get("config") or {}),
        # Declared and checked, never installed. Same rule as a service, and
        # for the same reason: fetching code from the internet and running it
        # is the operator's decision, not a side effect of starting a server.
        "requires": [str(name) for name in requires],
        "folder": folder,
    }


def missing_requirements(manifest):
    """What a plugin says it needs and its interpreter does not have.

    Asked of **the interpreter in its own `command`**, not this one. A plugin
    with a virtualenv is the normal case, and asking the wrong Python reports
    as missing whatever is sitting right there.
    """
    wanted = manifest.get("requires") or []
    if not wanted:
        return []
    interpreter = manifest["command"][0]
    if "python" not in Path(interpreter).name:
        # Not a Python plugin, or one started through a wrapper. Saying
        # nothing beats guessing about somebody else's runtime.
        return []
    probe = ("import importlib.metadata as meta, sys\n"
             "missing = []\n"
             "for name in sys.argv[1:]:\n"
             "    for spelling in (name, name.replace('_', '-')):\n"
             "        try:\n"
             "            meta.distribution(spelling)\n"
             "            break\n"
             "        except Exception:\n"
             "            pass\n"
             "    else:\n"
             "        missing.append(name)\n"
             "print('\\n'.join(missing))\n")
    try:
        result = subprocess.run([interpreter, "-c", probe] + list(wanted),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def discover(folder):
    """Every plugin folder under here, with what is wrong with each."""
    found, problems = [], []
    folder = Path(folder)
    if not folder.is_dir():
        return found, problems
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            found.append(read_manifest(child))
        except WorkerError as exc:
            problems.append(str(exc))
    return found, problems


class Worker:
    """One plugin, one process, and everything that keeps it at arm's length."""

    def __init__(self, manifest, root, host=None):
        self.manifest = manifest
        self.id = manifest["id"]
        self.root = Path(root)
        self.host = host
        self.proc = None
        self.state = "stopped"
        self.ready = False
        self.error = ""
        self.commands = {}          # name -> {help, permission}
        self.started_at = 0.0
        self.deaths = []
        self.dropped = 0
        self.said_dropped = False
        self.sent = 0
        self.received = 0

        self._queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._pending = {}          # id -> (Event, box)
        self._next_id = 1
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._hello = threading.Event()
        self._ready = threading.Event()

    # -------------------------------------------------------------- lifetime

    @property
    def data_dir(self):
        """Where this plugin may write. Its own, made before it starts."""
        path = self.root / "w4ve" / "plugins" / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return True, "%s is already running" % self.id
        self._stopping.clear()
        self._hello.clear()
        self._ready.clear()
        self.ready = False
        self.error = ""
        self.commands = {}
        self.state = "starting"
        environment = dict(os.environ)
        environment["W4VE_PLUGIN"] = self.id
        environment["W4VE_DATA_DIR"] = str(self.data_dir)
        # The SDK is W4VE's, so W4VE puts it on the path rather than making
        # every plugin work out where it lives. A plugin that ships its own
        # copy still wins: theirs is found first because sys.path is searched
        # in order and their folder is the working directory.
        sdk = sdk_path()
        if sdk:
            existing = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (str(sdk) + os.pathsep + existing
                                         if existing else str(sdk))
        try:
            self.proc = subprocess.Popen(
                self.manifest["command"],
                cwd=str(self.manifest["folder"]),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1)
        except OSError as exc:
            self.state = "failed"
            self.error = str(exc)
            return False, "%s could not start: %s" % (self.id, exc)
        self.started_at = time.time()
        threading.Thread(target=self._read, daemon=True,
                         name="w4ve-worker-%s" % self.id).start()
        threading.Thread(target=self._read_errors, daemon=True,
                         name="w4ve-worker-%s-err" % self.id).start()
        threading.Thread(target=self._pump, daemon=True,
                         name="w4ve-worker-%s-events" % self.id).start()

        # Alive is not ready. A process that starts and never speaks is exactly
        # the failure this whole design exists to contain, so it is named.
        if not self._hello.wait(HANDSHAKE_TIMEOUT):
            self.state = "failed"
            self.error = ("said nothing in %ds, so it is running and not alive"
                          % HANDSHAKE_TIMEOUT)
            self.stop()
            return False, "%s %s" % (self.id, self.error)
        # And hello is not ready either: between the two the plugin registers
        # its commands and runs its own setup. Returning at `hello` means the
        # first command arrives before the plugin has said it owns one.
        if not self._ready.wait(HANDSHAKE_TIMEOUT):
            self.state = "failed"
            self.error = ("said hello and then never finished starting "
                          "(no `ready` in %ds)" % HANDSHAKE_TIMEOUT)
            self.stop()
            return False, "%s %s" % (self.id, self.error)
        self.state = "running"
        self.ready = True
        return True, "%s %s started" % (self.id, self.manifest["version"])

    def stop(self, grace=STOP_GRACE):
        self._stopping.set()
        self.ready = False
        if self.proc is None:
            self.state = "stopped"
            return True, "%s was not running" % self.id
        self._write({"type": "shutdown", "reason": "stopping"})
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # It was asked. Now it is told.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.state = "stopped"
        # Everybody still waiting gets an answer, because a caller left hanging
        # by a worker's death is the same hang this design forbids.
        self._fail_pending("%s stopped" % self.id)
        return True, "%s stopped" % self.id

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------- the wire

    def _write(self, message):
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            with self._lock:
                self.proc.stdin.write(json.dumps(message) + "\n")
                self.proc.stdin.flush()
            self.sent += 1
            return True
        except (OSError, ValueError):
            # It went away between the check and the write. Normal.
            return False

    def _read(self):
        stream = self.proc.stdout
        while not self._stopping.is_set():
            try:
                line = stream.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            self.received += 1
            try:
                message = json.loads(line)
            except ValueError:
                self._log("warn", "sent something that is not JSON: %s"
                          % line.strip()[:120])
                continue
            try:
                self._handle(message)
            except Exception as exc:      # noqa: BLE001
                # A bad message from a worker must never kill its reader: the
                # reader dying is how a worker becomes silently deaf.
                self._log("warn", "message could not be handled: %s" % exc)
        self._died()

    def _read_errors(self):
        """stderr is the worker's log, by the contract. Nothing is parsed."""
        stream = self.proc.stderr
        while not self._stopping.is_set():
            try:
                line = stream.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            self._log("info", line.rstrip())

    def _handle(self, message):
        kind = message.get("type")
        if kind == "hello":
            self._welcome(message)
        elif kind == "command":
            name = str(message.get("name") or "").strip()
            if name:
                self.commands[name] = {
                    "help": message.get("help", ""),
                    "permission": message.get("permission", "user"),
                }
        elif kind == "ready":
            self._ready.set()
        elif kind == "log":
            self._log(message.get("level", "info"), message.get("message", ""))
        elif kind == "say":
            if self.host:
                self.host.say(self.id, message.get("text", ""),
                              message.get("player"))
        elif kind == "call":
            self._answer_call(message)
        elif kind == "result":
            self._settle(message)

    def _welcome(self, message):
        wanted = int(message.get("protocol") or PROTOCOL)
        if wanted != PROTOCOL:
            self.error = ("speaks protocol %d and this w4ve speaks %d"
                          % (wanted, PROTOCOL))
            self._log("error", self.error)
            self._stopping.set()
            return
        self._write({
            "type": "welcome",
            "protocol": PROTOCOL,
            "server": str(self.root),
            "config": self.manifest["config"],
            "data_dir": str(self.data_dir),
        })
        self._hello.set()

    def _answer_call(self, message):
        """Something the worker asked the core for."""
        answer = {"type": "result", "id": message.get("id"), "ok": True}
        try:
            if self.host is None:
                raise WorkerError("nothing to ask")
            answer["value"] = self.host.call(self.id, message.get("method", ""),
                                             message.get("params") or {})
        except Exception as exc:          # noqa: BLE001
            answer["ok"] = False
            answer["error"] = str(exc)
        self._write(answer)

    def _settle(self, message):
        """An answer to something the core asked."""
        waiting = self._pending.pop(message.get("id"), None)
        if waiting is None:
            return
        event, box = waiting
        box.append(message)
        event.set()

    def _fail_pending(self, why):
        for waiting in list(self._pending.values()):
            event, box = waiting
            box.append({"ok": False, "error": why})
            event.set()
        self._pending.clear()

    # ------------------------------------------------------------- speaking

    def ask(self, message, timeout=CALL_TIMEOUT):
        """Send something and wait for its answer, or for the deadline.

        The deadline is the whole point. A worker that never answers gets the
        caller an error with that word in it, and the caller carries on.
        """
        if not self.alive():
            return {"ok": False, "error": "%s is not running" % self.id}
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
        message["id"] = message_id
        done, box = threading.Event(), []
        self._pending[message_id] = (done, box)
        if not self._write(message):
            self._pending.pop(message_id, None)
            return {"ok": False, "error": "%s could not be reached" % self.id}
        if not done.wait(timeout):
            self._pending.pop(message_id, None)
            return {"ok": False,
                    "error": "%s did not answer in %ss" % (self.id, timeout)}
        return box[0]

    def invoke(self, command, args, source="console", timeout=CALL_TIMEOUT):
        """Somebody typed one of this worker's commands."""
        return self.ask({"type": "invoke", "command": command,
                         "args": args, "source": source}, timeout)

    def deliver(self, event, payload):
        """Hand an event over, without ever waiting for it to be taken.

        ⚠️ This is called from the guardian's console reader. Blocking here
        would mean a slow plugin delaying the guardian noticing that Minecraft
        died, which is the exact thing that must not happen.
        """
        if not self.alive():
            return False
        try:
            self._queue.put_nowait({"type": "event", "event": event,
                                    "payload": payload})
            return True
        except queue.Full:
            self.dropped += 1
            if not self.said_dropped:
                self.said_dropped = True
                self._log("warn", "cannot keep up: events are being dropped")
            return False

    def _pump(self):
        """One thread per worker, moving events from its queue onto the wire."""
        while not self._stopping.is_set():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._write(message)

    # ------------------------------------------------------------- the rest

    def _log(self, level, text):
        if self.host and text:
            self.host.log(self.id, level, text)

    def _died(self):
        if self._stopping.is_set():
            return
        # ⚠️ `wait` and not `poll`: the reader thread notices the pipe close
        # before the kernel has reaped the process, so `poll()` here answers
        # None and the journal says "exit None", which tells nobody anything.
        code = None
        if self.proc is not None:
            try:
                code = self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                code = self.proc.poll()
        self.state = "crashed"
        self.ready = False
        self._fail_pending("%s died" % self.id)
        self.deaths = [t for t in self.deaths if time.time() - t < CRASH_WINDOW]
        self.deaths.append(time.time())
        self._log("error", "died (exit %s)" % code)
        if self.host:
            self.host.worker_died(self)

    def status(self):
        return {
            "id": self.id,
            "version": self.manifest["version"],
            "state": self.state,
            "ready": self.ready,
            "pid": self.proc.pid if self.alive() else None,
            "commands": sorted(self.commands),
            "dropped": self.dropped,
            "sent": self.sent,
            "received": self.received,
            "error": self.error,
            "uptime": int(time.time() - self.started_at) if self.alive() else 0,
        }


def named_events(line):
    """The events a console line means, beyond being a console line.

    Parsing is borrowed from the MCDR compatibility layer rather than written
    again, and not to save typing: that code already gets right the thing that
    is easy to get wrong. **A player typing "Hugo joined the game" in chat must
    not become a join event for Hugo.** It is told apart because a chat line
    arrives with a player attached and a server line does not.
    """
    try:
        from mcdr.info import parse_server_line, player_event
    except ImportError:
        return []
    try:
        info = parse_server_line(line)
    except Exception:                 # noqa: BLE001
        return []
    out = []
    if info.player is not None and info.content:
        out.append(("player_said", {"player": info.player,
                                    "message": info.content}))
    found = player_event(info)
    if found:
        what, who = found
        out.append(("player_%s" % what, {"player": who}))
    return out


class WorkerHost:
    """Every native plugin of one server, and the core they talk to."""

    def __init__(self, root, guardian=None):
        self.root = Path(root)
        self.guardian = guardian
        self.workers = {}
        self.problems = []

    # ------------------------------------------------------------- loading

    def load(self, folder=None):
        """Find the plugins and start them. Returns (started, problems)."""
        folder = Path(folder or (self.root / "w4ve" / "plugins"))
        found, problems = discover(folder)
        self.problems = list(problems)
        started = []
        for manifest in found:
            # Said before starting it, because a plugin that dies on its first
            # import gives an error about a module and not about a plugin, and
            # the two send somebody looking in different places.
            absent = missing_requirements(manifest)
            if absent:
                self.problems.append(
                    "%s needs %s, which %s does not have: pip install %s"
                    % (manifest["id"], ", ".join(absent),
                       manifest["command"][0], " ".join(absent)))
            worker = Worker(manifest, self.root, host=self)
            self.workers[worker.id] = worker
            ok, message = worker.start()
            (started if ok else self.problems).append(
                worker.id if ok else message)
        return started, self.problems

    def stop_all(self):
        for worker in list(self.workers.values()):
            worker.stop()

    def reload(self, plugin_id):
        """Stop it, read its manifest again, start it.

        Reading the manifest again is the point: a reload that keeps the old
        one cannot pick up a changed command or a changed config, which is
        most of what somebody reloads a plugin for.
        """
        worker = self.workers.get(plugin_id)
        if worker is None:
            return False, "no plugin called %r" % plugin_id
        folder = worker.manifest["folder"]
        worker.stop()
        try:
            manifest = read_manifest(folder)
        except WorkerError as exc:
            return False, str(exc)
        fresh = Worker(manifest, self.root, host=self)
        self.workers[plugin_id] = fresh
        return fresh.start()

    # --------------------------------------------------------- what they get

    def dispatch(self, event, payload):
        """Hand an event to everybody. Never waits for any of them."""
        for worker in list(self.workers.values()):
            worker.deliver(event, payload)

    def console_line(self, line):
        """A line from the server's console, as one or two events.

        Called from the guardian's reader thread, which is why `deliver` never
        blocks: this thread is also how the guardian learns that Minecraft
        died.

        Every line goes out as `console`, and the ones that mean something get
        a named event too. Without that a plugin has to write the regex for
        "somebody joined" itself, which means every plugin writes it, some of
        them write it wrong, and none of them get the subtlety below right.
        """
        self.dispatch("console", {"line": line})
        for event, payload in named_events(line):
            self.dispatch(event, payload)

    def command(self, text, source="console"):
        """Somebody typed something. Returns (claimed, lines)."""
        parts = text.strip().split()
        if not parts:
            return False, []
        name = parts[0]
        for worker in list(self.workers.values()):
            if name not in worker.commands:
                continue
            answer = worker.invoke(name, parts[1:], source)
            if answer.get("ok"):
                value = answer.get("value")
                if value is None:
                    return True, []
                return True, value if isinstance(value, list) else [str(value)]
            return True, ["%s: %s" % (name, answer.get("error", "no answer"))]
        return False, []

    def help_lines(self):
        out = []
        for worker in sorted(self.workers.values(), key=lambda w: w.id):
            for name, entry in sorted(worker.commands.items()):
                out.append("%s  %s" % (name, entry.get("help", "")))
        return out

    # ------------------------------------------------- what they can ask for

    def call(self, plugin_id, method, params):
        """What a worker may ask the core to do.

        Deliberately short. Every entry here is something the core can do
        safely on somebody else's behalf, and a method that is not listed is
        an error rather than an unknown behaviour.
        """
        if method == "server_status":
            if self.guardian is None:
                return {"running": False}
            proc = self.guardian.watched.get("server")
            return {"running": bool(proc and proc.state == "running"),
                    "ready": bool(proc and proc.ready)}
        if method == "execute":
            # Straight into the server console. Whoever installs a plugin is
            # trusting it with the server, and pretending otherwise with a
            # half-list of allowed commands would be theatre.
            text = str(params.get("command", "")).strip()
            if not text:
                raise WorkerError("execute needs a command")
            return self._to_server(text)
        if method == "plugins":
            return [w.status() for w in self.workers.values()]
        raise WorkerError("no such method: %s" % method)

    def say(self, plugin_id, text, player=None):
        if not text:
            return False
        if player:
            payload = json.dumps({"text": str(text)})
            return self._to_server("tellraw %s %s" % (player, payload))
        return self._to_server("say %s" % text)

    def _to_server(self, command):
        if self.guardian is None:
            return False
        proc = self.guardian.watched.get("server")
        if proc is None:
            return False
        ok, _message = proc.send(command)
        return ok

    def log(self, plugin_id, level, text):
        line = "[%s] %s" % (plugin_id, text)
        if self.guardian is not None:
            self.guardian.log(line)
        else:
            print(line)

    def worker_died(self, worker):
        """A worker's process ended without being asked to.

        Restarted, unless it has been dying often, which is the same rule the
        server follows and for the same reason.
        """
        if len(worker.deaths) >= CRASH_LIMIT:
            self.log(worker.id, "error",
                     "died %d times in %d minutes, leaving it alone"
                     % (len(worker.deaths), CRASH_WINDOW // 60))
            return
        self.log(worker.id, "info", "restarting it")
        ok, message = worker.start()
        if not ok:
            self.log(worker.id, "error", message)

    def status(self):
        return {"workers": [w.status() for w in self.workers.values()],
                "problems": list(self.problems)}
