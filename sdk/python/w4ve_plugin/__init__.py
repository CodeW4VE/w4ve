"""Writing a W4VE plugin in Python.

The protocol (RFC-0003) is deliberately simple enough that you could speak it
with `print` and `input`. This exists so you do not have to, and so the boring
parts are boring: one file, standard library only, no install.

    from w4ve_plugin import Plugin

    plugin = Plugin("hello", "1.0.0")

    @plugin.command("!!hello", help="say hello")
    def hello(source, args):
        return "hello, %s" % (args[0] if args else "world")

    @plugin.on("player_joined")
    def joined(payload):
        plugin.say("welcome, %s" % payload["player"])

    plugin.run()

What is worth knowing, because it is what makes a plugin behave:

- **`run()` never returns** until the core says to stop, or its input closes.
- **Your handlers run one at a time**, on a thread of their own. Take a long
  time in one and your plugin is slow. That is a promise this design can make
  cheaply, because a slow plugin is now only slow for itself.
- **`print()` goes to the core as a protocol message and would corrupt it.**
  Use `plugin.log()`, which writes to stderr where the contract says logs go.
  This is the one sharp edge of using stdout as the wire, and it is why `log`
  exists rather than telling people to remember.
"""

import json
import queue
import sys
import threading
import traceback

PROTOCOL = 1

__all__ = ["Plugin", "PROTOCOL"]


class Plugin:
    """One plugin: what it answers to, and what it is told."""

    def __init__(self, plugin_id, version="0.0.0", name=None):
        self.id = plugin_id
        self.version = str(version)
        self.name = name or plugin_id
        self.config = {}
        self.data_dir = ""
        self.server_root = ""
        self._commands = {}
        self._listeners = {}
        self._pending = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._running = False
        self._on_start = []
        self._on_stop = []

    # ------------------------------------------------------------ declaring

    def command(self, name, help="", permission="user"):
        """Register a command. Whatever it returns is shown to whoever ran it.

        Return a string for one line, a list for several, or None to say
        nothing.
        """
        def decorate(function):
            self._commands[name] = {"function": function, "help": help,
                                    "permission": permission}
            return function
        return decorate

    def on(self, event):
        """Register a listener. Several for one event are all called."""
        def decorate(function):
            self._listeners.setdefault(event, []).append(function)
            return function
        return decorate

    def on_start(self, function):
        """Called once, after the core has told us our config."""
        self._on_start.append(function)
        return function

    def on_stop(self, function):
        """Called once, when the core asks us to leave. Save things here."""
        self._on_stop.append(function)
        return function

    # ------------------------------------------------------------- speaking

    def log(self, message, level="info"):
        """Say something to the operator. **Use this and never `print`.**"""
        sys.stderr.write("%s\n" % message)
        sys.stderr.flush()

    def say(self, text, player=None):
        """Put a line in front of everybody in the game, or one player."""
        self._send({"type": "say", "text": str(text), "player": player})

    def execute(self, command):
        """Run something on the server console. Waits for the core to confirm."""
        return self._call("execute", {"command": command})

    def server_status(self):
        """Is the server up, and is it ready? Those are different questions."""
        return self._call("server_status", {})

    def plugins(self):
        """What other native plugins are loaded here."""
        return self._call("plugins", {})

    # --------------------------------------------------------------- inside

    def _send(self, message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def _call(self, method, params):
        with self._lock:
            call_id = self._next_id
            self._next_id += 1
        done, box = threading.Event(), []
        self._pending[call_id] = (done, box)
        self._send({"type": "call", "id": call_id, "method": method,
                    "params": params})
        # Safe from a handler, because the reading thread settles answers
        # itself rather than queueing them behind the work it is waiting on.
        if not done.wait(15):
            self._pending.pop(call_id, None)
            raise TimeoutError("the core did not answer %s" % method)
        answer = box[0]
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error", "the core said no"))
        return answer.get("value")

    def run(self):
        """Talk to the core until it stops. Does not return before then.

        ⚠️ **Two threads, and the reason is a deadlock that is easy to write
        and hard to see.** Reading and running handlers on the same thread
        looks simpler and works until a handler asks the core something: it
        sends the question and waits for the answer, the answer arrives on the
        thread it is blocking, and the plugin hangs for ever holding a
        conversation with itself. Found by the tests the first time a command
        called `server_status()`.

        So: this thread only reads and hands over. A second one runs handlers,
        strictly in order, which keeps the "one at a time" promise while
        letting a handler wait for the core.
        """
        self._send({"type": "hello", "plugin": self.id, "version": self.version,
                    "protocol": PROTOCOL,
                    "capabilities": sorted(self._commands)})
        self._running = True
        self._work = queue.Queue()
        worker = threading.Thread(target=self._work_loop, daemon=True,
                                  name="w4ve-plugin-handlers")
        worker.start()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.log("the core sent something that is not JSON")
                continue
            # Answers are settled right here, not queued: whoever is waiting
            # for one may be the handler thread, and queueing it behind their
            # own work is the deadlock all over again.
            if message.get("type") == "result":
                self._settle(message)
                continue
            if message.get("type") == "shutdown":
                self._running = False
                self._work.put(None)
                break
            self._work.put(message)
        self._running = False
        self._work.put(None)
        worker.join(timeout=10)
        self._finish()

    def _work_loop(self):
        while True:
            message = self._work.get()
            if message is None:
                return
            try:
                self._handle(message)
            except Exception:               # noqa: BLE001
                # A handler that throws must not end the plugin. It gets said
                # and the next message is read: crashing on one bad event is
                # how a plugin becomes unavailable for everything else too.
                self.log(traceback.format_exc())

    def _settle(self, message):
        waiting = self._pending.pop(message.get("id"), None)
        if waiting:
            event, box = waiting
            box.append(message)
            event.set()

    def _handle(self, message):
        kind = message.get("type")
        if kind == "welcome":
            self.config = message.get("config") or {}
            self.data_dir = message.get("data_dir", "")
            self.server_root = message.get("server", "")
            for name, entry in self._commands.items():
                self._send({"type": "command", "name": name,
                            "help": entry["help"],
                            "permission": entry["permission"]})
            for function in self._on_start:
                function()
            # ⚠️ Said last, and it is not a nicety. Without it the core knows
            # the plugin is alive but not whether it has finished registering,
            # so `start()` returns and the first command lands before the
            # plugin has said it owns one. Found by the tests the first time
            # they asked a freshly started plugin for its commands.
            self._send({"type": "ready"})
        elif kind == "event":
            for function in self._listeners.get(message.get("event"), []):
                function(message.get("payload") or {})
            for function in self._listeners.get("*", []):
                function({"event": message.get("event"),
                          "payload": message.get("payload") or {}})
        elif kind == "invoke":
            self._run_command(message)
        elif kind == "result":
            self._settle(message)

    def _run_command(self, message):
        name = message.get("command")
        entry = self._commands.get(name)
        answer = {"type": "result", "id": message.get("id"), "ok": True}
        if entry is None:
            answer["ok"] = False
            answer["error"] = "no such command"
        else:
            try:
                value = entry["function"](message.get("source", "console"),
                                          message.get("args") or [])
                answer["value"] = value
            except Exception as exc:        # noqa: BLE001
                answer["ok"] = False
                answer["error"] = str(exc)
                self.log(traceback.format_exc())
        self._send(answer)

    def _finish(self):
        for function in self._on_stop:
            try:
                function()
            except Exception:               # noqa: BLE001
                self.log(traceback.format_exc())
