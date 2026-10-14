"""Local player authority: paired instances, short proofs, sessions and grants.

An issuer secret trusts that Minecraft instance to attest connected players.
Display names are not account links. Administrative mutation stays on the local
W4VE control plane; this module is never a public player HTTP service.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid


class IdentityDenied(Exception):
    pass


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid identity key")
    return value


def unpack(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid proof encoding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Authority:
    PROOF_LIFETIME = 300
    SESSION_LIFETIME = 900
    CLOCK_SKEW = 30

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("authority database may not be a symlink")
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_communities (id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_instances (
                    community TEXT NOT NULL,instance TEXT NOT NULL,secret TEXT NOT NULL,
                    enabled INTEGER NOT NULL,PRIMARY KEY(community,instance));
                CREATE TABLE IF NOT EXISTS identity_proofs (
                    community TEXT NOT NULL,instance TEXT NOT NULL,nonce TEXT NOT NULL,
                    expires INTEGER NOT NULL,PRIMARY KEY(community,instance,nonce));
                CREATE TABLE IF NOT EXISTS identity_sessions (
                    digest TEXT PRIMARY KEY,community TEXT NOT NULL,instance TEXT NOT NULL,
                    subject TEXT NOT NULL,name TEXT NOT NULL,audience TEXT NOT NULL,expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_grants (
                    community TEXT NOT NULL,audience TEXT NOT NULL,resource TEXT NOT NULL,
                    subject TEXT NOT NULL,operation TEXT NOT NULL,
                    PRIMARY KEY(community,audience,resource,subject,operation));
                CREATE TABLE IF NOT EXISTS identity_blocked (
                    community TEXT NOT NULL,subject TEXT NOT NULL,PRIMARY KEY(community,subject));
            """)
            db.execute("BEGIN IMMEDIATE")
            meta = dict(db.execute("SELECT key,value FROM identity_meta"))
            if meta and meta.get("schema") != "1":
                raise ValueError("unsupported identity schema")
            if not meta:
                db.executemany("INSERT INTO identity_meta VALUES (?,?)",
                               [("schema", "1"), ("epoch", secrets.token_hex(16))])

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _changed(self, db, community):
        db.execute("INSERT INTO identity_communities VALUES (?,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1",
                   (community,))

    def register_instance(self, community, instance, secret=None, enabled=True):
        key(community)
        key(instance)
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        secret = secrets.token_hex(32) if secret is None else secret
        if not isinstance(secret, str) or not 32 <= len(secret) <= 256:
            raise ValueError("pairing secret must have 32..256 characters")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO identity_instances VALUES (?,?,?,?) ON CONFLICT(community,instance) DO UPDATE SET secret=excluded.secret,enabled=excluded.enabled",
                       (community, instance, secret, int(enabled)))
            # Re-pair/rotation invalidates every session issued by this instance.
            db.execute("DELETE FROM identity_sessions WHERE community=? AND instance=?", (community, instance))
            self._changed(db, community)
        return {"community": community, "instance": instance, "secret": secret, "enabled": enabled}

    def set_grants(self, community, audience, resource, subject, operations):
        for value in (community, audience, resource):
            key(value)
        if subject != "*":
            key(subject)
        if not isinstance(operations, list) or len(operations) > 64:
            raise ValueError("operations must be a bounded list")
        operations = sorted(set(key(item) for item in operations))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM identity_grants WHERE community=? AND audience=? AND resource=? AND subject=?",
                       (community, audience, resource, subject))
            db.executemany("INSERT INTO identity_grants VALUES (?,?,?,?,?)",
                           [(community, audience, resource, subject, operation) for operation in operations])
            self._changed(db, community)

    def set_blocked(self, community, subject, blocked):
        key(community)
        key(subject)
        if type(blocked) is not bool:
            raise ValueError("blocked must be boolean")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if blocked:
                db.execute("INSERT OR IGNORE INTO identity_blocked VALUES (?,?)", (community, subject))
                db.execute("DELETE FROM identity_sessions WHERE community=? AND subject=?", (community, subject))
            else:
                db.execute("DELETE FROM identity_blocked WHERE community=? AND subject=?", (community, subject))
            self._changed(db, community)

    def create_session(self, proof, audience):
        key(audience)
        if not isinstance(proof, str) or len(proof) > 2048:
            raise IdentityDenied("proof refused")
        try:
            version, body, signature = proof.split(".")
            if version != "w4ve1":
                raise ValueError("unknown proof")
            payload = json.loads(unpack(body).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid proof")
            community, instance = key(payload["community"]), key(payload["instance"])
            if payload["audience"] != audience:
                raise ValueError("wrong audience")
            player = uuid.UUID(payload["uuid"]).hex
            name, nonce = payload["name"], key(payload["nonce"])
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
                raise ValueError("invalid player name")
            issued, expires = payload["issued"], payload["expires"]
            if type(issued) is not int or type(expires) is not int:
                raise ValueError("invalid proof clock")
            now = int(self.clock())
            if (issued > now + self.CLOCK_SKEW or expires <= now or expires <= issued
                    or expires - issued > self.PROOF_LIFETIME):
                raise ValueError("expired or unbounded proof")
            mac = unpack(signature)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise IdentityDenied("proof refused") from None
        subject = "minecraft:" + player
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            issuer = db.execute("SELECT secret,enabled FROM identity_instances WHERE community=? AND instance=?",
                                (community, instance)).fetchone()
            if issuer is None or not issuer["enabled"]:
                raise IdentityDenied("proof refused")
            expected = hmac.new(issuer["secret"].encode(), (version + "." + body).encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, mac):
                raise IdentityDenied("proof refused")
            if db.execute("SELECT 1 FROM identity_blocked WHERE community=? AND subject=?", (community, subject)).fetchone():
                raise IdentityDenied("account unavailable")
            db.execute("DELETE FROM identity_proofs WHERE expires<=?", (now,))
            db.execute("DELETE FROM identity_sessions WHERE expires<=?", (now,))
            try:
                db.execute("INSERT INTO identity_proofs VALUES (?,?,?,?)", (community, instance, nonce, expires))
            except sqlite3.IntegrityError:
                raise IdentityDenied("proof refused") from None
            until = now + self.SESSION_LIFETIME
            db.execute("INSERT INTO identity_sessions VALUES (?,?,?,?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), community, instance, subject, name, audience, until))
        return {"token": token, "expires": until}

    def _session(self, db, token, audience):
        if not isinstance(token, str) or not 1 <= len(token) <= 128:
            raise IdentityDenied("session unavailable")
        row = db.execute("""SELECT s.* FROM identity_sessions s JOIN identity_instances i
            ON i.community=s.community AND i.instance=s.instance
            WHERE s.digest=? AND s.audience=? AND s.expires>? AND i.enabled=1""",
                         (hashlib.sha256(token.encode()).hexdigest(), audience, int(self.clock()))).fetchone()
        if row is None:
            raise IdentityDenied("session unavailable")
        return row

    def _revision(self, db, community):
        epoch = db.execute("SELECT value FROM identity_meta WHERE key='epoch'").fetchone()[0]
        revision = db.execute("SELECT revision FROM identity_communities WHERE id=?", (community,)).fetchone()[0]
        return epoch + ":" + str(revision)

    def inspect(self, token, audience):
        key(audience)
        with self._db() as db:
            row = self._session(db, token, audience)
            return {"community": row["community"], "subject": row["subject"], "name": row["name"],
                    "origin": row["instance"], "audience": row["audience"], "expires": row["expires"],
                    "policy_revision": self._revision(db, row["community"])}

    def authorize(self, token, audience, resource, operation):
        key(audience)
        key(resource)
        key(operation)
        with self._db() as db:
            row = self._session(db, token, audience)
            grant = db.execute("SELECT 1 FROM identity_grants WHERE community=? AND audience=? AND resource=? AND operation=? AND subject IN (?, '*')",
                               (row["community"], audience, resource, operation, row["subject"])).fetchone()
            return {"allowed": grant is not None, "policy_revision": self._revision(db, row["community"])}
