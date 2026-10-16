"""Inside an isolated QA container: installed guardian, real Fabric and TCP actors.

Only /qa/work is accepted. Inputs must be copied to /qa/inputs, never production.
The caller provides a network-none container and runs bridge_game_actors.cjs in
that same network namespace after ready.signal. No host ports are needed.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid
import urllib.request
import zipfile


def wait_for(check, process, seconds=60):
    from w4ve_client import W4VEError
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and process.poll() is None:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, W4VEError):
            pass
        time.sleep(.1)
    raise AssertionError("isolated bridge game check timed out; inspect private QA logs")


def main():
    chat_enabled = os.environ.get("QA_CHAT_ENABLED", "true")
    if chat_enabled not in ("true", "false"):
        raise ValueError("invalid QA chat policy")
    chat_enabled = chat_enabled == "true"
    hub_mode = os.environ.get("QA_HUB_ENABLED", "false")
    if hub_mode not in ("true", "false"):
        raise ValueError("invalid QA hub mode")
    hub_mode = hub_mode == "true"
    relay_mode = os.environ.get('QA_REQUIRE_RELAY') == 'true'
    if relay_mode and not hub_mode:
        raise ValueError('relay fixture requires installed hub')
    if hub_mode and not chat_enabled:
        raise ValueError("hub ingestion fixture requires explicit chat capture")
    root = Path("/qa/work")
    inputs = Path("/qa/inputs")
    if root.exists():
        raise ValueError("refusing to overwrite a QA workspace")
    root.mkdir(mode=0o700)
    spec = importlib.util.spec_from_file_location("qa_install", inputs / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    archive = inputs / "w4ve-0.3.0.tar.gz"
    version, _, _ = installer.read_archive(archive)
    installer.install(archive, root / "runtime", root / "bin")
    release = root / "runtime/releases" / version
    sys.path[:0] = [str(release), str(release / "sdk/client")]
    import runtime
    from api import Tokens
    from gameevents import GameEvents
    from w4ve_client import W4VE
    community = root / "community"
    game = community / "game"
    game.mkdir(parents=True)
    with zipfile.ZipFile(inputs / "game.zip") as packed:
        for member in packed.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                raise ValueError("invalid QA game archive")
        packed.extractall(game)
    mods = game / "mods"
    mods.mkdir(exist_ok=True)
    for name in ("fabric-api.jar", "bridge.jar") + (() if hub_mode else ("wavechat.jar",)):
        shutil.copyfile(inputs / name, mods / name)
    # These offline accounts and their world exist solely in this network-none
    # container. This is not an online-authentication or production config test.
    properties = {
        "server-ip": "127.0.0.1", "server-port": "25565", "online-mode": "false",
        "white-list": "false", "enforce-secure-profile": "false", "enable-rcon": "false",
        "enable-query": "false", "level-name": "qa-world", "level-type": "minecraft:flat",
        "view-distance": "2", "simulation-distance": "2", "max-players": "3",
        "allow-nether": "false", "generate-structures": "false", "spawn-protection": "0",
        "generator-settings": json.dumps({"layers": [{"height": 1, "block": "minecraft:bedrock"},
            {"height": 2, "block": "minecraft:dirt"}, {"height": 1, "block": "minecraft:grass_block"}],
            "biome": "minecraft:plains", "features": False, "lakes": False}, separators=(",", ":")),
    }
    (game / "server.properties").write_text("".join(key + "=" + value + "\n" for key, value in properties.items()))
    (game / "eula.txt").write_text("# Authorized disposable integration test only.\neula=true\n")
    offline_id = uuid.UUID(bytes=hashlib.md5(b"OfflinePlayer:BridgeQA_A").digest(), version=3)
    (game / "ops.json").write_text(json.dumps([{"uuid": str(offline_id), "name": "BridgeQA_A", "level": 4, "bypassesPlayerLimit": False}]))
    config = game / "config/wavechat"
    config.mkdir(parents=True)
    binding = config / "w4ve-identity.json"
    binding.write_text(json.dumps({"schema": 1, "owner": "wavechat-server", "audience": "wavechat",
        "community": "bridge-qa", "instance": "fabric-qa", "public_url": "https://qa.example.invalid",
        "secret": "QA_ONLY_NOT_A_REAL_SIGNING_SECRET_20261015"}))
    os.chmod(binding, 0o600)
    if hub_mode:
        import w4ve
        import package
        start = ['/qa/jre/bin/java', '-Xms256m', '-Xmx1024m', '-Duser.home=/tmp',
                 '-jar', 'fabric-server-launch.jar', 'nogui']
        if relay_mode:
            start[1:1] = ['-Djavax.net.ssl.trustStore=/qa/inputs/qa-trust.p12',
                          '-Djavax.net.ssl.trustStorePassword=fixture-only']
        (community / 'w4ve.toml').write_text(
            'plugins=false\n[server]\nkind="server"\nmc="1.21"\ngame_dir="game"\nflavor="fabric"\n'
            + 'start=' + json.dumps(start) + '\n[bridge]\nenabled=true\nchat=true\nretention=300\n')
        (game / 'whitelist.json').write_text('[]')
        # The previous literal belongs only to the capture-only fixture. The
        # installed package must enroll its own issuer before this boot starts.
        binding.unlink()
        server = w4ve.Server(community)
        installation = package.Install(inputs / 'wavechat-server.w4ve', server)
        actions, problems = installation.plan()
        if problems:
            raise AssertionError('installed QA hub package plan failed')
        installation.apply(actions)
        installation.commit()
        server.set_setting('wavechat-server', 'python_bin', sys.executable)
        public_url = 'https://localhost:8792' if relay_mode else 'https://qa.example.invalid'
        if w4ve.main(['--dir', str(community), 'configure', 'wavechat-server',
                      '--set', 'native_chat=true', '--set', 'public_url=' + public_url]):
            raise AssertionError('installed QA hub pairing failed')
        server = w4ve.Server(community)
        fields = dict(server.config['services']['wavechat-hub'])
        if relay_mode:
            fields['command'] += ['--ssl-keyfile', '/qa/inputs/qa-tls.key', '--ssl-certfile', '/qa/inputs/qa-tls.crt']
            (config / 'wavechat-server.json').write_text(json.dumps({'native_relay': True}))
        fields['w4ve_events'] = 'chat'
        fields['env'] = {**fields.get('env', {}), 'W4VE_CAPTURE_CHANNEL': 'general',
                         'W4VE_DATA_DIR': str(root / 'hub-data'), 'W4VE_UPLOAD': 'false', 'W4VE_DM': 'false'}
        server.set_service('wavechat-hub', fields, owner='wavechat-server')
        paired = json.loads(binding.read_text())
        fixture = root / 'fixture-identity.json'
        fixture_data = {key: paired[key] for key in ('secret', 'community', 'instance')}
        if relay_mode:
            # A second issuer is a synthetic QA origin, not a claim that another
            # Minecraft server was launched. Exercise the real authority/session
            # path; never fake a cross-origin message by changing a stored row.
            from identity import Authority
            authority = Authority(community / 'w4ve/identity/authority.sqlite3')
            fixture_data['remote'] = authority.register_instance(paired['community'], 'qa-remote')
            authority.set_instance_resources(paired['community'], 'qa-remote', 'wavechat', ['channel:general'])
        fixture.write_text(json.dumps(fixture_data))
        os.chmod(fixture, 0o600)
    token = Tokens(community).issue("bridge-game-qa", "admin")
    client = W4VE(root=community, token=token, timeout=2)
    (root / "launch.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path[:0]=" + repr([str(release), str(release / "sdk/client")]) + "\n"
        "from runtime import Guardian\n"
        "root=Path(" + repr(str(community)) + ")\n"
        "g=Guardian(root, {'plugins':False,'bridge':{'enabled':True,'chat':" + repr(chat_enabled) + ",'retention':300}})\n"
        "g.add_server(['/qa/jre/bin/java','-Xms256m','-Xmx1024m','-Duser.home=/tmp',"
        "'-jar','fabric-server-launch.jar','nogui'],root/'game',flavor='fabric')\n"
        "code,message=g.run(); print(message,flush=True); sys.exit(code)\n")
    if hub_mode:
        (root / 'launch.py').write_text(
            'import sys\nsys.path[:0]=' + repr([str(release), str(release / 'sdk/client')]) + '\n'
            'import w4ve,runtime\nfrom pathlib import Path\n'
            'g,_=w4ve.runtime_pieces(w4ve.Server(Path(' + repr(str(community)) + ')),runtime)\n'
            'assert "wavechat-hub" in g.watched\n'
            'code,message=g.run(); print(message,flush=True); sys.exit(code)\n')
    log_path = root / "guardian.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, str(root / "launch.py")], stdout=log, stderr=log)
        try:
            wait_for(lambda: any(event["kind"] == "server.started" for event in client.game_events()["events"]), process, 180)
            if hub_mode:
                def hub_ready():
                    try:
                        url = 'https://localhost:8792/ready' if relay_mode else 'http://127.0.0.1:8792/ready'
                        context = ssl.create_default_context(cafile=str(inputs / 'qa-tls.crt')) if relay_mode else None
                        with urllib.request.urlopen(url, timeout=2, context=context) as response:
                            return response.status == 200 and json.load(response).get('ok') is True
                    except OSError:
                        return False
                wait_for(hub_ready, process)
            (root / "ready.signal").write_text("network-none QA game only\n")
            wait_for(lambda: (root / "actors-done.json").exists(), process, 120)
            actors = json.loads((root / "actors-done.json").read_text())
            if actors.get("ok") is not True:
                raise AssertionError("TCP QA actors failed")
            if actors.get("controls") != ["QA_COMMAND_ME", "QA_COMMAND_SAY", "QA_PRIVATE"]:
                raise AssertionError("control messages did not really reach TCP clients")
            if actors.get("respawn") is not True:
                raise AssertionError("actual game respawn was not checked")
            if os.environ.get('QA_REQUIRE_RESPAWN_DISCOVERY') == 'true' and actors.get('respawnDiscovery') is not True:
                raise AssertionError('native discovery was not renewed after actual respawn')
            def captured():
                events = client.game_events(chat=chat_enabled)["events"]
                return events if len([event for event in events if event["kind"] == "player.left"]) == 3 else None
            events = wait_for(captured, process)
            joined = [event for event in events if event["kind"] == "player.joined"]
            chats = [event for event in events if event["kind"] == "player.chat"]
            expected_chat = ["QA_PUBLIC_FIRST", "QA_PUBLIC_BOB", "QA_PUBLIC_RESPAWN", "QA_PUBLIC_RECONNECTED"] if chat_enabled else []
            if len(joined) != 3 or [event["data"]["text"] for event in chats] != expected_chat:
                raise AssertionError("real game chat count or command/private exclusion failed")
            alice = [event for event in joined if event["data"]["name"] == "BridgeQA_A"]
            if len(alice) != 2 or alice[0]["data"]["uuid"] != alice[1]["data"]["uuid"] or alice[0]["data"]["connection"] == alice[1]["data"]["connection"]:
                raise AssertionError("actual TCP reconnection reused a context")
            proofs = actors["proofs"]
            for proof, join in zip(proofs, joined, strict=True):
                if proof != join["data"]["connection"]:
                    raise AssertionError("WaveChat identity and W4VE source have different real player contexts")
            for chat in chats:
                if chat["data"]["signature_sha256"] != "":
                    raise AssertionError("offline actors unexpectedly claimed signed provenance")
            if hub_mode:
                def ingested():
                    path = root / 'hub-data/wavechat-capture.sqlite3'
                    if not path.exists():
                        return False
                    database = sqlite3.connect(path)
                    try:
                        rows = database.execute('SELECT cursor,gap,imported,skipped FROM capture_cursor').fetchall()
                    finally:
                        database.close()
                    return len(rows) == 1 and rows[0][0] >= events[-1]['cursor'] and rows[0][1:] == (0, 4, 0)
                wait_for(ingested, process)
                database = sqlite3.connect(root / 'hub-data/wavechat-core.sqlite3')
                database.row_factory = sqlite3.Row
                try:
                    messages = [dict(row) for row in database.execute('SELECT * FROM core_messages ORDER BY rowid')]
                    echoes = database.execute('SELECT COUNT(*) FROM core_outbox').fetchone()[0]
                    if relay_mode:
                        relayed = [m for m in messages if m['body'] in ('QA_NATIVE_RELAY', 'QA_NATIVE_FILTERED')]
                        if len(relayed) != 2 or actors.get('nativeRelay') != 'passed' or actors.get('nativeFilterNoReplay') != 'passed':
                            raise AssertionError('real HTTPS to Minecraft relay not observed by both actors')
                        if database.execute('SELECT COUNT(*) FROM core_outbox WHERE delivered=1').fetchone()[0] != 2:
                            raise AssertionError('real relay did not acknowledge its delivery')
                        messages = [m for m in messages if m['id'] not in {r['id'] for r in relayed}]
                finally:
                    database.close()
                if ([message['body'] for message in messages] != expected_chat or echoes != (2 if relay_mode else 0)
                        or any(message['channel'] != 'general' or message['origin'] != paired['instance'] for message in messages)
                        or [message['subject'] for message in messages] != ['minecraft:' + chat['data']['uuid'] for chat in chats]):
                    raise AssertionError('actual Fabric capture did not reach canonical hub without origin echo')
                (root / 'hub-messages.json').write_text(json.dumps(messages, sort_keys=True, indent=2))
                if os.environ.get('QA_REQUIRE_PROVENANCE') == 'true':
                    database = sqlite3.connect(root / 'hub-data/wavechat-core.sqlite3')
                    try:
                        rows = database.execute("SELECT p.payload FROM core_provenance p JOIN core_messages m ON m.id=p.message WHERE m.body NOT IN ('QA_NATIVE_RELAY','QA_NATIVE_FILTERED') ORDER BY m.rowid").fetchall()
                    finally:
                        database.close()
                    provenance = [json.loads(row[0]) for row in rows]
                    expected_provenance = [{'kind': 'game-capture', 'boot': chat['boot'], 'event': chat['id'],
                        'connection': chat['data']['connection'], 'signature_sha256': chat['data']['signature_sha256']} for chat in chats]
                    if provenance != expected_provenance:
                        raise AssertionError('canonical hub lost actual Fabric capture provenance')
                    (root / 'hub-provenance.json').write_text(json.dumps(provenance, sort_keys=True, indent=2))
            process.terminate()
            if process.wait(timeout=60) != 0:
                raise AssertionError("guardian did not shut down cleanly")
            journal = GameEvents(community / "w4ve/game/events.sqlite3", chat=chat_enabled)
            final = journal.poll(chat=chat_enabled)["events"]
            with sqlite3.connect(community / "w4ve/game/events.sqlite3") as database:
                stored_chat = database.execute("SELECT COUNT(*) FROM game_events WHERE kind='player.chat'").fetchone()[0]
            if stored_chat != len(expected_chat):
                raise AssertionError("stored chat did not respect the explicit capture policy")
            if final[-1]["kind"] != "server.stopping" or not (game / "qa-world/level.dat").exists():
                raise AssertionError("game shutdown did not flush lifecycle and save world")
            pending = list((community / "w4ve/game/capture-spool").rglob("*.event.json"))
            if pending:
                raise AssertionError("successful game shutdown left captures unacknowledged")
            result = {"installed_guardian": version, "real_fabric_tcp_joins": len(joined), "public_chats": len(chats),
                "private_and_command_exclusion": "passed", "wavechat_shared_identity": "passed",
                "chat_opt_in": chat_enabled,
                "reconnection": "passed", "world_save_and_source_flush": "passed",
                "respawn_shared_context": "passed",
                "runtime_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "online_auth_gl_hub_relay": "not tested"}
            if hub_mode:
                result['hub_canonical_capture'] = 'passed'
                result['hub_messages'] = len(messages)
                result['origin_echo_suppressed'] = 'passed'
                result['online_auth_gl_relay'] = 'not tested'
                result['respawn_native_discovery'] = 'passed' if actors.get('respawnDiscovery') else 'not required'
                result['canonical_capture_provenance'] = 'passed' if os.environ.get('QA_REQUIRE_PROVENANCE') == 'true' else 'not required'
                result['real_https_to_game_relay'] = 'passed' if relay_mode else 'not tested'
                result['real_origin_filter_no_replay'] = 'passed' if relay_mode else 'not tested'
            (root / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2))
            print(json.dumps(result, sort_keys=True), flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
