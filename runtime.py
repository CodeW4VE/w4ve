"""The W4VE guardian: it owns processes and survives everything else.

RFC-0001 in code. The guardian is small on purpose. It does not parse chat, it
does not load plugins and it does not talk to the network: it starts processes,
watches them, relays their console and reports the truth about them. Everything
clever lives somewhere else and is allowed to crash without taking the server
down with it.

Nothing here imports anything outside the standard library, and nothing here
needs Python newer than 3.9, because that is what the mirror runs.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import rcon as rcon_module
except ImportError:          # a bare runtime.py still works, minus the fallback
    rcon_module = None

SCHEMA = 1

# How long we wait for a clean stop before we start escalating, and how long
# between each escalation. Saving a big world takes a while, and killing a
# server mid-save is how chunks get lost.
STOP_GRACE = 90
TERM_GRACE = 10
# A service is not a world. The long grace exists so Minecraft can save what
# people built; an image server or a bridge daemon has nothing to lose by
# being asked to leave now.
SERVICE_GRACE = 10
# How long a service waits for what it declared it comes after. Long enough
# for a daemon to bind a port, short enough that a boot never hangs on it.
DEPENDENCY_WAIT = 30

# Three deaths this close together and we stop trying. Retrying forever against
# a corrupt world is how an incident becomes data loss.
CRASH_WINDOW = 600
CRASH_LIMIT = 3

# A timer that runs longer than this is not a timer any more, and letting it
# pile up would eventually be a machine full of the same script.
TIMER_TIMEOUT = 300
# How often the disk is looked at. Storage fills up over days, not seconds.
STORAGE_EVERY = 300

CONSOLE_CAP = 8 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ process identity

def starttime_of(pid):
    """Return the boot-relative start time of a pid, or None.

    A pid is not an identity: the kernel hands them out again. The pair
    (pid, starttime) is one, because two processes cannot start at the same
    tick with the same number.
    """
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
    except (OSError, ValueError):
        return None
    # The command name sits in parentheses and may contain spaces, so the
    # fields only line up after the LAST closing paren.
    tail = stat[stat.rfind(")") + 1:].split()
    try:
        return int(tail[19])
    except (IndexError, ValueError):
        return None


def alive(pid, starttime=None):
    """Is this exact process still running?"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno != errno.EPERM:
            return False
    if starttime is None:
        return True
    seen = starttime_of(pid)
    # Without /proc (not Linux) we cannot tell a recycled pid from the real
    # one. Saying "alive" there is the honest answer, not a guarantee.
    return seen is None or seen == starttime


# -------------------------------------------------------------------- handlers

READY = re.compile(r'Done \([\d.]+s\)! For help')
STOPPING = re.compile(r'Stopping (?:the )?server')


class Handler:
    """What W4VE needs to know about one flavour of server."""

    name = "vanilla"
    packages = "none"
    companion = False
    note = "vanilla has nowhere to put mods or plugins."

    def ready(self, line):
        return bool(READY.search(line))

    def stopping(self, line):
        return bool(STOPPING.search(line))

    def stop_command(self):
        return "stop"

    def capabilities(self):
        caps = {
            "flavor": self.name,
            "managed": True,
            "packages": self.packages,
            "companion": self.companion,
            "rcon": True,
        }
        if self.note:
            caps["note"] = self.note
        return caps


class Fabric(Handler):
    name = "fabric"
    packages = "mods"
    companion = True
    note = ""


class Paper(Handler):
    """Managed, with reduced capabilities, decided on 19 August 2026.

    Leaving Paper merely observed would keep MCDR alive forever on the fifth
    MineWave server, and killing that dependency is the point of the project.
    So W4VE owns its process like any other, and says out loud the two things
    it cannot do there.
    """

    name = "paper"
    packages = "none"
    companion = False
    note = "Bukkit plugins are not installed from the W4VE catalog."


HANDLERS = {"fabric": Fabric, "paper": Paper, "vanilla": Handler}


def detect_flavor(game_dir):
    """Guess the flavour from what is on disk, never from the process name."""
    game_dir = Path(game_dir)
    if (game_dir / "mods").is_dir():
        return "fabric"
    jars = [p.name.lower() for p in game_dir.glob("*.jar")]
    if any("paper" in n or "spigot" in n or "purpur" in n for n in jars):
        return "paper"
    if (game_dir / "plugins").is_dir():
        return "paper"
    return "vanilla"


def handler_for(flavor):
    return HANDLERS.get(flavor, Handler)()


# ----------------------------------------------------------------- supervision

