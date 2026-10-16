"""A historical capture keeps the origin enrolled before its boot, never after."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from gameevents import GameEvents, SourceRuntime, SourceDenied
from identity import Authority


class CapturedBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.authority = Authority(self.root / "w4ve/identity/authority.sqlite3", clock=lambda: self.now)
        self.binding = self.authority.provision_binding("chat-piece", "chat", [
            {"id": "channel:general", "name": "General", "operations": ["chat.read", "chat.publish"]}])
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True, clock=lambda: self.now)
        self.player, self.connection = uuid.uuid4().hex, uuid.uuid4().hex

    def capture(self, source):
        result = None
        for sequence, (kind, data) in enumerate([
            ("server.started", {}), ("player.joined", {"uuid": self.player, "name": "Fixture", "connection": self.connection}),
            ("player.chat", {"uuid": self.player, "connection": self.connection, "text": "captured", "signature_sha256": ""}),
            ("player.left", {"uuid": self.player, "connection": self.connection})], 1):
            event = {"schema": 1, "boot": source.boot, "id": uuid.uuid4().hex, "sequence": sequence,
                     "kind": kind, "occurred": self.now, "data": data}
            self.journal.append(source, event)
            if kind == "player.chat":
                result = event
        return self.journal.poll(chat=True)["epoch"], result

    def test_history_survives_leave_retirement_and_reopen_with_the_original_binding(self):
        snapshot = self.authority.capture_bindings()
        source = self.journal.open_source(bindings=snapshot)
        epoch, event = self.capture(source)
        snapshot["owners"].clear()
        self.journal.retire_source(source)
        reopened = GameEvents(self.journal.path, chat=True, clock=lambda: self.now)
        captured = reopened.captured_chat(epoch, source.boot, event["id"])
        origin = captured["bindings"]["owners"]["chat-piece"]
        self.assertEqual(origin["community"], self.binding["community"])
        self.assertEqual(origin["instance"], self.binding["instance"])
        self.assertEqual(captured["event"]["data"]["name"], "Fixture")
        self.assertEqual(captured["event"]["data"]["connection"], self.connection)
        self.assertEqual(reopened.roster(source)["players"], [])
        self.assertNotIn(self.binding["secret"], json.dumps(captured))
        self.assertNotIn(source.token, json.dumps(captured))

    def test_a_pre_enrollment_boot_never_acquires_a_binding_from_later_configuration(self):
        source = self.journal.open_source()
        epoch, event = self.capture(source)
        self.assertTrue(self.authority.capture_bindings()["owners"])
        self.assertEqual(self.journal.captured_chat(epoch, source.boot, event["id"])["bindings"], {})

    def test_wrong_reference_expired_capture_and_disabled_chat_cannot_be_recovered(self):
        source = self.journal.open_source(bindings=self.authority.capture_bindings())
        epoch, event = self.capture(source)
        for reference in ((uuid.uuid4().hex, source.boot, event["id"]),
                          (epoch, uuid.uuid4().hex, event["id"]), (epoch, source.boot, uuid.uuid4().hex)):
            with self.assertRaises(SourceDenied):
                self.journal.captured_chat(*reference)
        joined = self.journal.poll()["events"][1]
        with self.assertRaises(SourceDenied):
            self.journal.captured_chat(epoch, source.boot, joined["id"])
        self.journal.chat = False
        with self.assertRaises(SourceDenied):
            self.journal.captured_chat(epoch, source.boot, event["id"])
        self.journal.chat = True
        self.now += self.journal.retention + 1
        with self.assertRaises(SourceDenied):
            self.journal.captured_chat(epoch, source.boot, event["id"])

    def test_disabled_issuer_is_not_snapshotted_and_invalid_binding_fields_are_refused(self):
        self.authority.register_instance(self.binding["community"], self.binding["instance"], enabled=False)
        self.assertEqual(self.authority.capture_bindings()["owners"], {})
        invalid = [[], {"epoch": "wrong", "owners": {}},
                   {"epoch": "a" * 32, "owners": {"piece": {"secret": "untrusted"}}},
                   {"epoch": "a" * 32, "owners": {str(i): {"community": "c", "instance": "i", "audience": "a"}
                                                       for i in range(65)}}]
        for bindings in invalid:
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                self.journal.open_source(bindings=bindings)

    def test_runtime_handover_keeps_snapshot_when_current_authority_policy_changes(self):
        live = set()
        manager = SourceRuntime(self.root, {"enabled": True, "chat": True}, lambda pid, started: (pid, started) in live)
        manager.journal.clock = lambda: self.now
        api = SimpleNamespace(address=None, socket_path=self.root / "w4ve/run/api.sock")
        source = manager.prepare(api)
        live.add((100, 1000))
        manager.bind(source, 100, 1000)
        self.journal = manager.journal
        epoch, event = self.capture(source)
        before = self.journal.captured_chat(epoch, source.boot, event["id"])["bindings"]
        self.authority.register_instance(self.binding["community"], self.binding["instance"], enabled=False)
        successor = SourceRuntime(self.root, {"enabled": True, "chat": True}, lambda pid, started: (pid, started) in live)
        successor.journal.clock = lambda: self.now
        self.assertTrue(successor.reattach(SimpleNamespace(pid=100, starttime=1000), api))
        self.assertEqual(successor.journal.captured_chat(epoch, source.boot, event["id"])["bindings"], before)
        self.assertEqual(self.authority.capture_bindings()["owners"], {})

    def test_existing_schema_is_migrated_to_an_explicitly_empty_binding(self):
        path = self.root / "legacy/events.sqlite3"
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE game_sources (
                boot TEXT PRIMARY KEY,digest TEXT NOT NULL UNIQUE,label TEXT NOT NULL,
                created INTEGER NOT NULL,retired INTEGER,last_sequence INTEGER NOT NULL DEFAULT 0,
                phase TEXT NOT NULL DEFAULT 'new',last_occurred INTEGER NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO game_sources(boot,digest,label,created) VALUES (?,?,?,?)",
                       ("a" * 32, "historical-digest", "server", 1000))
        GameEvents(path, chat=True, clock=lambda: self.now)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT bindings FROM game_sources").fetchone()[0], "{}")


if __name__ == "__main__":
    unittest.main()
