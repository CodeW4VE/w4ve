"""W4VE's `mcdreforged`, for processes that are not plugins.

A plugin gets its `mcdreforged` built in memory by the host, which is right for
a plugin: it is loaded by W4VE and the module can be handed to it. But the
pieces people actually run are not only plugins. **ChatBridge's daemon imports
`mcdreforged.utils.serializer` and it is a separate process**, started from a
command line, with no host anywhere near it. Under MCDR the answer is
`pip install mcdreforged` on a machine that then carries all of MCDR to use one
class.

So: a real package on disk, tiny, that re-exports what W4VE already has. Put
this folder on a process's `PYTHONPATH` and `import mcdreforged` works.

⚠️ Nothing is reimplemented here. Every name comes from `mcdr/`, so the daemon
and the plugin are running the same serializer, and a fix in one is a fix in
both. If this file ever starts holding behaviour of its own, that is the
mistake.
"""

import sys as _sys
from pathlib import Path as _Path

# The runtime lives two folders up (`compat/mcdreforged/` -> the repo root).
# Added rather than assumed: the process importing this was started by
# somebody else and knows nothing about where W4VE keeps its code.
_ROOT = str(_Path(__file__).resolve().parents[2])
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
