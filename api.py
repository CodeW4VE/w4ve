"""The local API: how something that is not the CLI asks W4VE to do things.

M8, and its criterion is worth quoting because it constrains the design more
than any feature list would: *an external client can inspect, plan and run
authorised operations without touching private files; its crash or its upgrade
does not stop servers; W4VE keeps working entirely through the CLI and does not
become a panel.*

Four decisions follow from that sentence, and each one is a thing this
deliberately does not do.

**It listens on a Unix socket, not a port.** A port is reachable from the rest
of the machine and, one bad firewall rule later, from the rest of the world. A
socket is a file with an owner and a mode, which is a permission system the
operating system already implements correctly. A port can be asked for, and it
then requires a token.

**A token carries scopes, and `read` is the default.** A client that only wants
to draw a dashboard must not be able to stop a world by accident, and the way
to guarantee that is not documentation.

**Nothing here reads `state.json` on a client's behalf.** Every answer comes
from asking the guardian the same way the CLI does. A second reader of private
files is a second thing to keep in step, and it is how a panel starts turning
into the truth.

**It is optional and separable.** It runs in a thread; if it will not start,
the guardian says so and carries on. A server must never fail to boot because
something wanted to watch it.

Standard library only, Python 3.9, same rules as the rest.
"""

import json
import os
import re
import secrets
import socket
import socketserver
import stat
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

VERSION = 1

# Operator scopes are cumulative: `write` implies `read`, because
# a client allowed to stop a server and not to look at it is a client that has
# to guess. `identity` is a separate service scope, never server control.
SCOPES = ("read", "write", "admin", "identity", "game-events", "game-chat")

# Every operation, and the least scope it needs. A method that is not in here
# does not exist, which is the only way to be sure the list is the whole list.
ROUTES = {
    ("GET", "/v1/hello"): None,          # no token: it says what this is
    ("GET", "/v1/status"): "read",
    ("GET", "/v1/processes"): "read",
    ("GET", "/v1/plugins"): "read",
    ("GET", "/v1/pieces"): "read",
    ("GET", "/v1/journal"): "read",
    ("POST", "/v1/command"): "write",
    ("POST", "/v1/server/start"): "write",
    ("POST", "/v1/server/stop"): "write",
    ("POST", "/v1/plugins/reload"): "admin",
    # Jobs: for the work that answers in minutes instead of milliseconds.
    ("GET", "/v1/jobs"): "read",
    ("POST", "/v1/jobs"): "write",
    ("GET", "/v1/jobs/{id}"): "read",
    ("POST", "/v1/jobs/{id}/cancel"): "write",
    ("POST", "/v1/identity/instances"): "admin",
    ("POST", "/v1/identity/grants"): "admin",
    ("POST", "/v1/identity/block"): "admin",
    ("POST", "/v1/identity/session"): "identity",
    ("POST", "/v1/identity/inspect"): "identity",
    ("POST", "/v1/identity/authorize"): "identity",
    ("POST", "/v1/identity/health"): "identity",
    ("POST", "/v1/identity/instance-resources"): "admin",
    ("POST", "/v1/identity/instance-session"): "identity",
    ("POST", "/v1/identity/instance-inspect"): "identity",
    ("POST", "/v1/identity/instance-recipients"): "identity",
    ("POST", "/v1/game/source/status"): "game-source",
    ("POST", "/v1/game/source/append"): "game-source",
    ("POST", "/v1/game/events/poll"): "game-events",
    ("POST", "/v1/game/events/authorize-chat"): "game-chat",
}

# The two routes above with an `{id}` in them, as (method, prefix, suffix).
# Kept as a separate list so ROUTES stays the whole list of what exists: a
# path that matches nothing here is a 404 before any token is even read.
WITH_ID = [
    ("GET", "/v1/jobs/", ""),
    ("POST", "/v1/jobs/", "/cancel"),
]


def resolve(method, path):
    """Turn a request path into a key of ROUTES, plus the id it carried.

    Returns (route, job_id), and (None, None) when nothing matches.
    """
    if (method, path) in ROUTES:
        return (method, path), None
    for verb, prefix, suffix in WITH_ID:
        if verb != method or not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if suffix:
            if not rest.endswith(suffix):
                continue
            rest = rest[:-len(suffix)]
        if not rest or "/" in rest:
            continue
        return (method, prefix + "{id}" + suffix), rest
    return None, None


