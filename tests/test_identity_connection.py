"""A paired issuer's connection context is MAC-bound and persists with player sessions."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from identity import Authority, IdentityDenied
from test_identity import proof


class ConnectionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private/identity.db"
        self.authority = Authority(self.path, clock=lambda: 1000)
        self.secret = self.authority.register_instance("alpha", "smp")["secret"]

    def test_connection_is_preserved_and_legacy_proof_has_empty_context(self):
        context = "a" * 32
        token = self.authority.create_session(proof(self.secret, connection=context), "wavechat")["token"]
        reopened = Authority(self.path, clock=lambda: 1000)
        self.assertEqual(reopened.inspect(token, "wavechat")["connection"], context)
        legacy = self.authority.create_session(proof(self.secret), "wavechat")["token"]
        self.assertEqual(reopened.inspect(legacy, "wavechat")["connection"], "")

    def test_invalid_context_cannot_become_a_session(self):
        for context in (None, True, 42, [], {}, "a" * 31, "A" * 32, "../path"):
            with self.subTest(context=context), self.assertRaises(IdentityDenied):
                self.authority.create_session(proof(self.secret, connection=context), "wavechat")

    def test_old_database_sessions_remain_legacy_after_additive_upgrade(self):
        token = self.authority.create_session(proof(self.secret), "wavechat")["token"]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE identity_sessions DROP COLUMN connection")
        reopened = Authority(self.path, clock=lambda: 1000)
        self.assertEqual(reopened.inspect(token, "wavechat")["connection"], "")


if __name__ == "__main__":
    unittest.main()
