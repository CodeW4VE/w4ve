#!/usr/bin/env python3
"""A plugin from the future, asking for a protocol nobody speaks."""
import json, sys
sys.stdout.write(json.dumps({"type": "hello", "plugin": "liar",
                             "version": "1.0.0", "protocol": 99}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    pass
