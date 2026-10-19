"""Exercise the maintenance parser and real child process with scoped grants."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import api
import w4ve


class ExecServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'w4ve.toml').write_text('[server]\nkind="server"\n')
        self.server = w4ve.Server(self.root)
        self.server.set_service('chat', {
            'command': [sys.executable, '-m', 'probe'], 'cwd': '.',
            'w4ve_api': 'identity', 'w4ve_events': 'chat'})
        (self.root / 'probe.py').write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "from w4ve_client import W4VE\n"
            "Path('probe.json').write_text(json.dumps({"
            "'argv':sys.argv[1:],'python':sys.executable,"
            "'identity':os.environ['W4VE_IDENTITY_TOKEN'],"
            "'events':os.environ['W4VE_EVENTS_TOKEN'],"
            "'ambient_operator':os.environ.get('W4VE_OPERATOR_TOKEN')}))\n"
            "raise SystemExit(7 if '--fail' in sys.argv else 0)\n")

    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = w4ve.main(['--dir', str(self.root), 'exec-service', *args])
        return code, output.getvalue()

    def test_parser_runs_configured_interpreter_preserves_child_options_and_exit_code(self):
        with patch.dict(os.environ, {'W4VE_OPERATOR_TOKEN': 'ambient-operator-fixture'}):
            code, output = self.run_cli('--python-module', 'probe', 'chat',
                                        '--', 'status', '--fail', '--plan', 'reviewed')
        self.assertEqual(code, 7)
        probe = json.loads((self.root / 'probe.json').read_text())
        self.assertEqual(probe['argv'], ['status', '--fail', '--plan', 'reviewed'])
        self.assertEqual(probe['python'], sys.executable)
        self.assertIsNone(probe['ambient_operator'])
        tokens = api.Tokens(self.root)
        self.assertEqual(tokens.check(probe['identity'])['scope'], 'identity')
        self.assertEqual(tokens.check(probe['events'])['scope'], 'game-chat')
        self.assertNotEqual(probe['identity'], probe['events'])
        self.assertNotIn(probe['identity'], output)
        self.assertNotIn(probe['events'], output)

    def test_dry_run_does_not_create_tokens_or_launch_child(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertEqual(self.run_cli('--dry-run', '--python-module', 'probe', 'chat', 'status')[0], 0)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')), before)

    def test_missing_service_and_operator_scope_refuse_launch(self):
        self.assertEqual(self.run_cli('--python-module', 'probe', 'missing', 'status')[0], 1)
        self.server.set_service('chat', {'command': [sys.executable, '-m', 'probe'], 'w4ve_api': 'admin'})
        self.assertEqual(self.run_cli('--python-module', 'probe', 'chat', 'status')[0], 1)
        self.assertFalse((self.root / 'probe.json').exists())


if __name__ == '__main__':
    unittest.main()
