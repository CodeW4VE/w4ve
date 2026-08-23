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
import secrets
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

VERSION = 1

# What a token may do. Cumulative on purpose: `write` implies `read`, because
# a client allowed to stop a server and not to look at it is a client that has
# to guess.
SCOPES = ("read", "write", "admin")

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
}

# A body bigger than this is not a command, it is a mistake or an attack.
MAX_BODY = 64 * 1024


class Tokens:
    """`w4ve/api-tokens.json`, mode 600, and nothing else reads it."""

    def __init__(self, root):
        self.path = Path(root) / "w4ve" / "api-tokens.json"

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

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
        token = secrets.token_urlsafe(32)
        data = self._read()
        data[token] = {"name": name, "scope": scope,
                       "issued": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())}
        self._write(data)
        return token

    def revoke(self, name):
        data = self._read()
        gone = [t for t, entry in data.items() if entry.get("name") == name]
        for token in gone:
            data.pop(token)
        if gone:
            self._write(data)
        return len(gone)

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


def allowed(scope, needed):
    """Is `scope` enough for something that needs `needed`?"""
    if needed is None:
        return True
    if scope not in SCOPES or needed not in SCOPES:
        return False
    return SCOPES.index(scope) >= SCOPES.index(needed)


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

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body is bigger than %d bytes" % MAX_BODY)
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
        route = (method, path)
        if route not in ROUTES:
            self._fail(404, "no such thing: %s %s" % (method, path))
            return
        needed = ROUTES[route]
        scope = "admin"
        if needed is not None and self.server.api.tokens_required:
            entry = self.server.api.tokens.check(self._token())
            if entry is None:
                # 401 and not 403: the difference is "who are you" versus "you
                # may not", and a client can only act on the first one.
                self._fail(401, "no valid token")
                return
            scope = entry.get("scope", "read")
            if not allowed(scope, needed):
                self._fail(403, "this token is %s and that needs %s"
                           % (scope, needed), needed=needed, scope=scope)
                return
        try:
            body = self._body() if method == "POST" else {}
        except ValueError as exc:
            self._fail(400, str(exc))
            return
        try:
            code, payload = self.server.api.run(route, body)
        except Exception as exc:            # noqa: BLE001
            # A bug in here must never be a bug in the guardian. It becomes a
            # 500 with the reason, and the server keeps running.
            self._fail(500, "%s: %s" % (type(exc).__name__, exc))
            return
        self._send(code, payload)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        request, _client = super().get_request()
        # BaseHTTPRequestHandler wants an address; a Unix socket has none.
        return request, ("local", 0)


class _TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Api:
    """The API of one server. Started by the guardian, or by nothing at all."""

    def __init__(self, root, guardian=None, address=None, tokens_required=None):
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
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if not self.address and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ answering

    def run(self, route, body):
        method, path = route
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
        return 404, {"ok": False, "error": "no such thing"}

    # ---------------------------------------------------------------- doing

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