class Watched:
    """One process the guardian owns, with an explicit state.

    The Minecraft server is the first one. The second halves that real pieces
    ship (an image service, a scanner, a bridge) use this same contract, which
    is the whole point: a cron nobody watches is how the ShapeBoard scan sat
    dead for three days.
    """

    def __init__(self, name, command, cwd, guardian, handler=None,
                 restart="never", stop_grace=STOP_GRACE, health=None,
                 health_every=30, watches=(), env=None, after=()):
        self.name = name
        self.command = command
        self.cwd = Path(cwd)
        self.guardian = guardian
        self.handler = handler
        self.restart = restart
        self.stop_grace = stop_grace
        self.health = health            # a URL, for things with no console
        self.health_every = health_every
        # Lines a piece asked us to look for, and what to do about them.
        self.watches = [dict(w) for w in watches]
        # Added to the environment this process is started with, never
        # replacing it: a service that lost PATH is a service that cannot find
        # its own interpreter.
        self.env = dict(env or {})
        # Names this one wants up before it starts. Not a graph to be proud
        # of: a straight list, resolved in the order things were declared.
        self.after = [str(name) for name in (after or [])]
        self.asked_to_come_back = False

        # The console, kept apart from the Popen object on purpose. A guardian
        # that inherited this server from another one has the two ends of its
        # console and no child process to wait on, and everything that talks to
        # the console has to work the same in both cases.
        self.proc = None
        self.stdin = None
        self.stdout = None
        # Console objects whose descriptor we already handed over and closed.
        # Held on purpose: see `let_go_of_console`.
        self._handed_over = []
        self.pid = None
        self.starttime = None
        self.owned = False
        self.state = "stopped"
        self.ready = None            # None means "we do not know yet"
        self.started_at = None
        self.exit_code = None
        self.asked_to_stop = False
        self.deaths = []
        self.give_up = False
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- helpers

    @property
    def console_path(self):
        return self.guardian.run_dir / ("%s.console.log" % self.name)

    def _become(self, state, why=""):
        old, self.state = self.state, state
        if old != state:
            self.guardian.log("%s: %s -> %s%s" % (self.name, old, state,
                                                  "  (%s)" % why if why else ""))
            self.guardian.snapshot()
            self.guardian.plugins_state(self.name, state, self.ready,
                                        self.exit_code)

    def status(self):
        running = alive(self.pid, self.starttime)
        info = {
            "name": self.name,
            "state": self.state,
            "ready": self.ready,
            # A pid is only worth printing while it means something. Showing
            # the number of a process that already died is how people go and
            # kill whatever inherited it.
            "pid": self.pid if running else None,
            "last_pid": self.pid,
            "owned": self.owned and running,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "give_up": self.give_up,
        }
        if self.started_at and running:
            info["uptime"] = int(time.time() - self.started_at)
        if self.handler:
            info["capabilities"] = self.handler.capabilities()
        if running and not self.owned and rcon_module is not None and self.handler:
            creds, _ = rcon_module.credentials_from(self.cwd)
            info["rcon"] = bool(creds)
        return info

    # ---------------------------------------------------------------- running

    def start(self):
        with self.lock:
            if alive(self.pid, self.starttime):
                return False, "%s is already running (pid %s)" % (self.name, self.pid)
            self.asked_to_stop = False
            self.exit_code = None
            self.ready = None
            self._become("starting", "start requested")
            # No shell in the middle. A `/bin/sh -c` steals the signals meant
            # for the server and lies about the process tree, which is exactly
            # the shape MCDR has today.
            environment = None
            if self.env:
                environment = dict(os.environ)
                environment.update(self.env)
            self.proc = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                start_new_session=True,   # its own process group, on purpose
            )
            self.stdin = self.proc.stdin
            self.stdout = self.proc.stdout
            self.pid = self.proc.pid
            self.starttime = starttime_of(self.pid)
            self.owned = True
            self.started_at = time.time()
            threading.Thread(target=self._read_console, daemon=True).start()
            threading.Thread(target=self._reap, daemon=True).start()
            if self.handler is None:
                # Nothing to read a start-up line out of. A service is running
                # the moment it is up; whether it is READY is what the health
                # check answers, and without one we say we do not know.
                self._become("running", "no console to wait on")
                if self.health:
                    threading.Thread(target=self._poll_health,
                                     daemon=True).start()
            self.guardian.snapshot()
            return True, "%s started (pid %d)" % (self.name, self.pid)

    def adopt(self, pid, starttime, started_at=None):
        """Take note of a process we did not start.

        We can watch it and we can kill it, but we do not hold its stdin, so
        console commands are not ours. Saying `owned: false` is the point: a
        runtime that pretends to be in control is worse than one that admits
        it is not.
        """
        self.pid = pid
        self.starttime = starttime
        self.owned = False
        self.started_at = started_at
        self.ready = None
        self._become("running", "adopted a process we did not start")
        threading.Thread(target=self._watch_foreign, daemon=True).start()

    def inherit(self, entry, stdin_fd, stdout_fd):
        """Take a running process AND its console from another guardian.

        The difference with `adopt` is the whole point of the handover: an
        adopted process can be watched and killed but not talked to, and this
        one can, because the two ends of its console came over the socket.

        It is still not our child, so there is no `proc` and nothing to wait
        on. `_reap` knows that and watches /proc instead.
        """
        self.pid = entry.get("pid")
        self.starttime = entry.get("starttime")
        self.started_at = entry.get("started_at")
        self.owned = True
        self.proc = None
        self.stdin = os.fdopen(stdin_fd, "w", buffering=1)
        self.stdout = os.fdopen(stdout_fd, "r", buffering=1)
        self.ready = entry.get("ready")
        self.exit_code = None
        self._become(entry.get("state") or "running",
                     "inherited from the previous guardian, console and all")
        threading.Thread(target=self._read_console, daemon=True).start()
        threading.Thread(target=self._reap, daemon=True).start()

    def console_fds(self):
        """The two ends of the console, or None if we do not hold them."""
        if not self.owned or not self.stdin or not self.stdout:
            return None
        try:
            return [self.stdin.fileno(), self.stdout.fileno()]
        except ValueError:          # already closed
            return None

    def let_go_of_console(self):
        """Drop our copies after they have been sent somewhere else.

        Sending a descriptor duplicates it in the receiver, so letting go here
        does not close it there. Doing it at once is what stops two guardians
        reading one console and each getting half the lines.

        ⚠️ The descriptor is closed, NOT the file object, and the difference is
        a hang. `_read_console` is sitting inside a read on that same object
        and holding its buffer lock; calling `.close()` waits for that lock,
        the lock is only released when a line arrives, and a quiet server
        therefore freezes the handover until somebody says something. Closing
        the descriptor underneath needs no lock.

        The objects are then kept, unclosed, so that garbage collection cannot
        close a descriptor number that something else has since been given.
        """
        fds = []
        for handle in (self.stdin, self.stdout):
            try:
                fds.append(handle.fileno())
            except (ValueError, AttributeError):
                pass
            if handle is not None:
                self._handed_over.append(handle)
        self.stdin = None
        self.stdout = None
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def send(self, text):
        if not alive(self.pid, self.starttime):
            return False, "%s is not running" % self.name
        if not self.owned or not self.stdin:
            # No stdin of ours, which is the normal state for a server we only
            # adopted. RFC-0001 says the fallback is RCON, so use it instead of
            # telling the caller to go and do it themselves.
            return self.send_over_rcon(text)
        try:
            self.stdin.write(text.rstrip("\n") + "\n")
            self.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            return False, "console is gone: %s" % exc
        return True, "sent"

    def rcon(self):
        """A logged-in RCON connection to this server, or why there is none."""
        if rcon_module is None:
            return None, "this w4ve has no rcon.py next to it"
        if self.handler is None:
            return None, "%s is a service, not a server: it has no RCON" % self.name
        creds, why = rcon_module.credentials_from(self.cwd)
        if not creds:
            return None, why
        try:
            return rcon_module.Rcon(creds["host"], creds["port"],
                                    creds["password"]).connect(), ""
        except rcon_module.RconError as exc:
            return None, str(exc)

    def send_over_rcon(self, text):
        console, why = self.rcon()
        if console is None:
            return False, "no console and no RCON: %s" % why
        try:
            answer = console.command(text.strip())
        except rcon_module.RconError as exc:
            return False, "RCON: %s" % exc
        finally:
            console.close()
        self.guardian.log("%s: sent %r over RCON" % (self.name, text.strip()))
        return True, answer.strip() or "sent over RCON"

    def stop(self, grace=None):
        if not alive(self.pid, self.starttime):
            self._become("stopped", "nothing to stop")
            return True, "%s was not running" % self.name
        grace = self.stop_grace if grace is None else grace
        self.asked_to_stop = True
        self._become("stopping", "stop requested")

        if self.owned and self.handler is not None:
            self.send(self.handler.stop_command())
        elif self.owned:
            # ⚠️ A service has no console to type `stop` into. Writing the word
            # into its stdin does nothing at all, and then the guardian sat
            # through the full ninety second grace waiting for an answer that
            # was never coming: `w4ve stop <service>` looked like it had hung.
            # A signal is how you ask a service to leave.
            self._signal(signal.SIGTERM, "a service is asked with a signal")
        else:
            # An adopted server still knows how to save itself: ask it over
            # RCON, and only reach for signals when there is no answer.
            asked, message = self.send_over_rcon(
                self.handler.stop_command() if self.handler else "stop")
            if not asked:
                self.guardian.log("%s: %s" % (self.name, message))
                self._signal(signal.SIGTERM, "adopted process, no console, no RCON")

        if self._wait_for_exit(grace):
            return True, "%s stopped" % self.name

        self.guardian.log("%s: did not stop in %ds, escalating" % (self.name, grace))
        self._signal(signal.SIGTERM, "grace expired")
        if self._wait_for_exit(TERM_GRACE):
            return True, "%s stopped after SIGTERM" % self.name

        self._signal(signal.SIGKILL, "SIGTERM ignored")
        self._wait_for_exit(5)
        self._become("killed", "had to be killed")
        return True, "%s killed" % self.name

    def _signal(self, sig, why):
        """Signal the whole process group, not just the process.

        The server spawns children of its own; a signal to the group is what
        actually reaches them.
        """
        self.guardian.log("%s: sending %s (%s)" % (self.name, sig.name, why))
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except OSError:
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass

    def _wait_for_exit(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not alive(self.pid, self.starttime):
                return True
            time.sleep(0.2)
        return not alive(self.pid, self.starttime)

    # ------------------------------------------------------------- background

    def _read_console(self):
        console = open(str(self.console_path), "a", buffering=1)
        try:
            for line in self.stdout:
                line = line.rstrip("\n")
                if console.tell() > CONSOLE_CAP:
                    console.close()
                    self.console_path.replace(
                        self.console_path.with_suffix(".log.1"))
                    console = open(str(self.console_path), "a", buffering=1)
                console.write(line + "\n")
                self.guardian.broadcast(self.name, line)
                self._check_watches(line)
                # Handed over, never called here: plugins run on their own
                # thread so a slow one cannot delay the guardian noticing that
                # the server is ready, or that it died.
                self.guardian.feed_plugins(self.name, line)
                if self.handler:
                    if self.state == "starting" and self.handler.ready(line):
                        self.ready = True
                        self._become("running", "server says it is ready")
                    elif self.handler.stopping(line) and not self.asked_to_stop:
                        self.asked_to_stop = True
                        self._become("stopping", "the server decided to stop")
        except (ValueError, OSError):
            pass
        finally:
            console.close()

    def _check_watches(self, line):
        """Act on the lines a piece told us to look for.

        A piece that stops the server on purpose (RegionCast copies region
        files while it is down) can ask for it to come back, instead of the
        operator installing a plugin whose whole job is that one line. The
        restart is not automatic policy, it is a request from the piece that is
        doing the work, and it is written to the journal either way.
        """
        for watch in self.watches:
            try:
                if not re.search(watch["on"], line):
                    continue
            except re.error:
                continue
            why = watch.get("why") or watch.get("name", "a watch")
            self.guardian.log("%s: matched %r (%s)"
                              % (self.name, watch.get("name", "?"), why))
            if watch.get("do") == "restart":
                # The stop that follows is expected, so it is not a crash and
                # does not count against the crash-loop budget.
                self.asked_to_stop = True
                self.asked_to_come_back = True

    def _check_health(self):
        """Is this service answering? True, False, whichever way it is asked.

        Two ways because services are not all web servers. ChatBridge's daemon
        speaks its own protocol over a TCP port and has no health URL at all,
        so asking it for one meant its readiness stayed unknown forever, and a
        service whose readiness is always unknown is a service nobody can wait
        for.
        """
        if str(self.health).startswith("tcp://"):
            where = str(self.health)[len("tcp://"):]
            host, _, port = where.rpartition(":")
            try:
                with socket.create_connection((host or "127.0.0.1",
                                               int(port)), timeout=5):
                    return True
            except (OSError, ValueError):
                return False
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self.health, timeout=5) as response:
                return 200 <= response.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _poll_health(self):
        # A service is allowed to take a moment to bind its port. Calling it
        # unhealthy while it is still starting is noise, so the first few
        # failures only mean "not yet".
        tries = 0
        while alive(self.pid, self.starttime) and not self.guardian.stopping.is_set():
            tries += 1
            healthy = self._check_health()
            if not healthy and self.ready is None and tries < 6:
                time.sleep(2)
                continue
            if healthy != self.ready:
                self.ready = healthy
                self.guardian.plugins_state(self.name, self.state, self.ready)
                self.guardian.log("%s: health %s (%s)"
                                  % (self.name, "ok" if healthy else "failing",
                                     self.health))
                self.guardian.snapshot()
            # ⚠️ A TCP check is asked once and then left alone. Opening a
            # port and closing it without saying anything is not a question,
            # it is a connection the other end has to explain: ChatBridge's
            # daemon logged `Failed reading client's login packet` every
            # thirty seconds, which is two thousand nine hundred false errors
            # a day in somebody else's log. An open port answers "it started",
            # not "it is well", and that answer does not change on its own.
            # An HTTP health check is a real question and keeps being asked.
            if healthy and str(self.health).startswith("tcp://"):
                self.guardian.log("%s: port open, not asking again "
                                  "(a TCP check says started, not healthy)"
                                  % self.name)
                return
            # Checks come quickly until the first success, because a service
            # that is up in a second should not read as unknown for half a
            # minute.
            time.sleep(self.health_every if self.ready else 2)

    def _reap(self):
        if self.proc is not None:
            code = self.proc.wait()
        else:
            # Inherited from another guardian: this process is not our child,
            # so there is nothing to wait on and no exit code to read. We watch
            # /proc instead and say we do not know how it ended, which is true.
            while alive(self.pid, self.starttime):
                time.sleep(0.5)
            code = None
        self.exit_code = code
        self.ready = False
        # "exit 0" and "exit, we could not see the code" are different things,
        # and the journal is the place where that difference gets read later.
        how = "exit %d" % code if code is not None else "exit, code unknown"
        if self.asked_to_come_back:
            self.asked_to_come_back = False
            self._become("stopped", "%s, a piece asked it to come back" % how)
            time.sleep(1)
            ok, message = self.start()
            self.guardian.log(message)
        elif self.asked_to_stop or self.state in ("stopping", "killed"):
            self._become("stopped", how)
        else:
            self._note_death()
            self._become("crashed", "%s, nobody asked for that" % how)
            self._maybe_restart()

    def _watch_foreign(self):
        """Adopted processes have no console, so all we can do is notice."""
        while alive(self.pid, self.starttime):
            time.sleep(1)
        if self.asked_to_stop or self.state == "stopping":
            self._become("stopped", "adopted process exited")
        else:
            self._note_death()
            self._become("crashed", "adopted process died")

    def _note_death(self):
        cut = time.time() - CRASH_WINDOW
        self.deaths = [t for t in self.deaths if t > cut] + [time.time()]
        if len(self.deaths) >= CRASH_LIMIT:
            self.give_up = True
            self.guardian.log(
                "%s: %d deaths in %d minutes, not restarting it again"
                % (self.name, len(self.deaths), CRASH_WINDOW // 60))

    def _maybe_restart(self):
        if self.restart == "never" or self.give_up:
            return
        self.guardian.log("%s: restarting (policy %s)" % (self.name, self.restart))
        time.sleep(2)
        self.start()


# -------------------------------------------------------------------- guardian

# A Unix socket path lives in a fixed size field in the kernel, 108 bytes with
# the trailing zero, and a server folder nested deep enough goes over it. Before
# this was handled the bind blew up in a thread and the guardian ran on with no
# control socket at all: alive, working, and unreachable. Anything that walks a
# path length limit has to fail loudly or route around it.
SOCK_MAX = 100


# --------------------------------------------------------------------- disk

def parse_size(text):
    """`2G` -> bytes. Duplicated from piecesettings on purpose.

    The runtime has to work on a machine where only `w4ve.py` and `runtime.py`
    were copied, so it does not get to depend on the settings module for two
    lines of arithmetic.
    """
    raw = str(text).strip().lower().replace("b", "")
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    unit = units.get(raw[-1:]) if raw else None
    try:
        return int(float(raw[:-1] if unit else raw) * (unit or 1))
    except ValueError:
        return 0


def parse_time(text):
    """`7d` -> seconds."""
    raw = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = units.get(raw[-1:]) if raw else None
    try:
        return int(float(raw[:-1] if unit else raw) * (unit or 1))
    except ValueError:
        return 0


def folder_usage(folder):
    """(bytes, files) under a folder, symlinks not followed."""
    total, count = 0, 0
    for path in Path(folder).rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


def purge_area(folder, quota=None, keep=None, by="mtime"):
    """Make room, oldest first, and never touch what is still inside the rules.

    `by="atime"` is what a cache wants: a song played every day stays even if
    it is the oldest thing there. `by="mtime"` is what a log wants. Getting
    this backwards is how a cache throws away exactly what people use.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return 0, 0
    files = []
    for path in folder.rglob("*"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            info = path.stat()
            age = info.st_atime if by == "atime" else info.st_mtime
            files.append((age, info.st_size, path))
        except OSError:
            continue
    files.sort()
    freed, gone = 0, 0
    moment = time.time()
    remaining = sum(size for _, size, _ in files)
    for age, size, path in files:
        too_old = keep is not None and (moment - age) > keep
        too_big = quota is not None and remaining > quota
        if not (too_old or too_big):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        freed += size
        gone += 1
        remaining -= size
    return freed, gone


def control_socket_for(run_dir):
    """Where this server's control socket lives, short enough to bind.

    Normally right next to the other runtime files. When that path is too long
    for the kernel, in a temp directory instead, and `control.sock.path` in the
    run folder says where it went so clients can still find it.
    """
    run_dir = Path(run_dir)
    natural = run_dir / "control.sock"
    if len(str(natural)) <= SOCK_MAX:
        return natural
    digest = hashlib.sha1(str(run_dir.resolve()).encode()).hexdigest()[:16]
    short = Path(tempfile.gettempdir()) / ("w4ve-%s.sock" % digest)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "control.sock.path").write_text(str(short) + "\n")
    except OSError:
        pass
    return short


def control_socket_of(root):
    """The client side of the same question."""
    run_dir = Path(root) / "w4ve" / "run"
    pointer = run_dir / "control.sock.path"
    if pointer.exists():
        try:
            return Path(pointer.read_text().strip())
        except OSError:
            pass
    return run_dir / "control.sock"


class Guardian:
    """One per server directory, and the lock file makes sure of it."""

    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.config = config or {}
        self.run_dir = self.root / "w4ve" / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.run_dir / "journal.log"
        self.state_path = self.run_dir / "state.json"
        self.sock_path = control_socket_for(self.run_dir)
        self.lock_path = self.run_dir / "lock"
        self.lock_fd = None
        self.watched = {}
        self.timers = {}
        self.storage = {}
        self.listeners = []
        self.listeners_lock = threading.Lock()
        self.echo = False           # print the console here, for `w4ve run`
        self.stopping = threading.Event()
        # Set once this guardian has given its server to another one. From
        # then on it writes nothing down: it is no longer the truth.
        self.handed_over = False
        # The MCDR compatible plugin host, if this server has plugins and this
        # w4ve carries the compatibility layer. None means "no plugins", which
        # is a normal server, not a broken one.
        self.plugins = None
        # Native plugins (RFC-0003), each one a process of its own.
        self.workers = None
        # The local API (M8), if this w4ve carries it and the server wants it.
        self.api = None

    # ------------------------------------------------------------------ setup

    def claim(self):
        """Take the instance lock, or say who has it.

        Two guardians over one server is the fastest way to start a world
        twice, and a world started twice is a world corrupted once.
        """
        self.lock_fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = Path(self.lock_path).read_text().strip()
            except OSError:
                pass
            os.close(self.lock_fd)
            self.lock_fd = None
            return False, holder
        os.ftruncate(self.lock_fd, 0)
        os.write(self.lock_fd, ("%d\n" % os.getpid()).encode())
        return True, ""

    def release(self):
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    def log(self, text):
        line = "%s  %s\n" % (now(), text)
        with open(str(self.journal), "a") as fh:
            fh.write(line)

    # ------------------------------------------------------------------ state

    def snapshot(self):
        """Write down what we believe, so the next guardian can check it.

        After a handover this does nothing: what we believe is out of date the
        moment the console leaves, and writing it would overwrite the note of
        the guardian that now actually owns the server.

        This file is a cache, never the truth. The truth is /proc, and every
        read of this file verifies (pid, starttime) before believing a word.
        """
        if self.handed_over:
            return
        data = {
            "schema": SCHEMA,
            "written": now(),
            "guardian_pid": os.getpid(),
            "processes": {},
        }
        for name, proc in self.watched.items():
            data["processes"][name] = {
                "state": proc.state,
                "ready": proc.ready,
                "pid": proc.pid,
                "starttime": proc.starttime,
                "owned": proc.owned,
                "started_at": proc.started_at,
                "command": proc.command,
                "cwd": str(proc.cwd),
                "flavor": proc.handler.name if proc.handler else None,
            }
        # The temporary name carries our pid. During a handover there are two
        # guardians alive over one directory for a moment, and with a shared
        # `state.json.tmp` one of them renames the other's file out from under
        # it and the loser dies with FileNotFoundError.
        tmp = self.state_path.with_suffix(".json.%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.state_path)

    def read_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------- the server

    def add_server(self, command, game_dir, flavor=None, stop_grace=STOP_GRACE,
                   restart="never", watches=()):
        flavor = flavor or detect_flavor(game_dir)
        proc = Watched("server", command, game_dir, self,
                       handler=handler_for(flavor), restart=restart,
                       stop_grace=stop_grace, watches=watches)
        self.watched["server"] = proc
        return proc

    def add_service(self, name, command, cwd, restart="on-failure",
                    health=None, health_every=30, env=None, mcdr=False,
                    stop_grace=SERVICE_GRACE, after=()):
        env = dict(env or {})
        if mcdr:
            # The service said it imports `mcdreforged`. ChatBridge's daemon
            # does, and it is not a plugin: it is a separate process started
            # from a command line. Rather than asking somebody to pip install
            # all of MCDR for one class, W4VE puts its own on the path.
            compat = mcdr_compat_path()
            if compat is None:
                self.log("%s asked for the mcdreforged compatibility package, "
                         "which is not next to this runtime" % name)
            else:
                existing = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH")
                env["PYTHONPATH"] = (str(compat) + os.pathsep + existing
                                     if existing else str(compat))
        proc = Watched(name, command, cwd, self, handler=None, restart=restart,
                       health=health, health_every=health_every, env=env,
                       stop_grace=stop_grace, after=after)
        self.watched[name] = proc
        return proc

    def add_timer(self, name, command, every, cwd=".", env=None, why=""):
        """Something to run again every so often, watched instead of cron'd.

        A cron entry is a promise nobody checks: the ShapeBoard scan was dead
        for three days and the only symptom was a board that stopped changing.
        A timer here runs in the guardian, its output goes to the journal, and
        a run that fails says so out loud the first time and then keeps count.
        """
        self.timers[name] = {
            "name": name,
            "command": list(command),
            "every": max(5, int(every)),
            "cwd": str(Path(self.root) / cwd),
            "env": dict(env or {}),
            "why": why,
            "last": 0.0,
            "runs": 0,
            "failures": 0,
            "streak": 0,
            "last_error": "",
        }
        return self.timers[name]

    def add_storage(self, name, folder, quota=None, keep=None, by="mtime",
                    why=""):
        """A folder a piece writes to, with a ceiling and a way to make room."""
        self.storage[name] = {
            "name": name,
            "dir": str(Path(self.root) / folder),
            "quota": quota,
            "keep": keep,
            "by": by,
            "why": why,
            "last_purge": 0.0,
        }
        Path(self.storage[name]["dir"]).mkdir(parents=True, exist_ok=True)
        return self.storage[name]

    def _housekeeping(self):
        """The loop that runs the timers and keeps the disk inside its lines.

        One thread for both because they are the same kind of work: something
        that has to happen on its own, that nobody is watching, and that must
        never be able to hold up the guardian noticing that the server died.
        """
        while not self.stopping.is_set():
            moment = time.time()
            for timer in list(self.timers.values()):
                if moment - timer["last"] < timer["every"]:
                    continue
                timer["last"] = moment
                self._run_timer(timer)
            for area in list(self.storage.values()):
                if moment - area["last_purge"] < STORAGE_EVERY:
                    continue
                area["last_purge"] = moment
                self._purge_storage(area)
            self.stopping.wait(1.0)

    def _run_timer(self, timer):
        environment = dict(os.environ)
        environment.update(timer["env"])
        try:
            result = subprocess.run(
                timer["command"], cwd=timer["cwd"], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=TIMER_TIMEOUT)
        except subprocess.TimeoutExpired:
            timer["failures"] += 1
            timer["streak"] += 1
            timer["last_error"] = "took longer than %ds" % TIMER_TIMEOUT
            self.log("timer %s %s" % (timer["name"], timer["last_error"]))
            return
        except OSError as exc:
            timer["failures"] += 1
            timer["streak"] += 1
            timer["last_error"] = str(exc)
            self.log("timer %s could not run: %s" % (timer["name"], exc))
            return
        timer["runs"] += 1
        if result.returncode == 0:
            if timer["streak"]:
                self.log("timer %s is working again after %d failures"
                         % (timer["name"], timer["streak"]))
            timer["streak"] = 0
            return
        timer["failures"] += 1
        timer["streak"] += 1
        tail = (result.stdout or b"").decode("utf-8", "replace").strip()
        timer["last_error"] = tail.splitlines()[-1] if tail else "exit %s" % result.returncode
        # Loud the first time and then quiet: a timer failing every minute
        # would bury everything else in the journal, which is how people learn
        # to ignore it.
        if timer["streak"] == 1 or timer["streak"] % 60 == 0:
            self.log("timer %s failed (%d in a row): %s"
                     % (timer["name"], timer["streak"], timer["last_error"]))

    def _purge_storage(self, area):
        quota = parse_size(area["quota"]) if area["quota"] else None
        keep = parse_time(area["keep"]) if area["keep"] else None
        freed, gone = purge_area(Path(area["dir"]), quota, keep, area["by"])
        if gone:
            self.log("storage %s: freed %d bytes in %d files"
                     % (area["name"], freed, gone))

    def wait_for_dependencies(self, proc, timeout=DEPENDENCY_WAIT):
        """Hold a service back until what it needs is actually up.

        Starting in the right order was never the problem: `start()` returns
        as soon as the process exists, and a daemon needs a moment more to
        bind its port. ChatBridge's !!online client came up first, failed to
        connect three times, and reconnected on its own ten seconds later. It
        healed itself, and it still filled the console with errors on every
        boot.

        What counts as up: ready, if the thing has a health check that can
        answer; alive, if it has none. Waiting for readiness that can never
        arrive would be worse than not waiting at all, so a service with no
        health check is not waited on beyond being alive.
        """
        for name in getattr(proc, "after", ()):
            other = self.watched.get(name)
            if other is None:
                self.log("%s waits for %s, which is not declared here"
                         % (proc.name, name))
                continue
            deadline = time.time() + timeout
            while time.time() < deadline and not self.stopping.is_set():
                if not alive(other.pid, other.starttime):
                    time.sleep(0.2)
                    continue
                if other.health is None or other.ready:
                    break
                time.sleep(0.2)
            else:
                self.log("%s waited %ds for %s and started anyway"
                         % (proc.name, timeout, name))
                continue
            self.log("%s waited for %s" % (proc.name, name))

    def recover(self):
        """Find processes from a previous life and take them back.

        Killing the guardian must never kill the server, so coming back and
        finding it still there is the normal case, not the exception.
        """
        old = self.read_state().get("processes", {})
        found = []
        for name, entry in old.items():
            pid, starttime = entry.get("pid"), entry.get("starttime")
            if not pid or not alive(pid, starttime):
                continue
            proc = self.watched.get(name)
            if proc is not None and proc.owned and proc.stdin:
                # Already ours, console and all: this one came over in a
                # handover. Adopting it again would throw the console away.
                continue
            if proc is None:
                proc = self.add_service(name, entry.get("command") or [],
                                        entry.get("cwd") or str(self.root),
                                        restart="never")
            proc.adopt(pid, starttime, entry.get("started_at"))
            found.append(name)
        if found:
            self.log("recovered: %s" % ", ".join(found))
        return found

    # -------------------------------------------------------------- handover

    def hand_over(self, conn):
        """Give a new guardian this server's console, and stand down.

        This is what closes the last hole in RFC-0001. The server already
        survived a guardian dying, but it only got its console back on the
        next start W4VE did, so updating the runtime meant a restart of the
        world. Now the descriptors move across the control socket and nothing
        stops.

        The order is the whole trick:

        1. send the descriptors (sending duplicates them in the receiver);
        2. close ours at once, or two guardians read one console and each
           gets half the lines;
        3. release the lock, so the new one can take it;
        4. leave without stopping anything.
        """
        moving = [(name, proc) for name, proc in self.watched.items()
                  if proc.console_fds()]
        if not moving:
            return {"ok": False,
                    "error": "nothing here holds a console to hand over"}
        if len(moving) > 1:
            # One socket message carries one array of descriptors, and pairing
            # them back up with the right process is guesswork this refuses to
            # do. Multi-process handover is a bigger change than it looks.
            return {"ok": False,
                    "error": "%d processes have consoles, and handover moves one"
                             % len(moving)}

        name, proc = moving[0]
        entry = {"name": name, "pid": proc.pid, "starttime": proc.starttime,
                 "started_at": proc.started_at, "state": proc.state,
                 "ready": proc.ready, "command": list(proc.command),
                 "cwd": str(proc.cwd)}
        payload = json.dumps({"ok": True, "handover": 1, "process": entry,
                              "guardian_pid": os.getpid()}) + "\n"
        try:
            socket.send_fds(conn, [payload.encode()], proc.console_fds())
        except OSError as exc:
            return {"ok": False, "error": "could not send the console: %s" % exc}
        proc.let_go_of_console()
        self.handed_over = True
        self.log("handed %s over to another guardian, console and all" % name)
        self.release()
        # The reply already went out with the descriptors, and saying it twice
        # would leave a stray line in the new guardian's buffer.
        return {"ok": True, "sent": True, "process": name}

    def take_over(self, timeout=15):
        """Take a running server, console included, from the guardian that has it.

        Returns (ok, message). Failing here is not fatal for the caller: the
        normal thing to do is carry on and start the server the usual way.
        """
        client = Client(self.root)
        if not client.connected():
            return False, "no guardian is running here to take over from"
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(self.sock_path))
            conn.sendall(b'{"cmd": "handover"}\n')
            data, fds, _flags, _addr = socket.recv_fds(conn, 65536, 2)
        except OSError as exc:
            for fd in locals().get("fds") or []:
                os.close(fd)
            return False, "the handover did not happen: %s" % exc
        finally:
            conn.close()

        try:
            answer = json.loads(data.decode().split("\n", 1)[0])
        except (ValueError, UnicodeDecodeError):
            for fd in fds:
                os.close(fd)
            return False, "the other guardian said something that is not JSON"
        if not answer.get("ok"):
            for fd in fds:
                os.close(fd)
            return False, answer.get("error", "the other guardian said no")
        if len(fds) != 2:
            for fd in fds:
                os.close(fd)
            return False, "expected two descriptors, got %d" % len(fds)

        entry = answer.get("process") or {}
        name = entry.get("name") or "server"
        proc = self.watched.get(name)
        if proc is None:
            proc = self.add_service(name, entry.get("command") or [],
                                    entry.get("cwd") or str(self.root),
                                    restart="never")
        # The old guardian releases its lock as it hands over, but it does it
        # after we have the descriptors, so the file can be a moment behind.
        limit = time.time() + timeout
        while time.time() < limit:
            got_it, holder = self.claim()
            if got_it:
                break
            time.sleep(0.1)
        else:
            for fd in fds:
                os.close(fd)
            return False, "took the console but never got the lock (%s has it)" % holder

        proc.inherit(entry, fds[0], fds[1])
        self.log("took %s over from guardian %s, without restarting it"
                 % (name, answer.get("guardian_pid")))
        self.snapshot()
        return True, "took over %s (pid %s), console and all" % (name, proc.pid)

    # ----------------------------------------------------------- control sock

    # ------------------------------------------------------------- the plugins

    def start_plugins(self):
        """Load this server's MCDR plugins, if it has any and we can.

        Every failure here is a note, never a stop: a server whose plugins
        will not load still has to start. That is the difference between a
        wrapper and a dependency, and W4VE is meant to be the first one.
        """
        if self.plugins is not None:
            return False, "plugins are already loaded"
        if not self.config.get("plugins", True):
            return False, "plugins are turned off for this server"
        try:
            from pluginhost import PluginRuntime
        except ImportError as exc:
            self.log("plugins: not available (%s)" % exc)
            return False, str(exc)
        runtime = PluginRuntime(self, server_name="server")
        ok, message = runtime.start()
        self.log("plugins: " + message if not message.startswith("plugins")
                 else message)
        if ok:
            self.plugins = runtime
        return ok, message

    def start_workers(self):
        """Start the native plugins: the ones that are processes (RFC-0003).

        Separate from `start_plugins` and deliberately so. Those are MCDR's,
        in-process because that is what they expect; these are W4VE's, each in
        its own process, and the guarantees are different enough that pretending
        they are one population would be a lie in the status output.
        """
        if self.workers is not None:
            return False, "native plugins are already loaded"
        folder = self.root / "w4ve" / "plugins"
        if not folder.is_dir():
            return False, "no native plugins here"
        try:
            from worker import WorkerHost
        except ImportError as exc:
            self.log("native plugins: not available (%s)" % exc)
            return False, str(exc)
        host = WorkerHost(self.root, guardian=self)
        started, problems = host.load(folder)
        self.workers = host
        for line in problems:
            self.log("native plugins: " + line)
        message = "%d native plugin%s" % (len(started),
                                          "s" if len(started) != 1 else "")
        self.log("native plugins: " + message)
        return True, message

    def start_api(self):
        """The local API, if this server asked for one.

        ⚠️ Never fatal. A server that will not boot because something wanted
        to watch it is worse than a server nobody can watch, and this is the
        kind of optional extra that has to prove it knows that.
        """
        section = self.config.get("api") or {}
        if not section.get("enabled", False):
            return False, "the api is off for this server"
        try:
            from api import Api
        except ImportError as exc:
            self.log("api: not available (%s)" % exc)
            return False, str(exc)
        instance = Api(self.root, guardian=self,
                       address=section.get("listen") or None,
                       tokens_required=section.get("tokens"))
        ok, message = instance.start()
        self.log("api: " + message)
        if ok:
            self.api = instance
        return ok, message

    def stop_api(self):
        if self.api is not None:
            self.api.stop()
            self.api = None

    def stop_plugins(self):
        if self.workers is not None:
            self.workers.stop_all()
            self.workers = None
        if self.plugins is None:
            return
        self.plugins.stop()
        self.plugins = None

    def feed_plugins(self, name, line):
        if self.plugins is not None:
            self.plugins.feed(name, line)
        if self.workers is not None and name == "server":
            # Never blocks: `console_line` drops rather than waits, because
            # this is the thread that also learns when Minecraft dies.
            self.workers.console_line(line)

    def plugins_state(self, name, state, ready, exit_code=None):
        if self.plugins is not None:
            self.plugins.state_changed(name, state, ready, exit_code)

    def plugins_console(self, text):
        """Offer a typed line to the plugins. `(claimed, what it answered)`.

        Native plugins are asked first. They are the ones whose failure is
        contained, so preferring them costs nothing, and a name registered on
        both sides is a conflict the operator can see in `status` either way.
        """
        if self.workers is not None:
            claimed, lines = self.workers.command(text)
            if claimed:
                return True, lines
        if self.plugins is None:
            return False, []
        return self.plugins.console(text)

    def broadcast(self, name, line):
        if self.echo:
            print(line if name == "server" else "[%s] %s" % (name, line),
                  flush=True)
        payload = json.dumps({"event": "console", "process": name,
                              "line": line}) + "\n"
        with self.listeners_lock:
            dead = []
            for conn in self.listeners:
                try:
                    conn.sendall(payload.encode())
                except OSError:
                    dead.append(conn)
            for conn in dead:
                self.listeners.remove(conn)

    def serve(self):
        if self.sock_path.exists():
            self.sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.sock_path))
        except OSError as exc:
            # Say it in the journal and on the way out. A guardian nobody can
            # talk to is worse than one that refuses to start, because it looks
            # like it is working.
            self.log("cannot open the control socket at %s: %s"
                     % (self.sock_path, exc))
            server.close()
            self.shutdown(stop_processes=False)
            raise
        os.chmod(str(self.sock_path), 0o660)
        server.listen(8)
        server.settimeout(0.5)
        self.log("control socket at %s" % self.sock_path)
        while not self.stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()
        server.close()
        if self.sock_path.exists():
            self.sock_path.unlink()

    def _client(self, conn):
        tailing = False
        try:
            buf = b""
            conn.settimeout(None)
            while not self.stopping.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    try:
                        request = json.loads(raw.decode())
                    except ValueError:
                        conn.sendall(b'{"ok": false, "error": "not json"}\n')
                        continue
                    if request.get("cmd") == "handover":
                        # Has to be answered here and not in `handle()`: the
                        # descriptors travel over this connection, and
                        # `handle()` only ever sees the parsed request.
                        reply = self.hand_over(conn)
                        if reply.get("ok"):
                            # The answer already went out WITH the descriptors.
                            # Sending it again would raise on a connection the
                            # other side has closed, and that exception used to
                            # eat the shutdown below: the old guardian handed
                            # over its console and then stayed up forever.
                            threading.Thread(target=self.shutdown,
                                             kwargs={"stop_processes": False},
                                             daemon=True).start()
                        else:
                            conn.sendall((json.dumps(reply) + "\n").encode())
                        continue
                    if request.get("cmd") == "tail":
                        tailing = True
                        with self.listeners_lock:
                            self.listeners.append(conn)
                        conn.sendall(b'{"ok": true, "tailing": true}\n')
                        continue
                    reply = self.handle(request)
                    conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        finally:
            if tailing:
                with self.listeners_lock:
                    if conn in self.listeners:
                        self.listeners.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def handle(self, request):
        cmd = request.get("cmd", "")
        name = request.get("process", "server")
        proc = self.watched.get(name)

        if cmd == "ping":
            return {"ok": True, "pong": True, "guardian_pid": os.getpid()}
        if cmd == "status":
            answer = {"ok": True, "root": str(self.root),
                      "processes": [p.status() for p in self.watched.values()]}
            if self.plugins is not None:
                answer["plugins"] = self.plugins.status()
            if self.workers is not None:
                answer["workers"] = self.workers.status()
            return answer
        if proc is None:
            return {"ok": False, "error": "no process called %r" % name}
        if cmd == "start":
            ok, message = proc.start()
            return {"ok": ok, "message": message}
        if cmd == "stop":
            ok, message = proc.stop(request.get("grace"))
            return {"ok": ok, "message": message}
        if cmd == "send":
            text = request.get("text", "")
            # `!!something` is a plugin command, not a Minecraft one. Sending
            # it to the server would only produce "Unknown command".
            if text.strip().startswith("!!"):
                claimed, lines = self.plugins_console(text)
                if claimed:
                    return {"ok": True, "message": "handled by the plugins",
                            "lines": lines}
            ok, message = proc.send(text)
            return {"ok": ok, "message": message}
        if cmd == "shutdown":
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {"ok": True, "message": "guardian shutting down"}
        return {"ok": False, "error": "unknown command %r" % cmd}

    # --------------------------------------------------------------- lifetime

    def shutdown(self, stop_processes=True):
        if self.stopping.is_set():
            return
        self.log("guardian shutting down")
        # Plugins first: `on_unload` is where a plugin saves what it was
        # keeping, and it is worth having a server to talk to while it does.
        self.stop_api()
        self.stop_plugins()
        if stop_processes:
            for proc in list(self.watched.values()):
                if alive(proc.pid, proc.starttime):
                    proc.stop()
        self.snapshot()
        self.stopping.set()

    def run(self, start_now=True, take_over=False):
        """Foreground on purpose: inside a container the runtime IS the process.

        With `take_over`, the guardian already running here is asked for its
        server first: the world keeps playing while the runtime under it is
        replaced. If there is nobody to take over from, or the handover fails,
        this says so and starts normally, because an upgrade that refuses to
        boot is worse than one that restarts.
        """
        inherited = []
        if take_over:
            ok, message = self.take_over()
            self.log("handover: " + message)
            if ok:
                inherited = [name for name, proc in self.watched.items()
                             if proc.owned and proc.stdin]
            else:
                print(message, flush=True)
        if not inherited:
            got_it, holder = self.claim()
            if not got_it:
                return 1, "another guardian holds this server (pid %s)" % (holder or "?")

        def bye(signum, _frame):
            self.log("got %s" % signal.Signals(signum).name)
            self.shutdown()

        signal.signal(signal.SIGINT, bye)
        signal.signal(signal.SIGTERM, bye)

        self.log("guardian up, pid %d, root %s" % (os.getpid(), self.root))
        # Anything inherited is already running AND already ours, console
        # included. Recovering it again would downgrade it to "adopted, no
        # console", which is exactly what the handover was for.
        recovered = list(inherited) + [n for n in self.recover()
                                       if n not in inherited]
        thread = threading.Thread(target=self.serve, daemon=True)
        thread.start()
        if self.timers or self.storage:
            # Its own thread: housekeeping must never be able to delay the
            # guardian noticing that the server died.
            threading.Thread(target=self._housekeeping, daemon=True).start()
            self.log("housekeeping: %d timers, %d storage areas"
                     % (len(self.timers), len(self.storage)))

        if start_now:
            for name, proc in self.watched.items():
                if name in recovered:
                    self.log("%s was already running, not starting a second one"
                             % name)
                    continue
                self.wait_for_dependencies(proc)
                # Services come up with the guardian: the whole point of a
                # piece declaring one is that nobody has to remember it.
                ok, message = proc.start()
                self.log(message)

        # After the server is started, not before: a plugin's `on_load` is
        # allowed to ask whether the server is running, and the honest answer
        # is easier to give once it is.
        self.start_plugins()
        self.start_workers()
        self.start_api()

        while not self.stopping.is_set():
            time.sleep(0.3)
        self.snapshot()
        self.release()
        return 0, "guardian stopped"


# ---------------------------------------------------------------------- client

class Client:
    """Talks to a guardian over its control socket, or says there is none."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sock_path = control_socket_of(self.root)

    def connected(self):
        """Is there a guardian, really?

        The socket file outliving the guardian is the normal case after a
        SIGKILL, so its existence proves nothing. Only a connection does.
        """
        if not self.sock_path.exists():
            return False
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2)
        try:
            conn.connect(str(self.sock_path))
            return True
        except OSError:
            return False
        finally:
            conn.close()

    def call(self, request, timeout=10):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(self.sock_path))
        except OSError as exc:
            conn.close()
            return {"ok": False, "error": "no guardian here (%s)" % exc.strerror}
        try:
            conn.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return {"ok": False, "error": "guardian hung up"}
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0].decode())
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            conn.close()

    def tail(self, on_line, timeout=None, stop=None, ready=None):
        """Follow the console until the caller says to stop.

        `stop` is a callable asked between reads, so a follower can be told to
        let go without waiting for the next line: a quiet server would keep a
        cancelled job hanging for as long as nobody talks. `ready` is set once
        the guardian has confirmed the subscription, which is what lets a
        caller send a command *after* it is listening and not lose the first
        line of the answer.
        """
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # With `stop` we wake up regularly to ask it, and the caller's timeout
        # stops being the socket's problem.
        conn.settimeout(0.5 if stop is not None else timeout)
        conn.connect(str(self.sock_path))
        conn.sendall(b'{"cmd": "tail"}\n')
        buf = b""
        try:
            while True:
                if stop is not None and stop():
                    return
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(raw.decode())
                    except ValueError:
                        continue
                    if message.get("tailing") and ready is not None:
                        ready.set()
                    if message.get("event") == "console":
                        on_line(message["process"], message["line"])
        finally:
            conn.close()

    def offline_status(self):
        """What we can tell without a guardian, by checking /proc ourselves."""
        path = self.root / "w4ve" / "run" / "state.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        out = []
        for name, entry in (data.get("processes") or {}).items():
            running = alive(entry.get("pid"), entry.get("starttime"))
            out.append({
                "name": name,
                # No guardian means no console and no readiness, so `ready`
                # stays None. The state itself we can check ourselves: the
                # process is there or it is not, and `stale` says the note we
                # are reading is older than the fact.
                "state": "running" if running else "stopped",
                "ready": None,
                "pid": entry.get("pid") if running else None,
                "owned": False,
                "started_at": entry.get("started_at"),
                "stale": not running,
                "capabilities": None,
            })
        return {"ok": True, "root": str(self.root), "guardian": False,
                "processes": out}


