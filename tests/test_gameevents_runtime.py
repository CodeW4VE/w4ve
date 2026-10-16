"""Private descriptor recovery and PID/starttime binding, without game binaries."""
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
from pathlib import Path
import os
from types import SimpleNamespace
import tempfile
import unittest

from gameevents import SourceRuntime, SourceDenied, EventConflict


class SourceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.live = set()
        self.bridge = self.manager()
        self.api = SimpleNamespace(address=None, socket_path=self.root / "w4ve/run/api.sock")
        self.proc = SimpleNamespace(pid=100, starttime=5000)

    def manager(self):
        manager = SourceRuntime(self.root, {"enabled": True, "chat": True}, lambda pid, started: (pid, started) in self.live)
        manager.journal.clock = lambda: self.now
        return manager

    def start(self):
        source = self.bridge.prepare(self.api)
        self.live.add((self.proc.pid, self.proc.starttime))
        self.bridge.bind(source, self.proc.pid, self.proc.starttime)
        return source

    def test_handover_preserves_boot_secret_and_descriptor_uses_local_endpoint(self):
        source = self.start()
        before = json.loads(self.bridge.path.read_text())
        successor = self.manager()
        self.api.address = "0.0.0.0:0"
        self.api.httpd = SimpleNamespace(server_address=("0.0.0.0", 4321))
        self.now += 1
        self.assertTrue(successor.reattach(self.proc, self.api))
        after = json.loads(successor.path.read_text())
        self.assertEqual((after["boot"], after["token"]), (before["boot"], source.token))
        self.assertEqual(after["api_address"], "127.0.0.1:4321")
        self.assertEqual(after["last_alive"], self.now)
        self.assertEqual(successor.journal.authenticate(source.token), source)

    def test_capture_spool_metadata_is_private_and_stable_across_handover(self):
        self.start()
        before = json.loads(self.bridge.path.read_text())
        spool = self.root / "w4ve/game/capture-spool"
        self.assertEqual(before["spool"], str(spool))
        self.assertEqual(before["retention"], self.bridge.journal.retention)
        self.assertTrue(before["chat"])
        if os.name != "nt":
            self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
        capture = spool / "preserved-fixture"
        capture.write_bytes(b"private fixture")
        successor = self.manager()
        self.assertTrue(successor.reattach(self.proc, self.api))
        self.assertEqual(json.loads(successor.path.read_text())["spool"], before["spool"])
        self.assertEqual(capture.read_bytes(), b"private fixture")

    @unittest.skipIf(os.name == "nt", "symlink fixture requires Linux")
    def test_capture_spool_symlink_is_rejected_without_modifying_its_target(self):
        spool = self.root / "w4ve/game/capture-spool"
        spool.rmdir()
        target = self.root / "unrelated-directory"
        target.mkdir(mode=0o755)
        (target / "original").write_bytes(b"preserved")
        spool.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager()
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / "original").read_bytes(), b"preserved")

    def test_live_descriptor_cannot_be_replaced_and_restart_gets_a_new_capability(self):
        source = self.start()
        before = self.bridge.path.read_bytes()
        with self.assertRaises(EventConflict):
            self.bridge.prepare(self.api)
        self.assertEqual(self.bridge.path.read_bytes(), before)
        self.live.clear()
        self.now += 2
        self.bridge.reconcile()
        fresh = self.bridge.prepare(self.api)
        self.assertNotEqual(fresh.boot, source.boot)
        self.assertNotEqual(fresh.token, source.token)
        self.assertTrue(self.bridge.journal.source_state(source)["retired"])

    def test_reused_pid_cannot_reattach_to_the_previous_game_capability(self):
        source = self.start()
        self.live = {(self.proc.pid, self.proc.starttime + 1)}
        replacement = SimpleNamespace(pid=self.proc.pid, starttime=self.proc.starttime + 1)
        self.assertFalse(self.bridge.reattach(replacement, self.api))
        self.assertTrue(self.bridge.journal.source_state(source)["retired"])

    def test_long_outage_does_not_renew_an_old_boot_capture_window(self):
        source = self.start()
        self.live.clear()
        self.now = 5000
        successor = self.manager()
        self.assertFalse(successor.reattach(self.proc, self.api))
        self.assertTrue(successor.journal.source_state(source)["retired"])
        with self.assertRaises(SourceDenied):
            successor.journal.authenticate(source.token)

    def test_never_bound_pending_source_is_retired_and_not_adopted_as_a_player_session(self):
        source = self.bridge.prepare(self.api)
        self.now += 11
        self.bridge.reconcile()
        self.assertTrue(self.bridge.journal.source_state(source)["retired"])
        self.assertEqual(self.bridge.journal.roster(source)["players"], [])

    def test_corrupt_or_foreign_descriptor_is_preserved_without_creating_source(self):
        self.bridge.path.parent.mkdir(parents=True, exist_ok=True)
        for content in (b"not-json-private", b'{"schema":1,"owner":"another-tool"}'):
            self.bridge.path.write_bytes(content)
            with self.assertRaises(ValueError):
                self.bridge.prepare(self.api)
            self.assertEqual(self.bridge.path.read_bytes(), content)

    def test_concurrent_guardians_cannot_overwrite_new_endpoint_with_a_stale_heartbeat(self):
        source = self.start()
        successor = self.manager()
        ticks = itertools.count(1001)
        self.bridge.journal.clock = successor.journal.clock = lambda: next(ticks)
        new_api = SimpleNamespace(address="127.0.0.1:4321", socket_path=self.api.socket_path,
                                  httpd=SimpleNamespace(server_address=("127.0.0.1", 4321)))
        def observe():
            for _ in range(12):
                self.bridge.reconcile()
        def reattach():
            for _ in range(12):
                self.assertTrue(successor.reattach(self.proc, new_api))
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in (pool.submit(observe), pool.submit(reattach)):
                future.result(timeout=20)
        descriptor = json.loads(self.bridge.path.read_text())
        self.assertEqual(descriptor["api_address"], "127.0.0.1:4321")
        self.assertEqual(descriptor["token"], source.token)

    def test_unrecognized_policy_does_not_enable_a_source_by_accident(self):
        for policy in ({"enabled": False}, {"enabled": "true"}, {"enabled": True, "chta": True}):
            with self.assertRaises(ValueError):
                SourceRuntime(self.root, policy, lambda *args: True)


if __name__ == "__main__":
    unittest.main()
