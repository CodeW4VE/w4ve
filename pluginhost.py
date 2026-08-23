"""The plugin host, attached to a guardian that owns a real server.

`mcdr/host.py` can load plugins against nothing, which is what `w4ve plugins
--load` uses to rehearse a cutover. This is the other half: the same host,
wired to the process the guardian is watching, so a plugin's `server.say()`
reaches players and its `on_info` hears them.

Three decisions worth stating, because they are what separates this from
calling `feed_line` in the console loop:

  * **Plugins run on their own thread, never on the console reader.** The
    console reader is how the guardian learns the server is ready and how it
    notices a crash. A plugin that blocks for four seconds must not be able to
    delay that, and one that blocks forever must not be able to blind it.
  * **The queue has a bottom.** A server prints thousands of lines while it
    generates a world. If plugins fall behind, lines are dropped and the drop
    is reported once, rather than growing a queue until the machine swaps.
  * **This layer is optional.** `runtime.py` imports it inside a function and
    survives it being missing, the same way `w4ve.py` survives `runtime.py`
    being missing. A server whose plugins cannot load must still start.
"""

import os
import queue
import threading
from pathlib import Path

# How many console lines may wait for the plugins. Two seconds of a very
# chatty server: enough to absorb a world load, small enough to notice.
BACKLOG = 2000


