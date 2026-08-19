"""RCON, the fallback channel RFC-0001 promises.

The guardian talks to a server it started through its stdin. A server it only
adopted has no stdin of ours, and until now that meant the only way to stop it
was a signal, which works and saves the world but is blunter than the design
says. RCON is what covers that gap: it is already switched on in all five
MineWave servers, it is how a connected server is reached at all, and it is the
one channel that survives the guardian not being the parent.

The protocol is small enough to implement honestly: length prefixed little
endian packets, a login packet, then command packets. The two things that bite
are that a failed login answers with request id -1, and that long replies come
back split across several packets with no count and no terminator, so you have
to ask a question you already know the answer to and wait for it to come back.

Standard library only, Python 3.9.
"""

import re
import socket
import struct

AUTH = 3
AUTH_RESPONSE = 2
COMMAND = 2
RESPONSE = 0

# Packets carry a 4 byte length, and Minecraft refuses anything much over this.
MAX_PACKET = 4096
MAX_COMMAND = 1446


class RconError(Exception):
    """Something went wrong that the caller has to hear about."""


class Rcon:
    """One connection to one server. Not thread safe, on purpose: keep it short.

    Used as a context manager:

        with Rcon("127.0.0.1", 25575, password) as console:
            print(console.command("list"))
    """

    def __init__(self, host, port, password, timeout=10):
        self.host = host
        self.port = int(port)
        self.password = password
        self.timeout = timeout
        self.sock = None
        self._next_id = 0

    # ------------------------------------------------------------- lifecycle

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=self.timeout)
        except OSError as exc:
            raise RconError("cannot reach RCON at %s:%d (%s)"
                            % (self.host, self.port, exc.strerror or exc))
        self.sock.settimeout(self.timeout)
        self._login()
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -------------------------------------------------------------- protocol

    def _login(self):
        request = self._send(AUTH, self.password)
        packet_id, kind, _ = self._read_packet()
        # Some servers answer a login with an empty RESPONSE first, then the
        # real answer. Skipping it is not optional: without this the password
        # looks wrong on a server where it is right.
        if kind == RESPONSE:
            packet_id, kind, _ = self._read_packet()
        if packet_id == -1:
            raise RconError("RCON refused the password")
        if packet_id != request or kind != AUTH_RESPONSE:
            raise RconError("RCON said something unexpected while logging in")

    def command(self, text):
        """Run one command and return everything the server answered.

        A long answer arrives in several packets with nothing marking the end,
        so a second, harmless packet is sent behind it and its echo is what
        tells us the first answer is complete.
        """
        if len(text) > MAX_COMMAND:
            raise RconError("command is too long for RCON (%d characters)"
                            % len(text))
        wanted = self._send(COMMAND, text)
        sentinel = self._send(COMMAND, "")
        chunks = []
        while True:
            packet_id, _, body = self._read_packet()
            if packet_id == sentinel:
                break
            if packet_id == wanted:
                chunks.append(body)
        return "".join(chunks)

    def _send(self, kind, body):
        self._next_id += 1
        packet_id = self._next_id
        payload = struct.pack("<ii", packet_id, kind) + body.encode("utf-8") + b"\x00\x00"
        try:
            self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        except OSError as exc:
            raise RconError("RCON connection went away: %s" % exc)
        return packet_id

    def _read_exactly(self, count):
        data = b""
        while len(data) < count:
            try:
                block = self.sock.recv(count - len(data))
            except socket.timeout:
                raise RconError("RCON stopped answering")
            except OSError as exc:
                raise RconError("RCON connection went away: %s" % exc)
            if not block:
                raise RconError("RCON hung up")
            data += block
        return data

    def _read_packet(self):
        length = struct.unpack("<i", self._read_exactly(4))[0]
        if length < 10 or length > MAX_PACKET:
            raise RconError("RCON sent a packet of %d bytes, which is nonsense"
                            % length)
        payload = self._read_exactly(length)
        packet_id, kind = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", "replace")
        return packet_id, kind, body


# ------------------------------------------------------------------ discovery

def credentials_from(game_dir):
    """Read RCON settings out of server.properties, and say what is missing.

    Nothing here is written down anywhere else: the password lives in the
    server's own file, we read it when we need it and we never copy it into
    W4VE's state or logs.
    """
    path = game_dir / "server.properties"
    if not path.exists():
        return None, "there is no server.properties to read RCON settings from"
    settings = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        settings[key.strip()] = value.strip()
    if settings.get("enable-rcon", "false").lower() != "true":
        return None, "RCON is off in server.properties (enable-rcon=false)"
    password = settings.get("rcon.password", "")
    if not password:
        return None, "RCON is on but has no password set"
    return {
        # Bound on every interface usually, but we always come from this
        # machine: talking to a server over the network is not this tool's job.
        "host": "127.0.0.1",
        "port": int(settings.get("rcon.port", 25575)),
        "password": password,
    }, ""


def redact(text, password):
    """Never let a password reach a log line, even by accident."""
    if not password:
        return text
    return re.sub(re.escape(password), "***", text)
