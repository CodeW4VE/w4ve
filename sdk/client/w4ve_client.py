"""Talking to W4VE from something that is not W4VE.

One file, standard library only, no install. It exists so an integrator does
not have to learn that HTTP over a Unix socket needs a custom connection class,
which is the only genuinely awkward part of this API and has nothing to do with
what they came here to do.

    from w4ve_client import W4VE

    w4ve = W4VE("/path/to/server")        # the local socket
    print(w4ve.status()["processes"])
    w4ve.command("say hello")

Or against a port, which needs a token:

    w4ve = W4VE(address="127.0.0.1:8790", token="...")

Everything raises `W4VEError` with the reason rather than returning something
that has to be checked, because a client that forgets to check is a client that
reports success while nothing happened.
"""

import http.client
import json
import socket
import time
from pathlib import Path

__all__ = ["W4VE", "W4VEError", "Denied", "NotRunning"]


class W4VEError(Exception):
    """Something went wrong, and the message says what."""


class Denied(W4VEError):
    """The token is missing, wrong, or not allowed to do that."""


class NotRunning(W4VEError):
    """There is no guardian to ask. Different from being refused."""


class _UnixConnection(http.client.HTTPConnection):
    """HTTP over a Unix socket. The one awkward bit, hidden here."""

    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class W4VE:
    """One W4VE, local or over a port."""

    def __init__(self, root=None, address=None, token="", timeout=30):
        if not root and not address:
            raise W4VEError("say where: a server folder, or an address")
        self.root = Path(root).resolve() if root else None
        self.address = address
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing

    def _connect(self):
        if self.address:
            host, _, port = self.address.partition(":")
            return http.client.HTTPConnection(host or "127.0.0.1",
                                              int(port or 8790),
                                              timeout=self.timeout)
        path = self.root / "w4ve" / "run" / "api.sock"
        if not path.exists():
            raise NotRunning("no API at %s: is `w4ve run` going, and is the "
                             "api turned on in w4ve.toml?" % path)
        return _UnixConnection(str(path), timeout=self.timeout)

    def call(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = self._connect()
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            code = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise NotRunning("could not reach W4VE: %s" % exc)
        finally:
            connection.close()
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise W4VEError("W4VE answered something that is not JSON")
        if code in (401, 403):
            raise Denied(answer.get("error", "not allowed"))
        if code >= 400 or not answer.get("ok", True):
            raise W4VEError(answer.get("error") or "W4VE said no")
        return answer

    # --------------------------------------------------------------- asking

    def identity_health(self):
        """Probe the identity service using its scoped service credential."""
        return self.call("POST", "/v1/identity/health", {})

    def hello(self):
        """What this is and what it can do. The only route with no token."""
        return self.call("GET", "/v1/hello")

    def status(self):
        return self.call("GET", "/v1/status")

    def processes(self):
        return self.call("GET", "/v1/processes")["processes"]

    def plugins(self):
        return self.call("GET", "/v1/plugins")

    def pieces(self):
        return self.call("GET", "/v1/pieces")["pieces"]

    def journal(self):
        return self.call("GET", "/v1/journal")["lines"]

    # --------------------------------------------------------------- doing

    def command(self, text, process="server"):
        """Run something on the console. Returns whatever came back."""
        return self.call("POST", "/v1/command",
                         {"command": text, "process": process})

    def start(self, process="server"):
        return self.call("POST", "/v1/server/start", {"process": process})

    def stop(self, process="server"):
        return self.call("POST", "/v1/server/stop", {"process": process})

    def reload_plugin(self, plugin):
        return self.call("POST", "/v1/plugins/reload", {"plugin": plugin})

    # ----------------------------------------------------------------- jobs

    def start_job(self, command, process="server", until=None, timeout=None):
        """Ask for something that takes minutes. Comes back with an id, now.

        `until` is a regular expression: the first console line that matches
        it ends the job. Without one, the job collects lines until `timeout`
        and that is the answer.
        """
        body = {"command": command, "process": process}
        if until:
            body["until"] = until
        if timeout:
            body["timeout"] = timeout
        return self.call("POST", "/v1/jobs", body)

    def job(self, job_id):
        """How that job is going, and every line it has collected."""
        return self.call("GET", "/v1/jobs/" + str(job_id))

    def jobs(self):
        return self.call("GET", "/v1/jobs")["jobs"]

    def cancel_job(self, job_id):
        """Stop listening. The server carries on: it cannot be un-asked."""
        return self.call("POST", "/v1/jobs/%s/cancel" % job_id)

    def wait_for_job(self, job_id, every=1.0, timeout=None):
        """Poll until it finishes, and hand back the finished job.

        A convenience for scripts. Anything with a screen to draw should poll
        on its own clock instead of blocking in here.
        """
        started = time.time()
        while True:
            answer = self.job(job_id)
            if answer.get("state") != "running":
                return answer
            if timeout is not None and time.time() - started > timeout:
                raise W4VEError("job %s is still running after %ds"
                                % (job_id, timeout))
            time.sleep(every)

    # ---------------------------------------------------------- player identity

    def identity_session(self, proof, audience):
        return self.call("POST", "/v1/identity/session", {"proof": proof, "audience": audience})

    def identity_inspect(self, session, audience):
        return self.call("POST", "/v1/identity/inspect", {"session": session, "audience": audience})

    def identity_authorize(self, session, audience, resource, operation):
        return self.call("POST", "/v1/identity/authorize",
                         {"session": session, "audience": audience, "resource": resource, "operation": operation})

    # ------------------------------------------------------------ shortcuts

    def running(self):
        """Is the server up? A convenience, and the question people ask."""
        try:
            for process in self.processes():
                if process.get("name") == "server":
                    return process.get("state") == "running"
        except NotRunning:
            return False
        return False
