#!/usr/bin/env python3
"""A plugin that takes forever on purpose. It must only be slow for itself."""
import json, sys, time

sys.stdout.write(json.dumps({"type": "hello", "plugin": "slow",
                             "version": "1.0.0", "protocol": 1}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if message.get("type") == "welcome":
        sys.stdout.write(json.dumps({"type": "command", "name": "!!slow",
                                     "help": "never answers"}) + "\n")
        sys.stdout.write(json.dumps({"type": "ready"}) + "\n")
        sys.stdout.flush()
    elif message.get("type") == "invoke":
        time.sleep(3600)      # the whole point
    elif message.get("type") == "shutdown":
        break
