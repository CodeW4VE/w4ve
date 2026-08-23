"""Tests for permission.yml: the file, not an object pretending to be one.

The promise being tested is the one from W4VE-DISENO: a flat file, visible and
commented, editable by hand and while running. So the tests edit the file on
disk behind the running host's back and expect it to notice, and they check
that a command that writes to the file leaves somebody's comments and their
ordering alone. A permission system that eats the file it is configured from
is not editable by hand, whatever the docs say.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.permission import (ADMIN, GUEST, HELPER, OWNER, USER,  # noqa: E402
                             PermissionFile, level_from)


class PermissionFileTest(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "permission.yml"
        self.problems = []

    def open(self):
        return PermissionFile(self.path, on_problem=self.problems.append)

    def write(self, text):
        self.path.write_text(text)

    # ----------------------------------------------------------- the default

    def test_a_missing_file_is_written_with_its_own_explanation(self):
        perms = self.open()
        self.assertTrue(self.path.exists())
        text = self.path.read_text()
        self.assertIn("#", text, "the default file has to explain itself")
        self.assertIn("guest(0)", text)
        self.assertEqual(USER, perms.default_level)
        self.assertEqual(USER, perms.level_of("Anybody"))

    def test_levels_by_name_and_by_number(self):
        self.assertEqual(OWNER, level_from("owner"))
        self.assertEqual(OWNER, level_from(4))
        self.assertEqual(HELPER, level_from("HELPER"))
        self.assertEqual(GUEST, level_from("0"))
        self.assertEqual(OWNER, level_from(99), "out of range clamps")
        self.assertEqual(USER, level_from("nonsense"), "junk falls back")

    # -------------------------------------------------------------- reading

    def test_reads_the_shape_mcdr_writes(self):
        self.write("default_level: guest\n"
                   "owner:\n- TVTvirus\n"
                   "admin:\n- Manke\n- Hugo\n"
                   "helper:\nuser:\nguest:\n")
        perms = self.open()
        self.assertEqual(GUEST, perms.default_level)
        self.assertEqual(OWNER, perms.level_of("TVTvirus"))
        self.assertEqual(ADMIN, perms.level_of("Hugo"))
        self.assertEqual(GUEST, perms.level_of("Nobody"))
        self.assertEqual(["Hugo", "Manke"], perms.players(ADMIN))

    def test_reads_an_inline_list_too(self):
        self.write("default_level: user\nadmin: [Manke, \"Hugo\"]\n")
        perms = self.open()
        self.assertEqual(ADMIN, perms.level_of("Manke"))
        self.assertEqual(ADMIN, perms.level_of("Hugo"))

    def test_somebody_listed_twice_gets_the_higher_level_and_a_warning(self):
        self.write("default_level: user\nowner:\n- Dup\nuser:\n- Dup\n")
        perms = self.open()
        self.assertEqual(OWNER, perms.level_of("Dup"))
        self.assertTrue(any("both" in line for line in self.problems),
                        self.problems)

    def test_comments_and_blank_lines_are_not_data(self):
        self.write("# owner:\n# - NotReal\n\nadmin:\n- Real\n")
        perms = self.open()
        self.assertEqual(USER, perms.level_of("NotReal"))
        self.assertEqual(ADMIN, perms.level_of("Real"))

    # -------------------------------------------------------------- writing

    def test_setting_a_level_keeps_the_file_a_person_wrote(self):
        self.write("# my own note\n"
                   "default_level: user\n"
                   "\n"
                   "owner:\n- TVTvirus\n"
                   "admin:\n"
                   "helper:\n")
        perms = self.open()
        perms.set_level("Manke", "admin")
        text = self.path.read_text()
        self.assertIn("# my own note", text)
        self.assertIn("- TVTvirus", text)
        self.assertIn("- Manke", text)
        self.assertEqual(ADMIN, perms.level_of("Manke"))
        self.assertEqual(OWNER, perms.level_of("TVTvirus"))

    def test_moving_somebody_does_not_leave_them_in_two_places(self):
        perms = self.open()
        perms.set_level("Manke", "admin")
        perms.set_level("Manke", "guest")
        self.assertEqual(GUEST, perms.level_of("Manke"))
        self.assertEqual(1, self.path.read_text().count("- Manke"))

    def test_a_missing_section_is_added(self):
        self.write("default_level: user\nowner:\n")
        perms = self.open()
        perms.set_level("Someone", "helper")
        self.assertIn("helper:", self.path.read_text())
        self.assertEqual(HELPER, perms.level_of("Someone"))

    def test_remove_drops_them_to_the_default(self):
        perms = self.open()
        perms.set_level("Manke", "admin")
        self.assertTrue(perms.remove("Manke"))
        self.assertEqual(USER, perms.level_of("Manke"))
        self.assertFalse(perms.remove("Manke"), "removing twice is not an edit")

    def test_default_level_can_be_changed(self):
        perms = self.open()
        perms.set_default_level("helper")
        self.assertEqual(HELPER, perms.default_level)
        self.assertEqual(HELPER, self.open().default_level)

    def test_the_file_is_written_whole_or_not_at_all(self):
        perms = self.open()
        perms.set_level("Manke", "admin")
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name != "permission.yml"]
        self.assertEqual([], leftovers, "a temp file was left behind")

    # ------------------------------------------------------ edited by hand

    def test_an_edit_on_disk_is_picked_up_without_reloading_anything(self):
        perms = self.open()
        self.assertEqual(USER, perms.level_of("Manke"))
        self.write("default_level: user\nadmin:\n- Manke\n")
        self.assertEqual(ADMIN, perms.level_of("Manke"))

    def test_an_unchanged_file_is_not_re_read(self):
        perms = self.open()
        perms.level_of("Manke")
        self.assertFalse(perms.reload_if_changed())

    def test_an_unreadable_file_is_a_warning_not_a_crash(self):
        perms = self.open()
        self.path.unlink()
        self.path.mkdir()          # a directory where a file should be
        perms.load()
        self.assertEqual(USER, perms.level_of("Anybody"))
        self.assertTrue(self.problems)


if __name__ == "__main__":
    unittest.main()
