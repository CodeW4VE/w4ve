"""Tests for the `.w4ve` package: the piece that arrives whole.

Real zips, real files on disk, real w4ve.toml. What is being checked is not
that the happy path works, it is that a package cannot half install itself,
cannot write outside the server folder, and cannot take away a config somebody
edited.
"""

import json
import sys
import time
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import package  # noqa: E402
import w4ve  # noqa: E402
ROOT = Path(w4ve.__file__).resolve().parent


def manifest(**overrides):
    base = {
        "schema": 1,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "summary": "A piece for the tests.",
        "side": "server",
        "mc": ">=1.21",
        "components": [
            {"kind": "mod", "file": "mod/demo-1.0.0+1.21.jar",
             "mc": ">=1.21 <1.21.2", "install_dir": "mods", "reload": "restart"},
        ],
    }
    base.update(overrides)
    return base


def build(folder, data, files=()):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "w4ve.json").write_text(json.dumps(data, indent=2))
    for relative, content in files:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return folder


class PackCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-pkg-"))

    def test_pack_names_itself_after_id_and_version(self):
        folder = build(self.tmp / "src", manifest(),
                       [("mod/demo-1.0.0+1.21.jar", "not really a jar")])
        target, _ = package.pack(folder, self.tmp)
        self.assertEqual(target.name, "demo-1.0.0.w4ve")
        with zipfile.ZipFile(target) as archive:
            self.assertIn("w4ve.json", archive.namelist())
            self.assertIn("mod/demo-1.0.0+1.21.jar", archive.namelist())

    def test_required_server_mod_rejects_an_unsupported_minecraft(self):
        data = manifest()
        data["components"][0]["required"] = True
        data["components"].append({"kind": "asset", "file": "hub/main.py"})
        package_manifest = package.Manifest(data)
        actions, problems = package_manifest.install_plan("1.21.4")
        self.assertTrue(any("requires a server mod" in problem for problem in problems))
        self.assertFalse(any(action["kind"] == "mod" for action in actions))
        actions, problems = package_manifest.install_plan("1.21.1")
        self.assertEqual(problems, [])
        self.assertEqual(sum(action["kind"] == "mod" for action in actions), 1)

    def test_required_mod_flag_needs_a_boolean_and_compatibility(self):
        data = manifest()
        data["components"][0]["required"] = "yes"
        self.assertTrue(any("required must be a boolean" in p for p in package.Manifest(data).problems()))
        data["components"][0]["required"] = True
        data["components"][0].pop("mc")
        self.assertTrue(any("compatibility range" in p for p in package.Manifest(data).problems()))

    def test_a_manifest_that_lies_does_not_get_packed(self):
        folder = build(self.tmp / "src", manifest())   # the jar is missing
        with self.assertRaises(package.PackageError) as caught:
            package.pack(folder, self.tmp)
        self.assertIn("not in the folder", str(caught.exception))

    def test_every_problem_at_once(self):
        bad = manifest(id="Demo Package", version="", side="both ways",
                       components=[{"kind": "wat", "file": "x"}])
        problems = package.Manifest(bad).problems()
        self.assertTrue(any("id" in p for p in problems))
        self.assertTrue(any("version" in p for p in problems))
        self.assertTrue(any("side" in p for p in problems))
        self.assertTrue(any("unknown kind" in p for p in problems))

    def test_a_package_cannot_write_outside_the_server(self):
        bad = manifest(components=[{"kind": "mod", "file": "../../etc/passwd"}])
        problems = package.Manifest(bad).problems()
        self.assertTrue(any("escapes the package" in p for p in problems))

    def test_a_plugin_still_needs_a_name_to_land_anywhere(self):
        # This used to check that a `plugin` component was refused outright,
        # which was true while there was no platform to run one on. There is
        # one now (RFC-0003), so what is left to get wrong is the folder: a
        # plugin without a name has nowhere of its own to go, and every file
        # of every plugin would pile into w4ve/plugins/.
        bad = manifest(components=[{"kind": "plugin", "file": "plugin/x.py"}])
        problems = package.Manifest(bad).problems()
        self.assertTrue(any("needs a `name`" in p for p in problems), problems)

    def test_a_named_plugin_is_accepted(self):
        good = manifest(components=[{"kind": "plugin", "name": "mine",
                                     "file": "plugin/mine/main.py"}])
        self.assertEqual(package.Manifest(good).problems(), [])


class InstallCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-inst-"))
        self.root = self.tmp / "server-root"
        (self.root / "server" / "mods").mkdir(parents=True)
        (self.root / "w4ve").mkdir()
        (self.root / "w4ve.toml").write_text(
            '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n[pieces]\n')
        self.server = w4ve.Server(self.root)

    def package_with(self, data, files):
        folder = build(self.tmp / ("src-" + data["id"]), data, files)
        target, _ = package.pack(folder, self.tmp)
        return target

    def test_one_package_many_jars_one_install(self):
        data = manifest(components=[
            {"kind": "mod", "file": "mod/demo-1.0.0+1.21.jar",
             "mc": ">=1.21 <1.21.2", "reload": "restart"},
            {"kind": "mod", "file": "mod/demo-1.0.0+26.2.jar",
             "mc": ">=26.2 <26.3", "reload": "restart"},
        ])
        pkg = self.package_with(data, [("mod/demo-1.0.0+1.21.jar", "old"),
                                       ("mod/demo-1.0.0+26.2.jar", "new")])
        install = package.Install(pkg, self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        self.assertEqual(len(actions), 1)
        record = install.apply(actions)
        install.commit()
        landed = list((self.root / "server" / "mods").iterdir())
        self.assertEqual([p.name for p in landed], ["demo-1.0.0+1.21.jar"])
        self.assertTrue(record["pending_restart"])

    def test_two_jars_for_the_same_server_is_the_package_being_wrong(self):
        data = manifest(components=[
            {"kind": "mod", "file": "mod/a.jar", "mc": ">=1.21"},
            {"kind": "mod", "file": "mod/b.jar", "mc": ">=1.21"},
        ])
        pkg = self.package_with(data, [("mod/a.jar", "a"), ("mod/b.jar", "b")])
        _, problems = package.Install(pkg, self.server).plan()
        self.assertTrue(any("has to pick one" in p for p in problems))

    def test_a_client_package_is_not_for_a_server(self):
        pkg = self.package_with(manifest(id="clientish", side="client"),
                                [("mod/demo-1.0.0+1.21.jar", "x")])
        _, problems = package.Install(pkg, self.server).plan()
        self.assertTrue(any("client side" in p for p in problems))

    def test_a_service_lands_in_w4ve_toml_where_a_person_can_read_it(self):
        data = manifest(id="withservice", components=[
            {"kind": "asset", "file": "service/app.py",
             "install_dir": "w4ve/services/thing", "reload": "hot"},
            {"kind": "service", "name": "thing",
             "command": ["python3", "app.py"], "cwd": "w4ve/services/thing",
             "restart": "on-failure", "reload": "hot",
             "w4ve_api": "identity",
             "w4ve_events": "chat",
             "python": {"requires": ["json"]},
             "health": {"http": "http://127.0.0.1:9/health", "every": 15}},
        ])
        pkg = self.package_with(data, [("service/app.py", "print('hi')\n")])
        install = package.Install(pkg, self.server)
        actions, problems = install.plan()
        self.assertEqual(problems, [])
        install.apply(actions)
        install.commit()

        # The file went next to the runtime, not into the game folder.
        self.assertTrue((self.root / "w4ve/services/thing/app.py").exists())
        text = (self.root / "w4ve.toml").read_text()
        self.assertIn("[services.thing]", text)
        self.assertIn("installed by withservice", text)

        fresh = w4ve.Server(self.root)
        service = (fresh.config["services"]["thing"])
        self.assertEqual(service["command"], ["python3", "app.py"])
        self.assertEqual(service["health_url"], "http://127.0.0.1:9/health")
        self.assertEqual(service["python_requires"], ["json"])
        self.assertEqual(service["w4ve_api"], "identity")
        self.assertEqual(service["w4ve_events"], "chat")

    def test_removing_takes_out_what_it_put_in_and_leaves_the_rest(self):
        data = manifest(id="withservice", components=[
            {"kind": "asset", "file": "service/app.py",
             "install_dir": "w4ve/services/thing", "reload": "hot"},
            {"kind": "config", "file": "config/thing.json",
             "install_dir": "w4ve/services/thing", "reload": "hot",
             "keep_on_update": True},
            {"kind": "service", "name": "thing", "command": ["python3", "app.py"],
             "cwd": "w4ve/services/thing", "reload": "hot"},
        ])
        pkg = self.package_with(data, [("service/app.py", "print('hi')\n"),
                                       ("config/thing.json", "{}\n")])
        install = package.Install(pkg, self.server)
        install.apply(install.plan()[0])
        install.commit()

        edited = self.root / "w4ve/services/thing/thing.json"
        edited.write_text('{"mine": true}\n')      # the operator edits it

        result = package.remove(self.server, "withservice")
        self.assertIn("w4ve/services/thing/app.py", result["removed"])
        self.assertIn("w4ve/services/thing/thing.json", result["changed"])
        self.assertTrue(edited.exists(), "an edited config is not ours to delete")
        self.assertNotIn("[services.thing]", (self.root / "w4ve.toml").read_text())
        self.assertNotIn("withservice", self.server.state["pieces"])

    def test_a_failed_install_leaves_nothing_behind(self):
        data = manifest(components=[
            {"kind": "mod", "file": "mod/demo-1.0.0+1.21.jar",
             "mc": ">=1.21 <1.21.2", "reload": "restart"},
            {"kind": "asset", "file": "mod/second.txt",
             "install_dir": "w4ve/assets", "reload": "hot"},
        ])
        pkg = self.package_with(data, [("mod/demo-1.0.0+1.21.jar", "jar"),
                                       ("mod/second.txt", "text")])
        # Break the second member so applying blows up halfway through.
        install = package.Install(pkg, self.server)
        actions, _ = install.plan()
        actions[1]["member"] = "mod/does-not-exist.txt"
        with self.assertRaises(KeyError):
            install.apply(actions)
        self.assertEqual(list((self.root / "server" / "mods").iterdir()), [],
                         "the first file should have been rolled back")
        self.assertNotIn("demo", self.server.state["pieces"])

    def test_an_edited_config_survives_reinstalling(self):
        data = manifest(id="conf", components=[
            {"kind": "config", "file": "config/thing.json",
             "install_dir": "w4ve/services/thing", "reload": "hot",
             "keep_on_update": True},
        ])
        pkg = self.package_with(data, [("config/thing.json", '{"default": 1}\n')])
        first = package.Install(pkg, self.server)
        first.apply(first.plan()[0])
        first.commit()
        target = self.root / "w4ve/services/thing/thing.json"
        target.write_text('{"mine": true}\n')

        second = package.Install(pkg, self.server)
        second.apply(second.plan()[0])
        second.commit()
        self.assertEqual(target.read_text(), '{"mine": true}\n')
        self.assertTrue(target.with_suffix(".json.new").exists(),
                        "the new default should arrive next to it, not over it")


class InterruptedCase(unittest.TestCase):
    """An install killed halfway must not leave a piece half applied.

    Rolling back in memory only covers the process that is still alive. These
    kill one for real, at the worst moment, and check that the next command
    finds the mess and undoes it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-kill-"))
        self.root = self.tmp / "server-root"
        (self.root / "server" / "mods").mkdir(parents=True)
        (self.root / "w4ve").mkdir()
        (self.root / "w4ve.toml").write_text(
            '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n[pieces]\n')
        self.server = w4ve.Server(self.root)

    def a_package(self):
        data = manifest(id="halfway", components=[
            {"kind": "mod", "file": "mod/halfway-1.0.0+1.21.jar",
             "mc": ">=1.21 <1.21.2", "reload": "restart"},
            {"kind": "asset", "file": "service/app.py",
             "install_dir": "w4ve/services/halfway", "reload": "hot"},
            {"kind": "service", "name": "halfway",
             "command": ["python3", "app.py"], "cwd": "w4ve/services/halfway",
             "reload": "hot"},
        ])
        folder = build(self.tmp / "src", data,
                       [("mod/halfway-1.0.0+1.21.jar", "jar"),
                        ("service/app.py", "print('hi')\n")])
        return package.pack(folder, self.tmp)[0]

    def test_the_journal_survives_the_process_that_wrote_it(self):
        import os
        import signal
        import subprocess

        pkg = self.a_package()
        # Install in another process and kill it while it works. The sleep in
        # the middle is the only way to be sure the kill lands mid install.
        script = self.tmp / "slow_install.py"
        script.write_text(
            "import sys, time\n"
            "sys.path.insert(0, %r)\n"
            "import package, w4ve\n"
            "server = w4ve.Server(%r)\n"
            "install = package.Install(%r, server)\n"
            "actions, _ = install.plan()\n"
            "real = install._write\n"
            "def slow(*a, **kw):\n"
            "    real(*a, **kw)\n"
            "    time.sleep(5)\n"
            "install._write = slow\n"
            "install.apply(actions)\n"
            "install.commit()\n" % (str(ROOT), str(self.root), str(pkg)))
        child = subprocess.Popen([sys.executable, str(script)])
        deadline = time.time() + 15
        jar = self.root / "server/mods/halfway-1.0.0+1.21.jar"
        while time.time() < deadline and not jar.exists():
            time.sleep(0.1)
        self.assertTrue(jar.exists(), "the install never got started")
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)

        server = w4ve.Server(self.root)
        found = package.interrupted(server)
        self.assertTrue(found, "nothing recorded that an install was running")
        self.assertEqual(found["piece"], "halfway")

        result = package.roll_back_interrupted(server)
        self.assertFalse(jar.exists(), "the half installed jar is still there")
        self.assertIn(str(jar), result["undone"])
        self.assertIsNone(package.interrupted(w4ve.Server(self.root)),
                          "the journal should be gone once it is undone")

    def test_a_file_that_was_replaced_comes_back(self):
        pkg = self.a_package()
        jar = self.root / "server/mods/halfway-1.0.0+1.21.jar"
        jar.write_text("the version that was already there")

        install = package.Install(pkg, self.server)
        actions, _ = install.plan()
        # Blow up after the first file, the way a kill would.
        actions[1]["member"] = "does/not/exist"
        with self.assertRaises(KeyError):
            install.apply(actions)

        # In memory rollback already put it back; the journal must agree.
        self.assertEqual(jar.read_text(), "the version that was already there")
        self.assertIsNone(package.interrupted(w4ve.Server(self.root)))

    def test_state_survives_a_torn_write(self):
        self.server.state["pieces"]["something"] = {"version": "1.0.0"}
        self.server.save_state()
        self.server.state["pieces"]["another"] = {"version": "2.0.0"}
        self.server.save_state()
        # A power cut in the middle of the third write.
        self.server.state_path.write_text('{"schema": 1, "pieces": {"br')

        recovered = w4ve.Server(self.root)
        # The copy is the state as of the write before, so the last change is
        # gone and the record of the server is not. Losing one line beats
        # starting from an empty file and forgetting the whole server.
        self.assertIn("something", recovered.state["pieces"],
                      "it should have fallen back to the previous copy")
        self.assertNotIn("another", recovered.state["pieces"])

    def test_a_normal_write_is_never_torn_in_the_first_place(self):
        """The real defence: write to a temp name and rename over the top."""
        self.server.state["pieces"]["thing"] = {"version": "1.0.0"}
        self.server.save_state()
        leftovers = list(self.server.state_path.parent.glob("*.writing"))
        self.assertEqual(leftovers, [], "a temp file was left behind")
        self.assertEqual(json.loads(self.server.state_path.read_text())
                         ["pieces"]["thing"]["version"], "1.0.0")


class VersionCase(unittest.TestCase):

    def test_a_minecraft_suffix_is_not_a_newer_version(self):
        self.assertFalse(w4ve.newer("1.7.0+26.2", "1.7.0"))
        self.assertFalse(w4ve.newer("1.7.0", "1.7.0+26.2"))
        self.assertTrue(w4ve.newer("1.7.1+1.21", "1.7.0+26.2"))

    def test_our_fork_tags_still_sort_above_upstream(self):
        self.assertTrue(w4ve.newer("1.13.1-w4ve.1", "1.13.1"))




class EndToEndCase(unittest.TestCase):
    """The whole point, in one test: install a piece, and its second half runs.

    A jar and a process arrive in the same file, `w4ve run` brings both up, the
    health check turns readiness into something real, and removing the piece
    takes the service out of w4ve.toml again.
    """

    def setUp(self):
        import socket

        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-e2e-"))
        (self.tmp / "server" / "mods").mkdir(parents=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        (self.tmp / "w4ve.toml").write_text(
            '[server]\n'
            'kind     = "server"\n'
            'mc       = "1.21"\n'
            'game_dir = "server"\n'
            'start    = "%s %s --boot 0.3"\n\n'
            '[pieces]\n' % (sys.executable, HERE / "fakeserver.py"))
        self.guardian = None

    def tearDown(self):
        import runtime

        if self.guardian and self.guardian.poll() is None:
            self.guardian.terminate()
            try:
                self.guardian.wait(timeout=20)
            except Exception:
                self.guardian.kill()
        state = runtime.Client(self.tmp).offline_status() or {}
        for entry in state.get("processes", []):
            if entry.get("pid") and runtime.alive(entry["pid"]):
                import os
                import signal
                os.kill(entry["pid"], signal.SIGKILL)

    def test_a_piece_arrives_whole_and_its_service_runs(self):
        import subprocess
        import time

        import runtime

        data = manifest(id="served", components=[
            {"kind": "mod", "file": "mod/served-1.0.0+1.21.jar",
             "mc": ">=1.21 <1.21.2", "reload": "restart"},
            {"kind": "service", "name": "served-web",
             "command": [sys.executable, "-m", "http.server", str(self.port),
                         "--bind", "127.0.0.1"],
             "cwd": "w4ve/services/served", "restart": "on-failure",
             "reload": "hot",
             "health": {"http": "http://127.0.0.1:%d/" % self.port, "every": 5}},
            {"kind": "asset", "file": "service/index.html",
             "install_dir": "w4ve/services/served", "reload": "hot"},
        ])
        folder = build(self.tmp / "src", data,
                       [("mod/served-1.0.0+1.21.jar", "jar"),
                        ("service/index.html", "<h1>served</h1>")])
        pkg, _ = package.pack(folder, self.tmp)

        cli = str(ROOT / "w4ve.py")
        index = self.tmp / "index.json"
        index.write_text(json.dumps({"pieces": {}}))
        install = subprocess.run(
            [sys.executable, cli, "--dir", str(self.tmp), "--index", str(index), "install", str(pkg), "-y"],
            capture_output=True, text=True)
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertTrue((self.tmp / "server/mods/served-1.0.0+1.21.jar").exists())
        self.assertIn("[services.served-web]", (self.tmp / "w4ve.toml").read_text())

        self.guardian = subprocess.Popen(
            [sys.executable, cli, "--dir", str(self.tmp), "run", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        client = runtime.Client(self.tmp)
        deadline = time.time() + 30
        served = None
        while time.time() < deadline:
            reply = client.call({"cmd": "status"}) if client.connected() else {}
            found = [p for p in reply.get("processes", [])
                     if p["name"] == "served-web"]
            if found and found[0].get("ready"):
                served = found[0]
                break
            time.sleep(0.5)
        self.assertTrue(served, "the service never reported healthy")
        self.assertEqual(served["state"], "running")

        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port,
                                    timeout=5) as response:
            self.assertEqual(response.status, 200)

        remove = subprocess.run(
            [sys.executable, cli, "--dir", str(self.tmp), "--index", str(index), "remove", "served", "-y"],
            capture_output=True, text=True)
        self.assertEqual(remove.returncode, 0, remove.stdout + remove.stderr)
        self.assertFalse((self.tmp / "server/mods/served-1.0.0+1.21.jar").exists())
        self.assertNotIn("[services.served-web]",
                         (self.tmp / "w4ve.toml").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
