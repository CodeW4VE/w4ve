"""Instance-role credentials cannot become player identity or bypass member ACL."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from identity import Authority, IdentityDenied
from test_identity import proof as player_proof


def instance_proof(secret, **changes):
    payload = {"community": "alpha", "instance": "smp", "audience": "wavechat",
               "issued": 1000, "expires": 1120, "nonce": uuid.uuid4().hex}
    payload.update(changes)
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    body = "w4ve-instance1." + b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


class InstanceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private/authority.db"
        self.authority = Authority(self.path, clock=lambda: 1000)
        self.secret = self.authority.register_instance("alpha", "smp")["secret"]
        self.session = self.authority.create_instance_session(instance_proof(self.secret), "wavechat")["token"]
        self.alice = "466a0fbd7b87431cab58c1887f577deb"
        self.bob = uuid.uuid4().hex

    def test_instance_and_player_proofs_sessions_are_disjoint(self):
        claims = self.authority.inspect_instance(self.session, "wavechat")
        self.assertEqual(claims["kind"], "instance")
        self.assertEqual(claims["instance"], "smp")
        self.assertNotIn("secret", claims)
        self.assertNotIn("subject", claims)
        with self.assertRaises(IdentityDenied):
            self.authority.inspect(self.session, "wavechat")
        token = self.authority.create_session(player_proof(self.secret), "wavechat")["token"]
        with self.assertRaises(IdentityDenied):
            self.authority.inspect_instance(token, "wavechat")
        for note, method in ((player_proof(self.secret), self.authority.create_instance_session),
                             (instance_proof(self.secret), self.authority.create_session)):
            with self.assertRaises(IdentityDenied):
                method(note, "wavechat")

    def test_roster_requires_instance_resource_and_current_player_permission(self):
        roster = [self.alice, self.bob]
        self.authority.set_grants("alpha", "wavechat", "channel:private", "minecraft:" + self.alice, ["chat.read"])
        def players():
            return self.authority.instance_recipients(self.session, "wavechat", "channel:private", "chat.read", roster)["players"]
        self.assertEqual(players(), [])
        self.authority.set_instance_resources("alpha", "smp", "wavechat", ["channel:private"])
        self.assertEqual(players(), [self.alice])
        self.authority.set_blocked("alpha", "minecraft:" + self.alice, True)
        self.assertEqual(players(), [])
        self.authority.set_blocked("alpha", "minecraft:" + self.alice, False)
        self.authority.set_grants("alpha", "wavechat", "channel:private", "minecraft:" + self.alice, [])
        self.assertEqual(players(), [])

    def test_resource_revocation_survives_reconfiguration(self):
        resources = [{"id": "channel:general", "name": "General", "operations": ["chat.read"]}]
        binding = self.authority.provision_binding("chat", "wavechat", resources)
        token = self.authority.create_instance_session(instance_proof(binding["secret"], community=binding["community"], instance=binding["instance"]), "wavechat")["token"]
        def recipients():
            return self.authority.instance_recipients(token, "wavechat", "channel:general", "chat.read", [self.alice])["players"]
        self.assertEqual(recipients(), [self.alice])
        self.authority.set_instance_resources(binding["community"], binding["instance"], "wavechat", [])
        self.authority.provision_binding("chat", "wavechat", resources)
        self.assertEqual(recipients(), [])

    def test_replay_clock_audience_and_rotation_are_enforced(self):
        note = instance_proof(self.secret)
        self.authority.create_instance_session(note, "wavechat")
        reopened = Authority(self.path, clock=lambda: 1000)
        for bad in (note, instance_proof(self.secret, expires=999), instance_proof(self.secret, issued=True),
                    instance_proof(self.secret, audience="other"), instance_proof(self.secret, uuid=self.alice)):
            with self.assertRaises(IdentityDenied):
                reopened.create_instance_session(bad, "wavechat")
        with self.assertRaises(IdentityDenied):
            self.authority.inspect_instance(self.session, "other")
        self.authority.register_instance("alpha", "smp")
        with self.assertRaises(IdentityDenied):
            self.authority.inspect_instance(self.session, "wavechat")

    def test_bad_and_large_roster_cannot_bypass_typed_uuid(self):
        for roster in (["Alice"], ["minecraft:" + self.alice], [self.alice.upper()], [self.alice] * 101, True):
            with self.assertRaises(ValueError):
                self.authority.instance_recipients(self.session, "wavechat", "channel:general", "chat.read", roster)


if __name__ == "__main__":
    unittest.main()