# A job that nobody ever polls still has to stop being remembered.
JOBS_KEPT = 50
# Nothing may be asked to wait longer than this, whatever the body says.
JOB_MAX_SECONDS = 3600
JOB_DEFAULT_SECONDS = 600
# Lines kept per job. A chatty server must not turn a job into a memory leak.
JOB_LINES_KEPT = 500


class Job:
    """Work whose answer cannot be the reply to the request that asked for it.

    A backup is the case this exists for: `!!pb make` comes back in four
    minutes, and by then the HTTP request is long gone. So the request gets an
    id, a thread follows the console, and the client polls.

    What this deliberately does NOT do is pretend it can cancel the work.
    Cancelling a job stops us listening; the backup carries on, because there
    is no way to un-ask a server for one. Saying so is the honest version.
    """

    def __init__(self, api, command, process="server", until=None,
                 timeout=JOB_DEFAULT_SECONDS):
        self.api = api
        self.id = secrets.token_hex(8)
        self.command = command
        self.process = process
        self.until = re.compile(until) if until else None
        self.until_source = until or ""
        self.timeout = timeout
        self.state = "running"
        self.error = ""
        self.matched = ""
        self.started = time.time()
        self.finished = None
        self.lines = []
        self.dropped = 0
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._cancelled = threading.Event()
        self._thread = None

    # -------------------------------------------------------------- running

    def begin(self):
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def _work(self):
        import runtime
        client = runtime.Client(self.api.root)
        listening = threading.Event()

        def follow():
            try:
                client.tail(self._line, stop=self._should_stop, ready=listening)
            except OSError as exc:
                self._end("failed", "lost the console: %s" % exc)
            finally:
                listening.set()      # never leave the starter waiting

        follower = threading.Thread(target=follow, daemon=True)
        follower.start()
        # Subscribe first, send second. The other order loses the first line of
        # the answer, which for a short command is the whole answer.
        if not listening.wait(5):
            self._end("failed", "could not follow the console")
            return
        if self._done.is_set():
            return

        answer = self.api.guardian_says({"cmd": "send", "text": self.command,
                                         "process": self.process})
        if not answer.get("ok"):
            self._end("failed", answer.get("error") or answer.get("message")
                      or "the command was refused")
            return
        for line in answer.get("lines") or []:
            self._line(self.process, line)

        if self._done.wait(self.timeout):
            return
        # Out of time. With something to wait for, that is a failure; without
        # one, the deadline WAS the plan and what we collected is the answer.
        if self.until is None:
            self._end("done", "")
        else:
            self._end("failed", "waited %ds for %s and it never came"
                      % (self.timeout, self.until_source))

    def _line(self, process, line):
        if process != self.process or self._done.is_set():
            return
        with self._lock:
            if len(self.lines) >= JOB_LINES_KEPT:
                self.lines.pop(0)
                self.dropped += 1
            self.lines.append(line)
        if self.until is not None and self.until.search(line):
            self.matched = line
            self._end("done", "")

    def _should_stop(self):
        return self._done.is_set() or self._cancelled.is_set()

    def _end(self, state, error):
        with self._lock:
            if self.finished is not None:
                return
            self.state = state
            self.error = error
            self.finished = time.time()
        self._done.set()

    def cancel(self):
        if self.finished is not None:
            return False, "this job already finished (%s)" % self.state
        self._cancelled.set()
        self._end("cancelled", "")
        return True, ("stopped listening; whatever the server was doing "
                      "carries on, because it cannot be un-asked")

    # -------------------------------------------------------------- reading

    def brief(self):
        with self._lock:
            return {"id": self.id, "state": self.state, "command": self.command,
                    "process": self.process, "started": self.started,
                    "finished": self.finished, "lines": len(self.lines),
                    "error": self.error}

    def full(self):
        with self._lock:
            out = {"id": self.id, "state": self.state, "command": self.command,
                   "process": self.process, "until": self.until_source,
                   "timeout": self.timeout, "started": self.started,
                   "finished": self.finished, "matched": self.matched,
                   "error": self.error, "lines": list(self.lines)}
        if self.dropped:
            out["dropped"] = self.dropped
        return out


class Jobs:
    """Every job this API started, newest last, and a lid on how many."""

    def __init__(self, api):
        self.api = api
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()

    def start(self, command, process, until, timeout):
        job = Job(self.api, command, process=process, until=until,
                  timeout=timeout)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._forget_old()
        job.begin()
        return job

    def _forget_old(self):
        """Drop finished jobs past the limit. A running one is never dropped."""
        while len(self._order) > JOBS_KEPT:
            for i, jid in enumerate(self._order):
                if self._jobs[jid].finished is not None:
                    self._order.pop(i)
                    self._jobs.pop(jid, None)
                    break
            else:
                return              # all of them are still running

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def listing(self):
        with self._lock:
            return [self._jobs[j].brief() for j in self._order]

    def stop_all(self):
        for job in list(self._jobs.values()):
            if job.finished is None:
                job.cancel()


