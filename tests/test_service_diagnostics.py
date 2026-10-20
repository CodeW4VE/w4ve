"""Declared service diagnosis uses real children and never changes grant state."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import api
import package
import w4ve


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'w4ve.toml').write_text('[server]\nkind="server"\n')
        (self.root / 'probe.py').write_text('import json,os,sys,time\n'
            'if "slow" in sys.argv: time.sleep(30)\n'
            'if "bad" in sys.argv: print("not JSON secret-fixture")\n'
            'else: print(json.dumps({"ok":True,"result":{"healthy":"fail" not in sys.argv},"private":"secret-fixture"}))\n')
        self.server = w4ve.Server(self.root)
        self.raw = {'command': [sys.executable, '-m', 'probe'], 'w4ve_api': 'identity',
            'w4ve_events': 'chat', 'diagnostic': {'python_module': 'probe', 'args': ['doctor']}}
        self.server.set_service('chat', self.raw)

    def grants(self):
        api.service_environment(self.root, self.server.config, 'chat', w4ve_api='identity', w4ve_events='chat')

    def diagnose(self, *args):
        raw = {**self.raw, 'diagnostic': {**self.raw['diagnostic'], 'args': list(args)}}
        return w4ve.diagnose_service(self.server, 'chat', raw, timeout=.3 if 'slow' in args else 5)

    def test_missing_grants_are_not_created_and_disabled_diagnostics_do_not_need_them(self):
        path = api.Tokens(self.root).path
        self.assertFalse(path.exists())
        self.assertEqual(self.diagnose('doctor'), 'unavailable')
        self.assertFalse(path.exists())
        raw = {**self.raw, 'diagnostic': {**self.raw['diagnostic'], 'when_env': 'W4VE_CAPTURE_CHANNEL'}}
        self.assertEqual(w4ve.diagnose_service(self.server, 'chat', raw), 'disabled')
        self.assertFalse(path.exists())

    def test_health_unhealthy_invalid_response_and_timeout_preserve_registry(self):
        self.grants()
        path = api.Tokens(self.root).path
        before = path.read_bytes()
        self.assertEqual(self.diagnose('doctor'), 'healthy')
        self.assertEqual(self.diagnose('fail'), 'requires attention')
        self.assertEqual(self.diagnose('bad'), 'unavailable')
        self.assertEqual(self.diagnose('slow'), 'timed out')
        self.assertEqual(path.read_bytes(), before)

    def test_general_doctor_aggregates_without_echoing_private_diagnostic_output(self):
        self.grants()
        self.server.set_service('chat', {**self.raw, 'diagnostic': {'python_module': 'probe', 'args': ['fail']}})
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = w4ve.cmd_doctor(SimpleNamespace(dir=str(self.root)), {'pieces': {}})
        self.assertEqual(code, 1)
        self.assertIn('diagnostic requires attention', output.getvalue())
        self.assertNotIn('secret-fixture', output.getvalue())
        for token in api.Tokens(self.root)._read():
            self.assertNotIn(token, output.getvalue())

    def test_revoked_or_conflicting_scope_does_not_rotate_on_diagnosis(self):
        self.grants()
        registry = api.Tokens(self.root)
        registry.revoke('service-events:chat')
        before = registry.path.read_bytes()
        self.assertEqual(self.diagnose('doctor'), 'unavailable')
        self.assertEqual(registry.path.read_bytes(), before)

    def test_manifest_refuses_invalid_diagnostics(self):
        for declaration in (None, [], {'python_module': '../bad'}, {'python_module': 'ok', 'args': 'doctor'},
                            {'python_module': 'ok', 'when_env': 'not valid'}, {'python_module': 'ok', 'unknown': True}):
            self.assertTrue(package.diagnostic_problems(declaration))
        self.assertFalse(package.diagnostic_problems(self.raw['diagnostic']))


if __name__ == '__main__':
    unittest.main()