# ------------------------------------------------------------- config reading

def server_command(config, root, game_dir):
    """Work out how this server is started, from `[server] start`."""
    start = (config.get("server") or {}).get("start")
    if start:
        return shlex.split(start) if isinstance(start, str) else list(start)
    game = Path(root) / game_dir
    for candidate in ("fabric-server-launch.jar", "server.jar"):
        if (game / candidate).exists():
            java = str((config.get("server") or {}).get("java_bin") or "java")
            return [java, "-jar", candidate, "nogui"]
    return None


def watches_from(config):
    """Read `[watch.<name>]` blocks: a line to look for, and what to do."""
    out = []
    for name, entry in (config.get("watch") or {}).items():
        if not isinstance(entry, dict) or not entry.get("on"):
            continue
        out.append({
            "name": name,
            "on": entry["on"],
            "do": entry.get("do", "note"),
            "why": entry.get("why", ""),
        })
    return out


def services_from(config):
    """Read `[services.<name>]` blocks, the second halves of real pieces."""
    out = []
    for name, entry in (config.get("services") or {}).items():
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            continue
        env = {}
        declared = entry.get("env")
        if isinstance(declared, dict):
            env.update({str(k): str(v) for k, v in declared.items()})
        out.append({
            "name": name,
            "command": list(command),
            "cwd": entry.get("cwd") or ".",
            "restart": entry.get("restart") or "on-failure",
            # Two ways to ask, because services are not all web servers.
            # `health_tcp = "127.0.0.1:30001"` is what a daemon speaking its
            # own protocol can answer, and ChatBridge's is exactly that.
            "health": (entry.get("health_url")
                       or ("tcp://%s" % entry["health_tcp"]
                           if entry.get("health_tcp") else None)),
            "health_every": int(entry.get("health_every") or 30),
            "env": env,
            "stop_grace": int(entry.get("stop_grace") or SERVICE_GRACE),
            "after": ([entry["after"]] if isinstance(entry.get("after"), str)
                      else list(entry.get("after") or [])),
            # `mcdr = true` means: this process imports `mcdreforged` even
            # though it is not a plugin. ChatBridge's daemon does, and under
            # MCDR the answer was to pip install all of MCDR on the machine to
            # get one class out of it.
            "mcdr": bool(entry.get("mcdr")),
        })
    return out


