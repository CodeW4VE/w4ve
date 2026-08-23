"""What a plugin needs from pip, read from the plugin, checked, never installed.

Same rule the `.w4ve` package format already follows: dependencies are
**declared and checked, not installed**. W4VE does not run pip at somebody, it
tells them what is missing and lets them decide.

This exists because of what the acceptance rehearsal looks like without it.
Four of the twelve plugins MineWave runs did not load, and the reason was
never a compatibility hole: ChatBridge wants `colorlog`, PrimeBackup wants
`apscheduler`, Task wants `parse`. Finding that out one crash at a time, on
the day of the cutover, is the bad way to find it out. Reading it off the
plugin beforehand is the good one.

⚠️ Checked as **distributions**, not as importable modules. The pip name and
the import name disagree often enough to matter: `discord.py` imports as
`discord`, `pycryptodome` imports as `Crypto`, `websocket-client` imports as
`websocket`. Asking whether the distribution is installed is the question that
has an answer.
"""

import re
import zipfile
from pathlib import Path

try:
    from importlib import metadata as _metadata
except ImportError:                                  # pragma: no cover
    _metadata = None

# `name>=1.2`, `name==1.0`, `name[extra]~=2`, and a bare `name`.
NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$")

# Plugins declare MCDR itself. Not something to go and install: the whole
# point of this exercise is that it is not there.
IGNORED = {"mcdreforged"}


def parse_requirements(text):
    """`[(name, specifier)]` out of a requirements.txt, comments dropped."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            # `-r other.txt` and friends: a pointer, not a requirement.
            continue
        match = NAME.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.lower() in IGNORED:
            continue
        out.append((name, (match.group(3) or "").strip()))
    return out


# The interpreter these questions are asked of. None means "this one", which
# is right when w4ve is being run by the same Python the server will use, and
# wrong the rest of the time: a server with its own virtualenv is the normal
# case, not the exception, and asking the wrong interpreter reports packages as
# missing that are sitting right there.
_INTERPRETER = None

PROBE = """
import importlib.metadata as meta, sys
for name in sys.argv[1:]:
    try:
        meta.distribution(name)
        sys.exit(0)
    except Exception:
        pass
sys.exit(1)
"""


def use_interpreter(path):
    """Ask a different Python from now on. `None` goes back to this one."""
    global _INTERPRETER
    _INTERPRETER = str(path) if path else None


def interpreter():
    return _INTERPRETER


def installed(name):
    """Is this distribution installed? None when we cannot tell."""
    if _INTERPRETER:
        import subprocess

        try:
            probe = subprocess.run(
                [_INTERPRETER, "-c", PROBE, name, name.replace("_", "-")],
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return probe.returncode == 0
    if _metadata is None:
        return None
    try:
        _metadata.distribution(name)
        return True
    except Exception:
        # Some distributions only answer to the normalised name.
        try:
            _metadata.distribution(name.replace("_", "-"))
            return True
        except Exception:
            return False


def of(plugin):
    """What one plugin declares, split into what it needs and what it may need.

    A `requirements.txt` at the root is what the plugin needs to run at all.
    One further in is a variant's: ChatBridge ships five, one per front end
    (Discord, CQHTTP, Kaiheila…), and nobody installs all five. Reporting
    those as missing would bury the one line that matters under four that do
    not.
    """
    required, optional = [], {}
    for name, text in _requirement_files(plugin):
        entries = parse_requirements(text)
        if not entries:
            continue
        if name == "requirements.txt":
            required.extend(entries)
        else:
            optional[name] = entries
    return {"required": required, "optional": optional}


def missing(plugin):
    """Just the required ones that are not installed, as names."""
    return [name for name, _ in of(plugin)["required"]
            if installed(name) is False]


def _requirement_files(plugin):
    path = Path(plugin.path)
    shape = getattr(plugin, "shape", None)
    if shape == "folder":
        for child in sorted(path.rglob("requirements.txt")):
            try:
                yield (str(child.relative_to(path)).replace("\\", "/"),
                       child.read_text("utf-8"))
            except OSError:
                continue
        return
    if shape != "archive":
        return
    try:
        archive = zipfile.ZipFile(str(path))
    except (OSError, zipfile.BadZipFile):
        return
    with archive:
        for name in sorted(archive.namelist()):
            if not name.endswith("requirements.txt"):
                continue
            try:
                yield name, archive.read(name).decode("utf-8", "replace")
            except KeyError:
                continue


def install_line(names):
    """The command to fix it, ready to copy."""
    return "pip install " + " ".join(sorted(set(names)))
