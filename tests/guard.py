"""Smallest possible guardian host, used by the tests and as an example.

    python3 tests/guard.py <root> [--service NAME CMD...] -- <server command...>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime  # noqa: E402


def main(argv):
    root = argv[0]
    rest = argv[1:]
    services = []
    while rest and rest[0] == "--service":
        rest = rest[1:]
        name = rest[0]
        command, rest = [], rest[1:]
        while rest and rest[0] != "--":
            command.append(rest[0])
            rest = rest[1:]
        rest = rest[1:] if rest else rest
        services.append((name, command))
    if rest and rest[0] == "--":
        rest = rest[1:]

    guardian = runtime.Guardian(root)
    if rest:
        guardian.add_server(rest, root, flavor="fabric", stop_grace=3,
                            restart="never")
    for name, command in services:
        guardian.add_service(name, command, root, restart="on-failure")
    code, message = guardian.run(start_now=bool(rest))
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
