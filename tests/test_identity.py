import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from identity import Authority, IdentityDenied


def proof(secret, **changes):
    payload = {"community": "alpha", "instance": "smp", "audience": "wavechat",
               "uuid": "466a0fbd7b87431cab58c1887f577deb", "name": "Alice",
               "issued": 1000, "expires": 1100, "nonce": uuid.uuid4().hex}
    payload.update(changes)
    def b64(data):
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    body = "w4ve1." + b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "authority.sqlite3"
        self.now = 1000
        self.authority = self.open()
        self.secret = self.authority.register_instance("alpha", "smp")["secret"]

    def open(self):
        return Authority(self.path, clock=lambda: self.now)

    def session(self, **changes):
        return self.authority.create_session(proof(self.secret, **changes), "wavechat")["token"]

    def test_verified_session_has_canonical_subject_not_display_name(self):
        token = self.session()
        claims = self.authority.inspect(token, "wavechat")
        self.assertEqual(claims["subject"], "minecraft:466a0fbd7b87431cab58c1887f577deb")
        self.assertEqual(claims["origin"], "smp")
        self.assertNotIn("secret", claims)
        self.assertNotIn("token", claims)
        self.assertEqual(self.open().inspect(token, "wavechat"), claims)

    def test_no_grants_is_deny(self):
        token = self.session()
        self.assertFalse(self.authority.authorize(token, "wavechat", "channel:general", "chat.publish")["allowed"])

    def test_grants_and_revocation_change_revision(self):
        token = self.session()
        first = self.authority.inspect(token, "wavechat")["policy_revision"]
        self.authority.set_grants("alpha", "wavechat", "channel:general", "*", ["chat.read"])
        self.assertTrue(self.authority.authorize(token, "wavechat", "channel:general", "chat.read")["allowed"])
        self.assertFalse(self.authority.authorize(token, "wavechat", "channel:general", "chat.publish")["allowed"])
        self.assertNotEqual(self.authority.inspect(token, "wavechat")["policy_revision"], first)
        self.authority.set_grants("alpha", "wavechat", "channel:general", "*", [])
        self.assertFalse(self.open().authorize(token, "wavechat", "channel:general", "chat.read")["allowed"])

    def test_two_communities_same_instance_and_player_are_isolated(self):
        secret_b = self.authority.register_instance("beta", "smp")["secret"]
        a = self.session()
        b = self.authority.create_session(proof(secret_b, community="beta"), "wavechat")["token"]
        self.authority.set_grants("alpha", "wavechat", "channel:general", "*", ["chat.read"])
        self.assertTrue(self.authority.authorize(a, "wavechat", "channel:general", "chat.read")["allowed"])
        self.assertFalse(self.authority.authorize(b, "wavechat", "channel:general", "chat.read")["allowed"])
        with self.assertRaises(IdentityDenied):
            self.authority.create_session(proof(self.secret, community="beta"), "wavechat")

    def test_wrong_audience_in_proof_and_session(self):
        with self.assertRaises(IdentityDenied):
            self.authority.create_session(proof(self.secret, audience="another-piece"), "wavechat")
        with self.assertRaises(IdentityDenied):
            self.authority.inspect(self.session(), "another-piece")

    def test_replay_remains_refused_after_restart(self):
        note = proof(self.secret)
        self.authority.create_session(note, "wavechat")
        with self.assertRaises(IdentityDenied):
            self.open().create_session(note, "wavechat")

    def test_rotation_invalidates_sessions_and_old_proofs(self):
        token = self.session()
        self.authority.register_instance("alpha", "smp")
        with self.assertRaises(IdentityDenied):
            self.authority.inspect(token, "wavechat")
        with self.assertRaises(IdentityDenied):
            self.session()

    def test_blocking_invalidates_session_and_prevents_new_proof(self):
        token = self.session()
        subject = self.authority.inspect(token, "wavechat")["subject"]
        self.authority.set_blocked("alpha", subject, True)
        with self.assertRaises(IdentityDenied):
            self.authority.inspect(token, "wavechat")
        with self.assertRaises(IdentityDenied):
            self.session()

    def test_disabling_instance_prevents_sessions(self):
        self.authority.register_instance("alpha", "smp", self.secret, enabled=False)
        with self.assertRaises(IdentityDenied):
            self.session()

    def test_invalid_signature_and_clock_fields(self):
        for fields in ({"expires": 999}, {"expires": 1500}, {"issued": 1040},
                       {"issued": True}, {"expires": float("nan")}, {"uuid": "not-a-uuid"},
                       {"name": "not a Minecraft name"}):
            with self.subTest(fields=fields):
                with self.assertRaises(IdentityDenied):
                    self.session(**fields)
        with self.assertRaises(IdentityDenied):
            self.authority.create_session(proof("different" * 8), "wavechat")

    def test_expired_session_cannot_authorize(self):
        token = self.session()
        self.now += Authority.SESSION_LIFETIME + 1
        with self.assertRaises(IdentityDenied):
            self.authority.authorize(token, "wavechat", "channel:general", "chat.read")

    def test_token_is_not_stored_in_cleartext(self):
        token = self.session()
        with self.authority._db() as db:
            digest = db.execute("SELECT digest FROM identity_sessions").fetchone()[0]
        self.assertNotEqual(digest, token)
        self.assertEqual(digest, hashlib.sha256(token.encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
