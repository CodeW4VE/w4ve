"""Tests for the short path: `w4ve init`, `w4ve run`, and the help around it.

The manifesto's happy path is two commands, and init is the one that decides
whether a stranger trusts the tool: it has to look before it writes, say what
it found and what it is going to do, never move or delete a file, and give a
script the same result a person gets. What is checked here is that it adopts
what is already there, makes a server only when asked and only with the EULA
accepted, and that `w4ve --help` shows the everyday commands and not thirty.
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import w4ve  # noqa: E402


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
        code = function(*args, **kwargs)
    return code, out.getvalue()


def init_args(folder, **extra):
    values = dict(dir=str(folder), name=None, mc=None, client=False, force=False,
                  dry_run=False, yes=True, create=False, accept_eula=False, memory=None)
    values.update(extra)
    return argparse.Namespace(**values)


class WhatVersionThisServerRuns(unittest.TestCase):

    def setUp(self):
        self.game = Path(tempfile.mkdtemp(prefix="w4ve-mc-"))

    def test_the_server_saying_it_wins(self):
        (self.game / "logs").mkdir()
        (self.game / "logs" / "latest.log").write_text(
            "[12:00:00] [Server thread/INFO]: Starting minecraft server version 1.21\n")
        self.assertEqual(w4ve.detect_mc(self.game)[0], "1.21")

    def test_the_jar_unpacked_into_versions_is_read(self):
        for version in ("1.21", "1.21.1"):
            folder = self.game / "versions" / version
            folder.mkdir(parents=True)
            (folder / f"server-{version}.jar").write_bytes(b"")
        self.assertEqual(w4ve.detect_mc(self.game)[0], "1.21.1")

    def test_the_version_inside_server_jar_is_read(self):
        with zipfile.ZipFile(self.game / "server.jar", "w") as jar:
            jar.writestr("version.json", json.dumps({"id": "26.2"}))
        self.assertEqual(w4ve.detect_mc(self.game)[0], "26.2")

    def test_a_number_in_a_file_name_is_not_a_version(self):
        (self.game / "mods").mkdir()
        (self.game / "mods" / "PrimeBackup-v1.13.1.jar").write_bytes(b"")
        self.assertEqual(w4ve.detect_mc(self.game), (None, None))

    def test_the_java_each_version_needs(self):
        self.assertEqual(w4ve.java_needed("1.21"), "21")
        self.assertEqual(w4ve.java_needed("1.20.4"), "17")
        self.assertEqual(w4ve.java_needed("26.2"), "25")


class InitAdoptsWhatIsThere(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="w4ve-init-"))

    def test_an_existing_fabric_server_gets_its_version_from_the_server(self):
        (self.root / "fabric-server-launch.jar").write_bytes(b"")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "latest.log").write_text(
            "Starting minecraft server version 1.21.1\n")
        code, out = quiet(w4ve.cmd_init, init_args(self.root), None)
        self.assertEqual(code, 0)
        text = (self.root / "w4ve.toml").read_text()
        self.assertIn('mc       = "1.21.1"', text)
        self.assertIn("a server: Fabric", out)
        self.assertIn("w4ve run", out)

    def test_the_mods_already_here_are_written_down(self):
        (self.root / "fabric-server-launch.jar").write_bytes(b"")
        (self.root / "mods").mkdir()
        (self.root / "mods" / "something.jar").write_bytes(b"")
        index = {"schema": 1, "pieces": {}, "profiles": {}}
        code, out = quiet(w4ve.cmd_init, init_args(self.root, mc="1.21"), index)
        self.assertEqual(code, 0)
        self.assertIn("write down the 1 mods and plugins already here", out)
        self.assertIn("not in the catalog, left alone", out)

    def test_dry_run_writes_nothing_at_all(self):
        code, out = quiet(w4ve.cmd_init, init_args(self.root, dry_run=True), None)
        self.assertEqual(code, 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("Plan", out)

    def test_a_folder_already_set_up_is_not_overwritten(self):
        (self.root / "w4ve.toml").write_text("# mine\n")
        code, _ = quiet(w4ve.cmd_init, init_args(self.root), None)
        self.assertEqual(code, 1)
        self.assertEqual((self.root / "w4ve.toml").read_text(), "# mine\n")

    def test_mcdr_is_named_and_left_in_charge(self):
        (self.root / "server").mkdir()
        (self.root / "server" / "server.jar").write_bytes(b"")
        (self.root / "config.yml").write_text(
            "working_directory: server\nstart_command: java -jar server.jar nogui\n")
        code, out = quiet(w4ve.cmd_init, init_args(self.root, mc="1.21"), None)
        self.assertEqual(code, 0)
        self.assertIn("MCDReforged", out)
        self.assertIn("MCDR is still the one that starts this server", out)
        self.assertTrue((self.root / "config.yml").exists())


class InitMakesAServerOnlyWhenAsked(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="w4ve-new-"))

    def test_an_empty_folder_without_create_gets_no_server(self):
        code, out = quiet(w4ve.cmd_init, init_args(self.root, mc="1.21"), None)
        self.assertEqual(code, 0)
        self.assertFalse((self.root / w4ve.LAUNCHER).exists())
        self.assertFalse((self.root / "eula.txt").exists())
        self.assertIn("--create", out)

    def test_without_the_eula_accepted_nothing_is_downloaded_or_written(self):
        with mock.patch.object(w4ve, "fabric_launcher_url") as launcher:
            code, _ = quiet(w4ve.cmd_init, init_args(self.root, mc="1.21", create=True), None)
        self.assertEqual(code, 1)
        launcher.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_create_needs_to_know_which_minecraft(self):
        code, _ = quiet(w4ve.cmd_init, init_args(self.root, create=True, accept_eula=True), None)
        self.assertEqual(code, 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_new_server_has_its_launcher_eula_and_start_command(self):
        def fake_download(url, dest, expected_hashes=None, quiet=False):
            dest.write_bytes(b"launcher")
            return "x", None
        with mock.patch.object(w4ve, "fabric_launcher_url",
                               return_value=("https://example/jar", "0.19.5")), \
                mock.patch.object(w4ve, "download", side_effect=fake_download):
            code, out = quiet(w4ve.cmd_init, init_args(
                self.root, mc="1.21", create=True, accept_eula=True, memory=3), None)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / w4ve.LAUNCHER).read_bytes(), b"launcher")
        self.assertIn("eula=true", (self.root / "eula.txt").read_text())
        text = (self.root / "w4ve.toml").read_text()
        self.assertIn("-Xmx3G -jar fabric-server-launch.jar nogui", text)
        self.assertIn('java     = "21"', text)

    def test_fabric_unreachable_is_said_and_leaves_nothing_behind(self):
        with mock.patch.object(w4ve, "fabric_launcher_url",
                               side_effect=OSError("no network")):
            code, out = quiet(w4ve.cmd_init, init_args(
                self.root, mc="1.21", create=True, accept_eula=True), None)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "w4ve.toml").exists())


class TheFirstScreen(unittest.TestCase):

    def test_plain_w4ve_shows_where_to_start(self):
        code, out = quiet(w4ve.main, [])
        self.assertEqual(code, 0)
        self.assertIn("w4ve init", out)
        self.assertIn("w4ve run", out)

    def test_help_shows_the_everyday_commands_and_not_the_rest(self):
        text = w4ve.build_parser().format_help()
        commands = text.split("everyday commands:")[1].split("getting started:")[0]
        for name in w4ve.EVERYDAY:
            self.assertIn(f"    {name} ", commands)
        for hidden in ("adopt", "exec-service", "permissions", "pack"):
            self.assertNotIn(f"    {hidden} ", commands)

    def test_a_hidden_command_still_works(self):
        with self.assertRaises(SystemExit) as stop, \
                contextlib.redirect_stdout(io.StringIO()):
            w4ve.main(["adopt", "--help"])
        self.assertEqual(stop.exception.code, 0)

    def test_help_names_lists_every_command_for_scripts(self):
        code, out = quiet(w4ve.main, ["help", "--names"])
        names = out.split()
        self.assertEqual(code, 0)
        self.assertIn("self-update", names)
        self.assertIn("exec-service", names)
        self.assertEqual(len(names), len(set(names)))


class SelfUpdate(unittest.TestCase):

    def release(self, tag, assets=()):
        return json.dumps({"tag_name": tag, "html_url": "https://example",
                           "assets": [{"name": n, "browser_download_url": u}
                                      for n, u in assets]}).encode()

    def run_update(self, *argv, **patches):
        args = w4ve.build_parser().parse_args(["self-update", *argv])
        return quiet(w4ve.cmd_self_update, args, None)

    def test_the_newest_says_so_and_does_nothing(self):
        with mock.patch.object(w4ve, "fetch", return_value=self.release("v" + w4ve.VERSION)):
            code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn("is the newest", out)

    def test_check_only_reports(self):
        with mock.patch.object(w4ve, "fetch", return_value=self.release("v99.0.0")):
            code, out = self.run_update("--check")
        self.assertEqual(code, 0)
        self.assertIn("99.0.0 is out", out)

    def test_a_checkout_is_never_replaced(self):
        with mock.patch.object(w4ve, "fetch", return_value=self.release("v99.0.0")), \
                mock.patch.object(w4ve, "installed_layout", return_value=None):
            code, out = self.run_update("-y")
        self.assertEqual(code, 2)

    def test_a_release_with_a_bad_checksum_is_refused(self):
        work = Path(tempfile.mkdtemp(prefix="w4ve-upd-"))
        (work / "w4ve-99.0.0.tar.gz").write_bytes(b"archive")
        (work / "w4ve-99.0.0.tar.gz.sha256").write_text("0" * 64 + "  w4ve-99.0.0.tar.gz\n")
        (work / "install.py").write_text("raise SystemExit(0)\n")
        assets = [(name, (work / name).as_uri()) for name in
                  ("w4ve-99.0.0.tar.gz", "w4ve-99.0.0.tar.gz.sha256", "install.py")]
        prefix = work / "prefix"
        with mock.patch.object(w4ve, "fetch", return_value=self.release("v99.0.0", assets)), \
                mock.patch.object(w4ve, "installed_layout", return_value=(prefix, work / "bin")):
            code, _ = self.run_update("-y")
        self.assertEqual(code, 1)
        self.assertFalse(prefix.exists())

    def test_a_real_release_is_installed_beside_the_old_one(self):
        import build_distribution
        work = Path(tempfile.mkdtemp(prefix="w4ve-upd-"))
        archive = build_distribution.build(ROOT, work / "dist")
        version = archive.name[len("w4ve-"):-len(".tar.gz")]
        assets = [(archive.name, archive.as_uri()),
                  (archive.name + ".sha256", Path(str(archive) + ".sha256").as_uri()),
                  ("install.py", (ROOT / "install.py").as_uri())]
        prefix, bin_dir = work / "prefix", work / "bin"
        with mock.patch.object(w4ve, "fetch", return_value=self.release("v" + version, assets)), \
                mock.patch.object(w4ve, "installed_layout", return_value=(prefix, bin_dir)), \
                mock.patch.object(w4ve, "VERSION", "0.0.1"):
            code, out = self.run_update("-y")
        self.assertEqual(code, 0, out)
        self.assertTrue((prefix / "releases" / version / "w4ve.py").is_file())
        self.assertTrue((bin_dir / "w4ve").is_file())
        self.assertTrue((prefix / "install.py").is_file(), "kept for going back offline")
        self.assertIn("--rollback 0.0.1", out)


if __name__ == "__main__":
    unittest.main()
