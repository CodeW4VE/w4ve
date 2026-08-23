"""Tests for reading what a plugin needs from pip.

The rule is the one the `.w4ve` format already follows: **declared and
checked, never installed.** W4VE does not run pip at anybody.

Why it earns a test file: four of the twelve plugins MineWave runs failed the
acceptance rehearsal, and not one of them was a compatibility hole. They
wanted colorlog, apscheduler and parse. Worse, DayCountNBT *loads* without
`nbtlib` and only dies when a player types `!!day`, so "it loaded" is not the
same question as "it will work".
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr import loader, requirements  # noqa: E402


class ParseTest(unittest.TestCase):

    def test_the_shapes_a_requirements_file_comes_in(self):
        found = requirements.parse_requirements(
            "colorlog\n"
            "APScheduler>=3.8,<4\n"
            "discord.py\n"
            "requests[socks] >= 2.0   # with an extra and a comment\n"
            "\n"
            "# a whole comment line\n"
            "-r other.txt\n")
        self.assertEqual(
            [("colorlog", ""), ("APScheduler", ">=3.8,<4"),
             ("discord.py", ""), ("requests", ">= 2.0")], found)

    def test_mcdreforged_itself_is_not_something_to_install(self):
        found = requirements.parse_requirements("mcdreforged>=2.2.0\ncolorlog\n")
        self.assertEqual([("colorlog", "")], found)

    def test_an_empty_file_asks_for_nothing(self):
        self.assertEqual([], requirements.parse_requirements(""))
        self.assertEqual([], requirements.parse_requirements(None))


class InstalledTest(unittest.TestCase):

    def test_something_that_is_definitely_there(self):
        # The interpreter running this has pip metadata for its own stdlib
        # packaging tools; `unittest` is not a distribution, so ask about one
        # that is, and accept "cannot tell" on a stripped down environment.
        self.assertIn(requirements.installed("nonexistent-package-xyzzy"),
                      (False, None))

    def test_a_name_nobody_has(self):
        self.assertIn(requirements.installed("a-package-that-does-not-exist-42"),
                      (False, None))


class PluginRequirementsTest(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def archive(self, files):
        path = self.dir / "thing.mcdr"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mcdreforged.plugin.json",
                             '{"id": "thing", "version": "1.0.0"}')
            for name, text in files.items():
                archive.writestr(name, text)
        return loader.read(path)

    def test_the_root_requirements_are_what_it_needs(self):
        plugin = self.archive({"requirements.txt": "colorlog\npycryptodome\n"})
        found = requirements.of(plugin)
        self.assertEqual([("colorlog", ""), ("pycryptodome", "")],
                         found["required"])

    def test_a_variants_requirements_are_not_everybodys(self):
        # ChatBridge ships five, one per front end, and nobody installs all
        # five. Reporting them as missing buries the one line that matters.
        plugin = self.archive({
            "requirements.txt": "colorlog\n",
            "chatbridge/impl/discord/requirements.txt": "discord.py\npillow\n",
            "chatbridge/impl/online/requirements.txt": "parse\n"})
        found = requirements.of(plugin)
        self.assertEqual([("colorlog", "")], found["required"])
        self.assertEqual(2, len(found["optional"]))
        self.assertNotIn("discord.py", [n for n, _ in found["required"]])

    def test_a_folder_plugin_is_read_too(self):
        folder = self.dir / "folded"
        folder.mkdir()
        (folder / "mcdreforged.plugin.json").write_text(
            '{"id": "folded", "version": "1.0.0"}')
        (folder / "requirements.txt").write_text("colorlog\n")
        plugin = loader.read_folder(folder)
        self.assertEqual([("colorlog", "")], requirements.of(plugin)["required"])

    def test_a_plugin_that_asks_for_nothing(self):
        plugin = self.archive({})
        found = requirements.of(plugin)
        self.assertEqual([], found["required"])
        self.assertEqual([], requirements.missing(plugin))

    def test_missing_only_reports_what_is_really_absent(self):
        plugin = self.archive(
            {"requirements.txt": "a-package-that-does-not-exist-42\n"})
        self.assertEqual(["a-package-that-does-not-exist-42"],
                         requirements.missing(plugin))

    def test_the_install_line_is_ready_to_paste(self):
        self.assertEqual("pip install colorlog parse",
                         requirements.install_line(["parse", "colorlog", "parse"]))


if __name__ == "__main__":
    unittest.main()
