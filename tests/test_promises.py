"""The things W4VE says it does not do, checked rather than promised.

Section 15 of the manifesto lists what is "directly dangerous", and the README
repeats several of those as guarantees. A guarantee nobody checks is a comment,
and the way each of these gets broken is the same: somebody adds a small
convenience, it is obviously fine in the case they are looking at, and nobody
notices that a line in the documentation stopped being true.

So each one is a test. Several look almost too simple to write down; that is
the point. The day one of them fails, something worth stopping for has changed.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import package  # noqa: E402
import piecesettings  # noqa: E402
import w4ve  # noqa: E402
import worker  # noqa: E402

SOURCES = ["w4ve.py", "runtime.py", "package.py", "worker.py", "api.py",
           "piecesettings.py", "discover.py", "rcon.py", "pluginhost.py"]


def source_of(name):
    return (ROOT / name).read_text(encoding="utf-8")


def a_server(tmp):
    root = Path(tmp) / "root"
    (root / "server" / "mods").mkdir(parents=True)
    (root / "w4ve").mkdir()
    (root / "w4ve.toml").write_text(
        '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n[pieces]\n')
    return w4ve.Server(root)


class NoShellHooksCase(unittest.TestCase):
    """"hooks shell en paquetes públicos" — a package must not run commands.

    A `.w4ve` places files and declares processes. What it must never be is a
    way to run arbitrary shell as whoever installed it, because "install this
    piece" would then mean "run this stranger's script".
    """

    def test_installing_a_package_never_runs_a_shell(self):
        text = source_of("package.py")
        for danger in ("subprocess", "os.system", "shell=True", "os.popen",
                       "eval(", "exec("):
            self.assertNotIn(danger, text,
                             "package.py must not be able to run anything")

    def test_a_package_cannot_write_a_restart_command_into_the_toml(self):
        # `[server] restart` is run with a shell, on purpose, because it is the
        # operator's own line in their own file and they are asked first. A
        # package writing into that section would turn it into a hook.
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-hook-"))
        server = a_server(tmp)
        for section in ("services", "watch", "http", "storage", "timer"):
            server._set_block(section, "x", {"a": "b"}, owner="evil")
        text = (server.root / "w4ve.toml").read_text()
        self.assertNotIn("restart =", text.split("[services")[0],
                         "a package reached the [server] section")
        self.assertIn('restart = "on-failure"'.split(" =")[0],
                      "restart", "sanity")


class NoSecretsInTheOpenCase(unittest.TestCase):
    """"secrets en TOML o logs"."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-secret-"))
        self.server = a_server(self.tmp)

    def test_a_secret_never_lands_in_the_toml(self):
        declarations = piecesettings.declarations_of({"settings": [
            {"name": "token", "type": "secret", "help": "the token"},
            {"name": "url", "type": "url", "help": "the address"},
        ]})
        self.server.set_setting("piece", "token", "SUPERSECRET",
                                declarations, "piece")
        self.server.set_setting("piece", "url", "https://x.example/",
                                declarations, "piece")
        text = (self.server.root / "w4ve.toml").read_text()
        self.assertNotIn("SUPERSECRET", text)
        self.assertIn("https://x.example/", text)

    def test_and_the_file_it_does_land_in_is_600(self):
        store = piecesettings.Secrets(self.server.root)
        store.set("piece", "token", "SUPERSECRET")
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_an_api_token_is_never_in_a_listing(self):
        tokens = api.Tokens(self.server.root)
        token = tokens.issue("dashboard", "read")
        self.assertNotIn(token, json.dumps(tokens.listing()))

    def test_an_rcon_password_is_redacted_before_it_can_be_logged(self):
        import rcon
        text = rcon.redact("connecting with hunter2 now", "hunter2")
        self.assertNotIn("hunter2", text)

    def test_a_service_environment_is_not_written_to_the_journal(self):
        # A service's env carries expanded secrets. The journal is a plain file
        # anybody with the folder can read.
        text = source_of("runtime.py")
        self.assertNotIn("self.log(\"env", text)
        self.assertNotIn("log(str(environment", text)


