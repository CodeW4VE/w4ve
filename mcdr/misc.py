"""The smaller pieces of the MCDR API, found by loading real plugins.

Every one of these exists because a plugin failed to load without it, which is
a better filter than reading the documentation and guessing what matters.
DigAll needs `@new_thread`, MinecraftDataAPI needs `Version` and
`ServerInterface.si()`.

Reimplemented from documented behaviour, never copied: MCDR is LGPL-3.0.
"""

import functools
import re
import threading


def new_thread(arg=None):
    """Run this function on its own thread, which is what MCDR's does.

    Used both bare and with a name:

        @new_thread
        def slow(): ...

        @new_thread('DigAll reseed')
        def also_slow(): ...

    The thread is a daemon: a plugin's background work must never be the reason
    a server cannot shut down.
    """

    def decorate(func, name=None):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            thread = threading.Thread(target=func, args=args, kwargs=kwargs,
                                      name=name or func.__name__, daemon=True)
            thread.start()
            # Plugins keep the thread to join it later, so hand it back.
            wrapper.thread = thread
            return thread
        wrapper.original = func
        return wrapper

    if callable(arg):
        return decorate(arg)
    return lambda func: decorate(func, arg)


class FunctionThread(threading.Thread):
    """What `@new_thread` hands back, for plugins that type the annotation."""

    def get_return_value(self, block=False, timeout=None):
        if block:
            self.join(timeout)
        return getattr(self, "_return_value", None)


NUMBERS = re.compile(r"\d+")


class Version:
    """A version that can be compared, which is all plugins use it for.

    MinecraftDataAPI asks whether the server is older than 1.19 to decide how
    to read a data response. Getting that comparison wrong would make it read
    the wrong format, so it is worth more than a string compare.
    """

    def __init__(self, text, allow_wildcard=True):
        self.raw = str(text or "")
        self.parts = tuple(int(n) for n in NUMBERS.findall(self.raw)) or (0,)

    def _key(self, other):
        theirs = other if isinstance(other, Version) else Version(other)
        width = max(len(self.parts), len(theirs.parts))
        mine = self.parts + (0,) * (width - len(self.parts))
        yours = theirs.parts + (0,) * (width - len(theirs.parts))
        return mine, yours

    def __eq__(self, other):
        mine, yours = self._key(other)
        return mine == yours

    def __lt__(self, other):
        mine, yours = self._key(other)
        return mine < yours

    def __le__(self, other):
        return self < other or self == other

    def __gt__(self, other):
        return not self <= other

    def __ge__(self, other):
        return not self < other

    def __hash__(self):
        return hash(self.parts)

    def __str__(self):
        return self.raw

    def __repr__(self):
        return "Version(%r)" % self.raw


class VersionRequirement:
    """`>=1.19`, the shape a plugin's dependency is written in."""

    def __init__(self, text):
        self.raw = str(text or "*").strip()

    def accept(self, version):
        if self.raw in ("*", "", "any"):
            return True
        version = version if isinstance(version, Version) else Version(version)
        for token in self.raw.split():
            match = re.match(r"(>=|<=|==|=|>|<|\^|~)?\s*([\d.]+)", token)
            if not match:
                continue
            op, bound = match.group(1) or "==", Version(match.group(2))
            if op == ">=" and not version >= bound:
                return False
            if op == ">" and not version > bound:
                return False
            if op == "<=" and not version <= bound:
                return False
            if op == "<" and not version < bound:
                return False
            if op in ("==", "=") and not version == bound:
                return False
            if op in ("^", "~") and version.parts[:1] != bound.parts[:1]:
                return False
        return True
