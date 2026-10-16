"""Resolve only stored capture IDs, without minting online player credentials."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

import api
from gameevents import GameEvents
from identity import Authority
from w4ve_client import W4VE, Denied, W4VEError


class CapturedChatApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "w4ve.toml").write_text('[services.chat]\nw4ve_events="chat"\n', encoding="utf-8")
        self.authority = Authority(self.root / "w4ve/identity/authority.sqlite3")
        self.binding = self.authority.provision_binding("chat-piece", "chat", [
            {"id": "channel:general", "name": "General", "operations": ["chat.read", "chat.publish"]}])
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True)
        self.source = self.journal.open_source(bindings=self.authority.capture_bindings())
        self.player, self.context = uuid.uuid4().hex, uuid.uuid4().hex
        events = [("server.started", {}), ("player.joined", {
            "uuid": self.player, "name": "Fixture", "connection": self.context}),
            ("player.chat", {"uuid": self.player, "connection": self.context,
                             "text": "captured before leaving", "signature_sha256": ""}),
            ("player.left", {"uuid": self.player, "connection": self.context})]
        for sequence, (kind, data) in enumerate(events, 1):
            event = {"schema": 1, "boot": self.source.boot, "id": uuid.uuid4().hex,
                     "sequence": sequence, "kind": kind, "occurred": int(time.time()), "data": data}
            self.journal.append(self.source, event)
            if kind == "player.chat":
                self.event = event
        self.epoch = self.journal.poll(chat=True)["epoch"]
        self.api = api.Api(self.root, address="127.0.0.1:0", game_events=self.journal, tokens_required=True)
        self.assertTrue(self.api.start()[0])
        self.addCleanup(self.api.stop)
        self.client = self.with_token(self.api.tokens.service_events("chat", "chat"))
        self.reference = {"epoch": self.epoch, "boot": self.source.boot, "id": self.event["id"],
                          "owner": "chat-piece", "resource": "channel:general"}

    def with_token(self, token):
        return W4VE(address="127.0.0.1:%d" % self.api.httpd.server_address[1], token=token, timeout=4)

    def resolve(self):
        return self.client.game_event_authorize_chat(self.epoch, self.source.boot,
                                                     self.event["id"], "chat-piece", "channel:general")

    def test_departed_player_and_retired_boot_authorize_only_the_stored_actor_without_a_session(self):
        self.journal.retire_source(self.source)
        answer = self.resolve()
        self.assertTrue(answer["allowed"])
        self.assertEqual(answer["kind"], "captured-chat")
        self.assertEqual(answer["community"], self.binding["community"])
        self.assertEqual(answer["origin"], self.binding["instance"])
        self.assertEqual(answer["subject"], "minecraft:" + self.player)
        self.assertEqual(answer["connection"], self.context)
        self.assertEqual(answer["event"]["data"]["text"], self.event["data"]["text"])
        self.assertEqual(self.journal.roster(self.source)["players"], [])
        self.assertNotIn(self.source.token, json.dumps(answer))
        self.assertNotIn(self.binding["secret"], json.dumps(answer))
        self.assertNotIn("token", answer)
        with self.authority._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM identity_sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM identity_instance_sessions").fetchone()[0], 0)

    def test_current_sharing_grants_and_blocks_are_checked_on_each_resolution(self):
        community, instance = self.binding["community"], self.binding["instance"]
        first = self.resolve()
        self.authority.set_grants(community, "chat", "channel:general", "*", ["chat.read"])
        self.assertFalse(self.resolve()["allowed"])
        self.authority.set_grants(community, "chat", "channel:general", "*", ["chat.read", "chat.publish"])
        self.authority.set_blocked(community, "minecraft:" + self.player, True)
        blocked = self.resolve()
        self.assertFalse(blocked["allowed"])
        self.assertNotEqual(blocked["policy_revision"], first["policy_revision"])
        self.authority.set_blocked(community, "minecraft:" + self.player, False)
        self.authority.set_instance_resources(community, instance, "chat", [])
        self.assertFalse(self.resolve()["allowed"])
        self.authority.set_instance_resources(community, instance, "chat", ["channel:general"])
        self.assertTrue(self.resolve()["allowed"])
        self.authority.register_instance(community, instance, enabled=False)
        with self.assertRaises(Denied):
            self.resolve()

    def test_source_reader_identity_and_operator_read_tokens_cannot_authorize_chat(self):
        for token in (self.source.token, "", self.api.tokens.service_identity("chat"),
                      self.api.tokens.issue("presence-only", "game-events"), self.api.tokens.issue("reader", "read")):
            with self.subTest(token_kind="redacted"), self.assertRaises(Denied):
                self.with_token(token).call("POST", "/v1/game/events/authorize-chat", self.reference)
        (self.root / "w4ve.toml").write_text('[services.chat]\nw4ve_events="presence"\n')
        with self.assertRaises(Denied):
            self.resolve()

    def test_no_payload_or_alternative_actor_is_accepted_from_the_reader(self):
        for field, value in (("uuid", uuid.uuid4().hex), ("text", "forged"), ("connection", uuid.uuid4().hex)):
            with self.subTest(field=field), self.assertRaises(W4VEError):
                self.client.call("POST", "/v1/game/events/authorize-chat", {**self.reference, field: value})
        for field, value in (("owner", "another-piece"), ("boot", uuid.uuid4().hex),
                             ("id", uuid.uuid4().hex), ("epoch", uuid.uuid4().hex)):
            with self.subTest(field=field), self.assertRaises(Denied):
                self.client.call("POST", "/v1/game/events/authorize-chat", {**self.reference, field: value})
        answer = self.client.call("POST", "/v1/game/events/authorize-chat",
                                  {**self.reference, "resource": "channel:unshared"})
        self.assertFalse(answer["allowed"])

    def test_replacement_authority_epoch_and_expiry_do_not_relabel_old_captures(self):
        with self.authority._db() as db:
            db.execute("UPDATE identity_meta SET value=? WHERE key='epoch'", (uuid.uuid4().hex,))
        with self.assertRaises(Denied):
            self.resolve()
        self.journal.clock = lambda: time.time() + self.journal.retention + 1
        with self.assertRaises(Denied):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