# A body bigger than this is not a command, it is a mistake or an attack.
MAX_BODY = 64 * 1024


class Tokens:
    """`w4ve/api-tokens.json`, mode 600, and nothing else reads it."""

    def __init__(self, root):
        self.path = Path(root) / "w4ve" / "api-tokens.json"

    def _read(self, strict=False):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(not isinstance(entry, dict) for entry in data.values()):
                raise ValueError("invalid token registry")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if strict:
                raise ValueError("cannot safely update token registry") from None
            return {}

    @contextmanager
    def _locked(self):
        """Serialize writers across CLI/guardian processes, including Windows."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Created closed before anything goes in it: writing first and
        # chmod'ing after leaves a window where the token is world readable.
        handle = os.open(str(self.path) + ".writing",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(self.path) + ".writing", self.path)
        os.chmod(self.path, 0o600)

    def issue(self, name, scope="read"):
        if scope not in SCOPES:
            raise ValueError("scope is %r, not one of %s"
                             % (scope, ", ".join(SCOPES)))
        with self._locked():
            return self._issue(self._read(strict=True), name, scope)

    def _issue(self, data, name, scope):
        token = secrets.token_urlsafe(32)
        data[token] = {"name": name, "scope": scope,
                       "issued": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())}
        self._write(data)
        return token

    def service_identity(self, name):
        """Reuse one least-privilege token per declared service, without escalation."""
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise ValueError("invalid service name")
        owner = "service:" + name
        with self._locked():
            data = self._read(strict=True)
            found = [(token, entry) for token, entry in data.items() if entry.get("name") == owner]
            if found:
                if len(found) != 1 or found[0][1].get("scope") != "identity":
                    raise ValueError("service token registry conflicts; revoke the service token first")
                return found[0][0]
            return self._issue(data, owner, "identity")

    def existing_service(self, name, scope):
        """Read an already issued exact service grant; diagnosis never mints one."""
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
            raise ValueError('invalid service name')
        if scope not in ('identity', 'game-events', 'game-chat'):
            raise ValueError('invalid service scope')
        owner = ('service:' if scope == 'identity' else 'service-events:') + name
        found = [(token, entry) for token, entry in self._read(strict=True).items() if entry.get('name') == owner]
        if len(found) != 1 or found[0][1].get('scope') != scope:
            raise ValueError('existing service grant unavailable')
        return found[0][0]

    def revoke(self, name):
        with self._locked():
            data = self._read(strict=True)
            gone = [t for t, entry in data.items() if entry.get("name") == name]
            for token in gone:
                data.pop(token)
            if gone:
                self._write(data)
        return len(gone)

    def service_events(self, name, policy):
        """A separate, revocable reader credential; changing policy rotates it.

        No identity, producer or operator privileges are inherited. Passing
        None removes the reader grant, including at guardian reconfiguration.
        """
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise ValueError("invalid service name")
        if policy not in (None, "presence", "chat"):
            raise ValueError("w4ve_events must be presence or chat")
        owner = "service-events:" + name
        scope = {"presence": "game-events", "chat": "game-chat"}.get(policy)
        with self._locked():
            data = self._read(strict=True)
            found = [(token, entry) for token, entry in data.items() if entry.get("name") == owner]
            if (len(found) > 1 or any(entry.get("scope") not in ("game-events", "game-chat") for _, entry in found)):
                raise ValueError("service event token registry conflicts; revoke it first")
            if scope and found and found[0][1].get("scope") == scope:
                return found[0][0]
            for token, _ in found:
                del data[token]
            if scope:
                return self._issue(data, owner, scope)
            if found:
                self._write(data)
            return ""

    def listing(self):
        """Names and scopes. **Never the tokens themselves.**"""
        return sorted(({"name": e.get("name"), "scope": e.get("scope"),
                        "issued": e.get("issued")}
                       for e in self._read().values()),
                      key=lambda e: e["name"] or "")

    def check(self, token):
        """The entry for this token, or None. Constant time on purpose."""
        if not token:
            return None
        for known, entry in self._read().items():
            # `compare_digest` so a wrong token cannot be found one character
            # at a time by measuring how long the answer takes.
            if secrets.compare_digest(known, token):
                return entry
        return None

    def insecure(self):
        if not self.path.exists():
            return False
        return bool(stat.S_IMODE(self.path.stat().st_mode) & 0o077)

    def event_grant_active(self, entry):
        """Recheck managed reader declarations, even after a hand edit/removal.

        Operator-issued tokens are explicit grants in the registry itself.
        Managed service tokens additionally require their current declaration.
        Never cache this across requests or rely on the guardian's old config.
        """
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.startswith("service-events:"):
            return True
        try:
            from w4ve import read_toml
            config = read_toml(self.path.parent.parent / "w4ve.toml")
            service = (config.get("services") or {}).get(name[len("service-events:"):]) or {}
            policy = service.get('w4ve_events')
            if service.get('w4ve_events_when') and not service_condition(self.path.parent.parent, config,
                    name[len('service-events:'):], service['w4ve_events_when']):
                policy = None
            expected = {"presence": "game-events", "chat": "game-chat"}.get(policy)
            return expected is not None and expected == entry.get("scope")
        except (OSError, ValueError, TypeError, AttributeError):
            return False


def service_condition(root, config, name, condition):
    if not isinstance(condition, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', condition):
        raise ValueError('invalid service condition')
    from w4ve import Server, owner_of
    owner = owner_of(Server(root), 'services', name)
    if not owner:
        raise ValueError('conditional grants require an installed service owner')
    return ((config.get('settings') or {}).get(owner) or {}).get(condition) is True


def service_environment(root, config, name, *, env=None, w4ve_api=None, w4ve_events=None, game_dir=None, issue_tokens=True):
    """Same declared credentials/SDK for a supervised service or an operator's
    one-shot maintenance command. Never allocate operator scopes to a service.
    """
    root, env = Path(root), dict(env or {})
    if w4ve_api not in (None, 'identity') or w4ve_events not in (None, 'presence', 'chat'):
        raise ValueError('invalid service API grant')
    raw = ((config.get('services') or {}).get(name) or {})
    declared_events = w4ve_events
    if w4ve_events and raw.get('w4ve_events_when') and not service_condition(root, config, name, raw['w4ve_events_when']):
        w4ve_events = None
    for variable, condition in (raw.get('env_when') or {}).items():
        if not service_condition(root, config, name, condition):
            env.pop(variable, None)
    if (w4ve_api or w4ve_events) and (config.get('api') or {}).get('enabled') is False:
        raise ValueError('service requires the local API, which is explicitly disabled')
    if w4ve_api or w4ve_events:
        sdk = Path(__file__).resolve().parent / 'sdk/client'
        if not (sdk / 'w4ve_client.py').is_file():
            raise ValueError('W4VE client SDK is missing from this runtime')
        existing = env.get('PYTHONPATH') or os.environ.get('PYTHONPATH')
        env['PYTHONPATH'] = str(sdk) + (os.pathsep + existing if existing else '')
        env['W4VE_SERVER_ROOT'] = str(root)
    # Ordinary services still work with an absent/broken token registry.
    tokens = Tokens(root)
    if issue_tokens and declared_events and not w4ve_events:
        tokens.service_events(name, None)
    env['W4VE_EVENTS_TOKEN'] = ((tokens.service_events(name, w4ve_events) if issue_tokens else
        tokens.existing_service(name, {'presence': 'game-events', 'chat': 'game-chat'}[w4ve_events])) if w4ve_events else '')
    env['W4VE_EVENTS_ADDRESS'] = str((config.get('api') or {}).get('listen') or '')
    if w4ve_api:
        env['W4VE_IDENTITY_TOKEN'] = tokens.service_identity(name) if issue_tokens else tokens.existing_service(name, 'identity')
        game_dir = Path(game_dir) if game_dir is not None else root / (config.get('server') or {}).get('game_dir', '.')
        env['W4VE_GAME_DIR'] = str(game_dir.resolve())
        env['W4VE_IDENTITY_ADDRESS'] = str((config.get('api') or {}).get('listen') or '')
    return env


def allowed(scope, needed):
    """Is `scope` enough for something that needs `needed`?"""
    if needed is None:
        return True
    if scope not in SCOPES or needed not in SCOPES:
        return False
    if scope == "admin":
        return True
    if needed == "game-events" and scope == "game-chat":
        return True
    if scope in ("identity", "game-events", "game-chat") or needed in ("identity", "game-events", "game-chat"):
        return scope == needed
    hierarchy = ("read", "write", "admin")
    return hierarchy.index(scope) >= hierarchy.index(needed)


class Handler(BaseHTTPRequestHandler):
    """One request. Everything it can do is in ROUTES and nowhere else."""

    server_version = "w4ve/%d" % VERSION
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        """Quiet. The guardian's journal is the log, not stderr."""

    def handle_one_request(self):
        """Same as the parent, minus the traceback when a client hangs up.

        A client that closes mid-answer is normal (a dashboard refreshing, a
        `curl` interrupted), and the default handler prints a BrokenPipeError
        traceback for each one. Pages of stack trace for something that is not
        a problem is how a log stops being read.
        """
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message, **extra):
        answer = {"ok": False, "error": message}
        answer.update(extra)
        self._send(code, answer)

    def _body(self, limit=MAX_BODY):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > limit:
            raise ValueError("body is bigger than %d bytes" % limit)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("the body is not JSON")

    def _token(self):
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return self.headers.get("X-W4VE-Token", "").strip()

    # ------------------------------------------------------------ dispatch

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        route, job_id = resolve(method, path)
        if route is None:
            self._fail(404, "no such thing: %s %s" % (method, path))
            return
        needed = ROUTES[route]
        if needed == "game-source":
            self._game_source(route)
            return
        scope = "admin"
        if needed is not None and (self.server.api.tokens_required or path.startswith(("/v1/identity/", "/v1/game/"))):
            entry = self.server.api.tokens.check(self._token())
            if entry is None:
                # 401 and not 403: the difference is "who are you" versus "you
                # may not", and a client can only act on the first one.
                self._fail(401, "no valid token")
                return
            scope = entry.get("scope", "read")
            if path.startswith("/v1/game/events/") and not self.server.api.tokens.event_grant_active(entry):
                self._fail(403, "service event grant is no longer active")
                return
            if not allowed(scope, needed):
                self._fail(403, "this token is %s and that needs %s"
                           % (scope, needed), needed=needed, scope=scope)
                return
        try:
            body = self._body() if method == "POST" else {}
        except ValueError as exc:
            self._fail(400, str(exc))
            return
        if path == "/v1/game/events/poll" and isinstance(body, dict) and body.get("chat") is True:
            if not allowed(scope, "game-chat"):
                self._fail(403, "reading game chat requires a separate chat grant")
                return
        try:
            code, payload = self.server.api.run(route, body, job_id)
        except Exception as exc:            # noqa: BLE001
            # A bug in here must never be a bug in the guardian. It becomes a
            # 500 with the reason, and the server keeps running.
            self._fail(500, "%s: %s" % (type(exc).__name__, exc))
            return
        self._send(code, payload)

    def _game_source(self, route):
        # Source credentials are not operator/API tokens. Even admin cannot
        # fabricate a boot by presenting its token to this ingress.
        from gameevents import SourceDenied
        self.close_connection = True
        journal = self.server.api.game_events
        if journal is None:
            self._fail(503, "game event source is disabled")
            return
        if self.client_address[0] not in ("local", "127.0.0.1", "::1"):
            self._fail(403, "game source requires a local connection")
            return
        try:
            source = journal.authenticate(self._token())
        except SourceDenied:
            self._fail(401, "game source unavailable")
            return
        if not self.server.api._game_slots.acquire(blocking=False):
            self._fail(429, "game ingress is busy; retry the same event")
            return
        try:
            self.connection.settimeout(3)
            if self.headers.get("Transfer-Encoding"):
                self._fail(400, "game ingress requires a bounded Content-Length")
                return
            try:
                body = self._body(limit=16384)
            except (ValueError, RecursionError):
                self._fail(400, "invalid game ingress body")
                return
            code, payload = self.server.api.run_game_source(route, body, source)
            self._send(code, payload)
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            # Never include producer content or credentials in diagnostics.
            self._fail(500, "game ingress unavailable")
        finally:
            self.server.api._game_slots.release()


