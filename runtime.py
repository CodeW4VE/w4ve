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

# Three deaths this close together and we stop trying. Retrying forever against
# a corrupt world is how an incident becomes data loss.
CRASH_WINDOW = 600
CRASH_LIMIT = 3

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
                 health_every=30, watches=()):
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
        self.asked_to_come_back = False

        self.proc = None
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
            self.proc = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                start_new_session=True,   # its own process group, on purpose
            )
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

    def send(self, text):
        if not alive(self.pid, self.starttime):
            return False, "%s is not running" % self.name
        if not self.owned or not self.proc or not self.proc.stdin:
            # No stdin of ours, which is the normal state for a server we only
            # adopted. RFC-0001 says the fallback is RCON, so use it instead of
            # telling the caller to go and do it themselves.
            return self.send_over_rcon(text)
        try:
            self.proc.stdin.write(text.rstrip("\n") + "\n")
            self.proc.stdin.flush()
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

        if self.owned:
            self.send(self.handler.stop_command() if self.handler else "stop")
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
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                if console.tell() > CONSOLE_CAP:
                    console.close()
                    self.console_path.replace(
                        self.console_path.with_suffix(".log.1"))
                    console = open(str(self.console_path), "a", buffering=1)
                console.write(line + "\n")
                self.guardian.broadcast(self.name, line)
                self._check_watches(line)
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

    def _poll_health(self):
        import urllib.error
        import urllib.request

        # A service is allowed to take a moment to bind its port. Calling it
        # unhealthy while it is still starting is noise, so the first few
        # failures only mean "not yet".
        tries = 0
        while alive(self.pid, self.starttime) and not self.guardian.stopping.is_set():
            tries += 1
            try:
                with urllib.request.urlopen(self.health, timeout=5) as response:
                    healthy = 200 <= response.status < 400
            except (urllib.error.URLError, OSError, ValueError):
                healthy = False
            if not healthy and self.ready is None and tries < 6:
                time.sleep(2)
                continue
            if healthy != self.ready:
                self.ready = healthy
                self.guardian.log("%s: health %s (%s)"
                                  % (self.name, "ok" if healthy else "failing",
                                     self.health))
                self.guardian.snapshot()
            # Checks come quickly until the first success, because a service
            # that is up in a second should not read as unknown for half a
            # minute.
            time.sleep(self.health_every if self.ready else 2)

    def _reap(self):
        code = self.proc.wait()
        self.exit_code = code
        self.ready = False
        if self.asked_to_come_back:
            self.asked_to_come_back = False
            self._become("stopped", "exit %d, a piece asked it to come back" % code)
            time.sleep(1)
            ok, message = self.start()
            self.guardian.log(message)
        elif self.asked_to_stop or self.state in ("stopping", "killed"):
            self._become("stopped", "exit %d" % code)
        else:
            self._note_death()
            self._become("crashed", "exit %d, nobody asked for that" % code)
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
        self.listeners = []
        self.listeners_lock = threading.Lock()
        self.echo = False           # print the console here, for `w4ve run`
        self.stopping = threading.Event()

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

        This file is a cache, never the truth. The truth is /proc, and every
        read of this file verifies (pid, starttime) before believing a word.
        """
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
        tmp = self.state_path.with_suffix(".json.tmp")
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
                    health=None, health_every=30):
        proc = Watched(name, command, cwd, self, handler=None, restart=restart,
                       health=health, health_every=health_every)
        self.watched[name] = proc
        return proc

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
            if proc is None:
                proc = self.add_service(name, entry.get("command") or [],
                                        entry.get("cwd") or str(self.root),
                                        restart="never")
            proc.adopt(pid, starttime, entry.get("started_at"))
            found.append(name)
        if found:
            self.log("recovered: %s" % ", ".join(found))
        return found

    # ----------------------------------------------------------- control sock

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
            return {"ok": True, "root": str(self.root),
                    "processes": [p.status() for p in self.watched.values()]}
        if proc is None:
            return {"ok": False, "error": "no process called %r" % name}
        if cmd == "start":
            ok, message = proc.start()
            return {"ok": ok, "message": message}
        if cmd == "stop":
            ok, message = proc.stop(request.get("grace"))
            return {"ok": ok, "message": message}
        if cmd == "send":
            ok, message = proc.send(request.get("text", ""))
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
        if stop_processes:
            for proc in list(self.watched.values()):
                if alive(proc.pid, proc.starttime):
                    proc.stop()
        self.snapshot()
        self.stopping.set()

    def run(self, start_now=True):
        """Foreground on purpose: inside a container the runtime IS the process."""
        got_it, holder = self.claim()
        if not got_it:
            return 1, "another guardian holds this server (pid %s)" % (holder or "?")

        def bye(signum, _frame):
            self.log("got %s" % signal.Signals(signum).name)
            self.shutdown()

        signal.signal(signal.SIGINT, bye)
        signal.signal(signal.SIGTERM, bye)

        self.log("guardian up, pid %d, root %s" % (os.getpid(), self.root))
        recovered = self.recover()
        thread = threading.Thread(target=self.serve, daemon=True)
        thread.start()

        if start_now:
            for name, proc in self.watched.items():
                if name in recovered:
                    self.log("%s was already running, not starting a second one"
                             % name)
                    continue
                # Services come up with the guardian: the whole point of a
                # piece declaring one is that nobody has to remember it.
                ok, message = proc.start()
                self.log(message)

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

    def tail(self, on_line, timeout=None):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(self.sock_path))
        conn.sendall(b'{"cmd": "tail"}\n')
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(raw.decode())
                    except ValueError:
                        continue
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
        out.append({
            "name": name,
            "command": list(command),
            "cwd": entry.get("cwd") or ".",
            "restart": entry.get("restart") or "on-failure",
            "health": entry.get("health_url") or None,
            "health_every": int(entry.get("health_every") or 30),
        })
    return out
