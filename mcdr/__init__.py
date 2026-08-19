"""Running MCDReforged plugins under W4VE.

The manifesto accepts replacing MCDR on one condition: the plugins MineWave
depends on keep working. `docs/mcdr-compat-surface.md` counts what that costs,
by reading the plugins rather than guessing: 41 importable names, 23 interface
methods and 9 events for six of the eight acceptance plugins.

Everything here is reimplemented from the documented behaviour, never copied:
MCDR is LGPL-3.0, and copying its code would pull that licence onto W4VE.

Today this package finds plugins and reads what they declare, without running
any of them. The host that actually loads them comes next.
"""

from mcdr.loader import (Plugin, discover, duplicates, load_order,  # noqa: F401
                         read, report)
