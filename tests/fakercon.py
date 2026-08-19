"""A server that speaks RCON and nothing else, for the tests.

Enough of the protocol to be honest about it: it refuses a wrong password with
request id -1, it splits a long answer across packets the way a real server
does, and it answers the empty sentinel packet that marks the end of one.
"""

import socket
import struct
import threading

AUTH = 3
AUTH_RESPONSE = 2
COMMAND = 2
RESPONSE = 0

CHUNK = 200


class FakeRcon(threading.Thread):

    def __init__(self, password="secret", answers=None):
        threading.Thread.__init__(self, daemon=True)
        self.password = password
        self.answers = answers or {}
        self.seen = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.running = True

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        authed = False
        try:
            while self.running:
                header = self._read(conn, 4)
                if not header:
                    return
                length = struct.unpack("<i", header)[0]
                payload = self._read(conn, length)
                if not payload:
                    return
                packet_id, kind = struct.unpack("<ii", payload[:8])
                body = payload[8:-2].decode("utf-8", "replace")

                if kind == AUTH:
                    authed = body == self.password
                    # A wrong password is answered with id -1, which is the one
                    # detail everybody gets wrong the first time.
                    self._write(conn, packet_id if authed else -1,
                                AUTH_RESPONSE, "")
                elif kind == COMMAND:
                    if not authed:
                        self._write(conn, -1, RESPONSE, "")
                        continue
                    if body == "":
                        self._write(conn, packet_id, RESPONSE, "")
                        continue
                    self.seen.append(body)
                    answer = self.answers.get(body, "Unknown command")
                    for start in range(0, max(len(answer), 1), CHUNK):
                        self._write(conn, packet_id, RESPONSE,
                                    answer[start:start + CHUNK])
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read(conn, count):
        data = b""
        while len(data) < count:
            block = conn.recv(count - len(data))
            if not block:
                return b""
            data += block
        return data

    @staticmethod
    def _write(conn, packet_id, kind, body):
        payload = struct.pack("<ii", packet_id, kind) + body.encode("utf-8") + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(payload)) + payload)
