"""Explicit, isolated Python preparation. Never called by service startup."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

REQUIREMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~][A-Za-z0-9.*+<>=!,~ -]+)?')


def requirements_valid(values):
    return (isinstance(values, list) and len(values) <= 128 and
            all(isinstance(v, str) and len(v) <= 512 and REQUIREMENT.fullmatch(v) for v in values))


def extras_valid(extras):
    return isinstance(extras, dict) and len(extras) <= 32 and all(
        isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name)
        and isinstance(spec, dict) and not set(spec) - {'when', 'requires'}
        and isinstance(spec.get('when', ''), str) and
        (not spec.get('when') or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', spec['when']))
        and requirements_valid(spec.get('requires', [])) for name, spec in extras.items())


def plan(server, name, *, python=None, include=(), wheelhouse=None, no_index=False):
    import w4ve
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('invalid service name')
    raw = (server.config.get('services') or {}).get(name)
    if not isinstance(raw, dict):
        raise ValueError('existing service required')
    owner = w4ve.owner_of(server, 'services', name)
    service, answers = w4ve.with_answers(server, owner, raw)
    command = service.get('command')
    if not isinstance(command, list) or len(command) < 2:
        raise ValueError('configured Python service command required')
    wanted = raw.get('python_requires') or []
    extras = raw.get('python_extras') or {}
    if not requirements_valid(wanted) or not extras_valid(extras) or any(extra not in extras for extra in include):
        raise ValueError('invalid Python requirements or extra selection')
    wanted = list(wanted)
    settings = server.settings_of(owner) if owner else {}
    selected = sorted(set(include) | {key for key, spec in extras.items() if spec.get('when') and settings.get(spec['when'])})
    for extra in selected:
        wanted.extend(extras[extra].get('requires', []))
    wanted = list(dict.fromkeys(wanted))
    if not wanted:
        raise ValueError('service declares no selected Python requirements')
    base_python = python or command[0]
    if not isinstance(base_python, str) or w4ve.settings_module().unresolved(base_python):
        raise ValueError('choose a configured Python interpreter')
    wheelhouse = Path(wheelhouse).resolve() if wheelhouse else None
    if wheelhouse and not wheelhouse.is_dir():
        raise ValueError('wheelhouse directory unavailable')
    return {'service': name, 'owner': owner, 'python': base_python, 'requirements': wanted,
            'extras': selected, 'wheelhouse': str(wheelhouse) if wheelhouse else '', 'no_index': bool(no_index)}


@contextmanager
def preparation_lock(root, name):
    from pairing import contained
    path = contained(root, 'w4ve/environments/' + name + '/prepare.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if not os.fstat(fd).st_size:
                os.write(fd, b'1')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def prepare(server, name, **options):
    import w4ve
    from pairing import contained
    reviewed = plan(server, name, **options)
    with preparation_lock(server.root, name):
        server = w4ve.Server(server.root)
        selected = plan(server, name, **options)
        if selected != reviewed:
            raise ValueError('service preparation plan changed; review again')
        configuration = (server.root / 'w4ve.toml').read_bytes()
        target = contained(server.root, 'w4ve/environments/' + name + '/' + uuid.uuid4().hex)
        interpreter = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        environment = {k: v for k, v in os.environ.items() if not k.startswith('W4VE_')}
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
            environment.pop(key, None)
        environment.update(PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')

        def run(command, timeout=300):
            result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                raise ValueError('Python preparation failed; previous service interpreter preserved')
            return result

        run([selected['python'], '-m', 'venv', str(target)], timeout=120)
        install = [str(interpreter), '-m', 'pip', 'install', '--disable-pip-version-check']
        if selected['no_index']:
            install.append('--no-index')
        if selected['wheelhouse']:
            install += ['--find-links', selected['wheelhouse']]
        run(install + selected['requirements'])
        run([str(interpreter), '-m', 'pip', 'check'])
        installed = json.loads(run([str(interpreter), '-m', 'pip', 'list', '--format=json']).stdout)
        (target / 'preparation.json').write_text(json.dumps({**selected, 'installed': installed}, sort_keys=True, indent=2) + '\n')
        if (server.root / 'w4ve.toml').read_bytes() != configuration:
            raise ValueError('service configuration changed during preparation; previous interpreter preserved')
        raw = (server.config.get('services') or {})[name]
        reference = re.fullmatch(r'\$\{setting:([a-z][a-z0-9_]*)\}', raw['command'][0])
        if reference and selected['owner']:
            server.set_setting(selected['owner'], reference[1], str(interpreter))
        else:
            fields = {**raw, 'command': [str(interpreter), *raw['command'][1:]]}
            server.set_service(name, fields, owner=selected['owner'])
        return {'interpreter': str(interpreter), 'requirements': selected['requirements'], 'extras': selected['extras']}
