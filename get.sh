#!/bin/sh
# Install W4VE in one line:
#
#   curl -fsSL https://github.com/CodeW4VE/w4ve/releases/latest/download/get.sh | sh
#
# It downloads the newest release (or W4VE_VERSION=0.5.0 for a given one),
# checks its SHA256, and hands it to install.py, which checks every file in
# it again. It installs for this user only, needs no root, and touches
# nothing outside ~/.local unless W4VE_PREFIX / W4VE_BIN say otherwise.
# Your shell profile is left alone: if ~/.local/bin is not on PATH, it says
# the line to add instead of adding it.
set -eu

if ! command -v python3 >/dev/null 2>&1; then
    echo "W4VE needs Python 3.11 or newer, and there is no python3 here." >&2
    echo "Debian/Ubuntu: sudo apt install python3   Fedora: sudo dnf install python3" >&2
    exit 1
fi

exec python3 - "$@" <<'PYTHON'
import hashlib, json, os, subprocess, sys, tempfile, urllib.request
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("W4VE needs Python 3.11 or newer, this is %d.%d." % sys.version_info[:2])
if sys.platform.startswith("win"):
    sys.exit("W4VE runs on Linux. On Windows, use a Linux VM or container.")

REPO = "https://api.github.com/repos/CodeW4VE/w4ve/releases"
wanted = os.environ.get("W4VE_VERSION", "").lstrip("v")
prefix = Path(os.environ.get("W4VE_PREFIX") or Path.home() / ".local/share/w4ve")
bin_dir = Path(os.environ.get("W4VE_BIN") or Path.home() / ".local/bin")

def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "w4ve-get"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()

try:
    release = json.loads(get(REPO + ("/tags/v" + wanted if wanted else "/latest")))
except Exception as err:
    sys.exit("Cannot ask GitHub for the W4VE release: %s" % err)
version = release["tag_name"].lstrip("v")
assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
archive = "w4ve-%s.tar.gz" % version
for name in (archive, archive + ".sha256", "install.py"):
    if name not in assets:
        sys.exit("The W4VE %s release has no %s, so it cannot be installed this way." % (version, name))

print("W4VE %s" % version)
with tempfile.TemporaryDirectory(prefix="w4ve-get-") as work:
    work = Path(work)
    for name in (archive, archive + ".sha256", "install.py"):
        (work / name).write_bytes(get(assets[name]))
    expected = (work / (archive + ".sha256")).read_text().split()[0]
    if hashlib.sha256((work / archive).read_bytes()).hexdigest() != expected:
        sys.exit("%s does not match its SHA256. Nothing was installed." % archive)
    print("  checksum ok")
    result = subprocess.run([sys.executable, str(work / "install.py"), "--archive",
                             str(work / archive), "--prefix", str(prefix),
                             "--bin-dir", str(bin_dir)])
    if result.returncode:
        sys.exit(result.returncode)
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "install.py").write_bytes((work / "install.py").read_bytes())

on_path = str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep)
print()
if not on_path:
    print("Add this line to your ~/.profile (or ~/.bashrc), then open a new terminal:")
    print('  export PATH="%s:$PATH"' % bin_dir)
    print()
print("Then, in your server folder:")
print("  w4ve init     look at it and set it up (or make a new server)")
print("  w4ve run      start it and look after it")
PYTHON
