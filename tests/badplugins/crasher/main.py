#!/usr/bin/env python3
"""A plugin that dies as soon as anything happens."""
import json, os, sys

sys.stdout.write(json.dumps({"type": "hello", "plugin": "crasher",
                             "version": "1.0.0", "protocol": 1}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line) if line.strip() else {}
    if message.get("type") == "welcome":
        sys.stdout.write(json.dumps({"type": "ready"}) + "\n")
        sys.stdout.flush()
    if message.get("type") == "event":
        os._exit(3)           # not an exception: gone, mid-sentence
