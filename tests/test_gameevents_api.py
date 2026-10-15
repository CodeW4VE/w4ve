"""Actual local HTTP/SDK: source capabilities cannot become operator identities."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/client")]
import api
from gameevents import GameEvents
from w4ve_client import W4VE, Denied, W4VEError


class GameEventApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True)
        self.source = self.journal.open_source()
        self.api = api.Api(self.root, address="127.0.0.1:0", game_events=self.journal, tokens_required=True)
        self.assertTrue(self.api.start()[0])
        self.addCleanup(lambda: self.api.stop())
        self.credentials = {scope: self.api.tokens.issue(scope, scope) for scope in api.SCOPES}
        self.event = {"schema": 1, "boot": self.source.boot, "id": uuid.uuid4().hex,
                      "sequence": 1, "kind": "server.started", "occurred": int(time.time()), "data": {}}

    def client(self, token):
        return W4VE(address="127.0.0.1:%d" % self.api.httpd.server_address[1], token=token, timeout=4)

    def request(self, path, token="", content=b"{}", **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.api.httpd.server_address[1], timeout=4)
        try:
            connection.request("POST", path, content, {"Authorization": "Bearer " + token, **headers})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_operator_and_identity_tokens_cannot_impersonate_a_source(self):
        for token in ("", "wrong", *self.credentials.values()):
            code, _ = self.request("/v1/game/source/append", token, b"invalid" * 4000)
            self.assertEqual(code, 401)  # Authentication precedes JSON parsing.
        source = self.client(self.source.token)
        self.assertTrue(source.game_source_append(self.event)["ok"])
        self.assertEqual(source.game_source_status()["sequence"], 1)
        for path in ("/v1/identity/health", "/v1/server/stop", "/v1/game/events/poll"):
            with self.subTest(path=path), self.assertRaises(Denied):
                source.call("POST", path, {})

    def test_game_content_is_not_available_to_regular_reader_or_identity_service(self):
        self.client(self.source.token).game_source_append(self.event)
        for scope in ("read", "write", "identity"):
            with self.assertRaises(Denied):
                self.client(self.credentials[scope]).game_events()
        self.api.tokens_required = False
        with self.assertRaises(Denied):
            self.client("").game_events()
        result = self.client(self.credentials["admin"]).game_events()
        self.assertEqual(result["events"][0]["kind"], "server.started")
        self.assertNotIn(self.source.token, json.dumps(result))

    def test_large_or_malformed_source_payload_never_mutates_and_leaks_no_content(self):
        for content in (b"secret-content" * 2000, b"{", b"[]"):
            code, answer = self.request("/v1/game/source/append", self.source.token, content)
            self.assertEqual(code, 400)
            self.assertNotIn("secret-content", json.dumps(answer))
        self.assertEqual(self.client(self.source.token).game_source_status()["sequence"], 0)
        forged = {**self.event, "boot": uuid.uuid4().hex}
        code, _ = self.request("/v1/game/source/append", self.source.token, json.dumps(forged).encode())
        self.assertEqual(code, 401)

    def test_retry_is_idempotent_and_conflict_is_409(self):
        producer = self.client(self.source.token)
        first = producer.game_source_append(self.event)
        self.assertEqual(producer.game_source_append(self.event)["cursor"], first["cursor"])
        different = {**self.event, "kind": "server.stopping"}
        code, _ = self.request("/v1/game/source/append", self.source.token, json.dumps(different).encode())
        self.assertEqual(code, 409)
        self.assertEqual(len(self.client(self.credentials["admin"]).game_events()["events"]), 1)

    def test_journal_pressure_returns_retryable_status_without_advancing_sequence(self):
        self.journal.max_events = 1
        producer = self.client(self.source.token)
        producer.game_source_append(self.event)
        next_event = {**self.event, "id": uuid.uuid4().hex, "sequence": 2, "kind": "server.stopping"}
        code, _ = self.request("/v1/game/source/append", self.source.token, json.dumps(next_event).encode())
        self.assertEqual(code, 429)
        self.assertEqual(producer.game_source_status()["sequence"], 1)
        self.journal.max_events = 2
        self.assertEqual(producer.game_source_append(next_event)["sequence"], 2)

    def test_ingress_errors_hide_proof_and_payload_but_leave_api_usable(self):
        with patch.object(self.journal, "append", side_effect=RuntimeError("private " + self.source.token)):
            code, answer = self.request("/v1/game/source/append", self.source.token, json.dumps(self.event).encode())
        self.assertEqual(code, 500)
        self.assertNotIn(self.source.token, json.dumps(answer))
        self.assertNotIn("private", json.dumps(answer))
        self.assertEqual(self.client(self.source.token).game_source_status()["sequence"], 0)

    def test_restart_reuses_persisted_source_capability_but_never_exposes_it(self):
        self.client(self.source.token).game_source_append(self.event)
        self.api.stop()
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True)
        self.api = api.Api(self.root, address="127.0.0.1:0", game_events=self.journal)
        self.assertTrue(self.api.start()[0])
        self.assertEqual(self.client(self.source.token).game_source_status()["sequence"], 1)
        self.assertTrue(self.client(self.source.token).game_source_append(self.event)["duplicate"])
        self.assertNotIn(self.source.token, json.dumps(self.client("").hello()))

    def test_disabled_source_does_not_create_a_journal_on_an_ordinary_api(self):
        self.api.game_events = None
        code, _ = self.request("/v1/game/source/append", self.source.token, json.dumps(self.event).encode())
        self.assertEqual(code, 503)
        with self.assertRaises(W4VEError):
            self.client(self.credentials["admin"]).game_events()


if __name__ == "__main__":
    unittest.main()
