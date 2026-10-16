"""Reader grants on actual HTTP, distinct from producer/identity/operator roles."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

import api
from gameevents import GameEvents
from w4ve_client import W4VE, Denied


class EventServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "w4ve.toml").write_text(
            '[services.presence]\nw4ve_events="presence"\n'
            '[services.chat]\nw4ve_events="chat"\n'
            '[services.other]\nw4ve_events="chat"\n', encoding="utf-8")
        self.tokens = api.Tokens(self.root)
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True)
        self.source = self.journal.open_source()
        self.api = api.Api(self.root, address="127.0.0.1:0", game_events=self.journal,
                           tokens_required=True)
        self.assertTrue(self.api.start()[0])
        self.addCleanup(self.api.stop)
        account, connection = uuid.uuid4().hex, uuid.uuid4().hex
        events = [("server.started", {}), ("player.joined", {
            "uuid": account, "name": "Fixture", "connection": connection}),
            ("player.chat", {"uuid": account, "connection": connection,
                             "text": "private fixture content", "signature_sha256": ""})]
        for sequence, (kind, data) in enumerate(events, 1):
            self.journal.append(self.source, {"schema": 1, "boot": self.source.boot,
                "id": uuid.uuid4().hex, "sequence": sequence, "kind": kind,
                "occurred": int(time.time()), "data": data})

    def client(self, token):
        return W4VE(address="127.0.0.1:%d" % self.api.httpd.server_address[1], token=token, timeout=4)

    def test_presence_reader_advances_over_hidden_chat_without_content_or_extra_powers(self):
        token = self.tokens.service_events("presence", "presence")
        client = self.client(token)
        result = client.game_events()
        self.assertEqual([event["kind"] for event in result["events"]], ["server.started", "player.joined"])
        self.assertEqual(result["cursor"], 3)
        self.assertNotIn("private fixture content", json.dumps(result))
        self.assertNotIn(self.source.token, json.dumps(result))
        with self.assertRaises(Denied):
            client.game_events(chat=True)
        for path in ("/v1/identity/health", "/v1/server/stop", "/v1/game/source/status"):
            with self.subTest(path=path), self.assertRaises(Denied):
                client.call("POST", path, {})
        with self.assertRaises(Denied):
            client.status()

    def test_chat_requires_both_service_grant_and_capture_policy(self):
        client = self.client(self.tokens.service_events("chat", "chat"))
        self.assertEqual(len(client.game_events(chat=True)["events"]), 3)
        self.assertEqual(len(client.game_events()["events"]), 2)
        self.journal.chat = False
        with self.assertRaises(Denied):
            client.game_events(chat=True)
        self.assertEqual(len(client.game_events()["events"]), 2)

    def test_reuse_downgrade_and_removal_revoke_previous_token_on_existing_socket_api(self):
        old = self.tokens.service_events("chat", "chat")
        identity = self.tokens.service_identity("chat")
        other = self.tokens.service_events("other", "chat")
        self.assertEqual(self.tokens.service_events("chat", "chat"), old)
        lower = self.tokens.service_events("chat", "presence")
        self.assertNotEqual(old, lower)
        with self.assertRaises(Denied):
            self.client(old).game_events(chat=True)
        self.tokens.service_events("chat", None)
        with self.assertRaises(Denied):
            self.client(lower).game_events()
        self.assertTrue(self.client(other).game_events(chat=True)["ok"])
        self.assertTrue(self.client(identity).identity_health()["ok"])
        self.assertNotIn(other, json.dumps(self.tokens.listing()))

    def test_hand_edited_policy_missing_service_and_invalid_config_fail_closed_immediately(self):
        client = self.client(self.tokens.service_events("chat", "chat"))
        self.assertTrue(client.game_events(chat=True)["ok"])
        config = self.root / "w4ve.toml"
        for body in ('[services.chat]\nw4ve_events="presence"\n', '', '{broken',
                     '[services.chat]\nw4ve_events=["chat"]\n'):
            config.write_text(body, encoding="utf-8")
            with self.subTest(body=body), self.assertRaises(Denied):
                client.game_events(chat=True)
        config.unlink()
        with self.assertRaises(Denied):
            client.game_events(chat=True)

    def test_service_edit_and_removal_revoke_events_without_revoking_other_services(self):
        from w4ve import Server
        (self.root / "w4ve.toml").write_text('[server]\nkind="server"\n', encoding="utf-8")
        server = Server(self.root)
        fields = {"command": ["python3", "main.py"], "w4ve_events": "chat"}
        server.set_service("chat", fields)
        token = self.tokens.service_events("chat", "chat")
        server.set_service("chat", fields)
        self.assertIsNotNone(self.tokens.check(token))
        server.set_service("chat", dict(fields, w4ve_events="presence"))
        self.assertIsNone(self.tokens.check(token))
        token = self.tokens.service_events("chat", "presence")
        other = self.tokens.service_events("other", "presence")
        server.remove_service("chat")
        self.assertIsNone(self.tokens.check(token))
        self.assertIsNotNone(self.tokens.check(other))

    def test_conflicting_registry_is_preserved(self):
        token = self.tokens.issue("service-events:chat", "admin")
        for policy in ("presence", "chat", None):
            with self.assertRaises(ValueError):
                self.tokens.service_events("chat", policy)
        self.assertEqual(self.tokens.check(token)["scope"], "admin")


class EventBootstrapTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "guardian requires Linux")
    def test_ordinary_service_does_not_depend_on_api_credentials(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            registry = api.Tokens(temp)
            registry.path.parent.mkdir()
            registry.path.write_text("{broken", encoding="utf-8")
            service = runtime.Guardian(temp).add_service("plain", ["true"], temp)
            self.assertEqual(service.env["W4VE_EVENTS_TOKEN"], "")
            self.assertEqual(registry.path.read_text(), "{broken")

    @unittest.skipIf(os.name == "nt", "guardian requires Linux")
    def test_events_only_service_gets_separate_credentials_and_enables_authenticated_api(self):
        import runtime
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "w4ve.toml").write_text('[services.reader]\nw4ve_events="chat"\n')
            guardian = runtime.Guardian(temp, {"api": {"listen": "127.0.0.1:0"}})
            service = guardian.add_service("reader", ["true"], temp, w4ve_events="chat")
            self.assertNotIn("W4VE_IDENTITY_TOKEN", service.env)
            token = service.env["W4VE_EVENTS_TOKEN"]
            self.assertEqual(api.Tokens(temp).check(token)["scope"], "game-chat")
            guardian.snapshot()
            self.assertNotIn(token, guardian.state_path.read_text())
            ok, message = guardian.start_api()
            self.assertTrue(ok, message)
            try:
                self.assertTrue(guardian.api.tokens_required)
                guardian.api.game_events = GameEvents(Path(temp) / "w4ve/game/events.sqlite3", chat=True)
                client = W4VE(address="127.0.0.1:%d" % guardian.api.httpd.server_address[1], token=token)
                self.assertTrue(client.game_events(chat=True)["ok"])
                with self.assertRaises(Denied):
                    client.identity_health()
            finally:
                guardian.stop_api()
            disabled = runtime.Guardian(temp, {"api": {"enabled": False}})
            with self.assertRaises(ValueError):
                disabled.add_service("no", ["true"], temp, w4ve_events="chat")

    @unittest.skipIf(os.name == "nt", "runtime configuration imports Linux guardian")
    def test_manifest_config_and_runtime_preserve_the_explicit_policy(self):
        import package
        import runtime
        base = {"name": "chat", "command": ["python3", "main.py"]}
        for policy in ("presence", "chat"):
            self.assertFalse(package.service_problems(dict(base, w4ve_events=policy)))
            parsed = runtime.services_from({"services": {"chat": dict(base, w4ve_events=policy)}})
            self.assertEqual(parsed[0]["w4ve_events"], policy)
        for policy in (True, [], "admin", "identity"):
            self.assertTrue(package.service_problems(dict(base, w4ve_events=policy)))


if __name__ == "__main__":
    unittest.main()
