#!/usr/bin/env python3
"""A plugin that tries to eat the machine. Its limit should stop it first."""
import json, sys

sys.stdout.write(json.dumps({"type": "hello", "plugin": "greedy",
                             "version": "1.0.0", "protocol": 1}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line) if line.strip() else {}
    if message.get("type") == "welcome":
        sys.stdout.write(json.dumps({"type": "command", "name": "!!eat",
                                     "help": "ask for a gigabyte"}) + "\n")
        sys.stdout.write(json.dumps({"type": "ready"}) + "\n")
        sys.stdout.flush()
    elif message.get("type") == "invoke":
        # A gigabyte, against a limit of sixty-four megabytes.
        hoard = bytearray(1024 * 1024 * 1024)
        sys.stdout.write(json.dumps({"type": "result", "id": message.get("id"),
                                     "ok": True, "value": len(hoard)}) + "\n")
        sys.stdout.flush()
