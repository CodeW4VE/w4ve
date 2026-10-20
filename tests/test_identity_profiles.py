import unittest
import test_identity
from identity import IdentityDenied, IdentityForbidden, IdentityConflict


class ProfileTests(unittest.TestCase):
    setUp = test_identity.AuthorityTests.setUp
    open = test_identity.AuthorityTests.open
    session = test_identity.AuthorityTests.session
    def grants(self, operations=('profile.read', 'profile.edit')):
        self.authority.set_grants('alpha', 'wavechat', 'profile:self', '*', list(operations))

    def test_profile_is_explicit_and_never_changes_verified_identity(self):
        token = self.session()
        with self.assertRaises(IdentityForbidden):
            self.authority.own_profile(token, 'wavechat')
        self.grants()
        first = self.authority.own_profile(token, 'wavechat')
        self.assertEqual(first['revision'], 0)
        edited = self.authority.own_profile(token, 'wavechat', update={'display_name': 'Presentation', 'bio': 'one\ntwo'}, revision=0)
        self.assertEqual(edited['revision'], 1)
        self.assertEqual(edited['account_name'], 'Alice')
        self.assertEqual(self.authority.inspect(token, 'wavechat')['name'], 'Alice')
        self.assertEqual(edited, self.open().own_profile(token, 'wavechat'))
        self.assertNotIn('token', edited)
        self.assertNotIn('links', edited)

    def test_profile_cas_revocation_and_other_subject_isolation(self):
        self.grants()
        token = self.session()
        profile = {'display_name': 'Alice edit', 'bio': 'private self view'}
        self.authority.own_profile(token, 'wavechat', update=profile, revision=0)
        with self.assertRaises(IdentityConflict):
            self.authority.own_profile(token, 'wavechat', update=profile, revision=0)
        other = self.session(uuid='b' * 32, name='Bob')
        self.assertEqual(self.authority.own_profile(other, 'wavechat')['revision'], 0)
        self.grants(('profile.read',))
        with self.assertRaises(IdentityForbidden):
            self.authority.own_profile(token, 'wavechat', update=profile, revision=1)
        self.authority.set_blocked('alpha', 'minecraft:466a0fbd7b87431cab58c1887f577deb', True)
        with self.assertRaises(IdentityDenied):
            self.authority.own_profile(token, 'wavechat')

    def test_profile_rejects_account_claims_and_unbounded_or_hidden_text(self):
        self.grants()
        token = self.session()
        for value in ({'display_name': 'x', 'bio': '', 'subject': 'forged'},
                      {'display_name': 'x', 'bio': '', 'discord_id': '123'},
                      {'display_name': 'x' * 49, 'bio': ''},
                      {'display_name': 'x\u202e', 'bio': ''},
                      {'display_name': 'x', 'bio': 'x' * 501},
                      {'display_name': 'x', 'bio': '\x00'}):
            with self.assertRaises(ValueError):
                self.authority.own_profile(token, 'wavechat', update=value, revision=0)
        self.assertEqual(self.authority.own_profile(token, 'wavechat')['revision'], 0)
        for revision in (None, True, -1, 2 ** 31):
            with self.assertRaises(ValueError):
                self.authority.own_profile(token, 'wavechat', update={'display_name': '', 'bio': ''}, revision=revision)

    def test_profile_survives_reconnect_but_not_community_or_audience_scope(self):
        self.grants()
        first = self.session(connection='a' * 32)
        self.authority.own_profile(first, 'wavechat', update={'display_name': 'Local alias', 'bio': ''}, revision=0)
        second = self.session(connection='b' * 32)
        self.assertEqual(self.authority.own_profile(second, 'wavechat')['display_name'], 'Local alias')
        with self.assertRaises(IdentityDenied):
            self.authority.own_profile(second, 'another-app')
        beta = self.authority.register_instance('beta', 'smp')['secret']
        self.authority.set_grants('beta', 'wavechat', 'profile:self', '*', ['profile.read'])
        third = self.authority.create_session(test_identity.proof(beta, community='beta'), 'wavechat')['token']
        self.assertEqual(self.authority.own_profile(third, 'wavechat')['display_name'], '')
        self.now += 901
        with self.assertRaises(IdentityDenied):
            self.authority.own_profile(second, 'wavechat')


if __name__ == '__main__':
    unittest.main()
