from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from identity import Authority, IdentityConflict
import w4ve


class MemberPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'w4ve.toml').write_text('[server]\nkind="server"\n')
        self.path = self.root / 'w4ve/identity/authority.sqlite3'
        self.authority = Authority(self.path)
        self.binding = self.authority.provision_binding('chat', 'wavechat',
            [{'id': 'channel:general', 'name': 'General', 'operations': ['chat.read']}])

    def policy(self, resource='profile:self', subject='*'):
        return Authority.read_binding_policy(self.path, 'chat', resource, subject)

    def cli(self, *arguments, root=None):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = w4ve.main(['--dir', str(root or self.root), 'permissions', 'chat',
                              '--resource', 'profile:self', '--subject', '*', *arguments])
        return code, out.getvalue()

    def test_cli_preview_and_apply_are_separate_and_never_mint_or_rotate(self):
        first = self.policy()
        with patch('w4ve.ask', side_effect=AssertionError('preview must not prompt')):
            code, output = self.cli('--grant', 'profile.read', '--grant', 'profile.edit')
        self.assertEqual(code, 0, output)
        self.assertIn('effective after: profile.edit, profile.read', output)
        self.assertEqual(self.policy(), first)
        code, output = self.cli('--grant', 'profile.read', '--grant', 'profile.edit', '--apply', '-y')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.policy()['direct'], ['profile.edit', 'profile.read'])
        self.assertEqual(self.policy('channel:general')['direct'], ['chat.read'])
        self.assertEqual(self.authority.provision_binding('chat', 'wavechat',
            [{'id': 'channel:general', 'name': 'General', 'operations': ['chat.read']}])['secret'], self.binding['secret'])
        second = self.policy()
        self.assertEqual(self.cli('--grant', 'profile.read', '--apply', '-y')[0], 0)
        self.assertEqual(self.policy(), second)
        self.assertNotIn(self.binding['secret'], output)

    def test_stale_preview_cannot_undo_concurrent_revocation(self):
        before = self.policy()
        self.authority.set_grants(self.binding['community'], 'wavechat', 'channel:general', '*', [])
        with self.assertRaises(IdentityConflict):
            self.authority.change_binding_grants('chat','profile:self','*',['profile.read'],before['revision'])
        self.assertEqual(self.policy()['direct'], [])
        self.assertEqual(self.policy('channel:general')['direct'], [])

    def test_subject_revocation_does_not_hide_wildcard_permission(self):
        before = self.policy()
        self.authority.change_binding_grants('chat','profile:self','*',['profile.read'],before['revision'])
        subject = 'minecraft:' + 'a' * 32
        player = self.policy(subject=subject)
        self.assertEqual(player['direct'], [])
        self.assertEqual(player['inherited'], ['profile.read'])
        updated = self.authority.change_binding_grants('chat','profile:self',subject,[],player['revision'])
        self.assertEqual(updated['effective'], ['profile.read'])
        self.assertEqual(updated['revision'], player['revision'])
        self.assertEqual(self.cli('--clear','--apply','-y')[0], 0)
        self.assertEqual(self.policy(subject=subject)['effective'], [])

    def test_dry_run_bad_input_and_unpaired_root_preserve_policy(self):
        before = self.policy()
        self.assertEqual(self.cli('--grant','profile.read','--apply','--dry-run','-y')[0], 0)
        self.assertEqual(self.policy(), before)
        self.assertEqual(self.cli('--clear','--grant','profile.read','--apply','-y')[0], 1)
        self.assertEqual(self.policy(), before)
        other = self.root / 'unpaired'; other.mkdir()
        (other/'w4ve.toml').write_text('[server]\nkind="server"\n')
        self.assertEqual(self.cli('--grant','profile.read',root=other)[0], 1)
        self.assertFalse((other/'w4ve/identity/authority.sqlite3').exists())
        with self.authority._db() as db:
            db.execute("DELETE FROM identity_meta WHERE key='epoch'")
        self.assertEqual(self.cli('--grant','profile.read','--apply','-y')[0], 1)
        with self.authority._db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM identity_meta WHERE key='epoch'").fetchone())
            self.assertEqual(db.execute('SELECT operation FROM identity_grants').fetchone()[0], 'chat.read')


if __name__ == '__main__': unittest.main()
