"""Source artifact/opt-in preparation preserves game data and conflicting mods."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

import sourceprep
import w4ve


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.java = os.environ.get('W4VE_TEST_JAVA') or shutil.which('java')
        if not self.java:
            self.skipTest('Java21 required for source preparation fixture')
        (self.root / 'w4ve.toml').write_text('[server]\nkind="server"\nmc="1.21"\ngame_dir="game"\nflavor="fabric"\nstart=' +
            json.dumps([self.java, '-jar', 'fabric-server-launch.jar']) + '\n[bridge]\nmax_events=1024\n')
        self.server = w4ve.Server(self.root)
        launcher = self.root / 'game/fabric-server-launch.jar'
        launcher.parent.mkdir()
        loader = self.root / 'game/libraries/net/fabricmc/fabric-loader/0.19.3/fabric-loader-0.19.3.jar'
        loader.parent.mkdir(parents=True)
        with zipfile.ZipFile(launcher, 'w') as jar:
            jar.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nClass-Path: libraries/net/fabricmc/fabric-loader/0.19.3/fabric-loader-0.19.3.jar\n')
        with zipfile.ZipFile(loader, 'w') as jar:
            jar.writestr('net/fabricmc/loader/impl/launch/server/FabricServerLauncher.class', b'fixture-only; not executed')
        self.bridge = self.jar('bridge.jar', 'w4ve-bridge')
        self.fabric = self.jar('fabric-api.jar', 'fabric-api')
        self.options = {'bridge': self.bridge, 'fabric_api': self.fabric, 'java': self.java}

    def jar(self, filename, mod_id, *, mc='>=1.21 <1.21.2', extra=''):
        target = self.root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, 'w') as jar:
            jar.writestr('fabric.mod.json', json.dumps({'id': mod_id, 'version': '1.0.0',
                'environment': 'server', 'depends': {'minecraft': mc, 'java': '>=21'}}))
            jar.writestr('fixture.txt', extra)
        return target

    def test_prepare_default_presence_then_explicit_chat_preserves_journal_and_other_policy(self):
        journal = self.root / 'w4ve/game/events.sqlite3'
        journal.parent.mkdir(parents=True)
        journal.write_bytes(b'preserved fixture evidence')
        selected = sourceprep.prepare(self.server, **self.options)
        current = w4ve.Server(self.root)
        self.assertTrue(current.config['bridge']['enabled'])
        self.assertFalse(current.config['bridge']['chat'])
        self.assertEqual(current.config['bridge']['max_events'], 1024)
        for item in selected['artifacts']:
            self.assertEqual(hashlib.sha256(Path(item['destination']).read_bytes()).hexdigest(), item['sha256'])
        sourceprep.prepare(current, **self.options, chat=True)
        self.assertTrue(w4ve.Server(self.root).config['bridge']['chat'])
        self.assertEqual(journal.read_bytes(), b'preserved fixture evidence')
        self.assertFalse((self.root / 'w4ve/api-tokens.json').exists())
        self.assertEqual(len(list((self.root / 'game/mods').glob('*.jar'))), 2)

    def test_conflicting_existing_mod_is_preserved_and_never_overwritten(self):
        existing = self.jar('game/mods/old-bridge.jar', 'w4ve-bridge', extra='operator variant')
        before = existing.read_bytes()
        with self.assertRaises(ValueError):
            sourceprep.prepare(self.server, **self.options, chat=True)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(len(list(existing.parent.glob('*.jar'))), 1)
        self.assertNotIn('enabled', w4ve.Server(self.root).config['bridge'])

    def test_dry_run_and_incompatible_artifact_do_not_change_files(self):
        before = (self.root / 'w4ve.toml').read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = w4ve.main(['--dir', str(self.root), 'source', 'prepare', '--dry-run',
                '--bridge-file', str(self.bridge), '--fabric-api', str(self.fabric), '--java', self.java, '--chat'])
        self.assertEqual(result, 0)
        self.assertEqual((self.root / 'w4ve.toml').read_bytes(), before)
        self.assertFalse((self.root / 'game/mods').exists())
        wrong = self.jar('wrong.jar', 'w4ve-bridge', mc='1.20.1')
        with self.assertRaises(ValueError):
            sourceprep.prepare(self.server, bridge=wrong, fabric_api=self.fabric, java=self.java)
        self.assertFalse((self.root / 'game/mods').exists())

    def test_bundled_artifacts_are_usable_from_installed_distribution(self):
        bundled = Path(sourceprep.__file__).resolve().parent / 'lib/w4ve-fabric-source.jar'
        if not bundled.is_file():
            self.skipTest('base distribution/source checkout has no optional bundled jars')
        selected = sourceprep.prepare(self.server, java=self.java, chat=False)
        self.assertEqual({item['id'] for item in selected['artifacts']}, {'w4ve-bridge', 'fabric-api'})


if __name__ == '__main__':
    unittest.main()
