"""Tests for reading a plugin's language files, which is what makes it talk.

Every case here came from running a plugin that MineWave actually has
installed, not from reading the MCDR docs:

  * DigAll 4.1.1 answered `!!dig-all` with `dig_all.command.sidebar_set`,
    because its `lang/` lives inside the `.mcdr` zip and only folders were
    being read.
  * StatsHelper 7.5.1 answered `!!stats` with a single `|`, because its whole
    help screen is a YAML block scalar and the reader took `|` as the value.
  * StatsHelper raised AttributeError on `preferred_language_context`, which
    it wraps that help screen in.

A plugin that loads but cannot form a sentence is not a plugin that works, so
these are correctness, not polish.
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.host import (Host, ConsoleCommandSource,  # noqa: E402
                       _read_simple_yaml_text)

PLUGIN = '''
from mcdreforged.api.all import *

PLUGIN_METADATA = {'id': 'talky', 'version': '1.0.0'}

def on_load(server, prev):
    server.register_command(
        Literal('!!talky').runs(lambda s: s.reply(server.tr('talky.hello'))))
    server.register_command(
        Literal('!!talky-help').runs(_help))

def _help(source):
    with source.preferred_language_context():
        source.reply(ServerInterface.si().tr('talky.help'))
'''

LANG = '''\
talky.hello: hi there
talky.help: |
  first line
  second line
talky.folded: >
  one
  two
talky.nested:
  deeper: from a nested key
'''


class YamlTest(unittest.TestCase):

    def test_a_block_scalar_keeps_its_line_breaks(self):
        out = _read_simple_yaml_text(LANG)
        self.assertEqual("first line\nsecond line\n", out["talky.help"])

    def test_a_folded_scalar_joins_the_lines(self):
        out = _read_simple_yaml_text(LANG)
        self.assertEqual("one two", out["talky.folded"])

    def test_plain_and_nested_keys_still_work(self):
        out = _read_simple_yaml_text(LANG)
        self.assertEqual("hi there", out["talky.hello"])
        self.assertEqual("from a nested key", out["talky.nested.deeper"])

    def test_a_key_after_a_block_is_not_swallowed_by_it(self):
        out = _read_simple_yaml_text("a: |\n  body\nb: after\n")
        self.assertEqual("body\n", out["a"])
        self.assertEqual("after", out["b"])

    def test_a_stripped_block_loses_its_trailing_newline(self):
        out = _read_simple_yaml_text("a: |-\n  body\n")
        self.assertEqual("body", out["a"])


class LanguageFileTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.output = []

    def host(self):
        host = Host(self.root, plugins_dir=self.plugins,
                    on_output=self.output.append)
        host.load_all()
        return host

    def say(self, host, line):
        mark = len(self.output)
        host.run_command(ConsoleCommandSource(host), line)
        return self.output[mark:]

    def as_folder(self):
        folder = self.plugins / "talky"
        (folder / "lang").mkdir(parents=True)
        (folder / "mcdreforged.plugin.json").write_text(
            '{"id": "talky", "version": "1.0.0", "entrypoint": "talky"}')
        (folder / "talky.py").write_text(PLUGIN)
        (folder / "lang" / "en_us.yml").write_text(LANG)

    def as_archive(self):
        path = self.plugins / "talky.mcdr"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mcdreforged.plugin.json",
                             '{"id": "talky", "version": "1.0.0",'
                             ' "entrypoint": "talky"}')
            archive.writestr("talky.py", PLUGIN)
            archive.writestr("lang/en_us.yml", LANG)

    def test_a_folder_plugin_speaks(self):
        self.as_folder()
        host = self.host()
        self.assertEqual(["hi there"], self.say(host, "!!talky"))

    def test_an_archive_plugin_speaks_too(self):
        # The case that was broken: every plugin MineWave runs ships as a
        # `.mcdr`, so this is the one that matters in production.
        self.as_archive()
        host = self.host()
        self.assertEqual(["hi there"], self.say(host, "!!talky"))

    def test_a_help_screen_in_a_block_scalar_comes_out_whole(self):
        self.as_archive()
        host = self.host()
        said = self.say(host, "!!talky-help")
        self.assertTrue(any("first line" in line for line in said), said)
        self.assertTrue(any("second line" in line for line in said), said)

    def test_preferred_language_context_is_a_context_manager(self):
        self.as_archive()
        host = self.host()
        source = ConsoleCommandSource(host)
        before = host.language
        with source.preferred_language_context():
            self.assertEqual(before, host.language)
        self.assertEqual(before, host.language)
        self.assertEqual(before, source.get_preference().language)

    def test_unloading_takes_the_translations_with_it(self):
        self.as_archive()
        host = self.host()
        self.assertIn("talky.hello", host.translations.get("en_us", {}))
        host.unload("talky")
        self.assertEqual({}, host.translations)


if __name__ == "__main__":
    unittest.main()


class TranslationCopyTest(unittest.TestCase):
    """A copy of a translation has to still be a translation.

    Found on a real 1.21 world: QuickBackupM's `command_run()` copies a text
    before styling it, and the copy came back as a plain RText holding the
    key. Its whole help screen read
    `quick_backup_multi.register.summory_help` on a server whose language file
    was loaded and correct.
    """

    def setUp(self):
        from mcdr.text import RTextMCDRTranslation

        self.text = RTextMCDRTranslation("greeting.hello", "world")
        self.text.set_translator(
            lambda key, *args, **kw: "hi %s" % (args[0] if args else ""))

    def test_a_copy_still_translates(self):
        clone = self.text.copy()
        self.assertEqual("hi world", clone.to_plain_text())

    def test_a_copy_keeps_the_styling_it_was_given(self):
        from mcdr.text import RAction, RColor

        self.text.set_color(RColor.gold)
        self.text.set_click_event(RAction.suggest_command, "!!qb")
        clone = self.text.copy()
        self.assertEqual(RColor.gold, clone.color)
        self.assertEqual("!!qb", clone.click_value)
        self.assertEqual("hi world", clone.to_plain_text())

    def test_styling_the_copy_does_not_touch_the_original(self):
        from mcdr.text import RColor

        clone = self.text.copy()
        clone.set_color(RColor.red)
        self.assertIsNone(self.text.color)

    def test_a_plain_text_copy_is_still_a_plain_text(self):
        from mcdr.text import RText

        original = RText("hello").set_hover_text("there")
        clone = original.copy()
        self.assertEqual("hello", clone.to_plain_text())
        # Hover text is kept as a text object, not a string, so compare it as
        # one.
        self.assertEqual("there", clone.hover_text.to_plain_text())
        self.assertIsInstance(clone, RText)
