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
        # So `run` can notice `running` went false instead of sitting in
        # accept() forever. See `stop`, which is where this matters.
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.running = True

    def stop(self):
        """Stop accepting, and only then close the socket.

        The order is the whole point. Closing the socket while `run` is parked
        in accept() does not wake it up: the thread stays blocked on a file
        descriptor NUMBER, and the kernel is now free to hand that number to
        the next socket anybody opens -- which, in a test file, is the next
        test's server. The zombie thread then accepts a connection meant for a
        server it is not, sees its own `running` is false, and drops it. What
        the client gets is a connection reset in the middle of logging in, one
        test too late to be believed, roughly one run in four.
        """
        self.running = False
        if self.is_alive():
            self.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
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
