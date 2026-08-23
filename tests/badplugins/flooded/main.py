#!/usr/bin/env python3
"""A plugin that reads its events far more slowly than they arrive."""
import json, sys, time

sys.stdout.write(json.dumps({"type": "hello", "plugin": "flooded",
                             "version": "1.0.0", "protocol": 1}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line) if line.strip() else {}
    if message.get("type") == "welcome":
        sys.stdout.write(json.dumps({"type": "ready"}) + "\n")
        sys.stdout.flush()
    if message.get("type") == "event":
        time.sleep(0.5)
    elif message.get("type") == "shutdown":
        break
