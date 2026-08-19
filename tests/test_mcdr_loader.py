"""Tests for finding MCDR plugins and reading what they declare.

The fixtures are the shapes MineWave actually has, including the messy ones:
two copies of the same plugin, backups sitting next to plugins, a folder of
history called `olds`, a plugin whose metadata is built at runtime, and a file
that does not parse at all. None of these are hypothetical; they all come from
the inventory of the five live servers.
"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcdr import loader  # noqa: E402


class ReadingCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-mcdr-"))

    def single(self, name, text):
        path = self.tmp / name
        path.write_text(text)
        return path

    def test_a_single_file_plugin(self):
        path = self.single("dig.py",
                           "PLUGIN_METADATA = {'id': 'dig', 'version': '1.0.0',\n"
                           "                   'name': 'Dig'}\n"
                           "def on_load(server, old):\n    pass\n")
        plugin = loader.read(path)
        self.assertEqual(plugin.id, "dig")
        self.assertEqual(plugin.version, "1.0.0")
        self.assertEqual(plugin.shape, "file")
        self.assertEqual(plugin.problems, [])

    def test_metadata_built_at_runtime_is_admitted_not_guessed(self):
        path = self.single("odd.py",
                           "import os\n"
                           "PLUGIN_METADATA = {'id': 'odd', 'version': os.environ['V']}\n")
        plugin = loader.read(path)
        self.assertTrue(any("built at runtime" in p for p in plugin.problems))

    def test_a_file_that_does_not_parse(self):
        path = self.single("rota.py", "esto no es python(")
        plugin = loader.read(path)
        self.assertTrue(any("will not parse" in p for p in plugin.problems))

    def test_a_folder_plugin_with_an_entrypoint(self):
        folder = self.tmp / "Here"
        (folder / "here").mkdir(parents=True)
        (folder / loader.MANIFEST).write_text(json.dumps({
            "id": "here", "version": "1.2.2", "name": "Here",
            "entrypoint": "here.entry",
            "description": {"en_us": "Broadcast your position", "zh_cn": "显示坐标"},
            "dependencies": {"mcdreforged": ">=2.1.0"},
        }))
        plugin = loader.read(folder)
        self.assertEqual(plugin.id, "here")
        self.assertEqual(plugin.entrypoint, "here.entry")
        self.assertEqual(plugin.description, "Broadcast your position")
        self.assertEqual(plugin.requires_mcdr, ">=2.1.0")
        # mcdreforged is a requirement on the host, not a plugin to load.
        self.assertEqual(plugin.dependencies, {})

    def test_the_entrypoint_defaults_to_the_id(self):
        folder = self.tmp / "Plain"
        folder.mkdir()
        (folder / loader.MANIFEST).write_text('{"id": "plain", "version": "1.0.0"}')
        self.assertEqual(loader.read(folder).entrypoint, "plain")

    def test_an_mcdr_archive(self):
        path = self.tmp / "DigAll-v4.2.1.mcdr"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(loader.MANIFEST,
                             '{"id": "dig_all", "version": "4.2.1"}')
        plugin = loader.read(path)
        self.assertEqual(plugin.id, "dig_all")
        self.assertEqual(plugin.shape, "archive")

    def test_a_pyz_keeps_its_manifest_one_level_down(self):
        """The official packer nests everything under the plugin's own folder."""
        path = self.tmp / "PrimeBackup-v1.13.1.pyz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("prime_backup/" + loader.MANIFEST,
                             '{"id": "prime_backup", "version": "1.13.1"}')
            archive.writestr("prime_backup/__init__.py", "")
        plugin = loader.read(path)
        self.assertEqual(plugin.id, "prime_backup")

    def test_a_zip_that_is_not_a_plugin(self):
        path = self.tmp / "something.mcdr"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("readme.txt", "hello")
        plugin = loader.read(path)
        self.assertTrue(any("no mcdreforged.plugin.json" in p
                            for p in plugin.problems))


class DiscoveryCase(unittest.TestCase):
    """A plugins folder as messy as the real ones."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-plugins-"))
        (self.tmp / "olds").mkdir()
        (self.tmp / "olds" / "online.py").write_text(
            "PLUGIN_METADATA = {'id': 'online', 'version': '1.0.0'}\n")
        (self.tmp / "plugin_bak").mkdir()
        (self.tmp / "config").mkdir()          # data, not a plugin
        (self.tmp / "online.py").write_text(
            "PLUGIN_METADATA = {'id': 'online', 'version': '1.4.0'}\n")
        (self.tmp / "online.py.bak-20260801").write_text("whatever")
        (self.tmp / "here.py.disabled").write_text("whatever")

    def test_history_and_backups_are_not_plugins(self):
        found = loader.discover(self.tmp)
        self.assertEqual([p.path.name for p in found], ["online.py"])

    def test_two_copies_of_one_plugin_are_reported(self):
        for name, version in (("MultiRconAPI-v1.0.0.mcdr", "1.0.0"),
                              ("MultiRconAPI-V2.0.1.pyz", "2.0.1")):
            with zipfile.ZipFile(self.tmp / name, "w") as archive:
                archive.writestr(loader.MANIFEST, json.dumps(
                    {"id": "multi_rcon_api", "version": version}))
        report = loader.report(self.tmp)
        self.assertTrue(any("2 copies of 'multi_rcon_api'" in p
                            for p in report["problems"]),
                        report["problems"])


class OrderCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w4ve-order-"))

    def plugin(self, pid, depends=None):
        folder = self.tmp / pid
        folder.mkdir()
        (folder / loader.MANIFEST).write_text(json.dumps({
            "id": pid, "version": "1.0.0",
            "dependencies": dict(depends or {}, mcdreforged=">=2.1.0"),
        }))

    def test_a_dependency_loads_first(self):
        self.plugin("minecraft_data_api")
        self.plugin("where_is", {"minecraft_data_api": "*"})
        order, problems = loader.load_order(loader.discover(self.tmp))
        self.assertEqual([p.id for p in order],
                         ["minecraft_data_api", "where_is"])
        self.assertEqual(problems, [])

    def test_a_missing_dependency_is_named_and_the_rest_still_load(self):
        """WhereIs without MinecraftDataAPI is a real MineWave state."""
        self.plugin("where_is", {"minecraft_data_api": "*"})
        self.plugin("dig_all")
        order, problems = loader.load_order(loader.discover(self.tmp))
        self.assertIn("dig_all", [p.id for p in order])
        self.assertTrue(any("where_is needs minecraft_data_api" in p
                            for p in problems), problems)

    def test_a_cycle_is_reported_rather_than_hanging(self):
        self.plugin("a", {"b": "*"})
        self.plugin("b", {"a": "*"})
        _, problems = loader.load_order(loader.discover(self.tmp))
        self.assertTrue(any("cycle" in p for p in problems), problems)

    def test_a_chain_three_deep(self):
        self.plugin("bottom")
        self.plugin("middle", {"bottom": "*"})
        self.plugin("top", {"middle": "*"})
        order, problems = loader.load_order(loader.discover(self.tmp))
        names = [p.id for p in order]
        self.assertLess(names.index("bottom"), names.index("middle"))
        self.assertLess(names.index("middle"), names.index("top"))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
