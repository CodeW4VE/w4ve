"""`RconConnection`: the RCON class MCDR hands plugins, over W4VE's own.

MultiRconAPI does `class Rcon(RconConnection)`, so this cannot be a wrapper
function or a duck typed stand-in: it has to be a class with that name that
behaves the way the one it inherits from behaves.

The connection underneath is `rcon.py`, which the guardian already uses as its
fallback channel. That is the point of doing it this way rather than writing a
second RCON client: one implementation, already tested against the two traps
of the protocol (a rejected password answers with id -1, and a long answer
arrives split with nothing marking the end).

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import rcon as _rcon
except ImportError:                                  # pragma: no cover
    _rcon = None


class RconConnection:
    """Connect, send commands, disconnect. The three things plugins use.

    Failure is reported the way MCDR reports it: `connect()` returns whether
    it worked and `send_command()` returns None when there is nothing to
    return. A plugin that checks neither behaves the same as it did before,
    and one that checks either gets a true answer.
    """

    def __init__(self, address, port, password, logger=None):
        self.address = address
        self.port = int(port)
        self.password = password
        self.logger = logger
        self.connection = None

    def connect(self):
        if _rcon is None:
            return False
        self.disconnect()
        try:
            connection = _rcon.Rcon(self.address, self.port, self.password)
            connection.connect()
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning("rcon to %s:%s failed: %s"
                                    % (self.address, self.port, exc))
            self.connection = None
            return False
        self.connection = connection
        return True

    def disconnect(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None

    def send_command(self, command, max_retry_time=3):
        """Run a command and return the answer, reconnecting once if dropped.

        The retry is not politeness: an RCON connection held open across a
        server restart is dead and says nothing about it until you write to
        it, which is exactly when a plugin asks its question.
        """
        for attempt in range(max(1, int(max_retry_time))):
            if self.connection is None and not self.connect():
                return None
            try:
                return self.connection.command(command)
            except Exception as exc:
                self.disconnect()
                if attempt + 1 >= max(1, int(max_retry_time)):
                    if self.logger is not None:
                        self.logger.warning("rcon command failed: %s" % exc)
                    return None
        return None

    # Plugins use it as a context manager as often as not.
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
