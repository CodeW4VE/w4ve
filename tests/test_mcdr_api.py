"""Tests for the MCDR API pieces: RText and Serializable.

The cases are taken from what the measured plugins actually do with them, not
from what the API allows. Here builds a coordinate that hovers and suggests a
teleport; every plugin declares a typed config and expects a missing field to
fall back rather than explode.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr.serializer import Serializable  # noqa: E402
from mcdr.text import (RAction, RColor, RStyle, RText, RTextList,  # noqa: E402
                       RTextMCDRTranslation)


class TextCase(unittest.TestCase):

    def test_plain_text(self):
        self.assertEqual(RText("hello").to_json_object(), {"text": "hello"})
        self.assertEqual(str(RText("hello")), "hello")

    def test_colour_and_styles(self):
        text = RText("careful", RColor.red, [RStyle.bold, RStyle.italic])
        data = text.to_json_object()
        self.assertEqual(data["color"], "red")
        self.assertTrue(data["bold"] and data["italic"])

    def test_the_coordinate_here_actually_builds(self):
        """`coord.h(...).c(...)` chained, then used, which is Here's pattern."""
        coord = RText("[10, 64, -30]", RColor.aqua)
        built = coord.h(RText("Overworld") + ": click to teleport").c(
            RAction.suggest_command, "/tp 10 64 -30")
        self.assertIs(built, coord, "chaining has to return the same object")
        data = json.loads(built.to_json_str())
        self.assertEqual(data["clickEvent"],
                         {"action": "suggest_command", "value": "/tp 10 64 -30"})
        self.assertEqual(data["hoverEvent"]["action"], "show_text")
        self.assertEqual(data["hoverEvent"]["contents"][1]["text"], "Overworld")

    def test_a_list_reads_as_one_line(self):
        line = RTextList(RText("TVTvirus", RColor.yellow), " @ ",
                         RText("[10, 64, -30]", RColor.aqua))
        self.assertEqual(line.to_plain_text(), "TVTvirus @ [10, 64, -30]")
        data = line.to_json_object()
        self.assertEqual(data[0], "",
                         "the empty first element stops formatting leaking")
        self.assertEqual(len(data), 4)

    def test_nested_lists_are_flattened(self):
        inner = RTextList("b", "c")
        outer = RTextList("a", inner, "d")
        self.assertEqual(outer.to_plain_text(), "abcd")
        self.assertEqual(len(outer.to_json_object()), 5)

    def test_adding_text_together(self):
        joined = RText("a") + RText("b")
        self.assertEqual(joined.to_plain_text(), "ab")
        self.assertEqual(("x" + RText("y")).to_plain_text(), "xy")

    def test_copy_does_not_share_the_original(self):
        original = RText("hi", RColor.red).c(RAction.run_command, "/list")
        clone = original.copy()
        clone.set_color(RColor.blue)
        self.assertEqual(original.color, "red")
        self.assertEqual(clone.click_value, "/list")

    def test_a_translation_shows_its_key_when_nothing_can_translate_it(self):
        text = RTextMCDRTranslation("prime_backup.backup.done")
        self.assertEqual(text.to_plain_text(), "prime_backup.backup.done")
        text.set_translator(lambda key, *a, **kw: "Backup done")
        self.assertEqual(text.to_plain_text(), "Backup done")


class HereConfig(Serializable):
    """The real config class out of Here 1.2.2."""

    highlight_time: int = 15
    display_voxel_waypoint: bool = True
    display_xaero_waypoint: bool = True
    click_to_teleport: bool = False
    use_rcon_if_possible: bool = True


class Nested(Serializable):
    enabled: bool = True
    name: str = "default"


class Outer(Serializable):
    inner: Nested = None
    count: int = 3


class SerializableCase(unittest.TestCase):

    def test_defaults_without_a_file(self):
        config = HereConfig()
        self.assertEqual(config.highlight_time, 15)
        self.assertTrue(config.use_rcon_if_possible)
        self.assertFalse(config.click_to_teleport)

    def test_a_file_that_only_sets_some_of_it(self):
        """A config written before the plugin gained an option still loads."""
        config = HereConfig.deserialize({"highlight_time": 30})
        self.assertEqual(config.highlight_time, 30)
        self.assertTrue(config.display_xaero_waypoint, "the rest keeps defaults")

    def test_a_wrong_type_keeps_the_default_and_says_so(self):
        config, problems = HereConfig.deserialize_reporting(
            {"display_voxel_waypoint": "si", "highlight_time": 30})
        self.assertTrue(config.display_voxel_waypoint,
                        "a bad value must not become the setting")
        self.assertEqual(config.highlight_time, 30, "the good ones still load")
        self.assertTrue(any("true or false" in p for p in problems), problems)

    def test_a_setting_nobody_reads_is_reported_not_deleted(self):
        _, problems = HereConfig.deserialize_reporting({"hilight_time": 30})
        self.assertTrue(any("hilight_time" in p for p in problems))

    def test_true_is_not_a_number(self):
        _, problems = HereConfig.deserialize_reporting({"highlight_time": True})
        self.assertTrue(any("whole number" in p for p in problems), problems)

    def test_round_trip(self):
        config = HereConfig.deserialize({"highlight_time": 42,
                                         "click_to_teleport": True})
        again = HereConfig.deserialize(json.loads(config.to_json_str()))
        self.assertEqual(config, again)

    def test_a_config_inside_a_config(self):
        outer = Outer.deserialize({"inner": {"name": "mine"}, "count": 9})
        self.assertEqual(outer.count, 9)
        self.assertEqual(outer.inner.name, "mine")
        self.assertTrue(outer.inner.enabled, "the nested defaults apply too")
        self.assertEqual(outer.serialize()["inner"]["name"], "mine")

    def test_instances_do_not_share_their_defaults(self):
        class WithList(Serializable):
            items: list = []

        first, second = WithList(), WithList()
        first.items.append("x")
        self.assertEqual(second.items, [],
                         "a mutable default must not be shared between plugins")


class MiscCase(unittest.TestCase):
    """The small pieces, each of which exists because a plugin needed it."""

    def test_new_thread_bare_and_named(self):
        from mcdr.misc import new_thread

        ran = []

        @new_thread
        def plain():
            ran.append("plain")

        @new_thread("DigAll reseed")
        def named():
            ran.append("named")

        plain().join(5)
        thread = named()
        thread.join(5)
        self.assertEqual(sorted(ran), ["named", "plain"])
        self.assertEqual(thread.name, "DigAll reseed")
        self.assertTrue(thread.daemon,
                        "a plugin thread must never hold a shutdown open")

    def test_versions_compare_the_way_minecraft_ones_do(self):
        from mcdr.misc import Version

        self.assertTrue(Version("1.18") < Version("1.19"))
        self.assertTrue(Version("1.21.4") > Version("1.21"))
        self.assertTrue(Version("1.9") < Version("1.10"),
                        "text comparison would get this backwards")
        self.assertEqual(Version("1.21"), Version("1.21.0"))

    def test_a_version_requirement(self):
        from mcdr.misc import VersionRequirement

        self.assertTrue(VersionRequirement(">=2.1.0").accept("2.15.5"))
        self.assertFalse(VersionRequirement(">=2.1.0").accept("2.0.5"))
        self.assertTrue(VersionRequirement("*").accept("anything"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
