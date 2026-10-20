"""Prepare the local Fabric source from reviewed/bundled artifacts; no downloads."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shlex
import subprocess
import tempfile
import zipfile


def artifact(path, expected_id, minecraft):
    import w4ve
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 48 * 1024 * 1024:
        raise ValueError('regular bounded Fabric artifact required')
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo('fabric.mod.json')
        if info.file_size > 65536:
            raise ValueError('invalid Fabric metadata')
        metadata = json.loads(archive.read(info))
    if not isinstance(metadata, dict) or not isinstance(metadata.get('depends', {}), dict):
        raise ValueError('invalid Fabric metadata')
    version = metadata.get('version')
    if (metadata.get('id') != expected_id or not isinstance(version, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}', version)
            or metadata.get('environment') not in ('server', '*')
            or w4ve.mod_declares_mc((metadata.get('depends') or {}).get('minecraft'), minecraft) is not True):
        raise ValueError('Fabric artifact is not compatible with this server')
    return {'id': expected_id, 'version': version, 'source': str(path.resolve()),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'java_range': metadata['depends'].get('java', '*'),
            'loader_range': metadata['depends'].get('fabricloader', '*')}


def loader_version(game, start):
    from pairing import contained
    candidates = [game / 'fabric-server-launch.jar', game / 'server.jar']
    if '-jar' in start and start.index('-jar') + 1 < len(start):
        candidate = game / start[start.index('-jar') + 1]
        if candidate not in candidates:
            candidates.insert(0, candidate)
    for launcher in candidates:
        if not launcher.is_file() or launcher.is_symlink():
            continue
        try:
            with zipfile.ZipFile(launcher) as jar:
                info = jar.getinfo('META-INF/MANIFEST.MF')
                if info.file_size > 65536:
                    continue
                manifest = jar.read(info).decode('utf-8').replace('\r\n ', '').replace('\n ', '')
            matches = re.findall(r'(libraries/net/fabricmc/fabric-loader/([0-9]+\.[0-9]+\.[0-9]+[^/\s]*)/[^\s]+\.jar)', manifest)
            if len(matches) != 1:
                continue
            library = contained(game, matches[0][0])
            with zipfile.ZipFile(library) as jar:
                jar.getinfo('net/fabricmc/loader/impl/launch/server/FabricServerLauncher.class')
            return matches[0][1]
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
    raise ValueError('cannot verify the Fabric loader from the configured server launcher/libraries')


def plan(server, *, bridge=None, fabric_api=None, chat=None, retention=None, java=None):
    import w4ve
    from pairing import contained
    if not isinstance(server.config.get('server') or {}, dict) or not isinstance(server.config.get('bridge') or {}, dict):
        raise ValueError('invalid server/source policy')
    if not server.mc or server.kind != 'server' or server.config_path.is_symlink():
        raise ValueError('configured server and Minecraft version required')
    game = (server.root / server.game_dir).resolve()
    if (server.config.get('server') or {}).get('flavor') != 'fabric' and not (game / 'fabric-server-launch.jar').is_file():
        raise ValueError('prepare an existing Fabric server first')
    start = (server.config.get('server') or {}).get('start') or []
    start = shlex.split(start) if isinstance(start, str) else start
    program = java or (start[0] if start and Path(start[0]).name.lower() in ('java', 'java.exe') else 'java')
    version = subprocess.run([program, '-version'], capture_output=True, text=True, timeout=5)
    major = re.search(r'(?:version\s+"|openjdk\s+|java\s+)([0-9]+)', version.stderr + version.stdout)
    if version.returncode or not major or int(major[1]) < 21:
        raise ValueError('the Fabric source requires a working Java21 or later')
    library = Path(__file__).resolve().parent / 'lib'
    artifacts = [artifact(bridge or library / 'w4ve-fabric-source.jar', 'w4ve-bridge', server.mc),
                 artifact(fabric_api or library / 'fabric-api.jar', 'fabric-api', server.mc)]
    loader = loader_version(game, start)
    for item in artifacts:
        if (w4ve.mod_declares_mc(item['java_range'], str(int(major[1]))) is not True
                or w4ve.mod_declares_mc(item['loader_range'], loader) is not True):
            raise ValueError('Java or Fabric loader is outside the selected artifact compatibility range')
    mods = contained(game, 'mods')
    for item in artifacts:
        existing = []
        if mods.exists():
            for path in mods.glob('*.jar'):
                metadata = w4ve.read_file_meta(path) or {}
                if metadata.get('id') == item['id']:
                    existing.append(path)
        if len(existing) > 1:
            raise ValueError('duplicate Fabric dependency; preserve and review the installed jars')
        if existing:
            current = existing[0]
            if current.is_symlink() or hashlib.sha256(current.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('existing Fabric dependency differs; review its managed upgrade first')
            destination = current
        else:
            destination = contained(game, 'mods/' + item['id'] + '-' + item['version'] + '.jar')
            if destination.exists() or destination.is_symlink():
                raise ValueError('target mod path already exists')
        item.update(destination=str(destination), reuse=bool(existing))
    current = server.config.get('bridge') or {}
    selected_chat = current.get('chat', False) if chat is None else chat
    selected_retention = current.get('retention', 300) if retention is None else retention
    if type(selected_chat) is not bool or type(selected_retention) is not int or not 5 <= selected_retention <= 3600:
        raise ValueError('invalid source chat or retention policy')
    record = contained(server.root, 'w4ve/source-preparation.json')
    if record.exists():
        if record.stat().st_size > 65536:
            raise ValueError('source preparation record is invalid')
        previous = json.loads(record.read_text())
        if not isinstance(previous, dict) or previous.get('owner') != 'w4ve-game-source':
            raise ValueError('source preparation record belongs to another setup')
    return {'game': str(game), 'java': str(program), 'java_major': int(major[1]), 'loader': loader, 'chat': selected_chat,
            'retention': selected_retention, 'artifacts': artifacts}


def atomic_write(path, content, mode=0o600, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.source-prepare-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if replace and path.exists() and hasattr(os, 'chown'):
            previous = path.stat()
            os.chown(temporary, previous.st_uid, previous.st_gid)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_source(server, chat, retention):
    before = server.config_path.read_bytes()
    lines = before.decode('utf-8').splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r'\s*\[bridge\]\s*(?:#.*)?', line)), None)
    if start is None:
        lines += ['', '[bridge]']; start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    for key, value in {'enabled': True, 'chat': chat, 'retention': retention}.items():
        replacement = key + ' = ' + json.dumps(value)
        index = next((i for i in range(start + 1, end) if re.match(r'\s*' + key + r'\s*=', lines[i])), None)
        if index is None:
            lines.insert(end, replacement); end += 1
        else:
            lines[index] = replacement
    content = ('\n'.join(lines) + '\n').encode()
    if server.config_path.read_bytes() != before:
        raise ValueError('configuration changed; review again')
    atomic_write(server.config_path, content, stat.S_IMODE(server.config_path.stat().st_mode))


def prepare(server, **options):
    import w4ve
    from pairing import contained
    from serviceprep import preparation_lock
    reviewed = plan(server, **options)
    with preparation_lock(server.root, 'game-source'):
        server = w4ve.Server(server.root)
        selected = plan(server, **options)
        if selected != reviewed:
            raise ValueError('source plan changed; review again')
        configuration = server.config_path.read_bytes()
        for item in selected['artifacts']:
            if item['reuse']:
                continue
            data = Path(item['source']).read_bytes()
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('source artifact changed; preserve the old setup')
            destination = Path(item['destination'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Refuse an actor creating the destination after our review.
            if destination.exists() or destination.is_symlink():
                raise ValueError('source destination changed; review again')
            atomic_write(destination, data, 0o644, replace=False)
        record = contained(server.root, 'w4ve/source-preparation.json')
        atomic_write(record, (json.dumps({'owner': 'w4ve-game-source', **selected}, sort_keys=True, indent=2) + '\n').encode())
        if server.config_path.read_bytes() != configuration:
            raise ValueError('configuration changed during preparation; source policy preserved')
        configure_source(server, selected['chat'], selected['retention'])
        return selected


def check(server):
    """Static dependency preflight, not a claim that the source process is live."""
    import w4ve
    game = (server.root / server.game_dir).resolve()
    found = {}
    for path in (game / 'mods').glob('*.jar'):
        identity = (w4ve.read_file_meta(path) or {}).get('id')
        if identity in ('w4ve-bridge', 'fabric-api'):
            if identity in found:
                raise ValueError('duplicate source dependency')
            found[identity] = path
    if set(found) != {'w4ve-bridge', 'fabric-api'}:
        raise ValueError('source dependencies missing')
    try:
        return plan(server, bridge=found['w4ve-bridge'], fabric_api=found['fabric-api'])
    except zipfile.BadZipFile:
        raise ValueError('source artifact became unreadable') from None
