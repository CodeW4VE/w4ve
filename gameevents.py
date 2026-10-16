"""Bounded typed game journal. Internal platform primitive, not a player session.

The guardian owns source creation/retirement. A source capability authorizes
only historical event ingestion for its boot; it never establishes current
presence or grants access to a consumer's channels. HTTP/companion wiring must
authenticate readers separately and reconcile process identity before exposing
live rosters. No stdout parsing and no WaveChat-specific channel policy here.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid


KINDS = frozenset(("server.started", "server.stopping", "player.joined", "player.left", "player.chat"))


class SourceDenied(Exception):
    pass


class ChatDisabled(SourceDenied):
    pass


class EventConflict(Exception):
    pass


class Backpressure(Exception):
    pass


@dataclass(frozen=True)
class Source:
    boot: str
    token: str = field(repr=False)


def hex_id(value, length=32):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{%d}" % length, value):
        raise ValueError("invalid event identifier")
    return value


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2 ** 53 - 1:
        raise ValueError("invalid event integer")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class GameEvents:
    def __init__(self, path, *, chat=False, retention=300, max_events=4096,
                 max_bytes=8 * 1024 * 1024, clock=time.time):
        if type(chat) is not bool or type(retention) is not int or not 5 <= retention <= 3600:
            raise ValueError("invalid game journal policy")
        if type(max_events) is not int or not 1 <= max_events <= 65536:
            raise ValueError("invalid journal event quota")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("invalid journal byte quota")
        self.path, self.chat, self.retention = Path(path), chat, retention
        self.max_events, self.max_bytes, self.clock = max_events, max_bytes, clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("game journal may not be a symlink")
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS game_sources (
                    boot TEXT PRIMARY KEY,digest TEXT NOT NULL UNIQUE,label TEXT NOT NULL,
                    created INTEGER NOT NULL,retired INTEGER,last_sequence INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'new',last_occurred INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS game_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,boot TEXT NOT NULL,
                    sequence INTEGER NOT NULL,event_id TEXT NOT NULL,digest TEXT NOT NULL,
                    kind TEXT NOT NULL,payload TEXT NOT NULL,bytes INTEGER NOT NULL,received INTEGER NOT NULL,
                    UNIQUE(boot,sequence),UNIQUE(boot,event_id),
                    FOREIGN KEY(boot) REFERENCES game_sources(boot));
                CREATE TABLE IF NOT EXISTS game_players (
                    boot TEXT NOT NULL,uuid TEXT NOT NULL,connection TEXT NOT NULL,name TEXT NOT NULL,
                    PRIMARY KEY(boot,uuid),FOREIGN KEY(boot) REFERENCES game_sources(boot));
            """)
            db.execute("BEGIN IMMEDIATE")
            if "bindings" not in {row[1] for row in db.execute("PRAGMA table_info(game_sources)")}:
                # Existing boots remain explicitly unattributed. Never backfill
                # their origin from a pairing created after capture.
                db.execute("ALTER TABLE game_sources ADD COLUMN bindings TEXT NOT NULL DEFAULT '{}'")
            for key, value in (("schema", "1"), ("epoch", uuid.uuid4().hex), ("floor", "0"), ("received", "0")):
                db.execute("INSERT OR IGNORE INTO game_meta VALUES (?,?)", (key, value))
            if self._meta(db, "schema") != "1":
                raise ValueError("unsupported game journal schema")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA wal_autocheckpoint=128")
        db.execute("PRAGMA max_page_count=32768")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _meta(db, key):
        return db.execute("SELECT value FROM game_meta WHERE key=?", (key,)).fetchone()[0]

    def _prune(self, db, now):
        # Received time never goes backwards. Expiry therefore removes a
        # contiguous prefix, so reset cursors cannot skip newer retained events.
        expired = db.execute("SELECT MAX(cursor) FROM game_events WHERE received<=?", (now - self.retention,)).fetchone()[0]
        if expired is not None:
            db.execute("DELETE FROM game_events WHERE cursor<=?", (expired,))
            db.execute("UPDATE game_meta SET value=? WHERE key='floor'", (str(expired),))
        old = [row[0] for row in db.execute("SELECT boot FROM game_sources WHERE retired IS NOT NULL AND retired<=? "
                 "AND NOT EXISTS (SELECT 1 FROM game_events e WHERE e.boot=game_sources.boot)", (now - self.retention,))]
        for boot in old:
            db.execute("DELETE FROM game_players WHERE boot=?", (boot,))
            db.execute("DELETE FROM game_sources WHERE boot=?", (boot,))

    def open_source(self, label="server", *, bindings=None):
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", label):
            raise ValueError("invalid game source label")
        bindings = self._bindings({} if bindings is None else bindings)
        source = Source(uuid.uuid4().hex, secrets.token_urlsafe(32))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db, int(self.clock()))
            if db.execute("SELECT COUNT(*) FROM game_sources").fetchone()[0] >= 64:
                raise Backpressure("game source quota reached")
            db.execute("INSERT INTO game_sources(boot,digest,label,created,bindings) VALUES (?,?,?,?,?)",
                       (source.boot, hashlib.sha256(source.token.encode()).hexdigest(), label, int(self.clock()), encode(bindings)))
        return source

    @staticmethod
    def _bindings(value):
        if value == {}:
            return {}
        from identity import key
        if (not isinstance(value, dict) or set(value) != {"epoch", "owners"}
                or not isinstance(value["owners"], dict) or len(value["owners"]) > 64):
            raise ValueError("invalid source identity bindings")
        hex_id(value["epoch"])
        for owner, binding in value["owners"].items():
            key(owner)
            if not isinstance(binding, dict) or set(binding) != {"community", "instance", "audience"}:
                raise ValueError("invalid source identity binding")
            for item in binding.values():
                key(item)
        return value

    def captured_chat(self, epoch, boot, event_id):
        """Internal immutable capture lookup; not a player or presence proof.

        Callers must separately check reader grants and current channel policy.
        Only references are accepted; no caller-supplied UUID/body/connection.
        """
        hex_id(epoch); hex_id(boot); hex_id(event_id)
        if not self.chat:
            raise ChatDisabled("game chat journal is disabled")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db, int(self.clock()))
            if self._meta(db, "epoch") != epoch:
                raise SourceDenied("capture unavailable")
            row = db.execute("""SELECT e.payload,s.bindings FROM game_events e
                JOIN game_sources s ON s.boot=e.boot
                WHERE e.boot=? AND e.event_id=? AND e.kind='player.chat'""", (boot, event_id)).fetchone()
            if row is None:
                raise SourceDenied("capture unavailable")
            return {"event": json.loads(row["payload"]), "bindings": self._bindings(json.loads(row["bindings"]))}

    @staticmethod
    def _source(db, source):
        if (not isinstance(source, Source) or not isinstance(source.boot, str)
                or not re.fullmatch(r"[0-9a-f]{32}", source.boot)
                or not isinstance(source.token, str) or not 16 <= len(source.token) <= 128):
            raise SourceDenied("game source unavailable")
        row = db.execute("SELECT * FROM game_sources WHERE boot=? AND digest=?",
                         (source.boot, hashlib.sha256(source.token.encode()).hexdigest())).fetchone()
        if row is None:
            raise SourceDenied("game source unavailable")
        return row

    def retire_source(self, source, *, captured_until=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._source(db, source)
            now = int(self.clock())
            cutoff = now if captured_until is None else integer(captured_until)
            cutoff = max(row["created"], min(now, cutoff))
            db.execute("UPDATE game_sources SET retired=COALESCE(retired,?) WHERE boot=?", (cutoff, source.boot))

    def authenticate(self, token):
        """Find the boot before reading an untrusted request body."""
        if not isinstance(token, str) or not 16 <= len(token) <= 128:
            raise SourceDenied("game source unavailable")
        with self._db() as db:
            row = db.execute("SELECT boot,retired FROM game_sources WHERE digest=?",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None or (row["retired"] is not None and row["retired"] <= int(self.clock()) - self.retention):
                raise SourceDenied("game source unavailable")
            return Source(row["boot"], token)

    def source_state(self, source):
        with self._db() as db:
            row = self._source(db, source)
            return {"boot": row["boot"], "sequence": row["last_sequence"], "retired": row["retired"] is not None,
                    "chat": self.chat, "retention": self.retention}

    def _event(self, event):
        if (not isinstance(event, dict) or set(event) != {"schema", "boot", "id", "sequence", "kind", "occurred", "data"}
                or type(event["schema"]) is not int or event["schema"] != 1):
            raise ValueError("invalid game event fields")
        hex_id(event["boot"]); hex_id(event["id"])
        integer(event["sequence"], 1); integer(event["occurred"])
        kind, data = event["kind"], event["data"]
        if not isinstance(kind, str) or kind not in KINDS or not isinstance(data, dict):
            raise ValueError("invalid game event kind")
        fields = {"server.started": set(), "server.stopping": set(),
                  "player.joined": {"uuid", "name", "connection"}, "player.left": {"uuid", "connection"},
                  "player.chat": {"uuid", "connection", "text", "signature_sha256"}}[kind]
        if set(data) != fields:
            raise ValueError("invalid game event data")
        if "uuid" in data:
            hex_id(data["uuid"]); hex_id(data["connection"])
        if "name" in data and (not isinstance(data["name"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", data["name"])):
            raise ValueError("invalid game account presentation")
        if kind == "player.chat":
            if not self.chat:
                raise ChatDisabled("game chat journal is disabled")
            if (not isinstance(data["text"], str) or not 1 <= len(data["text"]) <= 256
                    or any(ord(character) < 32 or ord(character) == 127 for character in data["text"])):
                raise ValueError("invalid game chat content")
            if data["signature_sha256"] != "":
                hex_id(data["signature_sha256"], 64)
        raw = encode(event).encode("utf-8")
        if len(raw) > 16384:
            raise ValueError("game event too large")
        return raw

    def append(self, source, event):
        raw = self._event(event)
        if not isinstance(source, Source) or event["boot"] != source.boot:
            raise SourceDenied("game source unavailable")
        digest, now = hashlib.sha256(raw).hexdigest(), int(self.clock())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._source(db, source)
            previous = db.execute("SELECT * FROM game_events WHERE boot=? AND (sequence=? OR event_id=?)",
                                  (source.boot, event["sequence"], event["id"])).fetchall()
            if previous:
                if len(previous) != 1 or previous[0]["digest"] != digest:
                    raise EventConflict("event identity already has different content")
                return {"cursor": previous[0]["cursor"], "sequence": event["sequence"], "duplicate": True}
            if event["sequence"] != row["last_sequence"] + 1:
                raise EventConflict("game event sequence requires reconciliation")
            if (event["occurred"] < row["created"] - 30 or event["occurred"] <= now - self.retention
                    or event["occurred"] > now + 30 or event["occurred"] < row["last_occurred"]
                    or (row["retired"] is not None and event["occurred"] > row["retired"])):
                raise EventConflict("game event is outside its capture window")
            kind, data = event["kind"], dict(event["data"])
            if (row["phase"] == "new") != (kind == "server.started"):
                raise EventConflict("game source lifecycle mismatch")
            if row["phase"] == "stopping" and kind not in ("player.left", "server.stopping"):
                raise EventConflict("game source is stopping")
            if "uuid" in data:
                player = db.execute("SELECT * FROM game_players WHERE boot=? AND uuid=?", (source.boot, data["uuid"])).fetchone()
                if kind == "player.joined":
                    if player is None and db.execute("SELECT COUNT(*) FROM game_players WHERE boot=?", (source.boot,)).fetchone()[0] >= 1000:
                        raise Backpressure("game roster quota reached")
                    db.execute("INSERT INTO game_players VALUES (?,?,?,?) ON CONFLICT(boot,uuid) DO UPDATE "
                               "SET connection=excluded.connection,name=excluded.name",
                               (source.boot, data["uuid"], data["connection"], data["name"]))
                elif kind == "player.left":
                    db.execute("DELETE FROM game_players WHERE boot=? AND uuid=? AND connection=?",
                               (source.boot, data["uuid"], data["connection"]))
                else:
                    if player is None or player["connection"] != data["connection"]:
                        raise EventConflict("game chat does not match its captured connection")
                    data["name"] = player["name"]
            if kind == "server.started":
                db.execute("UPDATE game_sources SET phase='running' WHERE boot=?", (source.boot,))
            elif kind == "server.stopping":
                db.execute("UPDATE game_sources SET phase='stopping' WHERE boot=?", (source.boot,))
            payload = encode({**event, "data": data})
            size = len(payload.encode("utf-8"))
            self._prune(db, now)
            count, used = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM game_events").fetchone()
            if count >= self.max_events or used + size > self.max_bytes:
                raise Backpressure("game journal is full; retry without dropping the event")
            received = max(now, int(self._meta(db, "received")))
            db.execute("UPDATE game_meta SET value=? WHERE key='received'", (str(received),))
            cursor = db.execute("INSERT INTO game_events(boot,sequence,event_id,digest,kind,payload,bytes,received) "
                                "VALUES (?,?,?,?,?,?,?,?)", (source.boot, event["sequence"], event["id"], digest,
                                kind, payload, size, received)).lastrowid
            db.execute("UPDATE game_sources SET last_sequence=?,last_occurred=? WHERE boot=?",
                       (event["sequence"], event["occurred"], source.boot))
            return {"cursor": cursor, "sequence": event["sequence"], "duplicate": False}

    def poll(self, cursor=0, *, epoch="", limit=100, chat=False):
        integer(cursor)
        if type(limit) is not int or not 1 <= limit <= 100 or type(chat) is not bool:
            raise ValueError("invalid game journal query")
        if chat and not self.chat:
            raise ChatDisabled("game chat journal is disabled")
        if not isinstance(epoch, str) or (epoch and not re.fullmatch(r"[0-9a-f]{32}", epoch)):
            raise ValueError("invalid game journal epoch")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db, int(self.clock()))
            current, floor = self._meta(db, "epoch"), int(self._meta(db, "floor"))
            tail = max(floor, db.execute("SELECT COALESCE(MAX(cursor),0) FROM game_events").fetchone()[0])
            reset = epoch != current or cursor < floor
            if reset:
                cursor = floor
            elif cursor > tail:
                raise ValueError("game journal cursor is ahead of the journal")
            rows = db.execute("SELECT * FROM game_events WHERE cursor>? ORDER BY cursor LIMIT ?", (cursor, limit)).fetchall()
            events = [{"cursor": row["cursor"], **json.loads(row["payload"])} for row in rows
                      if chat or row["kind"] != "player.chat"]
            return {"epoch": current, "reset": reset, "floor": floor,
                    "cursor": rows[-1]["cursor"] if rows else cursor, "tail": tail,
                    "events": events, "more": bool(rows and rows[-1]["cursor"] < tail)}

    def roster(self, source):
        with self._db() as db:
            row = self._source(db, source)
            # Retirement is authoritative even when old queued captures arrive.
            players = [] if row["retired"] is not None else [dict(player) for player in db.execute(
                "SELECT uuid,name,connection FROM game_players WHERE boot=? ORDER BY uuid", (source.boot,))]
            return {"boot": source.boot, "sequence": row["last_sequence"], "retired": row["retired"] is not None, "players": players}


def source_locked(action):
    @wraps(action)
    def locked(self, *args, **kwargs):
        with self._locked():
            return action(self, *args, **kwargs)
    return locked


class SourceRuntime:
    """Own the private boot descriptor; preserve it across guardian handover."""
    def __init__(self, root, policy, alive):
        if (not isinstance(policy, dict) or policy.get("enabled") is not True
                or not policy.keys() <= {"enabled", "chat", "retention", "max_events", "max_bytes"}):
            raise ValueError("invalid game source policy")
        self.root, self.alive = Path(root).resolve(), alive
        self.path = self.root / "w4ve/run/game-source.json"
        self.lock = threading.RLock()
        self.journal = GameEvents(self.root / "w4ve/game/events.sqlite3", **{
            key: value for key, value in policy.items() if key != "enabled"})
        self.spool = self.root / "w4ve/game/capture-spool"
        if self.spool.is_symlink():
            raise ValueError("capture spool may not be a symlink")
        self.spool.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.spool, 0o700)

    @contextmanager
    def _locked(self):
        # Both HTTP and guardian threads touch this file, and a handover may
        # briefly leave two guardian processes observing the same game boot.
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("invalid private game source descriptor")
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_size > 8192:
            raise ValueError("invalid private game source descriptor")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("owner") != "w4ve-game-source"
                or not isinstance(value.get("token"), str) or not 16 <= len(value["token"]) <= 128):
            raise ValueError("invalid private game source descriptor")
        hex_id(value.get("boot"))
        integer(value.get("created")); integer(value.get("last_alive"))
        if value.get("pid") is not None:
            integer(value["pid"], 1); integer(value.get("starttime"), 1)
        return value

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".game-source-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encode(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @source_locked
    def reconcile(self):
        self._reconcile()

    def _reconcile(self):
        value = self._read()
        if not value:
            return
        now = int(self.journal.clock())
        if value.get("pid") is not None and self.alive(value["pid"], value["starttime"]):
            if value["last_alive"] < now:
                self._write({**value, "last_alive": now})
        elif value.get("pid") is not None or now - value["created"] > 10:
            try:
                # Recovery after a long guardian outage must not grant an old
                # key a fresh capture window merely because we noticed it now.
                self.journal.retire_source(Source(value["boot"], value["token"]),
                                           captured_until=value["last_alive"] + (30 if value.get("pid") else 0))
            except SourceDenied:
                pass  # Expired, already pruned history is not a live source.

    @staticmethod
    def _endpoint(api):
        address = api.address or ""
        if address:
            # A wildcard listener is reached through loopback by its local
            # companion. The producer ingress separately rejects remote peers.
            host = api.httpd.server_address[0]
            if host not in ("127.0.0.1", "0.0.0.0"):
                raise ValueError("game source requires Unix or loopback API access")
            address = "127.0.0.1:%d" % api.httpd.server_address[1]
        return {"api_socket": str(api.socket_path), "api_address": address}

    @source_locked
    def prepare(self, api):
        previous = self._read()
        if previous:
            if previous.get("pid") is not None and self.alive(previous["pid"], previous["starttime"]):
                raise EventConflict("a live game source already owns this descriptor")
            try:
                self.journal.retire_source(Source(previous["boot"], previous["token"]),
                                           captured_until=previous["last_alive"] + 30)
            except SourceDenied:
                pass
        endpoint = self._endpoint(api)
        authority_path = self.root / "w4ve/identity/authority.sqlite3"
        bindings = {}
        if authority_path.exists() or authority_path.is_symlink():
            from identity import Authority
            bindings = Authority(authority_path).capture_bindings()
        source = self.journal.open_source(bindings=bindings)
        try:
            self._write({"schema": 1, "owner": "w4ve-game-source", "boot": source.boot, "token": source.token,
                         "pid": None, "starttime": None, "chat": self.journal.chat,
                         "created": int(self.journal.clock()), "last_alive": int(self.journal.clock()),
                         "spool": str(self.spool), "retention": self.journal.retention, **endpoint})
        except Exception:
            self.journal.retire_source(source)
            raise
        return source

    @source_locked
    def bind(self, source, pid, starttime):
        integer(pid, 1); integer(starttime, 1)
        value = self._read()
        if not value or value["boot"] != source.boot or value["token"] != source.token:
            raise EventConflict("game source descriptor changed before process binding")
        self._write({**value, "pid": pid, "starttime": starttime, "last_alive": int(self.journal.clock())})

    @source_locked
    def reattach(self, proc, api):
        value = self._read()
        if not value:
            return False
        if (not self.alive(value.get("pid"), value.get("starttime")) or not proc
                or value["pid"] != proc.pid or value["starttime"] != proc.starttime):
            self._reconcile()
            return False
        source = self.journal.authenticate(value["token"])
        if self.journal.source_state(source)["retired"]:
            return False
        self._write({**value, **self._endpoint(api), "last_alive": int(self.journal.clock())})
        return True
