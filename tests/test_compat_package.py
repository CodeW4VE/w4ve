"""The importable `mcdreforged`, checked from a process that is not a plugin.

Run in a subprocess on purpose. The point of `compat/mcdreforged/` is that a
program W4VE did not load can import it, so importing it from inside the test
process, which already has `mcdr` on its path, would prove nothing at all.

ChatBridge's daemon is the reason it exists: a separate process, started from a
command line, that imports `mcdreforged.utils.serializer` for one class.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402

COMPAT = ROOT / "compat"


def in_a_clean_process(source):
    """Run source with only the compat folder on PYTHONPATH."""
    return subprocess.run([sys.executable, "-c", source],
                          cwd=str(Path.home()),
                          env={"PYTHONPATH": str(COMPAT),
                               "PATH": "/usr/bin:/bin"},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=60)


class CompatPackageCase(unittest.TestCase):

    def test_the_three_paths_chatbridge_imports(self):
        result = in_a_clean_process(
            "from mcdreforged.utils.serializer import Serializable\n"
            "from mcdreforged.api.utils.serializer import Serializable as B\n"
            "from mcdreforged.api.utils import Serializable as C\n"
            "from mcdreforged.api.rcon import RconConnection\n"
            "assert Serializable is B is C, 'three names, one class'\n"
            "print('ok', RconConnection.__name__)\n")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("ok RconConnection", result.stdout)

    def test_it_is_the_same_class_the_plugins_get(self):
        """⚠️ Re-exported, never reimplemented.

        A second copy of Serializable living in compat/ would drift, and the
        daemon and the plugin would disagree about a config file they both
        read. The check is deliberately about identity, not behaviour.
        """
        result = in_a_clean_process(
            "from mcdreforged.utils.serializer import Serializable\n"
            "from mcdr.serializer import Serializable as Original\n"
            "assert Serializable is Original, 'compat holds a copy'\n"
            "print('same class')\n")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("same class", result.stdout)

    def test_a_real_config_class_round_trips_out_there(self):
        """The shape ChatBridge's own config has, declared and read back."""
        result = in_a_clean_process(
            "from mcdreforged.utils.serializer import Serializable\n"
            "class ClientConfig(Serializable):\n"
            "    aes_key: str = 'ThisIstheSecret'\n"
            "    server_port: int = 30001\n"
            "config = ClientConfig.deserialize({'server_port': 40000})\n"
            "assert config.server_port == 40000\n"
            "assert config.aes_key == 'ThisIstheSecret'\n"
            "print(config.serialize())\n")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("40000", result.stdout)

    def test_the_runtime_can_find_it(self):
        found = runtime.mcdr_compat_path()
        self.assertIsNotNone(found, "the guardian could not find compat/")
        self.assertTrue((found / "mcdreforged" / "__init__.py").exists())

    def test_a_service_that_asks_for_it_gets_it_on_its_path(self):
        """`mcdr = true` in a service block is what puts it there."""
        services = runtime.services_from({"services": {
            "bridge": {"command": "python daemon.py", "mcdr": True},
            "plain": {"command": "python other.py"},
        }})
        by_name = {s["name"]: s for s in services}
        self.assertTrue(by_name["bridge"]["mcdr"])
        self.assertFalse(by_name["plain"]["mcdr"])

    def test_a_service_keeps_the_environment_it_was_given(self):
        """⚠️ Merged, not replaced: a service without PATH finds nothing."""
        services = runtime.services_from({"services": {
            "bridge": {"command": "python daemon.py",
                       "env": {"CHATBRIDGE_HOME": "/srv/bridge"}},
        }})
        self.assertEqual({"CHATBRIDGE_HOME": "/srv/bridge"},
                         services[0]["env"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
