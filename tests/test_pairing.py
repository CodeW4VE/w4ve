"""Guided local enrollment preserves secrets, policy changes and existing files."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import w4ve
import pairing
from identity import Authority
from test_identity import proof

SPEC = {"audience": "wavechat", "enabled_setting": "native_chat", "url_setting": "public_url",
        "consumer_config": "w4ve/service/chat/native.json", "issuer_config": "config/wavechat/w4ve-identity.json",
        "resources": [{"id": "channel:general", "name": "General", "operations": ["chat.read", "chat.publish"]}]}


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "server").mkdir()
        (self.root / "w4ve.toml").write_text('[server]\nkind="server"\ngame_dir="server"\n')
        self.server = w4ve.Server(self.root)
        self.settings = {"native_chat": True, "public_url": "https://chat.example.test"}

    def configure(self):
        return pairing.configure(self.server, "wavechat-server", SPEC, self.settings)

    def test_generates_private_issuer_and_secret_free_consumer_without_copying_ids(self):
        result = self.configure()
        issuer = json.loads(Path(result["issuer_config"]).read_text())
        consumer = json.loads(Path(result["consumer_config"]).read_text())
        self.assertNotIn("secret", consumer)
        self.assertNotIn(issuer["secret"], json.dumps(result))
        self.assertEqual(consumer["community"], issuer["community"])
        self.assertEqual(consumer["instance"], issuer["instance"])
        authority = Authority(self.root / "w4ve/identity/authority.sqlite3")
        now = int(time.time())
        note = proof(issuer["secret"], community=issuer["community"], instance=issuer["instance"],
                     issued=now, expires=now + 100)
        token = authority.create_session(note, "wavechat")["token"]
        self.assertTrue(authority.authorize(token, "wavechat", "channel:general", "chat.publish")["allowed"])
        self.assertFalse(authority.authorize(token, "wavechat", "channel:private", "chat.read")["allowed"])
        if os.name != "nt":
            self.assertEqual(Path(result["issuer_config"]).stat().st_mode & 0o777, 0o600)

    def test_reconfigure_does_not_rotate_secret_invalidate_sessions_or_restore_revoked_grants(self):
        first = self.configure()
        issuer = json.loads(Path(first["issuer_config"]).read_text())
        authority = Authority(self.root / "w4ve/identity/authority.sqlite3")
        now = int(time.time())
        token = authority.create_session(proof(issuer["secret"], community=issuer["community"],
                                                instance=issuer["instance"], issued=now, expires=now + 100),
                                         "wavechat")["token"]
        authority.set_grants(issuer["community"], "wavechat", "channel:general", "*", [])
        revision = authority.inspect(token, "wavechat")["policy_revision"]
        self.assertEqual(self.configure(), first)
        self.assertEqual(json.loads(Path(first["issuer_config"]).read_text()), issuer)
        self.assertEqual(authority.inspect(token, "wavechat")["policy_revision"], revision)
        self.assertFalse(authority.authorize(token, "wavechat", "channel:general", "chat.read")["allowed"])

    def test_disabled_and_unsafe_addresses_do_not_enroll(self):
        self.settings["native_chat"] = False
        self.assertEqual(self.configure(), {"enabled": False})
        self.assertFalse((self.root / "w4ve/identity").exists())
        self.settings["native_chat"] = True
        for address in ("http://example.test", "https://u:p@example.test", "https://example.test/?token=x"):
            self.settings["public_url"] = address
            with self.assertRaises(ValueError):
                self.configure()
            self.assertFalse((self.root / "w4ve/identity").exists())

    def test_another_setup_and_corrupt_config_are_not_overwritten(self):
        first = self.configure()
        path = Path(first["issuer_config"])
        for value in ('{"owner":"another"}', '{broken'):
            path.write_text(value)
            with self.assertRaises(ValueError):
                self.configure()
            self.assertEqual(path.read_text(), value)

    def test_changed_declared_policy_and_disabled_issuer_are_not_automatically_reset(self):
        first = self.configure()
        changed = {**SPEC, "resources": [{"id": "channel:private", "name": "Private", "operations": ["chat.read"]}]}
        with self.assertRaises(ValueError):
            pairing.configure(self.server, "wavechat-server", changed, self.settings)
        issuer = json.loads(Path(first["issuer_config"]).read_text())
        authority = Authority(self.root / "w4ve/identity/authority.sqlite3")
        authority.register_instance(first["community"], first["instance"], issuer["secret"], enabled=False)
        with self.assertRaises(ValueError):
            self.configure()

    def test_cli_configure_writes_pairing_and_never_prints_the_secret(self):
        self.server.state["pieces"]["wavechat-server"] = {
            "version": "1.1.0", "pairing": SPEC,
            "settings": [{"name": "native_chat", "type": "bool", "default": True},
                         {"name": "public_url", "type": "url", "required": True}]}
        self.server.save_state()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = w4ve.main(["--dir", str(self.root), "configure", "wavechat-server",
                                "--set", "native_chat=true", "--set", "public_url=https://chat.example.test"])
        self.assertEqual(result, 0, err.getvalue())
        issuer = json.loads((self.root / "server/config/wavechat/w4ve-identity.json").read_text())
        self.assertNotIn(issuer["secret"], out.getvalue() + err.getvalue())
        self.assertNotIn(issuer["secret"], (self.root / "w4ve.toml").read_text())
        self.assertIn("paired", out.getvalue())

    def test_invalid_paths_and_duplicate_resources_are_refused(self):
        for relative in ("w4ve/../escape.json", "w4ve//bad.json", "w4ve\\bad.json"):
            with self.assertRaises(ValueError):
                pairing.configure(self.server, "wavechat-server", {**SPEC, "consumer_config": relative}, self.settings)
        duplicate = {**SPEC, "resources": SPEC["resources"] * 2}
        with self.assertRaises(ValueError):
            pairing.configure(self.server, "wavechat-server", duplicate, self.settings)

    @unittest.skipIf(os.name == "nt", "Windows symlink privilege is unavailable in this QA environment")
    def test_symlinked_parent_cannot_receive_pairing_files(self):
        (self.root / "w4ve").mkdir()
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / "w4ve/service").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.configure()
        self.assertFalse(list(target.iterdir()))

    def test_cli_dry_run_does_not_create_identity_or_change_settings(self):
        self.server.state["pieces"]["wavechat-server"] = {
            "version": "1.1.0", "pairing": SPEC,
            "settings": [{"name": "native_chat", "type": "bool", "default": True},
                         {"name": "public_url", "type": "url", "required": True}]}
        self.server.save_state()
        original = (self.root / "w4ve.toml").read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = w4ve.main(["--dir", str(self.root), "configure", "wavechat-server", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual((self.root / "w4ve.toml").read_bytes(), original)
        self.assertFalse((self.root / "w4ve/identity").exists())


if __name__ == "__main__":
    unittest.main()