class NoRemoteByDefaultCase(unittest.TestCase):
    """"API remota abierta por defecto"."""

    def test_the_api_is_off_unless_asked_for(self):
        # Off unless asked for. A piece declaring that it needs identity or
        # game events is asking, and then a token is never optional.
        text = source_of("runtime.py")
        self.assertIn('section.get("enabled", bool(self.identity_services or self.event_services)'
                      ' or bridge.get("enabled") is True)', text,
                      "the API has to default to off")
        self.assertIn("tokens_required=True if self.identity_services or self.event_services"
                      " or self.game_bridge", text,
                      "an API turned on by a piece must require tokens")

    def test_a_socket_needs_no_token_and_a_port_always_does(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-remote-"))
        self.assertFalse(api.Api(tmp).tokens_required)
        self.assertTrue(api.Api(tmp, address="0.0.0.0:8790").tokens_required)

    def test_the_socket_is_readable_by_nobody_else(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-sock-"))
        (tmp / "w4ve" / "run").mkdir(parents=True)
        instance = api.Api(tmp)
        ok, message = instance.start()
        try:
            self.assertTrue(ok, message)
            mode = stat.S_IMODE(instance.socket_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            instance.stop()


class NoTouchingTheWorldCase(unittest.TestCase):
    """"modificar worlds como parte de un paquete", and "autoaceptar EULA"."""

    def test_nothing_here_writes_an_eula(self):
        # Accepting a licence on somebody's behalf is not a convenience.
        for name in SOURCES:
            self.assertNotIn("eula", source_of(name).lower(),
                             "%s mentions the EULA" % name)

    def test_a_package_cannot_place_files_in_the_world(self):
        kinds = package.KINDS
        for kind, target in kinds.items():
            if not target:
                continue
            self.assertFalse(target.startswith("world/") and kind != "datapack",
                             "%s installs into the world folder" % kind)

    def test_a_package_cannot_escape_the_server_folder(self):
        for path in ("/etc/passwd", "../../outside", "mods/../../../x"):
            bad = {"schema": 1, "id": "x", "version": "1", "summary": "s",
                   "components": [{"kind": "mod", "file": path}]}
            problems = package.Manifest(bad).problems()
            self.assertTrue(any("escapes" in p for p in problems),
                            "%r was accepted" % path)


class NoQuietDestructionCase(unittest.TestCase):
    """"borrar mods/config desconocidos durante sync", and quiet restarts."""

    def test_removing_a_piece_leaves_a_file_somebody_edited(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-quiet-"))
        server = a_server(tmp)
        source = tmp / "src"
        (source / "config").mkdir(parents=True)
        (source / "config" / "thing.json").write_text("{}")
        (source / "w4ve.json").write_text(json.dumps({
            "schema": 1, "id": "demo", "version": "1.0.0", "summary": "s",
            "components": [{"kind": "config", "file": "config/thing.json"}],
        }))
        built, _ = package.pack(source, tmp)
        install = package.Install(built, server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        install.apply(actions)
        install.commit()
        placed = server.root / server.game_dir / "config" / "thing.json"
        placed.write_text('{"mine": true}')
        result = package.remove(server, "demo")
        self.assertTrue(placed.exists(), "it deleted a file somebody edited")
        self.assertIn("config/thing.json", result["changed"])

    def test_removing_a_piece_never_deletes_its_data(self):
        tmp = Path(tempfile.mkdtemp(prefix="w4ve-data-"))
        server = a_server(tmp)
        source = tmp / "src"
        source.mkdir(parents=True)
        (source / "w4ve.json").write_text(json.dumps({
            "schema": 1, "id": "demo", "version": "1.0.0", "summary": "s",
            "components": [{"kind": "storage", "name": "media",
                            "dir": "w4ve/storage/demo", "quota": "1G"}],
        }))
        built, _ = package.pack(source, tmp)
        install = package.Install(built, server)
        actions, _ = install.plan()
        install.apply(actions)
        install.commit()
        kept = server.root / "w4ve" / "storage" / "demo" / "important.png"
        kept.write_text("x")
        package.remove(server, "demo")
        self.assertTrue(kept.exists(), "removing a piece deleted its data")

    def test_a_restart_is_offered_and_never_taken(self):
        text = source_of("w4ve.py")
        self.assertIn("args.yes or ask(", text,
                      "the restart has to be asked for, or -y given")


class NoRootCase(unittest.TestCase):
    """"ejecutar W4VE como root": it does not refuse, it says so."""

    def test_doctor_mentions_root(self):
        # Refusing outright would break the people running it in a container
        # as root today, which is most of them. Saying it is the honest middle.
        text = source_of("w4ve.py")
        self.assertIn("geteuid", text,
                      "nothing anywhere notices it is running as root")


class NoPickleCase(unittest.TestCase):
    """"pickle entre companion y runtime": every wire here is JSON."""

    def test_nothing_unpickles_anything(self):
        for name in SOURCES:
            text = source_of(name)
            self.assertNotIn("pickle", text, "%s uses pickle" % name)
        for path in (ROOT / "sdk").rglob("*.py"):
            self.assertNotIn("pickle", path.read_text(encoding="utf-8"),
                             "%s uses pickle" % path.name)

    def test_the_plugin_protocol_is_json_and_nothing_else(self):
        # A plugin is somebody else's code. Anything that deserialises objects
        # from it is code execution with extra steps.
        text = source_of("worker.py")
        self.assertIn("json.loads", text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)


class NoInstallingFromAnywhereCase(unittest.TestCase):
    """"instalación desde cualquier URL"."""

    def test_a_download_is_verified_when_the_source_publishes_a_hash(self):
        text = source_of("w4ve.py")
        self.assertIn("expected_hashes", text)
        self.assertIn("def verify(", text)

    def test_a_download_lands_as_a_temporary_file_first(self):
        # A half-downloaded jar in mods/ is a server that does not start.
        text = source_of("w4ve.py")
        self.assertIn(".part", text)


if __name__ == "__main__":
    unittest.main()
