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

Also here: the command tree (`command`), so a plugin's own commands run when a
player types them, and a plugin's `lang/` files, so its messages come out as
sentences rather than as the keys they were looked up by.

What is not here yet: permissions read from a file, plugin hot reload, and the
MCDR commands themselves (`!!MCDR`). A plugin that reaches for a name this host
does not carry is told which name, which turns a compatibility hole into a one
line fix.
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