if hasattr(socketserver, "UnixStreamServer"):
    class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

        def get_request(self):
            request, _client = super().get_request()
            return request, ("local", 0)
else:
    class _UnixServer:
        def __init__(self, *args, **kwargs):
            raise OSError("Unix API sockets are unavailable here; use loopback TCP with scoped tokens")
class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Api:
    """The API of one server. Started by the guardian, or by nothing at all."""

    def __init__(self, root, guardian=None, address=None, tokens_required=None, game_events=None):
        self.root = Path(root)
        self.guardian = guardian
        self.tokens = Tokens(self.root)
        self.address = address           # "127.0.0.1:8790", or None for Unix
        # A Unix socket is already protected by its file mode, so a token is
        # optional there and required the moment it listens on a port.
        self.tokens_required = (bool(address) if tokens_required is None
                                else tokens_required)
        self.httpd = None
        self.thread = None
        self.socket_path = self.root / "w4ve" / "run" / "api.sock"
        self.socket_identity = None
        # Long work lives here and nowhere else: jobs are in memory on
        # purpose. Surviving a restart would mean a second place where the
        # truth about a server is written down, and there is already one.
        self.jobs = Jobs(self)
        self._authority = None
        self._authority_lock = threading.Lock()
        self.game_events = game_events
        self._game_slots = threading.BoundedSemaphore(4)

    # ------------------------------------------------------------- lifetime

    def start(self):
        try:
            if self.address:
                host, _, port = self.address.partition(":")
                self.httpd = _TcpServer((host or "127.0.0.1", int(port or 8790)),
                                        Handler)
            else:
                self.socket_path.parent.mkdir(parents=True, exist_ok=True)
                if self.socket_path.exists():
                    self.socket_path.unlink()
                self.httpd = _UnixServer(str(self.socket_path), Handler)
                # Only this user. The file mode is the whole authentication
                # story for a Unix socket, so it is set explicitly rather than
                # left to whatever umask happens to be in force.
                os.chmod(self.socket_path, 0o600)
                info = self.socket_path.stat()
                self.socket_identity = (info.st_dev, info.st_ino)
        except OSError as exc:
            return False, "the API could not listen: %s" % exc
        self.httpd.api = self
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True, name="w4ve-api")
        self.thread.start()
        where = self.address or str(self.socket_path)
        return True, "api on %s%s" % (where, "" if self.tokens_required
                                      else " (no token needed: local socket)")

    def stop(self):
        # Jobs first: a follower thread holding a socket to a guardian that is
        # going away is how a shutdown hangs.
        self.jobs.stop_all()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if not self.address and self.socket_path.exists():
            try:
                info = self.socket_path.stat()
                if (info.st_dev, info.st_ino) == self.socket_identity:
                    self.socket_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ answering

    def run_game_source(self, route, body, source):
        from gameevents import SourceDenied, ChatDisabled, EventConflict, Backpressure
        try:
            if self.guardian and getattr(self.guardian, "game_bridge", None):
                self.guardian.game_bridge.reconcile()
            if route[1] == "/v1/game/source/status":
                if body != {}:
                    raise ValueError("invalid source query")
                return 200, {"ok": True, **self.game_events.source_state(source)}
            return 200, {"ok": True, **self.game_events.append(source, body)}
        except ChatDisabled:
            return 403, {"ok": False, "error": "game chat journal is disabled"}
        except SourceDenied:
            return 401, {"ok": False, "error": "game source unavailable"}
        except EventConflict:
            return 409, {"ok": False, "error": "game event requires reconciliation"}
        except Backpressure:
            return 429, {"ok": False, "error": "game journal is full; retry the same event"}
        except (ValueError, TypeError, KeyError, RecursionError):
            return 400, {"ok": False, "error": "invalid game event"}

    def _game_poll(self, body):
        if self.game_events is None:
            return 503, {"ok": False, "error": "game event source is disabled"}
        if not isinstance(body, dict) or not body.keys() <= {"cursor", "epoch", "limit", "chat"}:
            return 400, {"ok": False, "error": "invalid game event query"}
        from gameevents import SourceDenied
        try:
            if self.guardian and getattr(self.guardian, "game_bridge", None):
                self.guardian.game_bridge.reconcile()
            return 200, {"ok": True, **self.game_events.poll(**body)}
        except SourceDenied:
            return 403, {"ok": False, "error": "game chat journal is disabled"}
        except (ValueError, TypeError):
            return 400, {"ok": False, "error": "invalid game event query"}

    def _identity(self, path, body):
        from identity import Authority, IdentityDenied
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "identity request must be an object"}
        with self._authority_lock:
            if self._authority is None:
                self._authority = Authority(self.root / "w4ve" / "identity" / "authority.sqlite3")
        authority = self._authority
        try:
            if path == "/v1/identity/health":
                # Version the guarantees, not the product release. Clients may
                # require these without guessing what a 0.x build implements.
                result = {"service": "identity", "schema": 1, "capabilities": {
                    "player_proof": 1, "instance_sessions": 1,
                    "connection_context": 1, "resource_recipients": 1,
                    "policy_revision": 1}}
            elif path == "/v1/identity/instances":
                result = authority.register_instance(body["community"], body["instance"],
                                                     body.get("secret"), body.get("enabled", True))
            elif path == "/v1/identity/grants":
                authority.set_grants(body["community"], body["audience"], body["resource"],
                                     body["subject"], body["operations"])
                result = {}
            elif path == "/v1/identity/block":
                authority.set_blocked(body["community"], body["subject"], body["blocked"])
                result = {}
            elif path == "/v1/identity/session":
                result = authority.create_session(body["proof"], body["audience"])
            elif path == "/v1/identity/inspect":
                result = authority.inspect(body["session"], body["audience"])
            elif path == "/v1/identity/authorize":
                result = authority.authorize(body["session"], body["audience"], body["resource"], body["operation"])
            elif path == "/v1/identity/instance-resources":
                authority.set_instance_resources(body["community"], body["instance"], body["audience"], body["resources"])
                result = {}
            elif path == "/v1/identity/instance-session":
                result = authority.create_instance_session(body["proof"], body["audience"])
            elif path == "/v1/identity/instance-inspect":
                result = authority.inspect_instance(body["session"], body["audience"])
            elif path == "/v1/identity/instance-recipients":
                result = authority.instance_recipients(body["session"], body["audience"], body["resource"], body["operation"], body["players"])
            else:
                return 404, {"ok": False, "error": "unknown identity operation"}
            return 200, {"ok": True, **result}
        except IdentityDenied:
            return 401, {"ok": False, "error": "identity unavailable"}
        except (ValueError, KeyError, TypeError):
            return 400, {"ok": False, "error": "invalid identity request"}

    def _captured_chat(self, body):
        if self.game_events is None:
            return 503, {"ok": False, "error": "game event source is disabled"}
        if not isinstance(body, dict) or set(body) != {"epoch", "boot", "id", "owner", "resource"}:
            return 400, {"ok": False, "error": "invalid capture reference"}
        from gameevents import SourceDenied
        from identity import Authority, IdentityDenied
        try:
            captured = self.game_events.captured_chat(body["epoch"], body["boot"], body["id"])
            with self._authority_lock:
                if self._authority is None:
                    self._authority = Authority(self.root / "w4ve/identity/authority.sqlite3")
            claims = self._authority.authorize_captured_chat(captured, body["owner"], body["resource"])
            return 200, {"ok": True, **claims, "event": captured["event"]}
        except (SourceDenied, IdentityDenied):
            return 403, {"ok": False, "error": "capture unavailable"}
        except (ValueError, TypeError, KeyError):
            return 400, {"ok": False, "error": "invalid capture reference"}

    def run(self, route, body, job_id=None):
        method, path = route
        if path == "/v1/game/events/authorize-chat":
            return self._captured_chat(body)
        if path == "/v1/game/events/poll":
            return self._game_poll(body)
        if path.startswith("/v1/identity/"):
            return self._identity(path, body)
        if path == "/v1/hello":
            return 200, {"ok": True, "software": "w4ve", "api": VERSION,
                         "root": str(self.root),
                         "tokens_required": self.tokens_required,
                         "routes": sorted("%s %s" % r for r in ROUTES)}
        if path == "/v1/status":
            return 200, {"ok": True, **self._status()}
        if path == "/v1/processes":
            return 200, {"ok": True,
                         "processes": self._status().get("processes", [])}
        if path == "/v1/plugins":
            status = self._status()
            return 200, {"ok": True, "mcdr": status.get("plugins"),
                         "native": status.get("workers")}
        if path == "/v1/pieces":
            return 200, {"ok": True, "pieces": self._pieces()}
        if path == "/v1/journal":
            return 200, {"ok": True, "lines": self._journal()}
        if path == "/v1/command":
            return self._command(body)
        if path == "/v1/server/start":
            return self._process(body, "start")
        if path == "/v1/server/stop":
            return self._process(body, "stop")
        if path == "/v1/plugins/reload":
            return self._reload(body)
        if path == "/v1/jobs":
            if method == "GET":
                return 200, {"ok": True, "jobs": self.jobs.listing()}
            return self._start_job(body)
        if path == "/v1/jobs/{id}":
            job = self.jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": "no job called %r" % job_id}
            return 200, {"ok": True, **job.full()}
        if path == "/v1/jobs/{id}/cancel":
            job = self.jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": "no job called %r" % job_id}
            ok, message = job.cancel()
            # The reason goes in `error` when it is one: a client reads that
            # field to find out what happened, and a 409 with nothing in it is
            # a "no" with no explanation.
            return (200 if ok else 409), {"ok": ok, "message": message,
                                          "error": "" if ok else message,
                                          "state": job.state}
        return 404, {"ok": False, "error": "no such thing"}

    def _start_job(self, body):
        """Ask for something long. The id comes back now, the answer later."""
        command = str(body.get("command", "")).strip()
        if not command:
            return 400, {"ok": False, "error": "no command"}
        until = body.get("until") or ""
        if until:
            try:
                re.compile(until)
            except re.error as exc:
                return 400, {"ok": False,
                             "error": "`until` is not a regular expression: %s" % exc}
        try:
            timeout = float(body.get("timeout") or JOB_DEFAULT_SECONDS)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "`timeout` is not a number"}
        if timeout <= 0:
            return 400, {"ok": False, "error": "`timeout` must be more than zero"}
        # Capped rather than refused: a client asking for a day is not wrong,
        # it just cannot have one.
        timeout = min(timeout, JOB_MAX_SECONDS)
        job = self.jobs.start(command, str(body.get("process", "server")),
                              until, timeout)
        # 202: taken, not done. The whole point of the route is that the
        # answer is not ready and pretending otherwise with a 200 would be a
        # lie a client cannot see through.
        return 202, {"ok": True, "id": job.id, "state": job.state,
                     "timeout": timeout,
                     "poll": "/v1/jobs/%s" % job.id}

    # ---------------------------------------------------------------- doing

    def guardian_says(self, request):
        """Public name of `_guardian_says`, for the jobs that live out here."""
        return self._guardian_says(request)

    def _guardian_says(self, request):
        """Ask the guardian, exactly as the CLI does.

        In-process when the API runs inside it, over the control socket when
        it does not. Either way the answer comes from the guardian and not
        from a file this reads behind its back.
        """
        if self.guardian is not None:
            return self.guardian.handle(request)
        import runtime
        return runtime.Client(self.root).call(request)

    def _status(self):
        answer = self._guardian_says({"cmd": "status"})
        if not answer.get("ok"):
            return {"running": False, "processes": [],
                    "error": answer.get("error", "no guardian is running")}
        return answer

    def _journal(self, lines=50):
        path = self.root / "w4ve" / "run" / "journal.log"
        try:
            return path.read_text(encoding="utf-8").splitlines()[-lines:]
        except OSError:
            return []

    def _pieces(self):
        """What is installed. Read-only, and from the generated state.

        The one file this does read, because it is the answer to the question
        and there is no process to ask: `state.json` is generated, not private.
        """
        try:
            data = json.loads((self.root / "w4ve" / "state.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [{"id": pid, "version": entry.get("version"),
                 "source": entry.get("source"),
                 "pending_restart": bool(entry.get("pending_restart"))}
                for pid, entry in sorted((data.get("pieces") or {}).items())]

    def _command(self, body):
        text = str(body.get("command", "")).strip()
        if not text:
            return 400, {"ok": False, "error": "no command"}
        answer = self._guardian_says({"cmd": "send", "text": text,
                                      "process": body.get("process", "server")})
        return (200 if answer.get("ok") else 409), {
            "ok": bool(answer.get("ok")),
            "message": answer.get("message", ""),
            "lines": answer.get("lines", []),
            "error": answer.get("error", ""),
        }

    def _process(self, body, what):
        answer = self._guardian_says({"cmd": what,
                                      "process": body.get("process", "server")})
        return (200 if answer.get("ok") else 409), {
            "ok": bool(answer.get("ok")),
            "message": answer.get("message", ""),
            "error": answer.get("error", ""),
        }

    def _reload(self, body):
        plugin = str(body.get("plugin", "")).strip()
        if not plugin:
            return 400, {"ok": False, "error": "no plugin named"}
        if self.guardian is not None and getattr(self.guardian, "workers", None):
            ok, message = self.guardian.workers.reload(plugin)
            if ok or "no plugin called" not in message:
                return (200 if ok else 409), {"ok": ok, "message": message}
        answer = self._guardian_says({"cmd": "send",
                                      "text": "!!w4ve plugin reload %s" % plugin})
        return (200 if answer.get("ok") else 409), {
            "ok": bool(answer.get("ok")),
            "message": answer.get("message", ""),
            "lines": answer.get("lines", []),
        }
