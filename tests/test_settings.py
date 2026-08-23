"""Tests for what a piece needs told to it.

The thing being checked here is not that a value round trips. It is the three
promises that make a big piece installable by somebody who is not its author:

- a secret never lands anywhere a secret must not land;
- an answer somebody gave by hand is never eaten by the tool;
- a service is not started with a hole where its token should be.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import package  # noqa: E402
import piecesettings  # noqa: E402
import w4ve  # noqa: E402


DECLARATIONS = [
    {"name": "public_url", "type": "url", "required": True,
     "title": "Public address",
     "help": "Where players' clients reach this piece.",
     "example": "https://chat.example.com"},
    {"name": "discord_token", "type": "secret", "required": True,
     "title": "Discord bot token",
     "help": "From the Bot tab. Not the client secret."},
    {"name": "music", "type": "bool", "default": False,
     "title": "Music",
     "help": "Let players play music. Off by default: it costs disk."},
    {"name": "quality", "type": "choice", "options": ["low", "high"],
     "default": "low", "title": "Quality", "help": "How good the audio is."},
]


def a_server(tmp):
    root = Path(tmp) / "root"
    (root / "server" / "mods").mkdir(parents=True)
    (root / "w4ve").mkdir()
    (root / "w4ve.toml").write_text(
        '[server]\nkind = "server"\nmc = "1.21"\ngame_dir = "server"\n\n[pieces]\n')
    return w4ve.Server(root)


class DeclarationCase(unittest.TestCase):

    def test_a_setting_with_no_help_is_a_setting_nobody_can_answer(self):
        problems = piecesettings.Declaration({"name": "x", "type": "text"}).problems()
        self.assertTrue(any("no help" in p for p in problems))

    def test_a_secret_cannot_carry_a_default(self):
        problems = piecesettings.Declaration(
            {"name": "token", "type": "secret", "help": "h",
             "default": "hunter2"}).problems()
        self.assertTrue(any("cannot have a default" in p for p in problems))

    def test_a_url_gets_its_trailing_slash(self):
        declaration = piecesettings.Declaration(
            {"name": "u", "type": "url", "help": "h"})
        self.assertEqual(declaration.coerce("https://example.com"),
                         "https://example.com/")

    def test_a_url_that_is_not_one_is_refused_with_words(self):
        declaration = piecesettings.Declaration(
            {"name": "u", "type": "url", "help": "h"})
        with self.assertRaises(piecesettings.SettingError) as caught:
            declaration.coerce("example.com")
        self.assertIn("http", str(caught.exception))

    def test_yes_and_no_in_the_words_people_use(self):
        declaration = piecesettings.Declaration(
            {"name": "b", "type": "bool", "help": "h"})
        for yes in ("yes", "y", "true", "on", "1", "si"):
            self.assertIs(declaration.coerce(yes), True)
        for no in ("no", "n", "false", "off", "0"):
            self.assertIs(declaration.coerce(no), False)

    def test_a_choice_only_takes_its_options(self):
        declaration = piecesettings.Declaration(
            {"name": "c", "type": "choice", "options": ["a", "b"], "help": "h"})
        self.assertEqual(declaration.coerce("a"), "a")
        with self.assertRaises(piecesettings.SettingError):
            declaration.coerce("z")

    def test_a_list_takes_commas_because_that_is_what_people_type(self):
        declaration = piecesettings.Declaration(
            {"name": "l", "type": "list", "help": "h"})
        self.assertEqual(declaration.coerce("a, b ,c"), ["a", "b", "c"])

    def test_sizes_and_times_are_read_the_way_they_are_written(self):
        self.assertEqual(piecesettings.parse_size("2G"), 2 * 1024 ** 3)
        self.assertEqual(piecesettings.parse_size("500m"), 500 * 1024 ** 2)
        self.assertEqual(piecesettings.parse_time("7d"), 604800)
        self.assertEqual(piecesettings.parse_time("30s"), 30)


class SecretsCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-sec-"))
        self.server = a_server(self.tmp)

    def test_a_secret_is_written_600_and_not_into_the_toml(self):
        declarations = piecesettings.declarations_of({"settings": DECLARATIONS})
        self.server.set_setting("demo", "discord_token", "hunter2",
                                declarations, "demo")
        secrets_path = self.server.root / "w4ve" / "secrets.json"
        self.assertTrue(secrets_path.exists())
        mode = stat.S_IMODE(secrets_path.stat().st_mode)
        self.assertEqual(mode, 0o600, "a token has to be readable by nobody else")
        self.assertNotIn("hunter2",
                         (self.server.root / "w4ve.toml").read_text())

    def test_the_toml_says_where_the_secrets_are_instead_of_holding_them(self):
        declarations = piecesettings.declarations_of({"settings": DECLARATIONS})
        self.server.write_settings("demo", {"public_url": "https://x.example/"},
                                   declarations, "demo")
        text = (self.server.root / "w4ve.toml").read_text()
        self.assertIn("[settings.demo]", text)
        self.assertIn("secrets.json", text)
        self.assertIn("discord_token", text)      # named, not valued

    def test_the_help_ends_up_as_the_comment(self):
        declarations = piecesettings.declarations_of({"settings": DECLARATIONS})
        self.server.write_settings(
            "demo", {"public_url": "https://x.example/", "music": False,
                     "quality": "low"}, declarations, "demo")
        text = (self.server.root / "w4ve.toml").read_text()
        self.assertIn("# Where players' clients reach this piece.", text)
        self.assertIn("# for example: https://chat.example.com", text)
        self.assertIn("# one of: low, high", text)
        self.assertIn("# installed by demo", text)

    def test_a_value_added_by_hand_is_kept(self):
        declarations = piecesettings.declarations_of({"settings": DECLARATIONS})
        self.server.write_settings("demo", {"public_url": "https://x.example/"},
                                   declarations, "demo")
        self.server.write_settings(
            "demo", {"public_url": "https://x.example/", "mine": "leave it"},
            declarations, "demo")
        text = (self.server.root / "w4ve.toml").read_text()
        self.assertIn('mine = "leave it"', text)
        self.assertIn("not declared by this piece", text)

    def test_forgetting_a_piece_forgets_its_secret(self):
        declarations = piecesettings.declarations_of({"settings": DECLARATIONS})
        self.server.set_setting("demo", "discord_token", "hunter2",
                                declarations, "demo")
        self.server.forget_secrets("demo")
        self.assertEqual(self.server.secrets_of("demo"), {})

    def test_a_widened_secrets_file_is_noticed(self):
        store = piecesettings.Secrets(self.server.root)
        store.set("demo", "token", "x")
        self.assertFalse(store.insecure())
        os.chmod(store.path, 0o644)
        self.assertTrue(store.insecure())


class ExpansionCase(unittest.TestCase):

    def test_references_are_replaced_everywhere_they_can_hide(self):
        out = piecesettings.expand(
            {"command": ["run", "--url", "${setting:public_url}"],
             "env": {"TOKEN": "${secret:discord_token}"}},
            {"public_url": "https://x/"}, {"discord_token": "hunter2"})
        self.assertEqual(out["command"][2], "https://x/")
        self.assertEqual(out["env"]["TOKEN"], "hunter2")

    def test_an_unknown_reference_is_left_standing_and_can_be_found(self):
        value = piecesettings.expand(["--token", "${secret:nope}"], {}, {})
        self.assertEqual(value[1], "${secret:nope}")
        self.assertEqual(piecesettings.unresolved(value), ["${secret:nope}"])

    def test_the_environment_is_what_a_service_actually_reads(self):
        env = piecesettings.environment_for(
            "demo", {"public_url": "https://x/", "music": True,
                     "roles": ["1", "2"]}, {"discord_token": "hunter2"})
        self.assertEqual(env["W4VE_PUBLIC_URL"], "https://x/")
        self.assertEqual(env["W4VE_MUSIC"], "true")
        self.assertEqual(env["W4VE_ROLES"], "1,2")
        self.assertEqual(env["W4VE_DISCORD_TOKEN"], "hunter2")
        self.assertEqual(env["W4VE_PIECE"], "demo")


class ManifestSettingsCase(unittest.TestCase):

    def test_a_manifest_carries_its_questions(self):
        manifest = package.Manifest({
            "schema": 1, "id": "demo", "version": "1.0.0", "summary": "s",
            "settings": DECLARATIONS,
            "components": [{"kind": "service", "name": "demo",
                            "command": ["true"]}],
        })
        self.assertEqual(manifest.problems(), [])
        self.assertEqual([d.name for d in manifest.settings],
                         ["public_url", "discord_token", "music", "quality"])

    def test_a_question_that_waits_on_a_question_that_is_not_there(self):
        manifest = package.Manifest({
            "schema": 1, "id": "demo", "version": "1.0.0", "summary": "s",
            "settings": [{"name": "bitrate", "type": "int", "default": 96,
                          "help": "h", "when": "music"}],
            "components": [{"kind": "service", "name": "demo",
                            "command": ["true"]}],
        })
        self.assertTrue(any("waits on" in p for p in manifest.problems()))


if __name__ == "__main__":
    unittest.main()
