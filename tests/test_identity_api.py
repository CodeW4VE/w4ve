"""Real loopback HTTP and SDK; no Minecraft/Discord or production access."""
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk/client"))
import api
from w4ve_client import W4VE, Denied, W4VEError
from test_identity import proof
from test_identity_instance import instance_proof


class IdentityApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = api.Api(self.temp.name, address="127.0.0.1:0", tokens_required=True)
        ok, message = self.api.start()
        self.assertTrue(ok, message)
        self.addCleanup(self.api.stop)
        address = "127.0.0.1:%d" % self.api.httpd.server_address[1]
        self.clients = {scope: W4VE(address=address, token=self.api.tokens.issue(scope, scope))
                        for scope in ("read", "write", "admin", "identity")}
        self.anonymous = W4VE(address=address)

    def admin(self, method, **body):
        return self.clients["admin"].call("POST", "/v1/identity/" + method, body)

    def mint(self):
        secret = self.admin("instances", community="alpha", instance="smp")["secret"]
        now = int(time.time())
        note = proof(secret, issued=now, expires=now + 100)
        return self.clients["identity"].identity_session(note, "wavechat")["token"]

    def test_only_admin_can_pair_grant_or_block(self):
        for scope in ("read", "write", "identity"):
            for route in ("instances", "grants", "block", "instance-resources"):
                with self.subTest(scope=scope, route=route):
                    with self.assertRaises(Denied):
                        self.clients[scope].call("POST", "/v1/identity/" + route, {})
        self.assertIsNone(self.api._authority)

    def test_anonymous_and_operator_reader_cannot_issue_player_sessions(self):
        for client in (self.anonymous, self.clients["read"], self.clients["write"]):
            with self.assertRaises(Denied):
                client.identity_session("not a proof", "wavechat")

    def test_identity_service_cannot_control_server(self):
        for route in ("/v1/server/stop", "/v1/plugins/reload", "/v1/command", "/v1/jobs"):
            with self.assertRaises(Denied):
                self.clients["identity"].call("POST", route, {})
        with self.assertRaises(Denied):
            self.clients["identity"].status()

    def test_identity_health_requires_scoped_credential(self):
        for client in (self.anonymous, self.clients["read"], self.clients["write"]):
            with self.assertRaises(Denied):
                client.identity_health()
        for scope in ("identity", "admin"):
            self.assertEqual(self.clients[scope].identity_health(),
                             {"ok": True, "service": "identity", "schema": 1, "capabilities": {
                                 "player_proof": 1, "instance_sessions": 1,
                                 "connection_context": 1, "resource_recipients": 1,
                                 "policy_revision": 1, "editable_profiles": 1}})
        self.api.tokens_required = False
        with self.assertRaises(Denied):
            self.anonymous.identity_health()

    def test_profile_sdk_requires_service_and_player_grants_and_rejects_claims(self):
        token = self.mint()
        service = self.clients['identity']
        with self.assertRaises(Denied):
            service.identity_profile(token, 'wavechat')
        self.admin('grants', community='alpha', audience='wavechat', resource='profile:self',
                   subject='*', operations=['profile.read', 'profile.edit'])
        first = service.identity_profile(token, 'wavechat')
        self.assertEqual(first['revision'], 0)
        changed = service.identity_update_profile(token, 'wavechat', {'display_name': 'Alias', 'bio': 'Community bio'}, 0)
        self.assertEqual(changed['account_name'], 'Alice')
        self.assertEqual(changed['revision'], 1)
        with self.assertRaises(W4VEError) as conflict:
            service.identity_update_profile(token, 'wavechat', {'display_name': 'Lost edit', 'bio': ''}, 0)
        self.assertEqual(conflict.exception.status, 409)
        for scope in ('read', 'write'):
            with self.assertRaises(Denied):
                self.clients[scope].identity_profile(token, 'wavechat')
        for body in ({'session': token, 'audience': 'wavechat', 'subject': 'forged'},
                     {'session': token, 'audience': 'wavechat', 'profile': None, 'revision': None}):
            with self.assertRaises(W4VEError):
                service.call('POST', '/v1/identity/profile-update', body)
        self.assertEqual(service.identity_profile(token, 'wavechat'), changed)

    def test_complete_sdk_session_grant_revoke_path(self):
        token = self.mint()
        service = self.clients["identity"]
        claims = service.identity_inspect(token, "wavechat")
        self.assertEqual(claims["origin"], "smp")
        self.assertTrue(claims["subject"].startswith("minecraft:"))
        self.assertNotIn("secret", claims)
        self.assertFalse(service.identity_authorize(token, "wavechat", "channel:general", "chat.read")["allowed"])
        self.admin("grants", community="alpha", audience="wavechat", resource="channel:general",
                   subject="*", operations=["chat.read"])
        self.assertTrue(service.identity_authorize(token, "wavechat", "channel:general", "chat.read")["allowed"])
        self.admin("block", community="alpha", subject=claims["subject"], blocked=True)
        with self.assertRaises(Denied):
            service.identity_inspect(token, "wavechat")

    def test_invalid_input_is_refused_and_api_survives(self):
        for data in ([], {}, {"community": False, "instance": "smp"}):
            with self.assertRaises(W4VEError):
                self.clients["admin"].call("POST", "/v1/identity/instances", data)
        with self.assertRaises(Denied) as caught:
            self.clients["identity"].identity_session("sensitive-invalid-proof", "wavechat")
        self.assertNotIn("sensitive-invalid-proof", str(caught.exception))
        self.assertTrue(self.anonymous.hello()["ok"])

    def test_instance_sdk_requires_explicit_resource_and_live_member_acl(self):
        secret = self.admin("instances", community="alpha", instance="smp")["secret"]
        now = int(time.time())
        service = self.clients["identity"]
        note = instance_proof(secret, issued=now, expires=now + 100)
        token = service.identity_instance_session(note, "wavechat")["token"]
        self.assertEqual(service.identity_instance_inspect(token, "wavechat")["kind"], "instance")
        with self.assertRaises(Denied):
            service.identity_inspect(token, "wavechat")
        alice = "466a0fbd7b87431cab58c1887f577deb"
        self.admin("grants", community="alpha", audience="wavechat", resource="channel:general",
                   subject="minecraft:" + alice, operations=["chat.read"])
        def recipients():
            return service.identity_instance_recipients(token, "wavechat", "channel:general",
                                                        "chat.read", [alice])["players"]
        self.assertEqual(recipients(), [])
        self.admin("instance-resources", community="alpha", instance="smp", audience="wavechat",
                   resources=["channel:general"])
        self.assertEqual(recipients(), [alice])
        self.admin("block", community="alpha", subject="minecraft:" + alice, blocked=True)
        self.assertEqual(recipients(), [])
        for client in (self.anonymous, self.clients["read"], self.clients["write"]):
            with self.assertRaises(Denied):
                client.identity_instance_inspect(token, "wavechat")
        with self.assertRaises(Denied):
            service.identity_instance_session(note, "wavechat")

    def test_identity_routes_require_tokens_even_with_other_local_checks_disabled(self):
        self.api.tokens_required = False
        with self.assertRaises(Denied):
            self.anonymous.identity_inspect("invalid", "wavechat")
        self.assertTrue(self.anonymous.hello()["ok"])

    def test_cli_can_issue_identity_scope_from_installed_runtime(self):
        (Path(self.temp.name) / "w4ve.toml").write_text('[server]\nkind = "server"\n')
        executable = Path(api.__file__).resolve().parent / "w4ve.py"
        result = subprocess.run([sys.executable, str(executable), "api", "token", "hub-cli", "--scope", "identity"],
                                cwd=self.temp.name, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(row["name"] == "hub-cli" and row["scope"] == "identity"
                            for row in self.api.tokens.listing()))


if __name__ == "__main__":
    unittest.main()