class PluginRuntime:
    """Owns the Host, its thread, and the queue between them."""

    def __init__(self, guardian, server_name="server", plugins_dir=None,
                 config_dir=None):
        self.guardian = guardian
        self.server_name = server_name
        self.plugins_dir = plugins_dir
        self.config_dir = config_dir
        self.host = None
        self.queue = queue.Queue(maxsize=BACKLOG)
        self.thread = None
        self.stopping = threading.Event()
        self.dropped = 0
        self._warned_about_drops = False
        self._last_state = None
        # Set while a typed command runs, so its answer can be handed back to
        # whoever typed it instead of only appearing in the console they are
        # not looking at. Only ever touched from the plugin thread.
        self._capture = None

    # ------------------------------------------------------------------ setup

    def start(self):
        """Load the plugins and start their thread. Returns (ok, message)."""
        try:
            from mcdr.host import Host
        except ImportError as exc:
            return False, "no MCDR compatibility in this w4ve (%s)" % exc

        proc = self.guardian.watched.get(self.server_name)
        root = self.guardian.root
        plugins_dir = self.plugins_dir or (root / "plugins")
        if not plugins_dir.is_dir():
            return False, "no plugins folder at %s" % plugins_dir

        # ⚠️ Plugins use paths relative to the working directory, and under
        # MCDR that directory is the server root: QuickBackupM keeps backups
        # in `./qb_multi` and looks for the world in `./server`. Run the
        # guardian from anywhere else and its backups land wherever you
        # happened to be standing, empty, without an error. Found the hard
        # way: a `!!qb make` on a real 1.21 world wrote its slot into the
        # W4VE source tree.
        moved = self._use_server_root(root)

        self.host = Host(root, plugins_dir=plugins_dir,
                         config_dir=self.config_dir,
                         server=proc, on_output=self._say)
        if moved:
            self.guardian.log("plugins: working directory is now %s, because "
                              "that is what plugin paths are relative to" % root)
        self.thread = threading.Thread(target=self._run, name="plugins",
                                       daemon=True)
        self.thread.start()
        self.submit(self.host.load_all)
        # After the plugins are loaded, the way MCDR raises it: a plugin can
        # tell "the guardian came up" apart from "the server came up", which
        # are different things when the server was already running and got
        # adopted.
        self.submit(self.host.dispatch, "on_mcdr_start")
        # Said from this thread rather than the plugin thread so the count is
        # in the journal before the first plugin line, which is what makes the
        # log readable when a plugin talks during on_load.
        return True, "plugins: loading from %s" % plugins_dir

    @staticmethod
    def _use_server_root(root):
        """Stand in the server root, the way MCDR does. True if we moved."""
        try:
            here = Path.cwd()
        except OSError:
            here = None
        if here == Path(root):
            return False
        try:
            os.chdir(str(root))
        except OSError:
            return False
        return True

    def stop(self):
        """Unload the plugins, giving them their on_unload."""
        if self.host is None:
            return
        self.stopping.set()
        try:
            self.queue.put_nowait(("call", self.host.dispatch,
                                   ("on_mcdr_stop",)))
            self.queue.put_nowait(("call", self.host.unload_all, ()))
        except queue.Full:
            pass
        self.queue.put(("quit", None, ()))
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                # A plugin that will not come back is not worth hanging the
                # guardian's shutdown over: the process is going away anyway.
                self.guardian.log("plugins: a plugin did not finish unloading")

    # ------------------------------------------------------------- the thread

    def _run(self):
        self.host._executor = threading.current_thread()
        while True:
            kind, target, args = self.queue.get()
            if kind == "quit":
                return
            try:
                target(*args)
            except Exception as exc:
                self.guardian.log("plugins: %s: %s" % (type(exc).__name__, exc))

    def submit(self, target, *args):
        """Queue work for the plugin thread, dropping it if we are behind."""
        if self.host is None:
            return False
        try:
            self.queue.put_nowait(("call", target, args))
            return True
        except queue.Full:
            self.dropped += 1
            if not self._warned_about_drops:
                self._warned_about_drops = True
                self.guardian.log(
                    "plugins: falling behind, console lines are being dropped "
                    "(a plugin is slow or stuck); the server is unaffected")
            return False

    # -------------------------------------------------------------- the feeds

    def feed(self, name, line):
        """One console line from a watched process."""
        if self.host is None or name != self.server_name:
            return
        self.submit(self.host.feed_line, line)

    def console(self, text):
        """A line an operator typed, offered to the plugins first.

        Returns `(claimed, lines)`. Claimed is how `!!w4ve` typed at the
        guardian works without being sent to Minecraft, where it would only
        produce an unknown command error. The lines are what it answered, so
        `w4ve plugins reload here` can print the answer to the terminal that
        asked instead of leaving it in a console nobody is watching.
        """
        if self.host is None:
            return False, []
        text = (text or "").strip()
        if not text.startswith("!!"):
            # Only the `!!` prefix is intercepted. Everything else is a
            # Minecraft command and belongs to the server, not to us.
            return False, []
        from mcdr.host import ConsoleCommandSource

        done = threading.Event()
        result = {}

        def run():
            source = ConsoleCommandSource(self.host)
            self._capture = []
            try:
                result["ran"] = self.host.run_command(source, text)
                result["lines"] = self._capture
            finally:
                self._capture = None
            done.set()

        if not self.submit(run):
            return False, []
        # Waited on deliberately: the operator typed it and is looking at the
        # console. A plugin stuck for a second is a second of a prompt not
        # coming back, not a line silently going nowhere.
        if not done.wait(timeout=5):
            return False, ["the plugins did not answer in five seconds"]
        return bool(result.get("ran")), list(result.get("lines") or [])

    def state_changed(self, name, state, ready):
        """The guardian's process changed state: turn it into MCDR events."""
        if self.host is None or name != self.server_name:
            return
        previous, self._last_state = self._last_state, (state, ready)
        if previous == (state, ready):
            return
        if state == "starting" and (previous or (None, None))[0] != "starting":
            self.submit(self.host.dispatch, "on_server_start")
        if ready and not (previous or (None, None))[1]:
            self.submit(self.host.dispatch, "on_server_startup")
        if state in ("stopped", "crashed") and \
                (previous or (None, None))[0] not in ("stopped", "crashed", None):
            self.submit(self.host.dispatch, "on_server_stop")

    # ------------------------------------------------------------------ output

    def _say(self, line):
        """Where a plugin's console output goes.

        Both places on purpose: the live console, for whoever is watching, and
        the journal, for whoever reads it tomorrow. Under MCDR a plugin's log
        line lands in a file; if it only went to the socket here, a plugin
        could report the exact thing that went wrong and nobody would ever
        find it, because nobody was attached at the time.
        """
        text = str(line)
        if self._capture is not None:
            self._capture.append(text)
        self.guardian.broadcast(self.server_name, text)
        self.guardian.log(text)

    def status(self):
        if self.host is None:
            return {"loaded": [], "problems": [], "dropped": self.dropped}
        return {"loaded": sorted(self.host.loaded),
                "problems": list(self.host.problems),
                "queued": self.queue.qsize(),
                "dropped": self.dropped}
