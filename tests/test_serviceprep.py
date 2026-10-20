"""Real offline wheel installation and activation/failure boundaries."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import serviceprep
import w4ve


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'w4ve.toml').write_text('[server]\nkind="server"\n')
        self.server = w4ve.Server(self.root)
        self.server.set_service('chat', {'command': [sys.executable, '-m', 'probe'],
            'python_requires': ['w4ve-prepare-fixture==1.0']})
        self.wheels = self.root / 'wheels'
        self.wheels.mkdir()
        with zipfile.ZipFile(self.wheels / 'w4ve_prepare_fixture-1.0-py3-none-any.whl', 'w') as wheel:
            wheel.writestr('prepare_fixture/__init__.py', 'VALUE=42\n')
            wheel.writestr('w4ve_prepare_fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: w4ve-prepare-fixture\nVersion: 1.0\n')
            wheel.writestr('w4ve_prepare_fixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: w4ve-qa\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            wheel.writestr('w4ve_prepare_fixture-1.0.dist-info/RECORD', '')

    def test_dry_run_has_no_environment_or_configuration_side_effects(self):
        before = (self.root / 'w4ve.toml').read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = w4ve.main(['--dir', str(self.root), 'prepare', 'chat', '--dry-run', '--no-index', '--wheelhouse', str(self.wheels)])
        self.assertEqual(code, 0)
        self.assertEqual((self.root / 'w4ve.toml').read_bytes(), before)
        self.assertFalse((self.root / 'w4ve/environments').exists())

    def test_real_offline_preparation_activates_an_isolated_interpreter(self):
        result = serviceprep.prepare(self.server, 'chat', wheelhouse=str(self.wheels), no_index=True)
        configured = w4ve.Server(self.root).config['services']['chat']['command'][0]
        self.assertEqual(configured, result['interpreter'])
        process = subprocess.run([configured, '-I', '-c', 'import prepare_fixture; assert prepare_fixture.VALUE == 42'], capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotEqual(configured, sys.executable)
        self.assertFalse((self.root / 'w4ve/api-tokens.json').exists())
        metadata = json.loads((Path(configured).parents[1] / 'preparation.json').read_text())
        self.assertIn({'name': 'w4ve-prepare-fixture', 'version': '1.0'}, metadata['installed'])

    def test_failed_offline_installation_keeps_previous_configuration(self):
        self.server.set_service('chat', {'command': [sys.executable, '-m', 'probe'], 'python_requires': ['missing-fixture==999']})
        before = (self.root / 'w4ve.toml').read_bytes()
        with self.assertRaises(ValueError):
            serviceprep.prepare(self.server, 'chat', wheelhouse=str(self.wheels), no_index=True)
        self.assertEqual((self.root / 'w4ve.toml').read_bytes(), before)
        self.assertEqual(w4ve.Server(self.root).config['services']['chat']['command'][0], sys.executable)

    def test_invalid_requirements_names_and_extras_are_refused_before_writes(self):
        for values in (['--index-url'], ['https://secret.example/pkg'], ['x\ny'], 'some-package'):
            self.assertFalse(serviceprep.requirements_valid(values))
        for name in ('../outside', 'bad/name', ''):
            with self.assertRaises(ValueError):
                serviceprep.prepare(self.server, name)
        self.assertFalse((self.root / 'w4ve/environments').exists())


if __name__ == '__main__':
    unittest.main()