def timers_from(config):
    """Read `[timer.<name>]` blocks: something to run again every so often."""
    out = []
    for name, entry in (config.get("timer") or {}).items():
        if not isinstance(entry, dict) or not entry.get("command"):
            continue
        command = entry["command"]
        if isinstance(command, str):
            command = shlex.split(command)
        out.append({
            "name": name,
            "command": list(command),
            "every": parse_time(entry.get("every") or "300s") or 300,
            "cwd": entry.get("cwd") or ".",
            "env": dict(entry.get("env") or {}),
            "why": entry.get("why", ""),
        })
    return out


def storage_from(config):
    """Read `[storage.<name>]` blocks: a folder with a ceiling."""
    out = []
    for name, entry in (config.get("storage") or {}).items():
        if not isinstance(entry, dict) or not entry.get("dir"):
            continue
        out.append({
            "name": name,
            "dir": entry["dir"],
            "quota": entry.get("quota"),
            "keep": entry.get("keep"),
            "by": entry.get("by", "mtime"),
            "why": entry.get("why", ""),
        })
    return out


def mcdr_compat_path():
    """Where the importable `mcdreforged` package lives, or None.

    Beside the runtime, so a server that only downloaded `w4ve.py` does not
    have it. Saying None here is what lets the caller explain that instead of
    starting a service that will die on its first import.
    """
    folder = Path(__file__).resolve().parent / "compat"
    return folder if (folder / "mcdreforged" / "__init__.py").exists() else None
