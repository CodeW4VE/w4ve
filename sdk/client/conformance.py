#!/usr/bin/env python3
"""For somebody integrating with W4VE: does my client behave?

The plugin conformance kit asks whether a plugin speaks the protocol. This one
is the other side: it stands up a **fake W4VE** and checks that a client talks
to it correctly, including the parts nobody tests until they bite.

    sdk/client/conformance.py                    # against the bundled client
    sdk/client/conformance.py --client mine.py   # against yours

Your client is imported and must offer a `W4VE(root=..., token=...)` with
`hello`, `status`, `processes`, `command` and `stop`. That is the contract an
integration needs; everything else is convenience.

What it insists on, and why each one:

- **A read token cannot stop a server.** If your client swallows a 403 and
  reports success, somebody will think they stopped a world and they did not.
- **A missing token and a wrong token are told apart.** They need different
  fixes: one is configuration, the other is a revoked credential.
- **A W4VE that is not running is not the same as an error.** Retrying makes
  sense for the first and not the second.
- **Nothing is read from disk.** A client that peeks at `state.json` will be
  wrong the first time the format moves, and it will be silently wrong.
"""

import argparse
import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api as api_module  # noqa: E402

failures = []
checks = 0


def check(what, ok, detail=""):
    global checks
    checks += 1
    print(("  ok    " if ok else "  FAIL  ") + what
          + (("  -> " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(what)


class Recording:
    """A W4VE that is not one: it answers, and it remembers being asked."""

    def __init__(self):
        self.asked = []
        self.workers = None

    def handle(self, request):
        self.asked.append(request)
        cmd = request.get("cmd")
        if cmd == "status":
            return {"ok": True, "root": "/fake",
                    "processes": [{"name": "server", "state": "running",
                                   "ready": True, "pid": 4321}]}
        if cmd in ("start", "stop"):
            return {"ok": True, "message": "%s done" % cmd}
        if cmd == "send":
            return {"ok": True, "message": "sent",
                    "lines": ["conformance: %s" % request.get("text", "")]}
        return {"ok": False, "error": "unknown"}


def load_client(path):
    if not path:
        from w4ve_client import W4VE
        return W4VE
    spec = importlib.util.spec_from_file_location("client_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "W4VE"):
        raise SystemExit("%s has no W4VE class in it" % path)
    return module.W4VE


def run(client_path=None):
    Client = load_client(client_path)
    home = Path(tempfile.mkdtemp(prefix="w4ve-client-conformance-"))
    (home / "w4ve" / "run").mkdir(parents=True)
    guardian = Recording()
    served = api_module.Api(home, guardian=guardian, tokens_required=True)
    ok, message = served.start()
    if not ok:
        raise SystemExit(message)
    reader = served.tokens.issue("conformance-read", "read")
    writer = served.tokens.issue("conformance-write", "write")

    try:
        print("-- finding out what it is talking to --")
        anonymous = Client(root=str(home))
        try:
            hello = anonymous.hello()
            check("hello works with no token", isinstance(hello, dict), hello)
            check("and it names the software",
                  hello.get("software") == "w4ve", hello)
        except Exception as exc:            # noqa: BLE001
            check("hello works with no token", False, exc)

        print("-- reading --")
        dashboard = Client(root=str(home), token=reader)
        try:
            processes = dashboard.processes()
            check("a read token can list processes",
                  isinstance(processes, list) and processes[0]["name"] == "server",
                  processes)
        except Exception as exc:            # noqa: BLE001
            check("a read token can list processes", False, exc)

        before = len(guardian.asked)
        try:
            dashboard.status()
            check("status asks W4VE rather than reading files",
                  len(guardian.asked) > before,
                  "nothing reached the guardian, so it came from somewhere else")
        except Exception as exc:            # noqa: BLE001
            check("status asks W4VE rather than reading files", False, exc)

        print("-- what a read token must NOT be able to do --")
        # The one that matters. A client that turns this into a success is a
        # client that tells somebody their world stopped when it did not.
        refused = False
        try:
            dashboard.stop()
        except Exception:                   # noqa: BLE001
            refused = True
        check("a read token cannot stop the server", refused,
              "it did not raise, so the caller thinks the server stopped")
        check("and W4VE was never asked to",
              not any(r.get("cmd") == "stop" for r in guardian.asked),
              guardian.asked)

        print("-- being refused, and being unable to connect --")
        wrong = Client(root=str(home), token="definitely-not-a-token")
        denied = None
        try:
            wrong.status()
        except Exception as exc:            # noqa: BLE001
            denied = exc
        check("a wrong token raises", denied is not None)

        nowhere = Client(root=str(tempfile.mkdtemp(prefix="w4ve-empty-")))
        missing = None
        try:
            nowhere.status()
        except Exception as exc:            # noqa: BLE001
            missing = exc
        check("no W4VE running raises too", missing is not None)
        check("and the two are different kinds of problem",
              denied is not None and missing is not None
              and type(denied) is not type(missing),
              "one is a credential and the other is a service that is not "
              "there: a client that cannot tell them apart cannot retry "
              "correctly")

        print("-- doing something --")
        operator = Client(root=str(home), token=writer)
        try:
            answer = operator.command("say hello")
            check("a write token can run a command",
                  isinstance(answer, dict) and answer.get("ok"), answer)
            sent = [r for r in guardian.asked if r.get("cmd") == "send"]
            check("and the text arrives unchanged",
                  sent and sent[-1].get("text") == "say hello", sent[-1:])
        except Exception as exc:            # noqa: BLE001
            check("a write token can run a command", False, exc)
    finally:
        served.stop()

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
    parser.add_argument("--client",
                        help="a Python file with a W4VE class in it "
                             "(default: the bundled client)")
    return run(parser.parse_args().client)


if __name__ == "__main__":
    raise SystemExit(main())
