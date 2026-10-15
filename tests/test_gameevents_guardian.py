"""Linux integration: real guardian processes and an explicitly synthetic producer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

GAME = '''import json,os,sys,time,uuid
from pathlib import Path
from w4ve_client import W4VE, W4VEError
descriptor=Path(os.environ['W4VE_GAME_SOURCE_CONFIG'])
config=json.loads(descriptor.read_text())
client=W4VE(root=descriptor.parents[2],token=config['token'],timeout=2)
sequence=0
player='a'*32
connection='b'*32
def emit(kind,data):
    global sequence
    event={'schema':1,'boot':config['boot'],'id':uuid.uuid4().hex,'sequence':sequence+1,
           'kind':kind,'occurred':int(time.time()),'data':data}
    deadline=time.monotonic()+10
    while True:
        try:
            client.game_source_append(event)
            sequence+=1
            return
        except W4VEError:
            if time.monotonic()>deadline: raise
            time.sleep(.1)
def chat():
    emit('player.chat',{'uuid':player,'connection':connection,'text':'synthetic fixture', 'signature_sha256':''})
def probe():
    Path('probe.json').write_text(json.dumps({'boot':config['boot'],'sequence':sequence}))
emit('server.started',{})
emit('player.joined',{'uuid':player,'connection':connection,'name':'TestPlayer'})
chat()
probe()
print('[00:00:00] [Server thread/INFO]: Done (0.1s)! For help, type "help"',flush=True)
for command in sys.stdin:
    if command.strip()=='chat':
        chat(); probe()
    if command.strip()=='stop':
        emit('server.stopping',{})
        emit('player.left',{'uuid':player,'connection':connection})
        probe()
        break
'''


@unittest.skipIf(os.name == "nt", "guardian process integration requires Linux")
class GuardianSourceTests(unittest.TestCase):
    def setUp(self):
        import runtime
        import api
        self.runtime = runtime
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game_dir = self.root / "game"
        self.game_dir.mkdir()
        (self.game_dir / "synthetic.py").write_text(GAME)
        self.hosts, self.logs = [], []
        self.addCleanup(self.cleanup)
        self.admin = api.Tokens(self.root).issue("source-qa", "admin")
        self.runtime_root = Path(runtime.__file__).resolve().parent
        self.env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(self.runtime_root), str(self.runtime_root / "sdk/client")]))

    def cleanup(self):
        for host in reversed(self.hosts):
            if host.poll() is None:
                host.terminate()
                try: host.wait(timeout=15)
                except subprocess.TimeoutExpired: host.kill(); host.wait(timeout=5)
        for log in self.logs:
            log.close()

    def start(self, takeover=False):
        host = ("import sys; import runtime; from pathlib import Path; "
                "root=Path(sys.argv[1]); g=runtime.Guardian(root, "
                "{'plugins':False,'bridge':{'enabled':True,'chat':True}}); "
                "g.add_server([sys.executable,str(root/'game/synthetic.py')],root/'game',flavor='vanilla'); "
                "code,message=g.run(take_over=(sys.argv[2]=='yes')); print(message,flush=True); sys.exit(code)")
        log = (self.root / ("guardian-%d.log" % len(self.hosts))).open("w")
        self.logs.append(log)
        process = subprocess.Popen([sys.executable, "-c", host, str(self.root), "yes" if takeover else "no"],
                                   env=self.env, stdout=log, stderr=log)
        self.hosts.append(process)
        return process

    def wait(self, condition, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value: return value
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        self.fail("synthetic guardian fixture did not reach expected state; inspect its local log")

    def status(self):
        result = self.runtime.Client(self.root).call({"cmd": "status"})
        return next((item for item in result.get("processes", []) if item["name"] == "server"), {})

    def test_child_receives_private_source_and_can_flush_during_graceful_shutdown(self):
        from w4ve_client import W4VE, Denied
        from gameevents import GameEvents, Source
        host = self.start()
        before = self.wait(lambda: self.status() if self.status().get("ready") else None)
        sdk = W4VE(self.root, token=self.admin)
        events = sdk.game_events(chat=True)["events"]
        self.assertEqual([item["kind"] for item in events], ["server.started", "player.joined", "player.chat"])
        self.assertEqual(events[-1]["data"]["name"], "TestPlayer")
        descriptor = json.loads((self.root / "w4ve/run/game-source.json").read_text())
        self.assertEqual((descriptor["pid"], descriptor["starttime"]),
                         (before["pid"], self.runtime.starttime_of(before["pid"])))
        self.assertNotIn(descriptor["token"], (self.root / "w4ve/run/state.json").read_text())
        with self.assertRaises(Denied):
            W4VE(self.root, token=descriptor["token"]).command("stop")
        host.terminate()
        self.assertEqual(host.wait(timeout=20), 0)
        journal = GameEvents(self.root / "w4ve/game/events.sqlite3", chat=True)
        self.assertEqual(journal.poll(chat=True)["events"][-1]["kind"], "player.left")
        self.assertEqual(journal.roster(Source(descriptor["boot"], descriptor["token"]))["players"], [])
        self.assertFalse(self.runtime.alive(before["pid"], descriptor["starttime"]))

    def test_guardian_handover_keeps_source_boot_and_can_accept_more_events(self):
        from w4ve_client import W4VE
        first = self.start()
        before = self.wait(lambda: self.status() if self.status().get("ready") else None)
        descriptor = json.loads((self.root / "w4ve/run/game-source.json").read_text())
        second = self.start(takeover=True)
        self.wait(lambda: first.poll() is not None)
        self.assertEqual(first.returncode, 0)
        self.assertIsNone(second.poll())
        self.wait(lambda: self.status().get("pid") == before["pid"])
        after = json.loads((self.root / "w4ve/run/game-source.json").read_text())
        self.assertEqual((after["boot"], after["token"]), (descriptor["boot"], descriptor["token"]))
        response = self.runtime.Client(self.root).call({"cmd": "send", "text": "chat"})
        self.assertTrue(response.get("ok"))
        self.wait(lambda: json.loads((self.game_dir / "probe.json").read_text())["sequence"] == 4)
        events = W4VE(self.root, token=self.admin).game_events(chat=True)["events"]
        self.assertEqual(len(events), 4)
        self.assertEqual(events[-1]["data"]["text"], "synthetic fixture")
        second.terminate()
        self.assertEqual(second.wait(timeout=20), 0)
