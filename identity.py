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
import unicodedata


class IdentityDenied(Exception):
    pass


class IdentityForbidden(IdentityDenied):
    pass


class IdentityConflict(Exception):
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
                    subject TEXT NOT NULL,name TEXT NOT NULL,audience TEXT NOT NULL,expires INTEGER NOT NULL,
                    connection TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS identity_grants (
                    community TEXT NOT NULL,audience TEXT NOT NULL,resource TEXT NOT NULL,
                    subject TEXT NOT NULL,operation TEXT NOT NULL,
                    PRIMARY KEY(community,audience,resource,subject,operation));
                CREATE TABLE IF NOT EXISTS identity_profiles (
                    community TEXT NOT NULL,subject TEXT NOT NULL,display_name TEXT NOT NULL,
                    bio TEXT NOT NULL,revision INTEGER NOT NULL,
                    PRIMARY KEY(community,subject));
                CREATE TABLE IF NOT EXISTS identity_blocked (
                    community TEXT NOT NULL,subject TEXT NOT NULL,PRIMARY KEY(community,subject));
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    owner TEXT PRIMARY KEY,audience TEXT NOT NULL,community TEXT NOT NULL,
                    instance TEXT NOT NULL,resources TEXT NOT NULL,transport_initialized INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS identity_instance_sessions (
                    digest TEXT PRIMARY KEY,community TEXT NOT NULL,instance TEXT NOT NULL,
                    audience TEXT NOT NULL,expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_instance_resources (
                    community TEXT NOT NULL,instance TEXT NOT NULL,audience TEXT NOT NULL,resource TEXT NOT NULL,
                    PRIMARY KEY(community,instance,audience,resource));
            """)
            db.execute("BEGIN IMMEDIATE")
            if "connection" not in {row[1] for row in db.execute("PRAGMA table_info(identity_sessions)")}:
                db.execute("ALTER TABLE identity_sessions ADD COLUMN connection TEXT NOT NULL DEFAULT ''")
            binding_columns = {row[1] for row in db.execute("PRAGMA table_info(identity_bindings)")}
            if "transport_initialized" not in binding_columns:
                db.execute("ALTER TABLE identity_bindings ADD COLUMN transport_initialized INTEGER NOT NULL DEFAULT 0")
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
            db.execute("DELETE FROM identity_instance_sessions WHERE community=? AND instance=?", (community, instance))
            self._changed(db, community)
        return {"community": community, "instance": instance, "secret": secret, "enabled": enabled}

    @staticmethod
    def normalized_resources(resources):
        if not isinstance(resources, list) or not 1 <= len(resources) <= 64:
            raise ValueError("a bounded resource policy is required")
        normalized = []
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) != {"id", "name", "operations"}:
                raise ValueError("invalid resource declaration")
            rid = key(resource["id"])
            name, operations = resource["name"], resource["operations"]
            if not isinstance(name, str) or not 1 <= len(name) <= 128:
                raise ValueError("invalid resource name")
            if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
                raise ValueError("invalid resource operations")
            normalized.append({"id": rid, "name": name,
                               "operations": sorted(set(key(op) for op in operations))})
        if len({item["id"] for item in normalized}) != len(normalized):
            raise ValueError("duplicate resource")
        if len(json.dumps(normalized).encode("utf-8")) > 12288:
            raise ValueError("resource policy is too large")
        return normalized

    def provision_binding(self, owner, audience, resources):
        """Local operator setup. Existing policy/issuer revocations are never reset."""
        key(owner)
        key(audience)
        normalized = self.normalized_resources(resources)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute("SELECT * FROM identity_bindings WHERE owner=?", (owner,)).fetchone()
            if binding is not None:
                if binding["audience"] != audience or binding["resources"] != encoded:
                    raise ValueError("binding policy changed; explicit migration required")
                issuer = db.execute("SELECT * FROM identity_instances WHERE community=? AND instance=?",
                                    (binding["community"], binding["instance"])).fetchone()
                if issuer is None or not issuer["enabled"]:
                    raise ValueError("paired instance is disabled or missing")
                if not binding["transport_initialized"]:
                    # Explicit configure upgrades a pre-transport binding once.
                    # Later resource revocations remain untouched.
                    db.executemany("INSERT OR IGNORE INTO identity_instance_resources VALUES (?,?,?,?)",
                                   [(issuer["community"], issuer["instance"], audience, item["id"]) for item in normalized])
                    db.execute("UPDATE identity_bindings SET transport_initialized=1 WHERE owner=?", (owner,))
                    self._changed(db, issuer["community"])
                return {"community": issuer["community"], "instance": issuer["instance"],
                        "secret": issuer["secret"], "resources": normalized}
            community, instance, secret = "c_" + uuid.uuid4().hex, "i_" + uuid.uuid4().hex, secrets.token_hex(32)
            db.execute("INSERT INTO identity_instances VALUES (?,?,?,1)", (community, instance, secret))
            db.execute("INSERT INTO identity_bindings VALUES (?,?,?,?,?,1)",
                       (owner, audience, community, instance, encoded))
            for resource in normalized:
                db.execute("INSERT INTO identity_instance_resources VALUES (?,?,?,?)",
                           (community, instance, audience, resource["id"]))
                db.executemany("INSERT INTO identity_grants VALUES (?,?,?,?,?)",
                               [(community, audience, resource["id"], "*", op)
                                for op in resource["operations"]])
            self._changed(db, community)
            return {"community": community, "instance": instance, "secret": secret, "resources": normalized}

    def set_instance_resources(self, community, instance, audience, resources):
        for value in (community, instance, audience):
            key(value)
        if not isinstance(resources, list) or len(resources) > 64:
            raise ValueError("invalid instance resources")
        resources = sorted(set(key(resource) for resource in resources))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM identity_instances WHERE community=? AND instance=?", (community, instance)).fetchone():
                raise ValueError("unknown paired instance")
            db.execute("DELETE FROM identity_instance_resources WHERE community=? AND instance=? AND audience=?", (community, instance, audience))
            db.executemany("INSERT INTO identity_instance_resources VALUES (?,?,?,?)",
                           [(community, instance, audience, resource) for resource in resources])
            db.execute("UPDATE identity_bindings SET transport_initialized=1 WHERE community=? AND instance=? AND audience=?",
                       (community, instance, audience))
            self._changed(db, community)

    def capture_bindings(self):
        """Internal boot snapshot, without issuer secrets or player sessions.

        A boot created before enrollment must not acquire an origin later.
        The authority epoch prevents relabeling after replacing its database.
        """
        with self._db() as db:
            db.execute("BEGIN")
            rows = db.execute("""SELECT b.owner,b.community,b.instance,b.audience
                FROM identity_bindings b JOIN identity_instances i
                ON i.community=b.community AND i.instance=b.instance
                WHERE i.enabled=1 ORDER BY b.owner LIMIT 65""").fetchall()
            if len(rows) > 64:
                raise ValueError("too many capture bindings")
            return {"epoch": db.execute("SELECT value FROM identity_meta WHERE key='epoch'").fetchone()[0],
                    "owners": {row["owner"]: {name: row[name] for name in ("community", "instance", "audience")}
                               for row in rows}}

    def authorize_captured_chat(self, captured, owner, resource):
        """Internal: authorize a journal lookup, never a caller-supplied actor.

        This returns no session or bearer credential. Leaving the game does
        not invalidate captured history; current blocks/sharing/grants do.
        """
        key(owner); key(resource)
        event, bindings = captured["event"], captured["bindings"]
        if event["kind"] != "player.chat" or not bindings:
            raise IdentityDenied("capture has no enrolled origin")
        origin = bindings["owners"].get(owner)
        if origin is None:
            raise IdentityDenied("capture has no enrolled origin")
        community, instance, audience = (origin[name] for name in ("community", "instance", "audience"))
        subject = "minecraft:" + event["data"]["uuid"]
        with self._db() as db:
            db.execute("BEGIN")
            epoch = db.execute("SELECT value FROM identity_meta WHERE key='epoch'").fetchone()[0]
            binding = db.execute("""SELECT b.community,b.instance,b.audience FROM identity_bindings b
                JOIN identity_instances i ON i.community=b.community AND i.instance=b.instance
                WHERE b.owner=? AND i.enabled=1""", (owner,)).fetchone()
            if epoch != bindings["epoch"] or binding is None or tuple(binding) != (community, instance, audience):
                raise IdentityDenied("capture origin is no longer available")
            shared = db.execute("""SELECT 1 FROM identity_instance_resources
                WHERE community=? AND instance=? AND audience=? AND resource=?""",
                                (community, instance, audience, resource)).fetchone()
            blocked = db.execute("SELECT 1 FROM identity_blocked WHERE community=? AND subject=?",
                                 (community, subject)).fetchone()
            operations = {row[0] for row in db.execute("""SELECT operation FROM identity_grants
                WHERE community=? AND audience=? AND resource=? AND subject IN (?, '*')""",
                                                       (community, audience, resource, subject))}
            return {"kind": "captured-chat", "community": community, "origin": instance,
                    "audience": audience, "subject": subject, "name": event["data"]["name"],
                    "connection": event["data"]["connection"], "resource": resource,
                    "allowed": shared is not None and blocked is None and {"chat.read", "chat.publish"} <= operations,
                    "policy_revision": self._revision(db, community)}

    def create_instance_session(self, proof, audience):
        """Typed issuer role: cannot be inspected or used as a player session."""
        key(audience)
        try:
            if not isinstance(proof, str) or len(proof) > 2048:
                raise ValueError()
            version, body, signature = proof.split(".")
            if version != "w4ve-instance1":
                raise ValueError()
            payload = json.loads(unpack(body))
            if not isinstance(payload, dict) or set(payload) != {"community", "instance", "audience", "issued", "expires", "nonce"}:
                raise ValueError()
            community, instance, nonce = key(payload["community"]), key(payload["instance"]), key(payload["nonce"])
            now, issued, expires = int(self.clock()), payload["issued"], payload["expires"]
            if (payload["audience"] != audience or type(issued) is not int or type(expires) is not int
                    or issued > now + self.CLOCK_SKEW or expires <= now or expires <= issued
                    or expires - issued > self.PROOF_LIFETIME):
                raise ValueError()
            received = unpack(signature)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise IdentityDenied("instance proof refused") from None
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            issuer = db.execute("SELECT * FROM identity_instances WHERE community=? AND instance=? AND enabled=1", (community, instance)).fetchone()
            if issuer is None or not hmac.compare_digest(received, hmac.new(issuer["secret"].encode(),
                        (version + "." + body).encode(), hashlib.sha256).digest()):
                raise IdentityDenied("instance proof refused")
            db.execute("DELETE FROM identity_proofs WHERE expires<=?", (now,))
            db.execute("DELETE FROM identity_instance_sessions WHERE expires<=?", (now,))
            try:
                db.execute("INSERT INTO identity_proofs VALUES (?,?,?,?)", (community, instance, nonce, expires))
            except sqlite3.IntegrityError:
                raise IdentityDenied("instance proof refused") from None
            until = now + 300
            db.execute("INSERT INTO identity_instance_sessions VALUES (?,?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), community, instance, audience, until))
        return {"token": token, "expires": until}

    def _instance_session(self, db, token, audience):
        if not isinstance(token, str) or not 1 <= len(token) <= 128:
            raise IdentityDenied("instance session unavailable")
        row = db.execute("""SELECT s.* FROM identity_instance_sessions s JOIN identity_instances i
            ON s.community=i.community AND s.instance=i.instance
            WHERE s.digest=? AND s.audience=? AND s.expires>? AND i.enabled=1""",
                         (hashlib.sha256(token.encode()).hexdigest(), audience, int(self.clock()))).fetchone()
        if row is None:
            raise IdentityDenied("instance session unavailable")
        return row

    def inspect_instance(self, token, audience):
        key(audience)
        with self._db() as db:
            row = self._instance_session(db, token, audience)
            return {"kind": "instance", "community": row["community"], "instance": row["instance"],
                    "audience": row["audience"], "expires": row["expires"],
                    "policy_revision": self._revision(db, row["community"])}

    def instance_recipients(self, token, audience, resource, operation, players):
        """Trust a paired server's bounded connected roster, then apply live member ACL."""
        for value in (audience, resource, operation):
            key(value)
        if not isinstance(players, list) or len(players) > 100:
            raise ValueError("invalid connected roster")
        subjects = []
        for player in players:
            if not isinstance(player, str) or not re.fullmatch(r"[0-9a-f]{32}", player):
                raise ValueError("invalid connected player UUID")
            subjects.append("minecraft:" + player)
        with self._db() as db:
            row = self._instance_session(db, token, audience)
            resource_granted = db.execute("SELECT 1 FROM identity_instance_resources WHERE community=? AND instance=? AND audience=? AND resource=?",
                                          (row["community"], row["instance"], audience, resource)).fetchone()
            allowed = []
            if resource_granted:
                for subject in sorted(set(subjects)):
                    blocked = db.execute("SELECT 1 FROM identity_blocked WHERE community=? AND subject=?", (row["community"], subject)).fetchone()
                    grant = db.execute("SELECT 1 FROM identity_grants WHERE community=? AND audience=? AND resource=? AND operation=? AND subject IN (?, '*')",
                                       (row["community"], audience, resource, operation, subject)).fetchone()
                    if not blocked and grant:
                        allowed.append(subject[len("minecraft:"):])
            return {"players": allowed, "policy_revision": self._revision(db, row["community"])}

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
            connection = payload.get("connection", "")
            if not isinstance(connection, str) or (connection and not re.fullmatch(r"[0-9a-f]{32}", connection)):
                raise ValueError("invalid connection context")
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
            db.execute("INSERT INTO identity_sessions(digest,community,instance,subject,name,audience,expires,connection) VALUES (?,?,?,?,?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), community, instance, subject, name, audience, until, connection))
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
                    "connection": row["connection"],
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

    def own_profile(self, token, audience, *, update=None, revision=None):
        """Editable community presentation; never changes verified account claims.

        Deliberately self-only. Reading another player's profile or claiming an
        external account requires a separate privacy/proof contract.
        """
        key(audience)
        if update is not None:
            if (not isinstance(update, dict) or set(update) != {'display_name', 'bio'}
                    or type(revision) is not int or not 0 <= revision < 2 ** 31 - 1):
                raise ValueError('invalid profile update')
            for field, maximum in (('display_name', 48), ('bio', 500)):
                value = update[field]
                if (not isinstance(value, str) or len(value) > maximum
                        or any(unicodedata.category(char).startswith('C') and not (field == 'bio' and char == '\n') for char in value)):
                    raise ValueError('invalid profile text')
            update = {field: value.strip() for field, value in update.items()}
        elif revision is not None:
            raise ValueError('revision requires a profile update')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE' if update is not None else 'BEGIN')
            session = self._session(db, token, audience)
            operation = 'profile.edit' if update is not None else 'profile.read'
            grant = db.execute("SELECT 1 FROM identity_grants WHERE community=? AND audience=? AND resource='profile:self' AND operation=? AND subject IN (?, '*')",
                               (session['community'], audience, operation, session['subject'])).fetchone()
            if grant is None:
                raise IdentityForbidden('profile operation not granted')
            row = db.execute('SELECT display_name,bio,revision FROM identity_profiles WHERE community=? AND subject=?',
                             (session['community'], session['subject'])).fetchone()
            profile = dict(row) if row else {'display_name': '', 'bio': '', 'revision': 0}
            if update is not None:
                if revision != profile['revision']:
                    raise IdentityConflict('profile revision changed')
                profile = {**update, 'revision': revision + 1}
                db.execute('INSERT INTO identity_profiles VALUES (?,?,?,?,?) ON CONFLICT(community,subject) DO UPDATE SET display_name=excluded.display_name,bio=excluded.bio,revision=excluded.revision',
                           (session['community'], session['subject'], profile['display_name'], profile['bio'], profile['revision']))
            return {'community': session['community'], 'subject': session['subject'],
                    'account_name': session['name'], **profile}
