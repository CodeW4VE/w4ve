"""Running MCDReforged plugins under W4VE.

The manifesto accepts replacing MCDR on one condition: the plugins MineWave
depends on keep working. `docs/mcdr-compat-surface.md` counts what that costs,
by reading the plugins rather than guessing: 41 importable names, 23 interface
methods and 9 events for six of the eight acceptance plugins.

Everything here is reimplemented from the documented behaviour, never copied:
MCDR is LGPL-3.0, and copying its code would pull that licence onto W4VE.

What is here today: finding plugins and reading what they declare without
running them (`loader`), the text objects their output is built from (`text`),
the typed config objects they all declare (`serializer`), the console line
turned into an event (`info`), the interface they are handed (`interface`) and
the host that loads them and runs their events (`host`).

Verified against a real one: Here 1.2.2, unmodified, loads with no MCDR
installed, writes its config, answers `!!here` in chat and produces the same
tellraw it produces under MCDR, waypoint buttons and all.

What is not here yet: the command tree, so a plugin that calls
`register_command` loads and is told its commands cannot be routed. Saying so
is the point. A compatibility layer that pretends is worse than one with a
hole in it.
"""

from mcdr.loader import (Plugin, discover, duplicates, load_order,  # noqa: F401
                         read, report)
from mcdr.host import Host  # noqa: F401
from mcdr.info import Info, parse_server_line  # noqa: F401
from mcdr.interface import (PermissionLevel, PluginServerInterface,  # noqa: F401
                            ServerInterface)
from mcdr.serializer import Serializable  # noqa: F401
from mcdr.text import (RAction, RColor, RStyle, RText, RTextBase,  # noqa: F401
                       RTextList, RTextMCDRTranslation, RTextTranslation)
