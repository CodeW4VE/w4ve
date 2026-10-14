"""Declarative local enrollment for pieces; operator setup, never a player API."""
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlsplit
from identity import Authority, key


def declaration(value):
    if not isinstance(value, dict) or set(value) != {
            "audience", "enabled_setting", "url_setting", "consumer_config", "issuer_config", "resources"}:
        raise ValueError("invalid pairing declaration")
    for name in ("audience", "enabled_setting", "url_setting"):
        key(value[name])
    for name in ("consumer_config", "issuer_config"):
        relative = value[name]
        if not isinstance(relative, str) or "\\" in relative or ":" in relative:
            raise ValueError("invalid pairing path")
        path = PurePosixPath(relative)
        if path.is_absolute() or path.as_posix() != relative or any(part in (".", "..") for part in path.parts):
            raise ValueError("pairing path escapes its root")
    if not value["consumer_config"].startswith("w4ve/") or not value["issuer_config"].startswith("config/"):
        raise ValueError("pairing files must use runtime and game config directories")
    return {**value, "resources": Authority.normalized_resources(value["resources"])}


def contained(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("pairing path escapes its root")
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("pairing path may not contain symlinks")
        current = current.parent
    return path


def write_owned(path, payload):
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ValueError("existing pairing file is unreadable; preserved") from None
        if (not isinstance(previous, dict) or any(previous.get(field) != payload[field]
                                                 for field in ("schema", "owner", "community", "instance"))):
            raise ValueError("existing pairing file belongs to another setup; preserved")
        payload = {**previous, **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pairing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(server, owner, spec, settings):
    spec = declaration(spec)
    if settings.get(spec["enabled_setting"]) is not True:
        return {"enabled": False}
    public_url = settings.get(spec["url_setting"], "").rstrip("/")
    url = urlsplit(public_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment):
        raise ValueError("pairing requires a public HTTPS address without credentials/query/fragment")
    consumer = contained(server.root, spec["consumer_config"])
    game_root = Path(server.root) / server.game_dir
    issuer = contained(game_root, spec["issuer_config"])
    authority = Authority(Path(server.root) / "w4ve/identity/authority.sqlite3")
    binding = authority.provision_binding(owner, spec["audience"], spec["resources"])
    common = {"schema": 1, "owner": owner, "community": binding["community"],
              "instance": binding["instance"], "audience": spec["audience"], "public_url": public_url}
    write_owned(issuer, {**common, "secret": binding["secret"]})
    write_owned(consumer, {**common, "resources": binding["resources"]})
    return {"enabled": True, "community": binding["community"], "instance": binding["instance"],
            "resources": binding["resources"], "consumer_config": str(consumer), "issuer_config": str(issuer)}
