"""Service credential reuse and runtime injection, without production resources."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import api
ROOT = Path(api.__file__).resolve().parent


class ServiceTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_concurrent_registration_reuses_one_identity_token(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            tokens = list(pool.map(lambda _: api.Tokens(self.root).service_identity("chat"), range(6)))
        self.assertEqual(len(set(tokens)), 1)
        registry = api.Tokens(self.root)
        self.assertEqual(registry.check(tokens[0])["scope"], "identity")
        self.assertNotIn(tokens[0], json.dumps(registry.listing()))
        other = registry.issue("operator", "admin")
        self.assertEqual(registry.revoke("service:chat"), 1)
        self.assertIsNone(registry.check(tokens[0]))
        self.assertIsNotNone(registry.check(other))
        self.assertNotEqual(registry.service_identity("chat"), tokens[0])

    def test_conflicting_scope_and_corrupt_registry_are_preserved(self):
        registry = api.Tokens(self.root)
        token = registry.issue("service:chat", "admin")
        with self.assertRaises(ValueError):
            registry.service_identity("chat")
        self.assertEqual(registry.check(token)["scope"], "admin")
        registry.path.write_text("{broken", encoding="utf-8")
        for mutate in (lambda: registry.issue("operator"), lambda: registry.revoke("operator"),
                       lambda: registry.service_identity("chat")):
            with self.assertRaises(ValueError):
                mutate()
            self.assertEqual(registry.path.read_text(), "{broken")

    def test_separate_cli_processes_do_not_lose_writes(self):
        script = "import sys; from api import Tokens; Tokens(sys.argv[1]).issue(sys.argv[2], 'read')"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.root), "op%d" % i],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for i in range(6)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, err.decode())
            self.assertEqual(out, b"")
        self.assertEqual(len(api.Tokens(self.root).listing()), 6)

    def test_removing_service_revokes_only_its_credential(self):
        from w4ve import Server
        (self.root / "w4ve.toml").write_text('[server]\nkind = "server"\n')
        server = Server(self.root)
        registry = api.Tokens(self.root)
        removed = registry.service_identity("chat")
        kept = registry.service_identity("other")
        server.set_service("chat", {"command": ["python3", "main.py"], "w4ve_api": "identity"})
        server.remove_service("chat")
        self.assertIsNone(registry.check(removed))
        self.assertIsNotNone(registry.check(kept))
        self.assertNotIn("chat", server.config.get("services", {}))


@unittest.skipIf(os.name == "nt", "guardian requires Linux")
class RuntimeBootstrapTests(unittest.TestCase):
    def test_exited_unreaped_process_is_not_alive(self):
        import runtime
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                stat = Path("/proc/%d/stat" % process.pid).read_text()
                fields = stat[stat.rfind(")") + 1:].split()
                if fields[0] == "Z":
                    break
                time.sleep(0.02)
            self.assertEqual(fields[0], "Z")
            self.assertFalse(runtime.alive(process.pid))
            self.assertFalse(runtime.alive(process.pid, runtime.starttime_of(process.pid)))
        finally:
            process.wait(timeout=5)

    def test_old_api_shutdown_does_not_unlink_replacement_socket(self):
        from w4ve_client import W4VE
        with tempfile.TemporaryDirectory() as temp:
            old, new = api.Api(temp), api.Api(temp)
            self.assertTrue(old.start()[0])
            try:
                self.assertTrue(new.start()[0])
                old.stop()
                self.assertTrue(W4VE(temp).hello()["ok"])
            finally:
                old.stop()
                new.stop()

    def test_first_service_request_finds_api_before_service_start(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            probe = Path(temp) / "probe.json"
            code = ("import os,json; from pathlib import Path; from w4ve_client import W4VE; "
                    "result=W4VE(os.environ['W4VE_SERVER_ROOT'], "
                    "token=os.environ['W4VE_IDENTITY_TOKEN']).identity_health(); "
                    "result['game_dir']=os.environ['W4VE_GAME_DIR']; "
                    "Path('probe.json').write_text(json.dumps(result))")
            host = ("import sys; import runtime; "
                    "g=runtime.Guardian(sys.argv[1], {'plugins':False}); "
                    "g.add_service('chat', [sys.executable,'-c',sys.argv[2]],sys.argv[1], "
                    "restart='never',w4ve_api='identity'); g.run()")
            environment = dict(os.environ, PYTHONPATH=str(ROOT))
            process = subprocess.Popen([sys.executable, "-c", host, temp, code], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not probe.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                journal = Path(temp) / "w4ve/run/journal.log"
                self.assertTrue(probe.exists(), journal.read_text() if journal.exists() else "guardian failed to start")
                self.assertTrue(json.loads(probe.read_text())["ok"])
                self.assertEqual(json.loads(probe.read_text())["game_dir"], str(Path(temp).resolve()))
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    out, err = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, err.decode())

    def test_runtime_injects_sdk_and_reuses_credential_without_leaking_status(self):
        import runtime
        from w4ve_client import W4VE
        with tempfile.TemporaryDirectory() as temp:
            guardian = runtime.Guardian(temp)
            service = guardian.add_service("chat", [sys.executable, "-c", "pass"], temp,
                                           env={"PYTHONPATH": "/custom"}, w4ve_api="identity")
            self.assertEqual(service.env["W4VE_SERVER_ROOT"], temp)
            self.assertEqual(service.env["W4VE_GAME_DIR"], str(Path(temp).resolve()))
            self.assertTrue(service.env["PYTHONPATH"].endswith(os.pathsep + "/custom"))
            token = service.env["W4VE_IDENTITY_TOKEN"]
            again = runtime.Guardian(temp).add_service("chat", ["true"], temp, w4ve_api="identity")
            self.assertEqual(again.env["W4VE_IDENTITY_TOKEN"], token)
            guardian.snapshot()
            self.assertNotIn(token, guardian.state_path.read_text())
            ok, message = guardian.start_api()
            self.assertTrue(ok, message)
            try:
                self.assertTrue(W4VE(temp, token=token).identity_health()["ok"])
                from w4ve_client import Denied
                with self.assertRaises(Denied):
                    W4VE(temp, token=token).status()
                with self.assertRaises(Denied):
                    W4VE(temp).command("stop")
            finally:
                guardian.stop_api()

    def test_disabled_api_or_operator_scope_cannot_be_silently_overridden(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            for config, scope in (({"api": {"enabled": False}}, "identity"), ({}, "admin")):
                guardian = runtime.Guardian(temp, config)
                with self.assertRaises(ValueError):
                    guardian.add_service("chat", ["true"], temp, w4ve_api=scope)
                self.assertFalse(guardian.watched)
                self.assertFalse(api.Tokens(temp).listing())

    def test_service_game_directory_comes_from_config_not_piece_environment(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            guardian = runtime.Guardian(temp, {"server": {"game_dir": "games/technical"}})
            service = guardian.add_service("chat", ["true"], temp, w4ve_api="identity",
                                           env={"W4VE_GAME_DIR": "/incorrect-piece-default"})
            self.assertEqual(service.env["W4VE_GAME_DIR"], str((Path(temp) / "games/technical").resolve()))

    def test_registered_game_process_cwd_is_the_authoritative_directory(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            guardian = runtime.Guardian(temp, {"server": {"game_dir": "old-layout"}})
            current = Path(temp) / "actual-game"
            guardian.add_server(["true"], current, flavor="vanilla")
            service = guardian.add_service("chat", ["true"], temp, w4ve_api="identity")
            self.assertEqual(service.env["W4VE_GAME_DIR"], str(current.resolve()))

    def test_manifest_accepts_only_identity_service_scope(self):
        import package
        base = {"name": "chat", "command": ["python3", "main.py"]}
        self.assertFalse(package.service_problems(dict(base, w4ve_api="identity")))
        self.assertTrue(package.service_problems(dict(base, w4ve_api="admin")))


if __name__ == "__main__":
    unittest.main()
